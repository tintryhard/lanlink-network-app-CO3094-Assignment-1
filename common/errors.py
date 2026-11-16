"""Stable business errors shared by the wire, workers and the UI."""
from enum import Enum


class ErrorCode(str, Enum):
    """Stable error codes; they appear on the wire in ERROR and FILE_RESULT messages."""
    BAD_FRAME = "BAD_FRAME"
    HEADER_TOO_LARGE = "HEADER_TOO_LARGE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    PAYLOAD_LENGTH_MISMATCH = "PAYLOAD_LENGTH_MISMATCH"
    UNEXPECTED_EOF = "UNEXPECTED_EOF"
    BAD_JSON = "BAD_JSON"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_FIELD = "INVALID_FIELD"
    SPOOFED_FIELD = "SPOOFED_FIELD"
    BAD_PAYLOAD = "BAD_PAYLOAD"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    AUTH_FAILED = "AUTH_FAILED"
    ALREADY_LOGGED_IN = "ALREADY_LOGGED_IN"
    RECIPIENT_OFFLINE = "RECIPIENT_OFFLINE"
    RECIPIENT_UNKNOWN = "RECIPIENT_UNKNOWN"
    QUEUE_FULL = "QUEUE_FULL"
    TIMEOUT = "TIMEOUT"
    CONNECTION_LOST = "CONNECTION_LOST"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    UNSAFE_FILENAME = "UNSAFE_FILENAME"
    TRANSFER_UNKNOWN = "TRANSFER_UNKNOWN"
    TRANSFER_STATE = "TRANSFER_STATE"
    SEQ_MISMATCH = "SEQ_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    DISK_FULL = "DISK_FULL"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    REJECTED_BY_PEER = "REJECTED_BY_PEER"
    CANCELLED_BY_PEER = "CANCELLED_BY_PEER"
    CANCELLED_LOCAL = "CANCELLED_LOCAL"
    INTERNAL = "INTERNAL"


USER_MESSAGES = {
    ErrorCode.BAD_FRAME: "Invalid frame.",
    ErrorCode.HEADER_TOO_LARGE: "Header too large.",
    ErrorCode.PAYLOAD_TOO_LARGE: "Payload too large.",
    ErrorCode.PAYLOAD_LENGTH_MISMATCH: "Payload length mismatch.",
    ErrorCode.UNEXPECTED_EOF: "Connection closed mid-frame.",
    ErrorCode.BAD_JSON: "Invalid JSON.",
    ErrorCode.UNSUPPORTED_VERSION: "Unsupported protocol version.",
    ErrorCode.UNKNOWN_TYPE: "Unknown message type.",
    ErrorCode.MISSING_FIELD: "Missing required field.",
    ErrorCode.INVALID_FIELD: "Invalid field.",
    ErrorCode.SPOOFED_FIELD: "Invalid sender field.",
    ErrorCode.BAD_PAYLOAD: "Invalid payload.",
    ErrorCode.NOT_AUTHENTICATED: "Not logged in.",
    ErrorCode.AUTH_FAILED: "Wrong username or password.",
    ErrorCode.ALREADY_LOGGED_IN: "This account is already logged in.",
    ErrorCode.RECIPIENT_OFFLINE: "User is offline.",
    ErrorCode.RECIPIENT_UNKNOWN: "User not found.",
    ErrorCode.QUEUE_FULL: "Too many pending events.",
    ErrorCode.TIMEOUT: "Request timed out.",
    ErrorCode.CONNECTION_LOST: "Connection lost.",
    ErrorCode.FILE_TOO_LARGE: "File too large.",
    ErrorCode.FILE_NOT_FOUND: "File not found.",
    ErrorCode.SOURCE_CHANGED: "File changed while sending.",
    ErrorCode.UNSAFE_FILENAME: "Unsafe file name.",
    ErrorCode.TRANSFER_UNKNOWN: "Unknown transfer.",
    ErrorCode.TRANSFER_STATE: "Invalid transfer state.",
    ErrorCode.SEQ_MISMATCH: "Chunk out of order.",
    ErrorCode.SIZE_MISMATCH: "File size mismatch.",
    ErrorCode.HASH_MISMATCH: "Checksum mismatch.",
    ErrorCode.DISK_FULL: "Disk full.",
    ErrorCode.PERMISSION_DENIED: "Permission denied.",
    ErrorCode.REJECTED_BY_PEER: "Declined by recipient.",
    ErrorCode.CANCELLED_BY_PEER: "Cancelled by the other side.",
    ErrorCode.CANCELLED_LOCAL: "Cancelled.",
    ErrorCode.INTERNAL: "Something went wrong.",
}


def user_message(code: ErrorCode) -> str:
    """Return the central translation; never display unknown peer text as a code."""
    return USER_MESSAGES.get(code, USER_MESSAGES[ErrorCode.INTERNAL])


class LanLinkError(Exception):
    """Business failure with a stable code and a password-free message."""
    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        """Store the code; the message defaults to the code's user-facing text."""
        self.code = ErrorCode(code)
        self.message = message or user_message(self.code)
        super().__init__(self.message)


class ProtocolError(LanLinkError):
    """Fatal malformed frame; do not resynchronise the stream."""


class TransferError(LanLinkError):
    """Expected transfer failure including disk and source-file errors."""
