# Outline: 2 Protocol Specification (owner: report-protocol)

## 1. Heading tree and content plan

- **2 Protocol Specification**: two-sentence intro covering protocol v1 (`PROTOCOL_VERSION = 1`), TCP+TLS for sessions and UDP for discovery only. (~3 lines)
- **2.1 Design rationale**: explicit boundaries are needed because TCP is a byte stream, which is why the protocol uses a length prefix instead of the Spec §6 "End-of-Message flag". The JSON header is readable in Wireshark and is canonical (`sort_keys`, compact separators). File bytes go in a separate binary payload, so there is no base64. Files: `common/protocol.py` (module docstring, `encode_message`). (~1 paragraph)
- **2.2 Frame format**
  - **2.2.1 Byte layout and byte order**: a `uint32` big-endian (`struct "!I"`) header length, then the UTF-8 JSON header, then `payload_length` bytes. The prefix counts header bytes only. Files: `protocol.py` `encode_message`, `check_frame_prefix`. (Figure F1, ~6 lines)
  - **2.2.2 How a receiver reads a frame**: `FrameDecoder` has three stages (4-byte prefix, then N header bytes, then `payload_length` bytes). It is fed by `recv(65536)` in `SocketTransport.receive`. The header is validated before the payload buffer is allocated. After an error the decoder stays poisoned, and EOF in the middle of a frame raises `UNEXPECTED_EOF`. Files: `protocol.py`, `transport.py`. (~8 lines)
  - **2.2.3 Worked example**: the `chat_private_CLIENT_TO_SERVER` frame from `tests/fixtures/protocol_cases.json` (prefix `0000005c` = 92 header bytes, 18-byte payload), shown as hex beside the JSON. (Listing L1, ~10 lines)
- **2.3 Common header fields and identifier formats**: `COMMON_FIELDS` (`version`, `type`, `request_id`, `payload_length`), plus `ref_request_id` for reply matching. Formats: `ID_RE`, `TRANSFER_RE` (uuid4 hex), `HASH_RE` (SHA-256 hex). Validation is strict: exact field set, no bool posing as int, no duplicate JSON keys, no NaN. Files: `protocol.py`. (Table T1, ~6 lines)
- **2.4 Message catalogue**: one table per group, with columns type / direction / header fields (required, optional) / payload / reply. Sources: `SCHEMAS`, `CLIENT_ONLY`, `SERVER_ONLY`, direction additions in `_validate_header`, `max_payload_for`, `builders.py`, and the handlers in `server/app.py`.
  - **2.4.1 Session**: `HELLO`, `HELLO_ACK` (`limits` object with 8 keys), `AUTH` (password = payload, up to 256 B), `AUTH_OK`, `USER_LIST` (empty request; the reply payload is JSON `{"users":[{username,online,ip,port}]}`), `LOGOUT`, and `PING`/`PONG` marked unused. (T2)
  - **2.4.2 Chat**: `CHAT_PUBLIC`, `CHAT_PRIVATE`, `CHAT_ACK` (`status` must be `"accepted"`). This subsection notes that the forwarded copy adds `sender`, `message_id` and `timestamp_ms`. (T3)
  - **2.4.3 File transfer**: the 7 `FILE_*` types, which party sends each one, and how the server relays it (`FILE_CONTENT_TYPES`). It covers the `ok`/`error_code` rule in `FILE_RESULT` and that `FILE_CANCEL.reason` must be an `ErrorCode`. (T4)
  - **2.4.4 Discovery**: `DISCOVER` and `SERVER_INFO` (UDP only; any TCP direction rejects them). (T5)
  - **2.4.5 Error**: `ERROR` (`code`, `message`, optional `ref_request_id`, `field`, `transfer_id`). (T6, ~1 short table each, ~1.25 pages total)
- **2.5 Error codes**: all 35 `ErrorCode` members grouped as framing/schema, session, routing, transfer and local-only. For each code: whether it travels on the wire (`ERROR.code`, `FILE_RESULT.error_code`, `FILE_CANCEL.reason`) or only closes the connection or stays local. Files: `errors.py`, `app.py`, `transfer/*.py`. (T7, ~0.5 page)
- **2.6 Limits and validation**
  - **2.6.1 Size limits and timeouts**: `ProtocolLimits` values, the per-type payload ceilings, the string and integer field bounds, and the timeouts (server, client, transfer engines). (T8)
  - **2.6.2 Malformed frame vs. bad request**: decoder or schema failure closes the connection without a reply (`_serve_client` catches `ProtocolError`). An `ERROR` is sent for a valid frame with a routing, state or auth problem, and also for `HELLO`-phase errors when the decoder is at a clean boundary. An unhandled valid type gets `ERROR` and then the connection closes, and so does a third failed `AUTH`. (~8 lines)
  - **2.6.3 Server-stamped fields**: `SERVER_FIELDS`. On `CLIENT_TO_SERVER` they produce `SPOOFED_FIELD` before any other check. The server stamps them in `_route_public` and `_route_private`, and adds `sender` to the forwarded `FILE_OFFER`. (~4 lines)
