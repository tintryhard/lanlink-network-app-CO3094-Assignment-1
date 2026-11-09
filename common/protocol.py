"""Protocol v1 contract. Pure codec: no sockets, disk I/O, TLS or Qt."""
from dataclasses import asdict, dataclass
from enum import Enum
import ipaddress
import json
import re
import struct
import unicodedata
from typing import Any, Mapping
from uuid import uuid4
from common.errors import ErrorCode, ProtocolError
from common.models import Message, MessageType

PROTOCOL_VERSION = 1


class Direction(Enum):
    CLIENT_TO_SERVER = "client_to_server"
    SERVER_TO_CLIENT = "server_to_client"
    UDP_REQUEST = "udp_request"
    UDP_REPLY = "udp_reply"
    ANY = "any"


FILE_MESSAGE_ROLES = {
    MessageType.FILE_OFFER: "sender", MessageType.FILE_CHUNK: "sender",
    MessageType.FILE_END: "sender", MessageType.FILE_ACCEPT: "receiver",
    MessageType.FILE_REJECT: "receiver", MessageType.FILE_RESULT: "receiver",
    MessageType.FILE_CANCEL: "either",
}


@dataclass(frozen=True)
class ProtocolLimits:
    max_header_bytes: int = 16 * 1024
    max_chunk_bytes: int = 64 * 1024
    max_file_bytes: int = 100 * 1024 * 1024
    max_chat_bytes: int = 8 * 1024
    max_user_list_bytes: int = 64 * 1024
    max_datagram_bytes: int = 1024
    heartbeat_interval_s: int = 15
    idle_timeout_s: int = 45

    def __post_init__(self) -> None:
        ceilings = {"max_header_bytes": 1048576, "max_chunk_bytes": 1048576,
                    "max_file_bytes": (1 << 63) - 1, "max_chat_bytes": 1048576,
                    "max_user_list_bytes": 1048576, "max_datagram_bytes": 65507,
                    "heartbeat_interval_s": 86400, "idle_timeout_s": 86400}
        for key, ceiling in ceilings.items():
            _integer(getattr(self, key), key, 1, ceiling)
        if self.idle_timeout_s <= self.heartbeat_interval_s:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "idle_timeout_s must exceed heartbeat_interval_s")

    def to_dict(self) -> dict[str, int]:
        """Advertise application limits in HELLO_ACK."""
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Mapping[str, int]) -> "ProtocolLimits":
        """Parse exactly the published, positive bounded integer fields."""
        if not isinstance(values, Mapping):
            raise ProtocolError(ErrorCode.INVALID_FIELD, "limits must be an object")
        keys = set(cls.__dataclass_fields__)
        if set(values) != keys:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "limits must contain exactly the published fields")
        return cls(**values)


