"""Post-authentication client: one reader dispatches chat, files and replies.

The client closes the socket on failure or close(); the caller may also use a
socket context manager. on_message runs on the reader thread and must return
promptly; it must not make synchronous client requests. Responses are correlated
by ref_request_id, including concurrent USER_LIST requests.

File transfers: the reader feeds incoming file messages to the transfer engines
(transfer.sender / transfer.receiver); a sender thread pumps chunks after
FILE_ACCEPT. transfer_events receives offers, decisions and errors (Messages)
plus one final TransferSnapshot when a transfer ends. Callers must drain this
queue; its default capacity is 128 and an overflow closes the connection.
"""
import json
import queue
import socket
import threading
from pathlib import Path
from typing import Any, Callable

from common.errors import ErrorCode, LanLinkError, ProtocolError, TransferError
from common.models import Message, MessageType, TransferSnapshot
from common.protocol import (build_chat_private, build_chat_public, build_file_accept,
                             build_file_reject, build_logout, build_user_list)
from common.transport import SocketTransport
from transfer.receiver import IncomingOffer, Receiver
from transfer.sender import SendSession, inspect_file

FILE_TYPES = {MessageType.FILE_OFFER, MessageType.FILE_ACCEPT, MessageType.FILE_REJECT,
              MessageType.FILE_CHUNK, MessageType.FILE_END, MessageType.FILE_RESULT,
              MessageType.FILE_CANCEL}