- **2.7 State diagrams**
  - **2.7.1 Server-side connection (`ConnectionState`)**: real transitions in `server/app.py`, `server/sessions.py` and `server/__main__.py`. (F2, ~5 lines of prose)
  - **2.7.2 File transfer (`TransferStatus`)**: `ALLOWED_TRANSITIONS` plus the message that triggers each edge (`_negotiate_file_transfer`, `_status_after_message`, `_fail_transfer_and_notify`, `transfer/sender.py`, `transfer/receiver.py`). (F3, ~6 lines)
- **2.8 UDP discovery**: datagram = exactly one frame of at most 1024 B. The client broadcasts to `255.255.255.255:<port>` (default 5000, the same number as the TCP port) and waits 2.0 s. The server replies by unicast to the datagram's source address and ignores junk. The client takes the server IP from the reply's source address and matches `ref_request_id`. Files: `server/discovery.py`, `client/discovery.py`, `protocol.decode_datagram`. (~8 lines)

Estimated length: about 3.5–4 pages. The 2–3 page target is not realistic with 21 types and 35 codes in tables.

## 2. Inventory (from code)

**Message types (21), `common/models.py` `MessageType`:** 19 on TCP and 2 on UDP.
TCP: HELLO, HELLO_ACK, AUTH, AUTH_OK, USER_LIST, LOGOUT, ERROR, CHAT_PUBLIC, CHAT_PRIVATE, CHAT_ACK,
FILE_OFFER, FILE_ACCEPT, FILE_REJECT, FILE_CHUNK, FILE_END, FILE_RESULT, FILE_CANCEL, PING, PONG.
UDP: DISCOVER, SERVER_INFO.

**Error codes (35), `common/errors.py` `ErrorCode`:** BAD_FRAME, HEADER_TOO_LARGE, PAYLOAD_TOO_LARGE,
PAYLOAD_LENGTH_MISMATCH, UNEXPECTED_EOF, BAD_JSON, UNSUPPORTED_VERSION, UNKNOWN_TYPE, MISSING_FIELD,
INVALID_FIELD, SPOOFED_FIELD, BAD_PAYLOAD, NOT_AUTHENTICATED, AUTH_FAILED, ALREADY_LOGGED_IN,
RECIPIENT_OFFLINE, RECIPIENT_UNKNOWN, QUEUE_FULL, TIMEOUT, CONNECTION_LOST, FILE_TOO_LARGE, FILE_NOT_FOUND,
SOURCE_CHANGED, UNSAFE_FILENAME, TRANSFER_UNKNOWN, TRANSFER_STATE, SEQ_MISMATCH, SIZE_MISMATCH,
HASH_MISMATCH, DISK_FULL, PERMISSION_DENIED, REJECTED_BY_PEER, CANCELLED_BY_PEER, CANCELLED_LOCAL, INTERNAL.

**States:** `ConnectionState` (5): CONNECTING, HELLO_ACCEPTED, AUTHENTICATED, CLOSING, CLOSED.
`TransferStatus` (8): OFFERED, ACCEPTED, TRANSFERRING, VERIFYING, COMPLETED, REJECTED, FAILED, CANCELLED (`common/models.py`).

**Constants and limits:**
- `common/protocol.py`: PROTOCOL_VERSION; ProtocolLimits.{max_header_bytes, max_chunk_bytes, max_file_bytes,
  max_chat_bytes, max_user_list_bytes, max_datagram_bytes, heartbeat_interval_s, idle_timeout_s} and their
  ceilings; MAX_INT; ID_RE; TRANSFER_RE; HASH_RE; SCHEMAS; CLIENT_ONLY; SERVER_ONLY; SERVER_FIELDS; COMMON_FIELDS;
  FILE_MESSAGE_ROLES; `max_payload_for` (AUTH 256); inline string maxima in `_validate_header` (username 32,
  client_name 64, client_version 32, server_name 64, host 255, message 256, field 64, status 16, reason 128,
  filename 255 chars and 255 UTF-8 bytes); depth limit 32 in `_plain`.
- `server/app.py`: FILE_CONTENT_TYPES; AUTH_READY_WAIT_S; TCPServer defaults (port 5000, max_clients 64,
  handshake_timeout 5.0, idle_timeout 45.0, transfer_timeout 60.0); 3 failed logins; 64 transfer IDs per connection.
