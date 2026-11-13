"""Message builders: one small function per message type.

Each builder fills in the fields of its type and validates the result through
the codec (common/protocol.py), so a builder can never produce an invalid message.
Optional fields use **({"key": value} if value else {}): the field is added only when given.
"""
import json
from typing import Any
from uuid import uuid4

from common.errors import ErrorCode, ProtocolError
from common.models import Message, MessageType
from common.protocol import DEFAULT_LIMITS, PROTOCOL_VERSION, ProtocolLimits, validate_message


def build_message(kind: MessageType, payload: bytes = b"", *, limits: ProtocolLimits = DEFAULT_LIMITS, **fields: Any) -> Message:
    """General builder for all catalogue types, including server-stamped messages."""
    h = {"version": PROTOCOL_VERSION, "type": kind.value, "request_id": uuid4().hex,
         "payload_length": len(payload), **fields}
    return validate_message(h, payload, limits)


def _text_bytes(text: str) -> bytes:
    """Encode chat or password text as strict UTF-8; bad text is a BAD_PAYLOAD error."""
    try:
        return text.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeError):
        raise ProtocolError(ErrorCode.BAD_PAYLOAD, "Invalid UTF-8 text") from None


def build_hello(client_name: str = "LanLink", client_version: str = "1.0") -> Message:
    """Client's first message on a new connection."""
    return build_message(MessageType.HELLO, client_name=client_name, client_version=client_version)


def build_hello_ack(ref_request_id: str, server_name: str, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Server's reply to HELLO; tells the client the server name and limits."""
    return validate_message({"version": 1, "type": "HELLO_ACK", "request_id": uuid4().hex,
                             "payload_length": 0, "ref_request_id": ref_request_id,
                             "server_name": server_name, "limits": limits.to_dict()}, b"", limits)


def build_auth(username: str, password: str) -> Message:
    """Login request; the password travels as the UTF-8 payload, never in the header."""
    return build_message(MessageType.AUTH, _text_bytes(password), username=username)


def build_auth_ok(ref_request_id: str, username: str) -> Message:
    """Successful login reply to an AUTH request."""
    return build_message(MessageType.AUTH_OK, ref_request_id=ref_request_id, username=username)


def build_chat_public(text: str, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Chat message for every online user."""
    return build_message(MessageType.CHAT_PUBLIC, _text_bytes(text), limits=limits)


def build_chat_private(recipient: str, text: str, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Chat message for one recipient."""
    return build_message(MessageType.CHAT_PRIVATE, _text_bytes(text), recipient=recipient, limits=limits)


def build_file_offer(transfer_id: str, recipient: str, filename: str, size: int,
                     sha256: str, chunk_size: int, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Sender proposes a file (name, size, SHA-256, chunk size) to a recipient."""
    return build_message(MessageType.FILE_OFFER, transfer_id=transfer_id, recipient=recipient,
                         filename=filename, size=size, sha256=sha256, chunk_size=chunk_size, limits=limits)


def build_file_accept(transfer_id: str) -> Message:
    """Recipient agrees to receive an offered file."""
    return build_message(MessageType.FILE_ACCEPT, transfer_id=transfer_id)


def build_file_reject(transfer_id: str, reason: str | None = None) -> Message:
    """Recipient declines an offered file, with an optional reason."""
    return build_message(MessageType.FILE_REJECT, transfer_id=transfer_id, **({"reason": reason} if reason else {}))


def build_file_chunk(transfer_id: str, seq: int, data: bytes, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """One numbered piece (seq) of file content."""
    return build_message(MessageType.FILE_CHUNK, data, transfer_id=transfer_id, seq=seq, limits=limits)


def build_file_end(transfer_id: str, total_chunks: int, total_bytes: int, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Sender has sent every chunk; carries the totals for checking."""
    return build_message(MessageType.FILE_END, transfer_id=transfer_id, total_chunks=total_chunks,
                         total_bytes=total_bytes, limits=limits)


def build_file_result(transfer_id: str, ok: bool, received_bytes: int, sha256: str | None = None,
                      error_code: ErrorCode | None = None, *, limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Recipient's verdict: ok plus SHA-256, or an error code."""
    extra = {}
    if sha256 is not None:
        extra["sha256"] = sha256
    if error_code is not None:
        extra["error_code"] = error_code.value
    return build_message(MessageType.FILE_RESULT, transfer_id=transfer_id, ok=ok,
                         received_bytes=received_bytes, limits=limits, **extra)


def build_file_cancel(transfer_id: str, reason: ErrorCode | None = None) -> Message:
    """Either side stops a transfer, with an optional error code as reason."""
    return build_message(MessageType.FILE_CANCEL, transfer_id=transfer_id,
                         **({"reason": reason.value} if reason else {}))


def build_ping() -> Message:
    """Liveness probe (part of the protocol; the CLI does not send it)."""
    return build_message(MessageType.PING)


def build_pong(ref_request_id: str) -> Message:
    """Reply to a PING."""
    return build_message(MessageType.PONG, ref_request_id=ref_request_id)


def build_error(code: ErrorCode, message: str, *, ref_request_id: str | None = None,
                field: str | None = None, transfer_id: str | None = None) -> Message:
    """Error reply; the optional fields point at the request, field or transfer at fault."""
    extra = {k: v for k, v in {"ref_request_id": ref_request_id, "field": field,
                             "transfer_id": transfer_id}.items() if v is not None}
    return build_message(MessageType.ERROR, code=code.value, message=message, **extra)


def build_discover() -> Message:
    """UDP request asking whether a LanLink server is on this network."""
    return build_message(MessageType.DISCOVER)


def build_server_info(ref_request_id: str, server_name: str, port: int, tls: bool = True,
                      host: str | None = None) -> Message:
    """UDP reply to DISCOVER: server name, TCP port and whether TLS is on."""
    return build_message(MessageType.SERVER_INFO, ref_request_id=ref_request_id, server_name=server_name,
                         port=port, tls=tls, **({"host": host} if host is not None else {}))


def build_logout(reason: str | None = None) -> Message:
    """Client ends its session, with an optional reason."""
    return build_message(MessageType.LOGOUT, **({"reason": reason} if reason else {}))


def build_user_list(users: list[dict[str, Any]] | None = None, *, ref_request_id: str | None = None,
                    limits: ProtocolLimits = DEFAULT_LIMITS) -> Message:
    """Build an empty request, or a users response referencing its request ID."""
    payload = b"" if users is None else json.dumps({"users": users}, ensure_ascii=False,
                                                   separators=(",", ":"), sort_keys=True).encode("utf-8")
    return build_message(MessageType.USER_LIST, payload, limits=limits,
                         **({"ref_request_id": ref_request_id} if ref_request_id is not None else {}))


def build_chat_ack(ref_request_id: str, message_id: str, timestamp_ms: int) -> Message:
    """Server confirms it accepted a chat message (sent to the sender)."""
    return build_message(MessageType.CHAT_ACK, ref_request_id=ref_request_id,
                         message_id=message_id, timestamp_ms=timestamp_ms, status="accepted")