class PrivateChatClient:
    def __init__(self, transport: SocketTransport, *, timeout: float = 5.0,
                 on_message: Callable[[Message], None],
                 on_disconnect: Callable[[], None] | None = None,
                 transfer_events: queue.Queue | None = None,
                 download_dir: Path | str = 'downloads') -> None:
        self.transport = transport
        self.download_dir = Path(download_dir)
        self.timeout = timeout
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        # File decisions are asynchronous. The reader only enqueues; callers
        # decide on their own thread, never while holding up the socket reader.
        self.transfer_events = (transfer_events if transfer_events is not None
                                else queue.Queue(maxsize=128))
        self._lock = threading.Lock()
        self._pending: dict[str, queue.Queue[Message | BaseException]] = {}
        self._offers: dict[str, Message] = {}  # incoming offers awaiting accept/reject
        self._transfers: dict[str, SendSession | Receiver] = {}  # live transfers
        self._error = None
        self._stopped = threading.Event()
        self._cleanup_done = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name='lanlink-client-reader')
        self._reader.start()

    def _fail(self, error: BaseException) -> None:
        with self._lock:
            first = self._error is None
            if first:
                self._error = error
                pending = list(self._pending.values())
                self._pending.clear()
                # Clear first so connection_lost() below reports nothing:
                # the caller is already told about the disconnect.
                transfers = list(self._transfers.values())
                self._transfers.clear()
                self._stopped.set()
        if not first:
            # A concurrent reader/close must not notify disconnect before the
            # thread that claimed cleanup has actually closed the descriptor.
            self._cleanup_done.wait()
            return
        try:
            # No shared registry lock during socket operations. Shutdown wakes
            # receive(); close releases the descriptor, once per client session.
            try:
                self.transport.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.transport.connection.close()
            except OSError:
                pass
        finally:
            self._cleanup_done.set()
            for inbox in pending:
                inbox.put_nowait(error)
            for transfer in transfers:
                transfer.connection_lost()  # stops sending; deletes partial downloads

    def _read_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    message = self.transport.receive()
                except socket.timeout:
                    continue  # Idle reader; decoder retains any incomplete frame.
                if message is None:
                    raise EOFError('Server disconnected')
                if message.type in {MessageType.CHAT_PRIVATE, MessageType.CHAT_PUBLIC}:
                    self.on_message(message)
                    continue
                if (message.type in FILE_TYPES
                        or message.type == MessageType.ERROR and 'transfer_id' in message.header):
                    self._handle_file(message)
                    continue
                with self._lock:
                    ref = message.header.get('ref_request_id')
                    inbox = self._pending.pop(ref, None)
                if inbox is None:
                    raise ProtocolError(ErrorCode.INVALID_FIELD, 'Unexpected server response')
                inbox.put_nowait(message)
        except (OSError, EOFError, LanLinkError) as error:
            self._fail(error)
        finally:
            self._fail(EOFError('Client reader stopped'))
            if self.on_disconnect is not None:
                self.on_disconnect()

    def _request(self, message: Message, expected: MessageType) -> Message:
        inbox: queue.Queue[Message | BaseException] = queue.Queue(maxsize=1)
        ref = message.header['request_id']
        with self._lock:
            if self._error is not None:
                raise self._error
            if len(self._pending) >= 64:
                raise LanLinkError(ErrorCode.QUEUE_FULL)
            self._pending[ref] = inbox
        try:
            self.transport.send(message)
            try:
                reply = inbox.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError('Timed out waiting for server response') from None
        except ProtocolError:
            # SocketTransport validates/encodes before writing any bytes. A
            # rejected local message affects only this request, not the stream.
            with self._lock:
                self._pending.pop(ref, None)
            raise
        except (OSError, EOFError):
            # TimeoutError is an OSError too. Keep the existing close-on-timeout
            # policy: do not reuse a session with an unresolved/late response.
            self.close()
            raise
        if isinstance(reply, BaseException):
            raise reply
        if reply.type == MessageType.ERROR:
            # A correlated ERROR is a request failure; the reader stays active.
            raise LanLinkError(reply.header['code'])
        if reply.type != expected:
            self.close()
            raise ProtocolError(ErrorCode.INVALID_FIELD, 'Unexpected response type')
        return reply

    def send_private(self, recipient: str, text: str) -> Message:
        return self._request(build_chat_private(recipient, text), MessageType.CHAT_ACK)

    def send_public(self, text: str) -> Message:
        return self._request(build_chat_public(text), MessageType.CHAT_ACK)

    def _event(self, item: Message | TransferSnapshot) -> None:
        try:
            self.transfer_events.put_nowait(item)
        except queue.Full:
            # Never drop transfer state silently: close the connection instead.
            self._fail(LanLinkError(ErrorCode.QUEUE_FULL))

    def _on_update(self, snapshot: TransferSnapshot) -> None:
        """Engine callback: report a transfer's end once if it is still registered."""
        if not snapshot.is_terminal:
            return
        with self._lock:
            registered = self._transfers.pop(snapshot.transfer_id, None) is not None
        if registered:
            self._event(snapshot)

    def _handle_file(self, message: Message) -> None:
        """Reader thread: drive the engine for this transfer and report to the caller."""
        transfer_id, kind = message.header['transfer_id'], message.type
        with self._lock:
            if kind == MessageType.FILE_OFFER:
                self._offers[transfer_id] = message
            transfer = self._transfers.get(transfer_id)
            # Rejections and errors are reported as the raw message below, so
            # unregister first to avoid a second report from _on_update.
            if kind in {MessageType.FILE_REJECT, MessageType.ERROR}:
                self._transfers.pop(transfer_id, None)
        if isinstance(transfer, Receiver) and kind == MessageType.FILE_ACCEPT:
            transfer = None  # The server's echo of our own acceptance.
        if transfer is not None:
            try:
                # Receiver returns replies (e.g. FILE_RESULT); SendSession returns None.
                for reply in transfer.handle_message(message) or []:
                    self.transport.send(reply)
            except TransferError:
                pass  # The engine is now FAILED and _on_update reports it.
            if kind == MessageType.FILE_ACCEPT:
                threading.Thread(target=self._pump, args=(transfer,), daemon=True,
                                 name='lanlink-file-sender').start()
        if kind in {MessageType.FILE_OFFER, MessageType.FILE_ACCEPT,
                    MessageType.FILE_REJECT, MessageType.ERROR}:
            self._event(message)

    def _pump(self, sender: SendSession) -> None:
        """Send chunks and FILE_END on their own thread so the reader keeps reading."""
        try:
            for message in sender.outgoing():
                self.transport.send(message)  # The write lock keeps frames whole.
        except OSError:
            sender.connection_lost()

    def _send_file(self, message: Message) -> None:
        with self._lock:
            if self._error is not None:
                raise self._error
        try:
            self.transport.send(message)
        except OSError:
            self.close()
            raise

    def offer_file(self, recipient: str, path: Path | str) -> str:
        """Hash the file locally and send the offer; chunks follow once it is accepted."""
        limits = self.transport.limits
        info = inspect_file(path, limits=limits, cancel=self._stopped)
        sender = SendSession(info, recipient, limits=limits, on_update=self._on_update)
        offer = sender.offer_message()
        with self._lock:
            self._transfers[sender.transfer_id] = sender
        self._send_file(offer)
        return sender.transfer_id

    def _take_offer(self, transfer_id: str) -> Message | None:
        with self._lock:
            if transfer_id in self._transfers:
                raise LanLinkError(ErrorCode.TRANSFER_STATE)  # Already running here.
            return self._offers.pop(transfer_id, None)

    def accept_file(self, transfer_id: str) -> None:
        offer = self._take_offer(transfer_id)
        if offer is None:
            # Not offered to us: send anyway and let the server report the error.
            self._send_file(build_file_accept(transfer_id))
            return
        limits = self.transport.limits
        receiver = Receiver(IncomingOffer.from_message(offer, limits=limits), self.download_dir,
                            limits=limits, on_update=self._on_update)
        try:
            accept = receiver.accept()  # Reserves the file name and opens the .part file.
        except TransferError:
            self._send_file(build_file_reject(transfer_id))  # We cannot store it.
            raise
        # Register before sending FILE_ACCEPT, so the first chunk finds the receiver.
        with self._lock:
            self._transfers[transfer_id] = receiver
        self._send_file(accept)

    def reject_file(self, transfer_id: str, reason: str | None = None) -> None:
        self._take_offer(transfer_id)
        self._send_file(build_file_reject(transfer_id, reason))

    def users(self) -> list[dict[str, Any]]:
        reply = self._request(build_user_list(), MessageType.USER_LIST)
        return json.loads(reply.payload)['users']

    def logout(self) -> None:
        try:
            self.transport.send(build_logout())
            if not self._stopped.wait(self.timeout):
                raise TimeoutError('Timed out waiting for logout')
        finally:
            self.close()

    def close(self) -> None:
        self._fail(EOFError('Client closed'))
        if threading.current_thread() is not self._reader:
            self._reader.join()
