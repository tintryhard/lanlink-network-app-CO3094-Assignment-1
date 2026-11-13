"""Bounded TCP server (optionally TLS): authentication, chat routing and file transfer.

A UDP socket on the same port number answers DISCOVER so clients can find the server.
"""
from dataclasses import asdict, dataclass, field, replace
import ipaddress
import math
import socket
import ssl
import threading
import time
from uuid import uuid4

from common.errors import ErrorCode, ProtocolError, user_message
from common.handshake import accept_hello
from common.models import (ConnectionState, Message, MessageType, TransferStatus, UserInfo,
                           check_transition, is_terminal)
from common.protocol import (DEFAULT_LIMITS, Direction, build_auth_ok, build_chat_ack, build_error,
                             build_message, build_server_info, build_user_list, decode_datagram,
                             encode_message)
from common.transport import SocketTransport
from server.auth import CredentialStore


# Messages relayed unchanged between the two parties of an accepted transfer.
FILE_CONTENT_TYPES = {MessageType.FILE_CHUNK, MessageType.FILE_END,
                      MessageType.FILE_RESULT, MessageType.FILE_CANCEL}
# How long a route waits for the recipient's AUTH_OK to be sent. Kept well below
# the client's 5s request timeout so the sender always gets its ACK or ERROR.
AUTH_READY_WAIT_S = 1.0


@dataclass
class Connection:
    connection_id: str
    socket: socket.socket
    worker: threading.Thread
    state: ConnectionState = ConnectionState.CONNECTING
    transport: SocketTransport | None = None
    auth_ready: threading.Event = field(default_factory=threading.Event, repr=False)
    transfer_ids: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True)
class UserSession:
    username: str
    connection_id: str
    ip: str
    port: int
    online: bool = True


