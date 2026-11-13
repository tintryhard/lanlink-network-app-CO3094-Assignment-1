"""Records the server keeps in its registries (all guarded by TCPServer._lock)."""
from dataclasses import dataclass, field
import socket
import threading

from common.models import ConnectionState, Message, TransferStatus
from common.transport import SocketTransport


@dataclass
class Connection:
    """One accepted socket and its worker thread (key: connection_id)."""
    connection_id: str
    socket: socket.socket
    worker: threading.Thread
    state: ConnectionState = ConnectionState.CONNECTING
    transport: SocketTransport | None = None
    # field(default_factory=...) gives every record its own Event/set instead of one shared default.
    auth_ready: threading.Event = field(default_factory=threading.Event, repr=False)  # set once AUTH_OK is sent
    transfer_ids: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True)
class UserSession:
    """One logged-in user (key: username); frozen, so it is replaced rather than changed."""
    username: str
    connection_id: str
    ip: str
    port: int
    online: bool = True


@dataclass
class TransferSession:
    """One file transfer the server is relaying between sender and recipient (key: transfer_id)."""
    offer: Message
    sender_id: str
    recipient_id: str
    deadline: float
    status: TransferStatus = TransferStatus.OFFERED
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # orders this transfer's messages