MAX_INT = (1 << 63) - 1
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
TRANSFER_RE = re.compile(r"[0-9a-f]{32}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def _integer(value: Any, name: str, minimum: int = 0, maximum: int = MAX_INT) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(ErrorCode.INVALID_FIELD, f"{name} must be an integer in {minimum}..{maximum}")


DEFAULT_LIMITS = ProtocolLimits()

# Required/optional fields; direction-dependent additions are applied separately.
SCHEMAS = {
    MessageType.HELLO: ("client_name client_version", ""),
    MessageType.HELLO_ACK: ("ref_request_id server_name limits", ""),
    MessageType.AUTH: ("username", ""),
    MessageType.AUTH_OK: ("ref_request_id username", ""),
    MessageType.USER_LIST: ("", ""),
    MessageType.LOGOUT: ("", "reason"),
    MessageType.ERROR: ("code message", "ref_request_id field transfer_id"),
    MessageType.CHAT_PUBLIC: ("", ""),
    MessageType.CHAT_PRIVATE: ("recipient", ""),
    MessageType.CHAT_ACK: ("ref_request_id message_id timestamp_ms status", ""),
    MessageType.FILE_OFFER: ("transfer_id recipient filename size sha256 chunk_size", ""),
    MessageType.FILE_ACCEPT: ("transfer_id", ""),
    MessageType.FILE_REJECT: ("transfer_id", "reason"),
    MessageType.FILE_CHUNK: ("transfer_id seq", ""),
    MessageType.FILE_END: ("transfer_id total_chunks total_bytes", ""),
    MessageType.FILE_RESULT: ("transfer_id ok received_bytes", "sha256 error_code"),
    MessageType.FILE_CANCEL: ("transfer_id", "reason"),
    MessageType.PING: ("", ""),
    MessageType.PONG: ("ref_request_id", ""),
    MessageType.DISCOVER: ("", ""),
    MessageType.SERVER_INFO: ("ref_request_id server_name port tls", "host"),
}
CLIENT_ONLY = {MessageType.HELLO, MessageType.AUTH}
SERVER_ONLY = {MessageType.HELLO_ACK, MessageType.AUTH_OK, MessageType.ERROR, MessageType.CHAT_ACK}
SERVER_FIELDS = {"sender", "message_id", "timestamp_ms"}
COMMON_FIELDS = {"version", "type", "request_id", "payload_length"}


def _plain(value: Any, depth: int = 0) -> Any:
    """Convert immutable message mappings back to JSON-compatible containers."""
    # Header schemas are shallow; reject cycles/deep caller input before Python
    # recursion can escape as an untyped exception (including before encoding).
    if depth > 32:
        raise ProtocolError(ErrorCode.BAD_JSON, "Header nesting exceeds limit")
    if isinstance(value, Mapping):
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    return value


def _string(value: Any, name: str, maximum: int = 256) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ProtocolError(ErrorCode.INVALID_FIELD, f"{name} must be a nonempty string of at most {maximum} characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ProtocolError(ErrorCode.INVALID_FIELD, f"{name} contains invalid Unicode") from None
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ProtocolError(ErrorCode.INVALID_FIELD, f"{name} contains control characters")


def _username(value: Any, name: str) -> None:
    _string(value, name, 32)
    if any(char.isspace() for char in value):
        raise ProtocolError(ErrorCode.INVALID_FIELD, f"{name} contains whitespace")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(ErrorCode.BAD_JSON, "Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ProtocolError(ErrorCode.BAD_JSON, "Non-finite JSON number")


def _unicode_tree(value: Any) -> None:
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, dict):
        for key, item in value.items():
            _unicode_tree(key)
            _unicode_tree(item)
    elif isinstance(value, list):
        for item in value:
            _unicode_tree(item)


def _json(data: bytes) -> Any:
    try:
        value = json.loads(data.decode("utf-8", errors="strict"),
                           object_pairs_hook=_pairs, parse_constant=_constant)
        _unicode_tree(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError(ErrorCode.BAD_JSON, "Invalid JSON or Unicode") from None


def _effective_direction(kind: MessageType, h: dict[str, Any], direction: Direction) -> Direction:
    if direction == Direction.ANY:
        if kind == MessageType.DISCOVER:
            return Direction.UDP_REQUEST
        if kind == MessageType.SERVER_INFO:
            return Direction.UDP_REPLY
        if kind in SERVER_ONLY or SERVER_FIELDS.intersection(h) or (kind == MessageType.USER_LIST and h["payload_length"]):
            return Direction.SERVER_TO_CLIENT
        return Direction.CLIENT_TO_SERVER
    return direction


def _validate_header(h: dict[str, Any], limits: ProtocolLimits, direction: Direction) -> MessageType:
    if not isinstance(h, dict):
        raise ProtocolError(ErrorCode.BAD_JSON, "Header must be an object")
    if direction == Direction.CLIENT_TO_SERVER and SERVER_FIELDS.intersection(h):
        raise ProtocolError(ErrorCode.SPOOFED_FIELD)
    if COMMON_FIELDS - h.keys():
        raise ProtocolError(ErrorCode.MISSING_FIELD)
    _integer(h["version"], "version")
    if h["version"] != PROTOCOL_VERSION:
        raise ProtocolError(ErrorCode.UNSUPPORTED_VERSION)
    if not isinstance(h["type"], str):
        raise ProtocolError(ErrorCode.INVALID_FIELD, "type must be a string")
    try:
        kind = MessageType(h["type"])
    except ValueError:
        raise ProtocolError(ErrorCode.UNKNOWN_TYPE) from None
    d = _effective_direction(kind, h, direction)
    udp = {Direction.UDP_REQUEST: MessageType.DISCOVER, Direction.UDP_REPLY: MessageType.SERVER_INFO}
    if d in udp:
        if kind != udp[d]:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "Message not allowed in UDP direction")
    elif kind in udp.values() or (d == Direction.CLIENT_TO_SERVER and kind in SERVER_ONLY) or (d == Direction.SERVER_TO_CLIENT and kind in CLIENT_ONLY):
        raise ProtocolError(ErrorCode.INVALID_FIELD, "Message not allowed in this direction")
    required_text, optional_text = SCHEMAS[kind]
    required = COMMON_FIELDS | set(required_text.split())
    optional = set(optional_text.split())
    if d == Direction.SERVER_TO_CLIENT:
        if kind == MessageType.USER_LIST:
            required.add("ref_request_id")
        if kind in {MessageType.CHAT_PUBLIC, MessageType.CHAT_PRIVATE}:
            required |= SERVER_FIELDS
        if kind == MessageType.FILE_OFFER:
            required.add("sender")
    if required - h.keys():
        raise ProtocolError(ErrorCode.MISSING_FIELD)
    if h.keys() - required - optional:
        raise ProtocolError(ErrorCode.INVALID_FIELD, "Unknown header field")
    for name in ("request_id", "ref_request_id", "message_id"):
        if name in h and (not isinstance(h[name], str) or not ID_RE.fullmatch(h[name])):
            raise ProtocolError(ErrorCode.INVALID_FIELD, f"Invalid {name}")
    _integer(h["payload_length"], "payload_length")
    ceiling = max_payload_for(kind, limits)
    if kind == MessageType.USER_LIST and d == Direction.CLIENT_TO_SERVER:
        ceiling = 0
    if h["payload_length"] > ceiling:
        raise ProtocolError(ErrorCode.PAYLOAD_TOO_LARGE)
    if kind in {MessageType.AUTH, MessageType.CHAT_PUBLIC, MessageType.CHAT_PRIVATE, MessageType.FILE_CHUNK} and h["payload_length"] == 0:
        raise ProtocolError(ErrorCode.BAD_PAYLOAD)
    if kind == MessageType.USER_LIST and d == Direction.SERVER_TO_CLIENT and not h["payload_length"]:
        raise ProtocolError(ErrorCode.BAD_PAYLOAD)
    for name in ("username", "sender", "recipient"):
        if name in h:
            _username(h[name], name)
    for name, maximum in {"client_name": 64, "client_version": 32, "server_name": 64,
                          "host": 255, "message": 256, "field": 64, "status": 16}.items():
        if name in h:
            _string(h[name], name, maximum)
    if "reason" in h:
        _string(h["reason"], "reason", 128)
    for name in ("seq", "total_chunks", "timestamp_ms"):
        if name in h:
            _integer(h[name], name)
    for name in ("size", "total_bytes", "received_bytes"):
        if name in h:
            _integer(h[name], name)
            if h[name] > limits.max_file_bytes:
                raise ProtocolError(ErrorCode.FILE_TOO_LARGE)
    if "chunk_size" in h:
        _integer(h["chunk_size"], "chunk_size", 1, limits.max_chunk_bytes)
    if "port" in h:
        _integer(h["port"], "port", 1, 65535)
    for name in ("ok", "tls"):
        if name in h and type(h[name]) is not bool:
            raise ProtocolError(ErrorCode.INVALID_FIELD, f"{name} must be bool")
    for name, pattern in (("transfer_id", TRANSFER_RE), ("sha256", HASH_RE)):
        if name in h and (not isinstance(h[name], str) or not pattern.fullmatch(h[name])):
            raise ProtocolError(ErrorCode.INVALID_FIELD, f"Invalid {name}")
    if "filename" in h:
        name = h["filename"]
        try:
            _string(name, "filename", 255)
            safe = len(name.encode("utf-8")) <= 255 and name not in {".", ".."} and not any(c in name for c in "/\\")
        except ProtocolError:
            safe = False
        if not safe:
            raise ProtocolError(ErrorCode.UNSAFE_FILENAME)
    for name in ("code", "error_code"):
        if name in h:
            try:
                ErrorCode(h[name])
            except (ValueError, TypeError):
                raise ProtocolError(ErrorCode.INVALID_FIELD, f"Invalid {name}") from None
    if kind == MessageType.FILE_CANCEL and "reason" in h:
        try:
            ErrorCode(h["reason"])
        except ValueError:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "Invalid cancellation reason") from None
    if kind == MessageType.CHAT_ACK and h["status"] != "accepted":
        raise ProtocolError(ErrorCode.INVALID_FIELD, "Unknown acknowledgement status")
    if kind == MessageType.FILE_RESULT and ((not h["ok"]) != ("error_code" in h)):
        raise ProtocolError(ErrorCode.INVALID_FIELD, "error_code is required iff ok is false")
    if "limits" in h:
        ProtocolLimits.from_dict(h["limits"])
    return kind


def encode_message(msg: Message, limits: ProtocolLimits = DEFAULT_LIMITS) -> bytes:
    """Encode canonical JSON preceded by uint32 BE length; validate payload."""
    h = _plain(msg.header)
    if not isinstance(h, dict) or not isinstance(msg.payload, bytes):
        raise ProtocolError(ErrorCode.BAD_FRAME)
    h.setdefault("version", PROTOCOL_VERSION)
    h.setdefault("payload_length", len(msg.payload))
    validate_message(h, msg.payload, limits)
    try:
        data = json.dumps(h, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError(ErrorCode.BAD_JSON) from None
    check_frame_prefix(len(data), limits)
    frame = struct.pack("!I", len(data)) + data + msg.payload
    if msg.type in {MessageType.DISCOVER, MessageType.SERVER_INFO} and len(frame) > limits.max_datagram_bytes:
        raise ProtocolError(ErrorCode.PAYLOAD_TOO_LARGE, "Datagram exceeds application limit")
    return frame


def decode_header(data: bytes, limits: ProtocolLimits = DEFAULT_LIMITS, direction: Direction = Direction.ANY) -> dict[str, Any]:
    """Validate the header including per-type length before payload allocation."""
    if not isinstance(data, bytes):
        raise ProtocolError(ErrorCode.BAD_FRAME)
    check_frame_prefix(len(data), limits)
    h = _json(data)
    _validate_header(h, limits, direction)
    return h


def validate_message(header: Mapping[str, Any], payload: bytes, limits: ProtocolLimits = DEFAULT_LIMITS,
                     direction: Direction = Direction.ANY) -> Message:
    """Validate length, strict UTF-8 and payload semantics without I/O."""
    h = _plain(header)
    kind = _validate_header(h, limits, direction)
    if not isinstance(payload, bytes):
        raise ProtocolError(ErrorCode.BAD_PAYLOAD)
    if len(payload) != h["payload_length"]:
        raise ProtocolError(ErrorCode.PAYLOAD_LENGTH_MISMATCH)
    if kind in {MessageType.AUTH, MessageType.CHAT_PUBLIC, MessageType.CHAT_PRIVATE}:
        try:
            payload.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ProtocolError(ErrorCode.BAD_PAYLOAD, "Invalid UTF-8 payload") from None
    if kind == MessageType.USER_LIST and payload:
        _validate_users(payload)
    return Message(h, payload)


def check_frame_prefix(header_len: int, limits: ProtocolLimits = DEFAULT_LIMITS) -> None:
    """Reject zero/oversize header lengths before consuming header bytes."""
    if type(header_len) is not int or header_len <= 0:
        raise ProtocolError(ErrorCode.BAD_FRAME)
    if header_len > limits.max_header_bytes:
        raise ProtocolError(ErrorCode.HEADER_TOO_LARGE)


def max_payload_for(msg_type: MessageType, limits: ProtocolLimits = DEFAULT_LIMITS) -> int:
    """Return per-message application payload ceiling."""
    return {MessageType.AUTH: 256, MessageType.CHAT_PUBLIC: limits.max_chat_bytes,
            MessageType.CHAT_PRIVATE: limits.max_chat_bytes, MessageType.FILE_CHUNK: limits.max_chunk_bytes,
            MessageType.USER_LIST: limits.max_user_list_bytes}.get(msg_type, 0)


def _validate_users(payload: bytes) -> None:
    try:
        obj = _json(payload)
        if not isinstance(obj, dict) or set(obj) != {"users"} or not isinstance(obj["users"], list):
            raise ProtocolError(ErrorCode.BAD_PAYLOAD)
        names = set()
        for user in obj["users"]:
            if not isinstance(user, dict) or set(user) != {"username", "online", "ip", "port"}:
                raise ProtocolError(ErrorCode.BAD_PAYLOAD)
            _username(user["username"], "username")
            if user["username"] in names or type(user["online"]) is not bool:
                raise ProtocolError(ErrorCode.BAD_PAYLOAD)
            names.add(user["username"])
            if user["ip"] is not None:
                _string(user["ip"], "ip", 45)
                ipaddress.ip_address(user["ip"])
            if user["port"] is not None:
                _integer(user["port"], "port", 1, 65535)
    except (ProtocolError, ValueError):
        raise ProtocolError(ErrorCode.BAD_PAYLOAD, "Invalid USER_LIST payload") from None


class FrameDecoder:
    """Incremental socket-free decoder, permanently poisoned on corruption."""
    def __init__(self, limits: ProtocolLimits = DEFAULT_LIMITS, direction: Direction = Direction.ANY) -> None:
        self.limits = limits
        self.direction = direction
        self._buffer = bytearray()
        self._header_len = None
        self._header = None
        self._error = None

    def feed(self, data: bytes) -> list[Message]:
        """Return complete frames, retaining only the unfinished frame."""
        if self._error is not None:
            raise self._error
        messages = []
        view = memoryview(data)
        offset = 0
        try:
            while True:
                needed = (4 if self._header_len is None else self._header_len
                          if self._header is None else self._header["payload_length"])
                take = min(needed - len(self._buffer), len(view) - offset)
                self._buffer.extend(view[offset:offset + take])
                offset += take
                if len(self._buffer) < needed:
                    break
                if self._header_len is None:
                    self._header_len = struct.unpack("!I", self._buffer)[0]
                    check_frame_prefix(self._header_len, self.limits)
                elif self._header is None:
                    self._header = decode_header(bytes(self._buffer), self.limits, self.direction)
                else:
                    messages.append(validate_message(self._header, bytes(self._buffer), self.limits, self.direction))
                    self._header_len = None
                    self._header = None
                self._buffer.clear()
                if offset == len(view) and (self._header is None or self._header["payload_length"]):
                    break
        except ProtocolError as error:
            self._error = error
            self._buffer.clear()
            raise
        return messages

    def at_clean_boundary(self) -> bool:
        """False means EOF would be UNEXPECTED_EOF, not a clean close."""
        return self._error is None and self._header_len is None and not self._buffer

    def feed_eof(self) -> None:
        """Transport helper: classify EOF and poison a truncated stream."""
        if self._error is not None:
            raise self._error
        if not self.at_clean_boundary():
            self._error = ProtocolError(ErrorCode.UNEXPECTED_EOF)
            raise self._error


def decode_datagram(data: bytes, limits: ProtocolLimits = DEFAULT_LIMITS,
                    direction: Direction = Direction.UDP_REQUEST) -> Message:
    """Exactly one discovery frame, no trailing bytes or TCP message types."""
    if len(data) > limits.max_datagram_bytes:
        raise ProtocolError(ErrorCode.PAYLOAD_TOO_LARGE)
    if len(data) < 4:
        raise ProtocolError(ErrorCode.UNEXPECTED_EOF)
    n = struct.unpack("!I", data[:4])[0]
    check_frame_prefix(n, limits)
    if len(data) < 4 + n:
        raise ProtocolError(ErrorCode.UNEXPECTED_EOF)
    h = decode_header(data[4:4+n], limits, direction)
    if h["type"] not in {MessageType.DISCOVER, MessageType.SERVER_INFO}:
        raise ProtocolError(ErrorCode.INVALID_FIELD)
    if len(data) != 4 + n + h["payload_length"]:
        raise ProtocolError(ErrorCode.BAD_FRAME, "Datagram must contain exactly one frame")
    return validate_message(h, data[4+n:], limits, direction)


def build_message(kind: MessageType, payload: bytes = b"", *, limits: ProtocolLimits = DEFAULT_LIMITS, **fields: Any) -> Message:
    """General builder for all catalogue types, including server-stamped messages."""
    h = {"version": PROTOCOL_VERSION, "type": kind.value, "request_id": uuid4().hex,
         "payload_length": len(payload), **fields}
    return validate_message(h, payload, limits)


def _text_bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeError):
        raise ProtocolError(ErrorCode.BAD_PAYLOAD, "Invalid UTF-8 text") from None


def build_hello(client_name: str = "LanLink", client_version: str = "1.0") -> Message:
    return build_message(MessageType.HELLO, client_name=client_name, client_version=client_version)


def build_hello_ack(ref_request_id: str, server_name: str, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    return validate_message({"version": 1, "type": "HELLO_ACK", "request_id": uuid4().hex,
                             "payload_length": 0, "ref_request_id": ref_request_id,
                             "server_name": server_name, "limits": limits.to_dict()}, b"", limits)


def build_auth(username: str, password: str) -> Message:
    return build_message(MessageType.AUTH, _text_bytes(password), username=username)


def build_auth_ok(ref_request_id: str, username: str) -> Message:
    return build_message(MessageType.AUTH_OK, ref_request_id=ref_request_id, username=username)


def build_chat_public(text: str, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    return build_message(MessageType.CHAT_PUBLIC, _text_bytes(text), limits=limits)


def build_chat_private(recipient: str, text: str, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    return build_message(MessageType.CHAT_PRIVATE, _text_bytes(text), recipient=recipient, limits=limits)


def build_file_offer(transfer_id: str, recipient: str, filename: str, size: int,
                     sha256: str, chunk_size: int, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    return build_message(MessageType.FILE_OFFER, transfer_id=transfer_id, recipient=recipient,
                         filename=filename, size=size, sha256=sha256, chunk_size=chunk_size, limits=limits)


def build_file_accept(transfer_id: str) -> Message:
    return build_message(MessageType.FILE_ACCEPT, transfer_id=transfer_id)


def build_file_reject(transfer_id: str, reason: str | None = None) -> Message:
    return build_message(MessageType.FILE_REJECT, transfer_id=transfer_id, **({"reason": reason} if reason else {}))


def build_file_chunk(transfer_id: str, seq: int, data: bytes, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    return build_message(MessageType.FILE_CHUNK, data, transfer_id=transfer_id, seq=seq, limits=limits)


def build_file_end(transfer_id: str, total_chunks: int, total_bytes: int, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    return build_message(MessageType.FILE_END, transfer_id=transfer_id, total_chunks=total_chunks,
                         total_bytes=total_bytes, limits=limits)


def build_file_result(transfer_id: str, ok: bool, received_bytes: int, sha256: str | None = None,
                      error_code: ErrorCode | None = None, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    extra = {}
    if sha256 is not None:
        extra["sha256"] = sha256
    if error_code is not None:
        extra["error_code"] = error_code.value
    return build_message(MessageType.FILE_RESULT, transfer_id=transfer_id, ok=ok,
                         received_bytes=received_bytes, limits=limits, **extra)


def build_file_cancel(transfer_id: str, reason: ErrorCode | None = None) -> Message:
    return build_message(MessageType.FILE_CANCEL, transfer_id=transfer_id,
                         **({"reason": reason.value} if reason else {}))


def build_ping() -> Message:
    return build_message(MessageType.PING)


def build_pong(ref_request_id: str) -> Message:
    return build_message(MessageType.PONG, ref_request_id=ref_request_id)


def build_error(code: ErrorCode, message: str, *, ref_request_id: str | None = None,
                field: str | None = None, transfer_id: str | None = None) -> Message:
    extra = {k: v for k, v in {"ref_request_id": ref_request_id, "field": field,
                             "transfer_id": transfer_id}.items() if v is not None}
    return build_message(MessageType.ERROR, code=code.value, message=message, **extra)


def build_discover() -> Message:
    return build_message(MessageType.DISCOVER)


def build_server_info(ref_request_id: str, server_name: str, port: int, tls: bool = True,
                      host: str | None = None) -> Message:
    return build_message(MessageType.SERVER_INFO, ref_request_id=ref_request_id, server_name=server_name,
                         port=port, tls=tls, **({"host": host} if host is not None else {}))


def build_logout(reason: str | None = None) -> Message:
    return build_message(MessageType.LOGOUT, **({"reason": reason} if reason else {}))


def build_user_list(users: list[dict[str, Any]] | None = None, *, ref_request_id: str | None = None,
                    limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Build an empty request, or a users response referencing its request ID."""
    payload = b"" if users is None else json.dumps({"users": users}, ensure_ascii=False,
                                                   separators=(",", ":"), sort_keys=True).encode("utf-8")
    return build_message(MessageType.USER_LIST, payload, limits=limits,
                         **({"ref_request_id": ref_request_id} if ref_request_id is not None else {}))


def build_chat_ack(ref_request_id: str, message_id: str, timestamp_ms: int) -> Message:
    return build_message(MessageType.CHAT_ACK, ref_request_id=ref_request_id,
                         message_id=message_id, timestamp_ms=timestamp_ms, status="accepted")