@dataclass
class TransferSession:
    offer: Message
    sender_id: str
    recipient_id: str
    deadline: float
    status: TransferStatus = TransferStatus.OFFERED
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class TCPServer:
    def __init__(self, host: str = '127.0.0.1', port: int = 5000, *, max_clients: int = 64,
                 handshake_timeout: float = 5.0, idle_timeout: float = 45.0,
                 credentials: CredentialStore | None = None,
                 transfer_timeout: float = 60.0, tls: ssl.SSLContext | None = None) -> None:
        if type(max_clients) is not int or max_clients < 1:
            raise ValueError('max_clients must be a positive integer')
        if any(not math.isfinite(value) or value <= 0
               for value in (handshake_timeout, idle_timeout, transfer_timeout)):
            raise ValueError('timeouts must be positive and finite')
        address = socket.gethostbyname(host)
        if tls is None and not ipaddress.ip_address(address).is_loopback:
            raise ValueError('A plaintext server requires a loopback bind; use TLS on a LAN')
        self.tls = tls
        self.credentials = credentials
        self._users: dict[str, UserSession] = {}
        self.handshake_timeout = handshake_timeout
        self.idle_timeout = idle_timeout
        self.transfer_timeout = transfer_timeout
        self._transfers: dict[str, TransferSession] = {}
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._connections: dict[str, Connection] = {}
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((address, port))
            self._listener.listen(max_clients)
            self._listener.settimeout(0.1)
            self.address = self._listener.getsockname()
            # UDP discovery uses the TCP port number; 0.0.0.0 also receives broadcasts.
            self._discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self._discovery.bind((address, self.address[1]))
                self._discovery.settimeout(0.1)  # wake up to notice shutdown
            except BaseException:
                self._discovery.close()
                raise
        except BaseException:
            self._listener.close()
            raise
        self._discovery_thread = threading.Thread(target=self._answer_discovery,
                                                  name='lanlink-discovery', daemon=True)

    def connections(self) -> dict[str, Connection]:
        """Return copies of records under the registry lock."""
        with self._lock:
            return {key: replace(value, transfer_ids=set(value.transfer_ids))
                    for key, value in self._connections.items()}

    def users(self) -> dict[str, UserSession]:
        """Snapshot of authenticated sessions; credential data is never included."""
        with self._lock:
            return dict(self._users)

    def transfers(self) -> dict[str, TransferSession]:
        """Snapshots of active negotiation sessions (no file contents)."""
        with self._lock:
            return {key: replace(value) for key, value in self._transfers.items()}

    def serve_forever(self) -> None:
        self._discovery_thread.start()
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
                    worker = threading.Thread(target=self._serve_client, args=(key, connection),
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

    def _answer_discovery(self) -> None:
        """UDP thread: answer each DISCOVER with SERVER_INFO (name, TCP port, TLS on/off).

        The client takes the server's IP from the datagram's source address.
        """
        while not self._stopping.is_set():
            try:
                # One byte over the limit, so decode_datagram can reject oversize data.
                data, sender = self._discovery.recvfrom(DEFAULT_LIMITS.max_datagram_bytes + 1)
            except socket.timeout:
                continue
            except OSError:
                return  # closed by shutdown()
            try:
                request = decode_datagram(data)  # only a valid DISCOVER passes
            except ProtocolError:
                continue  # UDP has no session to report errors on: ignore junk
            reply = build_server_info(request.header['request_id'], 'LanLink', self.address[1],
                                      tls=self.tls is not None)
            try:
                self._discovery.sendto(encode_message(reply), sender)
            except OSError:
                pass

    def _serve_client(self, key: str, connection: socket.socket) -> None:
        """Worker thread for one connection: HELLO, then the message loop, then cleanup.

        On exit it removes the user, fails this connection's transfers and
        unregisters the connection.
        """
        try:
            if self.tls is not None:
                # Handshake here, not in serve_forever, so a slow client never blocks accept().
                connection.settimeout(self.handshake_timeout)
                try:
                    connection = self.tls.wrap_socket(connection, server_side=True)
                except BaseException:
                    connection.close()
                    raise
                with self._lock:
                    self._connections[key].socket = connection  # what shutdown() interrupts
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
                self._dispatch_client_messages(key, connection, transport)
        except (OSError, EOFError, ProtocolError):
            # A peer failure affects only this connection.
            pass
        finally:
            with self._lock:
                for username, session in list(self._users.items()):
                    if session.connection_id == key:
                        del self._users[username]
                record = self._connections.get(key)
                if record is not None:
                    record.state = ConnectionState.CLOSED
                    record.auth_ready.set()  # Release any route waiting for AUTH_OK.
            try:
                self._fail_transfers_of(key, ErrorCode.CONNECTION_LOST)
            finally:
                # Keep the worker registered until its notifications finish so
                # shutdown can still interrupt I/O and join it.
                with self._lock:
                    self._connections.pop(key, None)

    def _dispatch_client_messages(self, key: str, connection: socket.socket,
                      transport: SocketTransport) -> None:
        """Handle one client's messages in order until LOGOUT, disconnect or a fatal error.

        Each request either gets its normal reply or one ERROR with ref_request_id.
        """
        username = None
        failures = 0
        while not self._stopping.is_set():
            # Bound a whole request, even if a peer sends bytes slowly.
            message = self._receive_until_deadline(key, transport)
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
                transport.send(build_user_list(users, ref_request_id=message.header['request_id']))
                continue
            elif message.type == MessageType.CHAT_PRIVATE:
                code = self._route_private(username, message, transport)
                if code is None:
                    continue
            elif message.type == MessageType.CHAT_PUBLIC:
                self._route_public(username, message, transport)
                continue
            elif message.type in {MessageType.FILE_OFFER, MessageType.FILE_ACCEPT, MessageType.FILE_REJECT}:
                code = self._negotiate_file_transfer(key, username, message)
                if code is None:
                    continue
            elif message.type in FILE_CONTENT_TYPES:
                code = self._relay_file_content(key, message)
                if code is None:
                    continue
            elif message.type == MessageType.LOGOUT:
                return
            else:
                code = ErrorCode.INVALID_FIELD
            transport.send(build_error(code, user_message(code),
                                       ref_request_id=message.header['request_id'],
                                       transfer_id=message.header.get('transfer_id')))
            if failures >= 3 or message.type not in {
                    MessageType.AUTH, MessageType.USER_LIST, MessageType.LOGOUT,
                    MessageType.CHAT_PRIVATE, MessageType.CHAT_PUBLIC,
                    MessageType.FILE_OFFER, MessageType.FILE_ACCEPT, MessageType.FILE_REJECT,
                    *FILE_CONTENT_TYPES}:
                return

    def _receive_until_deadline(self, key: str, transport: SocketTransport) -> Message | None:
        """Receive the next message, waking up early to expire this client's transfers.

        FrameDecoder keeps partial reads across these timer wakeups. At least the
        worker that created/accepted a transfer observes its new deadline.
        """
        idle_deadline = time.monotonic() + self.idle_timeout
        while True:
            self._fail_transfers_of(key, ErrorCode.TIMEOUT, expired_only=True)
            with self._lock:
                deadlines = [s.deadline for s in self._transfers.values()
                             if key in (s.sender_id, s.recipient_id)]
            transport.read_deadline = min([idle_deadline] + deadlines)
            try:
                return transport.receive()
            except socket.timeout:
                self._fail_transfers_of(key, ErrorCode.TIMEOUT, expired_only=True)
                if time.monotonic() >= idle_deadline:
                    with self._lock:
                        busy = any(key in (s.sender_id, s.recipient_id)
                                   for s in self._transfers.values())
                    if not busy:
                        raise
                    # A receiver only gets chunks and sends nothing until FILE_RESULT;
                    # a live transfer counts as activity (stalled ones expire above).
                    idle_deadline = time.monotonic() + self.idle_timeout

    def _send_to_connection(self, key: str, message: Message) -> bool:
        """Send a message to a connected, authenticated client; return False on failure.

        A failed write shuts that socket down, because a partial frame breaks the stream.
        """
        with self._lock:
            target = self._connections.get(key)
        if target is None or not target.auth_ready.wait(AUTH_READY_WAIT_S):
            return False
        with self._lock:
            if self._stopping.is_set() or target.state != ConnectionState.AUTHENTICATED:
                return False
        try:
            target.transport.send(message)
            return True
        except OSError:
            try:
                target.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return False

    def _fail_transfer_and_notify(self, session: TransferSession, code: ErrorCode) -> None:
        """Mark the transfer FAILED, remove it and send ERROR(code) to both parties.

        The caller holds session.lock; the registry lock is never held during I/O.
        """
        transfer_id = session.offer.header['transfer_id']
        with self._lock:
            if self._transfers.get(transfer_id) is not session:
                return
            check_transition(session.status, TransferStatus.FAILED)
            session.status = TransferStatus.FAILED
            del self._transfers[transfer_id]
        error = build_error(code, user_message(code), transfer_id=transfer_id)
        for key in (session.sender_id, session.recipient_id):
            self._send_to_connection(key, error)

    def _fail_transfers_of(self, key: str, code: ErrorCode, *, expired_only: bool = False) -> None:
        """Fail the transfers this connection takes part in (only expired ones if asked)."""
        with self._lock:
            sessions = [s for s in self._transfers.values()
                        if key in (s.sender_id, s.recipient_id) and
                        (not expired_only or s.deadline <= time.monotonic())]
        for session in sessions:
            # Never acquire a session lock while holding the registry lock.
            with session.lock:
                if not expired_only or session.deadline <= time.monotonic():
                    self._fail_transfer_and_notify(session, code)

    def _negotiate_file_transfer(self, key: str, username: str, message: Message) -> ErrorCode | None:
        """Handle FILE_OFFER, FILE_ACCEPT and FILE_REJECT.

        An offer creates an OFFERED session and is forwarded to the recipient. A
        decision from the recipient moves it to ACCEPTED or REJECTED and is sent to
        the sender, then echoed back to the recipient.
        """
        transfer_id = message.header['transfer_id']
        if message.type == MessageType.FILE_OFFER:
            with self._lock:
                recipient = self._users.get(message.header['recipient'])
                target = self._connections.get(recipient.connection_id) if recipient else None
            if target is None:
                return ErrorCode.RECIPIENT_OFFLINE
            if target.connection_id == key:
                return ErrorCode.INVALID_FIELD
            if not target.auth_ready.wait(AUTH_READY_WAIT_S):
                return ErrorCode.CONNECTION_LOST
            
            session = TransferSession(message, key, target.connection_id,
                                      time.monotonic() + self.transfer_timeout)
            with session.lock:
                with self._lock:
                    if (self._stopping.is_set() or self._users.get(recipient.username) != recipient or
                            target.state != ConnectionState.AUTHENTICATED):
                        return ErrorCode.RECIPIENT_OFFLINE
                    if (transfer_id in self._transfers or
                            any(transfer_id in c.transfer_ids for c in self._connections.values())):
                        return ErrorCode.TRANSFER_STATE
                    # Bound live sessions and replay tombstones for each connection.
                    owner = self._connections[key]
                    if len(owner.transfer_ids) >= 64:
                        return ErrorCode.QUEUE_FULL
                    # Update both sites 
                    owner.transfer_ids.add(transfer_id)
                    self._transfers[transfer_id] = session
                header = dict(message.header)
                header.pop('type')
                forwarded = build_message(MessageType.FILE_OFFER, **dict(header, sender=username))
                if not self._send_to_connection(target.connection_id, forwarded):
                    self._fail_transfer_and_notify(session, ErrorCode.CONNECTION_LOST)
            return None
        with self._lock:
            session = self._transfers.get(transfer_id)
        if session is None:
            return ErrorCode.TRANSFER_UNKNOWN
        with session.lock:
            with self._lock:
                if self._transfers.get(transfer_id) is not session:
                    return ErrorCode.TRANSFER_UNKNOWN
                if session.recipient_id != key:
                    return ErrorCode.TRANSFER_STATE
                if session.status != TransferStatus.OFFERED:
                    return ErrorCode.TRANSFER_STATE
                expired = session.deadline <= time.monotonic()
                if not expired:
                    status = (TransferStatus.ACCEPTED if message.type == MessageType.FILE_ACCEPT
                              else TransferStatus.REJECTED)
                    check_transition(session.status, status)
                    session.status = status
                    session.deadline = time.monotonic() + self.transfer_timeout
                    if status == TransferStatus.REJECTED:
                        del self._transfers[transfer_id]
            if expired:
                self._fail_transfer_and_notify(session, ErrorCode.TIMEOUT)
                return ErrorCode.TRANSFER_UNKNOWN
            
            # Notify the sender first. Echo the decision to the recipient only
            # after forwarding succeeds; this is negotiation, never completion.
            if not self._send_to_connection(session.sender_id, message):
                if status == TransferStatus.ACCEPTED:
                    self._fail_transfer_and_notify(session, ErrorCode.CONNECTION_LOST)
                else:
                    self._send_to_connection(key, build_error(ErrorCode.CONNECTION_LOST,
                                          user_message(ErrorCode.CONNECTION_LOST), transfer_id=transfer_id))
            elif not self._send_to_connection(key, message) and status == TransferStatus.ACCEPTED:
                self._fail_transfer_and_notify(session, ErrorCode.CONNECTION_LOST)
        return None

    @staticmethod
    def _status_after_message(session: TransferSession, key: str, message: Message) -> TransferStatus | None:
        """New transfer state after this message, or None if this party may not send it now."""
        status, kind = session.status, message.type
        from_sender, from_recipient = key == session.sender_id, key == session.recipient_id
        if kind == MessageType.FILE_CANCEL and (from_sender or from_recipient):
            return TransferStatus.CANCELLED
        if from_sender and status in {TransferStatus.ACCEPTED, TransferStatus.TRANSFERRING}:
            if kind == MessageType.FILE_CHUNK:
                return TransferStatus.TRANSFERRING
            if kind == MessageType.FILE_END:
                return TransferStatus.VERIFYING
        if from_recipient and kind == MessageType.FILE_RESULT and status != TransferStatus.OFFERED:
            # A failed result may come early (e.g. bad chunk); success only after FILE_END.
            if not message.header['ok']:
                return TransferStatus.FAILED
            if status == TransferStatus.VERIFYING:
                return TransferStatus.COMPLETED
        return None

    def _relay_file_content(self, key: str, message: Message) -> ErrorCode | None:
        """Forward FILE_CHUNK/END/RESULT/CANCEL to the other party of the transfer.

        The server only checks who may send what and when; the receiving client
        checks sequence numbers, sizes and the SHA-256 digest.
        """
        transfer_id = message.header['transfer_id']
        with self._lock:
            session = self._transfers.get(transfer_id)
        if session is None:
            return ErrorCode.TRANSFER_UNKNOWN
        with session.lock:
            with self._lock:
                if self._transfers.get(transfer_id) is not session:
                    return ErrorCode.TRANSFER_UNKNOWN
                status = self._status_after_message(session, key, message)
                if status is None:
                    return ErrorCode.TRANSFER_STATE
                session.status = status
                session.deadline = time.monotonic() + self.transfer_timeout  # activity resets the timer
                finished = is_terminal(status)
                if finished:
                    del self._transfers[transfer_id]
            peer = session.recipient_id if key == session.sender_id else session.sender_id
            if not self._send_to_connection(peer, message) and not finished:
                self._fail_transfer_and_notify(session, ErrorCode.CONNECTION_LOST)
        return None

    def _route_public(self, username: str, message: Message,
                      sender_transport: SocketTransport) -> None:
        """Forward a public chat to every other online user, then ACK the sender."""
        with self._lock:
            targets = [(session, self._connections.get(session.connection_id))
                       for session in self._users.values()
                       if session.online and session.username != username]
        message_id = uuid4().hex
        timestamp_ms = int(time.time() * 1000)
        forwarded = build_message(MessageType.CHAT_PUBLIC, message.payload,
                                  sender=username, message_id=message_id, timestamp_ms=timestamp_ms)
        for session, target in targets:
            if target is None or not target.auth_ready.wait(AUTH_READY_WAIT_S):
                continue
            # Recheck the snapshot after waiting; never send to a replacement
            # login or a session removed by disconnect/shutdown.
            with self._lock:
                if (self._stopping.is_set() or self._users.get(session.username) != session or
                        self._connections.get(session.connection_id) is not target or
                        target.state != ConnectionState.AUTHENTICATED):
                    continue
            try:
                target.transport.send(forwarded)
            except OSError:
                # A failed/partial write invalidates only this recipient stream.
                try:
                    target.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        # Best-effort acceptance even with zero recipients or partial/all write
        # failures. The existing ACK schema carries no delivery count/receipt.
        sender_transport.send(build_chat_ack(message.header['request_id'], message_id, timestamp_ms))

    def _route_private(self, username: str, message: Message,
                       sender_transport: SocketTransport) -> ErrorCode | None:
        """Forward a private chat to its recipient, then ACK the sender."""
        recipient = message.header['recipient']
        with self._lock:
            session = self._users.get(recipient)
            target = self._connections.get(session.connection_id) if session else None
        if target is None:
            # No active session, whether the account exists or not.
            return ErrorCode.RECIPIENT_OFFLINE
        # Never send unsolicited chat before the target's AUTH_OK. Also released
        # by worker cleanup if its authentication reply could not be sent.
        if not target.auth_ready.wait(AUTH_READY_WAIT_S):
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

    def shutdown(self) -> None:
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
        self._discovery.close()
        if (self._discovery_thread.ident is not None
                and self._discovery_thread is not threading.current_thread()):
            self._discovery_thread.join()
        for record in records:
            try:
                record.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for record in records:
            if record.worker is not threading.current_thread():
                record.worker.join()
        with self._lock:
            self._transfers.clear()
