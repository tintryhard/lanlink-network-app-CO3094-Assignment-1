"""Post-authentication client: one reader dispatches chat and request replies.

The client closes the socket on failure or close(); the caller may also use a
socket context manager. on_message runs on the reader thread and must return
promptly; it must not make synchronous client requests. Responses are correlated
by ref_request_id, including concurrent USER_LIST requests.
"""
import json
import queue
import socket
import threading
from typing import Any, Callable

from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.models import Message, MessageType
from common.protocol import build_chat_private, build_logout, build_user_list
from common.transport import SocketTransport


class PrivateChatClient:
    def __init__(self, transport: SocketTransport, *, timeout: float = 5.0,
                 on_message: Callable[[Message], None],
                 on_disconnect: Callable[[], None] | None = None) -> None:
        self.transport = transport
        self.timeout = timeout
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self._lock = threading.Lock()
        self._pending: dict[str, queue.Queue[Message | BaseException]] = {}
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

    def _read_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    message = self.transport.receive()
                except socket.timeout:
                    continue  # Idle reader; decoder retains any incomplete frame.
                if message is None:
                    raise EOFError('Server disconnected')
                if message.type == MessageType.CHAT_PRIVATE:
                    self.on_message(message)
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
