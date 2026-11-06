"""Qt-free shared values; immutable snapshots and events cross worker boundaries."""
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from common.errors import ErrorCode, TransferError


class MessageType(str, Enum):
    HELLO = "HELLO"
    HELLO_ACK = "HELLO_ACK"
    AUTH = "AUTH"
    AUTH_OK = "AUTH_OK"
    USER_LIST = "USER_LIST"
    LOGOUT = "LOGOUT"
    ERROR = "ERROR"
    CHAT_PUBLIC = "CHAT_PUBLIC"
    CHAT_PRIVATE = "CHAT_PRIVATE"
    CHAT_ACK = "CHAT_ACK"
    FILE_OFFER = "FILE_OFFER"
    FILE_ACCEPT = "FILE_ACCEPT"
    FILE_REJECT = "FILE_REJECT"
    FILE_CHUNK = "FILE_CHUNK"
    FILE_END = "FILE_END"
    FILE_RESULT = "FILE_RESULT"
    FILE_CANCEL = "FILE_CANCEL"
    PING = "PING"
    PONG = "PONG"
    DISCOVER = "DISCOVER"
    SERVER_INFO = "SERVER_INFO"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class Message:
    """Defensive header copy; omit payload from repr to protect AUTH secrets."""
    header: Mapping[str, Any]
    payload: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "header", _freeze(self.header))

    @property
    def type(self) -> MessageType:
        return MessageType(self.header["type"])


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    TLS_ESTABLISHED = "tls_established"
    HELLO_ACCEPTED = "hello_accepted"
    AUTHENTICATED = "authenticated"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class TransferDirection(str, Enum):
    SEND = "send"
    RECEIVE = "receive"


class TransferStatus(str, Enum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS = MappingProxyType({
    TransferStatus.OFFERED: frozenset({TransferStatus.ACCEPTED, TransferStatus.REJECTED,
                                      TransferStatus.CANCELLED, TransferStatus.FAILED}),
    TransferStatus.ACCEPTED: frozenset({TransferStatus.TRANSFERRING, TransferStatus.CANCELLED,
                                       TransferStatus.FAILED}),
    TransferStatus.TRANSFERRING: frozenset({TransferStatus.VERIFYING, TransferStatus.CANCELLED,
                                           TransferStatus.FAILED}),
    TransferStatus.VERIFYING: frozenset({TransferStatus.COMPLETED, TransferStatus.FAILED,
                                       TransferStatus.CANCELLED}),
    TransferStatus.COMPLETED: frozenset(),
    TransferStatus.REJECTED: frozenset(),
    TransferStatus.FAILED: frozenset(),
    TransferStatus.CANCELLED: frozenset(),
})


def is_terminal(status: TransferStatus) -> bool:
    return not ALLOWED_TRANSITIONS[status]


def check_transition(old: TransferStatus, new: TransferStatus) -> None:
    if new not in ALLOWED_TRANSITIONS[old]:
        raise TransferError(ErrorCode.TRANSFER_STATE)


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    message: str


@dataclass(frozen=True)
class TransferSnapshot:
    transfer_id: str
    direction: TransferDirection
    peer: str
    filename: str
    total_bytes: int
    processed_bytes: int
    status: TransferStatus
    error: ErrorInfo | None = None
    saved_path: str | None = None

    @property
    def fraction(self) -> float:
        if self.total_bytes == 0:
            return 1.0 if self.status == TransferStatus.COMPLETED else 0.0
        return min(1.0, max(0.0, self.processed_bytes / self.total_bytes))

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)


@dataclass(frozen=True)
class UserInfo:
    username: str
    online: bool
    ip: str | None
    port: int | None


@dataclass(frozen=True)
class ConnectionChanged:
    state: ConnectionState
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    username: str
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class UsersChanged:
    users: tuple[UserInfo, ...]


@dataclass(frozen=True)
class MessageReceived:
    message_id: str
    sender: str
    recipient: str | None
    text: str
    timestamp_ms: int


@dataclass(frozen=True)
class MessageStatusChanged:
    request_id: str
    message_id: str
    status: str
    timestamp_ms: int


@dataclass(frozen=True)
class FileOffered:
    snapshot: TransferSnapshot


@dataclass(frozen=True)
class TransferUpdated:
    snapshot: TransferSnapshot


@dataclass(frozen=True)
class OperationFailed:
    error: ErrorInfo
    request_id: str | None = None
    field: str | None = None
    transfer_id: str | None = None
