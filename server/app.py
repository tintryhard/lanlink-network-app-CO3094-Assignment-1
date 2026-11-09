"""Bounded thread-per-client TCP server; HELLO, authentication and private messaging."""
from dataclasses import asdict, dataclass, field, replace
import ipaddress
import math
import socket
import threading
import time
from uuid import uuid4

from common.errors import ErrorCode, ProtocolError, user_message
from common.handshake import accept_hello
from common.models import ConnectionState, MessageType, UserInfo
from common.protocol import (Direction, build_auth_ok, build_chat_ack, build_error,
                             build_message, build_user_list)
from common.transport import SocketTransport


@dataclass
class Connection:
    connection_id: str
    socket: socket.socket
    worker: threading.Thread
    state: ConnectionState = ConnectionState.CONNECTING
    transport: SocketTransport | None = None
    auth_ready: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass(frozen=True)
class UserSession:
    username: str
    connection_id: str
    ip: str
    port: int
    online: bool = True


class TCPServer:
    def __init__(self, host='127.0.0.1', port=5000, *, max_clients=64,
                 handshake_timeout=5.0, idle_timeout=45.0, credentials=None):
        if type(max_clients) is not int or max_clients < 1:
            raise ValueError('max_clients must be a positive integer')
        if any(not math.isfinite(value) or value <= 0
               for value in (handshake_timeout, idle_timeout)):
            raise ValueError('timeouts must be positive and finite')
        address = socket.gethostbyname(host)
        if not ipaddress.ip_address(address).is_loopback:
            raise ValueError('TCP server requires a loopback bind until TLS is implemented')
        self.credentials = credentials
        self._users = {}
        self.handshake_timeout = handshake_timeout
        self.idle_timeout = idle_timeout
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._connections = {}
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((address, port))
            self._listener.listen(max_clients)
            self._listener.settimeout(0.1)
            self.address = self._listener.getsockname()
        except BaseException:
            self._listener.close()
            raise

    def connections(self):
        """Return copies of records under the registry lock."""
        with self._lock:
            return {key: replace(value) for key, value in self._connections.items()}

    def users(self):
        """Snapshot of authenticated sessions; credential data is never included."""
        with self._lock:
            return dict(self._users)

    def serve_forever(self):
        try:
            while not self._stopping.is_set():
                try:
                    connection, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                with self._lock:
                    if self._stopping.is_set() or len(self._connections) >= self.max_clients:
                        connection.close()
                        continue
                    key = uuid4().hex
                    worker = threading.Thread(target=self._run_client, args=(key, connection),
                                              name=f'lanlink-{key}')
                    self._connections[key] = Connection(key, connection, worker)
                    try:
                        worker.start()
                    except BaseException:
                        self._connections.pop(key)
                        connection.close()
                        raise
        finally:
            self.shutdown()

    def _run_client(self, key, connection):
        try:
            with connection:
                transport = SocketTransport(connection, direction=Direction.CLIENT_TO_SERVER)
                with self._lock:
                    self._connections[key].transport = transport
                transport.read_deadline = time.monotonic() + self.handshake_timeout
                connection.settimeout(self.handshake_timeout)
                try:
                    accept_hello(transport)
                except ProtocolError:
                    # A valid message in the wrong phase can receive an ERROR.
                    # Corrupt framing is closed without trying to resynchronise.
                    if transport.decoder.at_clean_boundary():
                        transport.send(build_error(ErrorCode.INVALID_FIELD, 'Expected HELLO before authentication'))
                    raise
                with self._lock:
                    if not self._stopping.is_set():
                        self._connections[key].state = ConnectionState.HELLO_ACCEPTED
                transport.read_deadline = None
                connection.settimeout(self.idle_timeout)
                self._session_loop(key, connection, transport)
        except (OSError, EOFError, ProtocolError):
            # A peer failure affects only this connection.
            pass
        finally:
            with self._lock:
                for username, session in list(self._users.items()):
                    if session.connection_id == key:
                        del self._users[username]
                record = self._connections.pop(key, None)
                if record is not None:
                    record.state = ConnectionState.CLOSED
                    record.auth_ready.set()  # Release any route waiting for AUTH_OK.

    def _session_loop(self, key, connection, transport):
        username = None
        failures = 0
        while not self._stopping.is_set():
            # Bound a whole request, even if a peer sends bytes slowly.
            transport.read_deadline = time.monotonic() + self.idle_timeout
            message = transport.receive()
            connection.settimeout(self.idle_timeout)
            if message is None:
                return
            code = None
            if message.type == MessageType.AUTH:
                if username is not None:
                    code = ErrorCode.ALREADY_LOGGED_IN
                elif (self.credentials is None or
                      not self.credentials.verify(message.header['username'], message.payload)):
                    code = ErrorCode.AUTH_FAILED
                    failures += 1
                else:
                    candidate = message.header['username']
                    ip, port = connection.getpeername()
                    # Check-and-insert must be one atomic operation. No I/O/hash under lock.
                    with self._lock:
                        if self._stopping.is_set():
                            return
                        if candidate in self._users:
                            code = ErrorCode.ALREADY_LOGGED_IN
                        else:
                            self._users[candidate] = UserSession(candidate, key, ip, port)
                            self._connections[key].state = ConnectionState.AUTHENTICATED
                            username = candidate
                    if code is None:
                        transport.send(build_auth_ok(message.header['request_id'], username))
                        with self._lock:
                            self._connections[key].auth_ready.set()
                        continue
            elif username is None:
                code = ErrorCode.NOT_AUTHENTICATED
            elif message.type == MessageType.USER_LIST:
                with self._lock:
                    users = [asdict(UserInfo(s.username, s.online, s.ip, s.port))
                             for s in sorted(self._users.values(), key=lambda s: s.username)]
                transport.send(build_user_list(users))
                continue
            elif message.type == MessageType.CHAT_PRIVATE:
                code = self._route_private(username, message, transport)
                if code is None:
                    continue
            elif message.type == MessageType.LOGOUT:
                return
            else:
                code = ErrorCode.INVALID_FIELD
            transport.send(build_error(code, user_message(code),
                                       ref_request_id=message.header['request_id']))
            if failures >= 3 or message.type not in {MessageType.AUTH, MessageType.USER_LIST, MessageType.LOGOUT, MessageType.CHAT_PRIVATE}:
                return

    def _route_private(self, username, message, sender_transport):
        recipient = message.header['recipient']
        with self._lock:
            session = self._users.get(recipient)
            target = self._connections.get(session.connection_id) if session else None
        if target is None:
            # No active session, whether the account exists or not.
            return ErrorCode.RECIPIENT_OFFLINE
        # Never send unsolicited chat before the target's AUTH_OK. Also released
        # by worker cleanup if its authentication reply could not be sent.
        if not target.auth_ready.wait(self.idle_timeout):
            return ErrorCode.CONNECTION_LOST
        with self._lock:
            if (self._stopping.is_set() or self._users.get(recipient) != session or
                    self._connections.get(session.connection_id) is not target or
                    target.state != ConnectionState.AUTHENTICATED):
                return ErrorCode.RECIPIENT_OFFLINE
        message_id = uuid4().hex
        timestamp_ms = int(time.time() * 1000)
        forwarded = build_message(MessageType.CHAT_PRIVATE, message.payload,
                                  recipient=recipient, sender=username,
                                  message_id=message_id, timestamp_ms=timestamp_ms)
        try:
            # Exactly the same transport/write lock used for this peer's replies.
            target.transport.send(forwarded)
        except OSError:
            # A partially written frame makes the recipient stream unusable.
            try:
                target.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return ErrorCode.CONNECTION_LOST
        # Acceptance is not a recipient read receipt (nor durable delivery).
        sender_transport.send(build_chat_ack(message.header['request_id'], message_id, timestamp_ms))
        return None

    def shutdown(self):
        """Stop admissions, interrupt blocked I/O and join all registered workers."""
        with self._lock:
            self._stopping.set()
            records = list(self._connections.values())
            for record in records:
                record.state = ConnectionState.CLOSING
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._listener.close()
        for record in records:
            try:
                record.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for record in records:
            if record.worker is not threading.current_thread():
                record.worker.join()