- `server/__main__.py`: --port 5000, --timeout 5.0, --max-clients 64, --idle-timeout 120.0, --transfer-timeout 60.0.
- `client/__main__.py`, `client/private_chat.py`: --port 5000, --timeout 5.0 (request timeout). `client/discovery.py`: wait 2.0, address 255.255.255.255.
- `transfer/sender.py`: accept_timeout_s 60, result_timeout_s 30, chunk_size = min(requested, max_chunk_bytes).
  `transfer/receiver.py`: idle_timeout_s 30.

## 3. Planned figures and tables

| ID | Working caption | Shows | Type | Source |
|---|---|---|---|---|
| F1 | Figure. LanLink frame layout | 4 B length, header, payload, byte order | ASCII block | inline |
| L1 | Figure. Example CHAT_PRIVATE frame in hex | fixture `frame_hex` split into prefix, header, payload | code block | inline |
| T1 | Table. Common header fields | field, type, required, meaning, format | Markdown table | inline |
| T2–T6 | Table. Session / Chat / File transfer / Discovery / Error messages | per-type catalogue | Markdown tables | inline |
| T7 | Table. Error codes | code, group, sent in, when | Markdown table | inline |
| T8 | Table. Size limits and timeouts | name, value, file, effect | Markdown table | inline |
| F2 | Figure. Server-side connection states | `ConnectionState` transitions | Mermaid stateDiagram-v2 | `report/diagrams/state-connection.mmd` |
| F3 | Figure. File transfer states | `TransferStatus` transitions with triggering messages | Mermaid stateDiagram-v2 | `report/diagrams/state-transfer.mmd` |

## 4. Rubric evidence (Spec.md §9)

- Step 1 (packet structure, message types, state diagrams): the main evidence.
- Step 2 (HELLO/HELLO_ACK acknowledgement), step 4 (AUTH, and USER_LIST carrying ip and port), step 5 (CHAT_* routing fields, CHAT_ACK), step 6 (chunking, seq, SHA-256), and step 7 (error codes, timeouts, malformed-frame handling): supporting evidence.

## 5. Open questions and gaps

1. **PING/PONG are worse than unused.** The server handles them in the `else` branch: it sends `ERROR INVALID_FIELD` and then closes the connection (`app.py` dispatch). Nothing sends PING (`build_ping` and `build_pong` have no callers).
2. **No heartbeat.** `heartbeat_interval_s=15` is advertised but never acted on, and the Spec §6 "UDP heartbeat" is not implemented. UDP is used for discovery only.
3. **The HELLO_ACK `limits` field is not used.** The server always sends `DEFAULT_LIMITS` (`idle_timeout_s=45`) even when it runs with `--idle-timeout 120`, and no client code reads `limits`.
4. **`RECIPIENT_UNKNOWN` is never raised.** An unknown recipient gets `RECIPIENT_OFFLINE`.
5. **`FILE_MESSAGE_ROLES` is defined but unused.** Its comment says the server checks it, but the roles are actually enforced in `_status_after_message`.
6. **The server transfer path does not call `check_transition`.** It allows `ACCEPTED -> VERIFYING` on `FILE_END` for an empty file, which is not in `ALLOWED_TRANSITIONS`, and a `TRANSFERRING` self-loop on each `FILE_CHUNK`. Should F3 show `ALLOWED_TRANSITIONS` and add a note, or add a dashed server-only edge?
7. **`CLOSING` is reached only from `shutdown()`.** LOGOUT, errors and disconnects go straight to `CLOSED`.
8. **The receiver does not read "exactly 4 bytes" with blocking reads.** The brief's wording would misdescribe the code, so 2.2.2 will describe the decoder stages over `recv(65536)` instead.
9. **Most framing and schema codes never appear in an `ERROR`.** The decoder raises them and the connection closes. T7 must say this per code.
10. **The fixture payload in L1 is an emoji ZWJ sequence**, which may not render in the PDF font. Show it as hex only, or generate an ASCII example with `encode_message`, which the fact-checker would then need to verify.
11. **Boundaries with other sections.** Section 1 owns TLS (rubric step 8) and threading; 2.1 and 2.8 only say that frames run inside TLS and that plaintext is allowed only on loopback. Section 3 owns message order: login, chat, file transfer, the FILE_ACCEPT echo to the recipient, and failure paths. Section 2 gives only the "reply" column and the state graphs. Timeouts are listed in T8 here, and section 3 shows them firing.
12. **Docs vs code.**
    - `FILE_RESULT.sha256` is optional in the code, but the docs list it as always present.
    - The docs leave out `ERROR.field` and `SERVER_INFO.host`. `host` is never set.
    - "Valid frame with a wrong request gets ERROR" is too broad (see item 9).
    - "Same transfer graph on sender, server and receiver" does not hold for the server (see item 6).
    - The schema allows `LOGOUT` S->C, but the server never sends it.
