"""Bounded thread-per-client TCP server; only HELLO/HELLO_ACK is supported."""
from dataclasses import dataclass, replace
import math
import socket
import threading
import time
from uuid import uuid4

from common.errors import ErrorCode, ProtocolError
from common.handshake import accept_hello
from common.models import ConnectionState
from common.protocol import Direction
from common.transport import SocketTransport


@dataclass
class Connection:
    connection_id: str
    socket: socket.socket
    worker: threading.Thread
    state: ConnectionState = ConnectionState.CONNECTING


class TCPServer:
    def __init__(self, host='127.0.0.1', port=5000, *, max_clients=64,
                 handshake_timeout=5.0, idle_timeout=45.0):
        if type(max_clients) is not int or max_clients < 1:
            raise ValueError('max_clients must be a positive integer')
        if any(not math.isfinite(value) or value <= 0
               for value in (handshake_timeout, idle_timeout)):
            raise ValueError('timeouts must be positive and finite')
        self.handshake_timeout = handshake_timeout
        self.idle_timeout = idle_timeout
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._connections = {}
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((host, port))
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
                transport.read_deadline = time.monotonic() + self.handshake_timeout
                connection.settimeout(self.handshake_timeout)
                accept_hello(transport)
                with self._lock:
                    if not self._stopping.is_set():
                        self._connections[key].state = ConnectionState.HELLO_ACCEPTED
                transport.read_deadline = None
                connection.settimeout(self.idle_timeout)
                # Keep the connection alive, but no later-stage messages are supported.
                if transport.receive() is not None:
                    raise ProtocolError(ErrorCode.INVALID_FIELD, 'Only HELLO is supported')
        except (OSError, EOFError, ProtocolError):
            # A peer failure affects only this connection.
            pass
        finally:
            with self._lock:
                record = self._connections.pop(key, None)
                if record is not None:
                    record.state = ConnectionState.CLOSED

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
