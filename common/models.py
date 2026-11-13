"""Shared values: message types, the Message record and transfer states.

The records are frozen (immutable), so threads can pass them around safely.
"""
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from common.errors import ErrorCode, TransferError


class MessageType(str, Enum):
    """Every message type of protocol v1; the value is the wire string in the header's `type`."""
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
    """Return a read-only copy: dicts become MappingProxyType, lists become tuples (recursively)."""
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
        """Freeze the header so no thread can change a message after it is built."""
        # A frozen dataclass forbids `self.header = ...`; object.__setattr__ bypasses that once.
        object.__setattr__(self, "header", _freeze(self.header))

    @property
    def type(self) -> MessageType:
        """The header's `type` as a MessageType."""
        return MessageType(self.header["type"])


class ConnectionState(str, Enum):
    """Lifecycle of one server-side connection, from accept to close."""
    CONNECTING = "connecting"
    HELLO_ACCEPTED = "hello_accepted"
    AUTHENTICATED = "authenticated"
    CLOSING = "closing"
    CLOSED = "closed"


class TransferDirection(str, Enum):
    """Whether this side sends or receives the file."""
    SEND = "send"
    RECEIVE = "receive"


class TransferStatus(str, Enum):
    """States of a file transfer; ALLOWED_TRANSITIONS lists the legal moves."""
    OFFERED = "offered"
    ACCEPTED = "accepted"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Legal next states for each status. Final states map to an empty set.
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
    """True for a final status (no further transition is allowed)."""
    return not ALLOWED_TRANSITIONS[status]


def check_transition(old: TransferStatus, new: TransferStatus) -> None:
    """Raise TransferError(TRANSFER_STATE) unless old -> new is a legal move."""
    if new not in ALLOWED_TRANSITIONS[old]:
        raise TransferError(ErrorCode.TRANSFER_STATE)


@dataclass(frozen=True)
class ErrorInfo:
    """An error code with its human-readable message."""
    code: ErrorCode
    message: str


@dataclass(frozen=True)
class TransferSnapshot:
    """Immutable picture of one transfer at a moment, handed to the UI."""
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
        """Progress from 0.0 to 1.0; an empty file is 1.0 only once completed."""
        if self.total_bytes == 0:
            return 1.0 if self.status == TransferStatus.COMPLETED else 0.0
        return min(1.0, max(0.0, self.processed_bytes / self.total_bytes))

    @property
    def is_terminal(self) -> bool:
        """True once the transfer has ended."""
        return is_terminal(self.status)


@dataclass(frozen=True)
class UserInfo:
    """One entry of the USER_LIST payload."""
    username: str
    online: bool
    ip: str | None
    port: int | None
