"""Post-authentication client: one reader dispatches chat and request replies.

The caller owns the socket. on_message runs on the reader thread and must return
promptly; it must not make synchronous client requests. Only one USER_LIST may be
outstanding because that existing response schema has no ref_request_id.
"""
import json
import queue
import socket
import threading

from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.models import MessageType
from common.protocol import build_chat_private, build_logout, build_user_list


class PrivateChatClient:
    def __init__(self, transport, *, timeout=5.0, on_message, on_disconnect=None):
        self.transport = transport
        self.timeout = timeout
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self._lock = threading.Lock()
        self._pending = {}
        self._user_request = None
        self._error = None
        self._stopped = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name='lanlink-client-reader')
        self._reader.start()

    def _fail(self, error):
        with self._lock:
            if self._error is None:
                self._error = error
            pending = list(self._pending.values())
            self._pending.clear()
            self._user_request = None
            self._stopped.set()
        for inbox in pending:
            inbox.put_nowait(error)

    def _read_loop(self):
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
                    ref = (self._user_request if message.type == MessageType.USER_LIST
                           else message.header.get('ref_request_id'))
                    inbox = self._pending.pop(ref, None)
                    if ref == self._user_request:
                        self._user_request = None
                if inbox is None:
                    raise ProtocolError(ErrorCode.INVALID_FIELD, 'Unexpected server response')
                inbox.put_nowait(message)
        except (OSError, EOFError, LanLinkError) as error:
            self._fail(error)
        finally:
            self._fail(EOFError('Client reader stopped'))
            if self.on_disconnect is not None:
                self.on_disconnect()

    def _request(self, message, expected):
        inbox = queue.Queue(maxsize=1)
        ref = message.header['request_id']
        with self._lock:
            if self._error is not None:
                raise self._error
            if len(self._pending) >= 64:
                raise LanLinkError(ErrorCode.QUEUE_FULL)
            if expected == MessageType.USER_LIST:
                if self._user_request is not None:
                    raise ValueError('A USER_LIST request is already outstanding')
                self._user_request = ref
            self._pending[ref] = inbox
        try:
            self.transport.send(message)
            try:
                reply = inbox.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError('Timed out waiting for server response') from None
        except (OSError, EOFError):
            self.close()  # Avoid misattributing a late USER_LIST to a later request.
            raise
        if isinstance(reply, BaseException):
            raise reply
        if reply.type == MessageType.ERROR:
            raise LanLinkError(reply.header['code'])
        if reply.type != expected:
            self.close()
            raise ProtocolError(ErrorCode.INVALID_FIELD, 'Unexpected response type')
        return reply

    def send_private(self, recipient, text):
        return self._request(build_chat_private(recipient, text), MessageType.CHAT_ACK)

    def users(self):
        reply = self._request(build_user_list(), MessageType.USER_LIST)
        return json.loads(reply.payload)['users']

    def logout(self):
        try:
            self.transport.send(build_logout())
            if not self._stopped.wait(self.timeout):
                raise TimeoutError('Timed out waiting for logout')
        finally:
            self.close()

    def close(self):
        self._fail(EOFError('Client closed'))
        try:
            self.transport.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if threading.current_thread() is not self._reader:
            self._reader.join()
