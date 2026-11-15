# 2 Protocol Specification

LanLink uses one application protocol, version 1. Chat, file and session messages travel as frames on one TCP connection per client, inside TLS when it is enabled (section 1.5). UDP carries only server discovery (2.8). The same schema check runs when a message is built, encoded and decoded, so neither side can send a message that breaks the field schema.

## 2.1 Design rationale

TCP delivers a byte stream, so one `recv()` can return part of a frame or several frames. Instead of an end-of-message flag, every frame starts with the length of its header, and the header states the length of its payload. The receiver therefore always knows how many bytes it still needs, and file data needs no escaping. The header is canonical JSON (sorted keys, no spaces, UTF-8): it is readable in Wireshark on a plaintext loopback server, and the same message always gives the same bytes, so the tests compare frames byte for byte. The payload stays outside the JSON as raw bytes, so file chunks need no base64 (which adds one third) and the login password never appears in the header.

## 2.2 Frame format

### 2.2.1 Byte layout and byte order

Figure 2 shows one frame. The first 4 bytes are an unsigned 32-bit integer in big-endian (network) byte order. It gives N, the length of the JSON header in bytes, and counts neither itself nor the payload. N must be between 1 and 16 384. The header is N bytes of UTF-8 JSON, and its `payload_length` field gives P, the number of payload bytes that follow. P may be 0. The next frame starts immediately after the payload. The largest valid frame is a `FILE_CHUNK` or a `USER_LIST` reply, both with a 65 536-byte payload ceiling: 4 + 16 384 + 65 536 = 81 924 bytes.

![Figure 2. Frame layout: length prefix, JSON header, payload.](../diagrams/frame-layout.svg)

### 2.2.2 How a receiver reads a frame

The receiver reads up to 64 KiB per `recv()` and feeds whatever arrives to a frame decoder. The decoder collects bytes in three stages and keeps any leftover bytes for the next stage:

1. **Exactly 4 bytes** of prefix. They are decoded as N and checked: 0 gives `BAD_FRAME`, and more than 16 384 gives `HEADER_TOO_LARGE`. The check happens before any header byte is buffered.
2. **Exactly N bytes** of header. They are parsed as strict JSON and validated against the schema of their type and direction (2.3, 2.4). The payload ceiling is checked here too, so a peer cannot make the receiver buffer more payload than the limit allows.
3. **Exactly `payload_length` bytes** of payload. They are checked against the type (for example strict UTF-8 for chat), and the complete message is returned.

Several frames can complete in one `recv()`, and one frame can span many. After any protocol error the decoder is "poisoned": every later call raises the same error, because the position of the next frame boundary is no longer known. The stream is never resynchronised. End of stream exactly at a frame boundary is a clean close. End of stream inside a frame gives `UNEXPECTED_EOF`.

### 2.2.3 Worked example: a `CHAT_PRIVATE` frame

Listing 1 shows the real bytes the encoder produces when a client sends the text `Hi Binh!` privately to user `binh`. The `request_id` is fixed at `r1` so that the output can be reproduced; a real client uses a random UUID in hex. The prefix `00 00 00 5b` gives N = 91 header bytes (offsets 0x04 to 0x5e). The header has `payload_length` 8, so the payload is the 8 bytes at offsets 0x5f to 0x66, and the frame is 103 bytes long. The keys appear in sorted order because encoding is canonical.

```
0000  00 00 00 5b 7b 22 70 61 79 6c 6f 61 64 5f 6c 65  ...[{"payload_le
0010  6e 67 74 68 22 3a 38 2c 22 72 65 63 69 70 69 65  ngth":8,"recipie
0020  6e 74 22 3a 22 62 69 6e 68 22 2c 22 72 65 71 75  nt":"binh","requ
0030  65 73 74 5f 69 64 22 3a 22 72 31 22 2c 22 74 79  est_id":"r1","ty
0040  70 65 22 3a 22 43 48 41 54 5f 50 52 49 56 41 54  pe":"CHAT_PRIVAT
0050  45 22 2c 22 76 65 72 73 69 6f 6e 22 3a 31 7d 48  E","version":1}H
0060  69 20 42 69 6e 68 21                             i Binh!

prefix  : 00 00 00 5b  -> N = 91
header  : {"payload_length":8,"recipient":"binh","request_id":"r1",
           "type":"CHAT_PRIVATE","version":1}
payload : 48 69 20 42 69 6e 68 21  -> "Hi Binh!" (8 bytes, UTF-8)
```

Listing 1. A client-to-server `CHAT_PRIVATE` frame in hex, as produced by the encoder.

## 2.3 Common header fields and identifier formats

Every header carries the four common fields in Table 2. A reply also carries `ref_request_id`.

Table 2. Common header fields.

| Field | JSON type | Required | Meaning and format |
|---|---|---|---|
| `version` | integer | every message | Protocol version. It must be `1`, otherwise `UNSUPPORTED_VERSION`. |
| `type` | string | every message | One of the 21 message types (`common/models.py`). An unknown value gives `UNKNOWN_TYPE`. |
| `request_id` | string | every message | ID of this message: 1 to 64 characters from `A-Z a-z 0-9 _ -`. The client and server use a random UUID in hex. |
| `payload_length` | integer | every message | Number of payload bytes after the header, from 0 to the ceiling of the type (Table 10, Appendix B). |
| `ref_request_id` | string | replies (per type, Tables 3 to 7) | The `request_id` of the message being answered. Same format as `request_id`. |

A `transfer_id` is 32 lowercase hex characters and a `sha256` is 64. Validation is strict. The field set must be exact: a missing field gives `MISSING_FIELD` and an extra one `INVALID_FIELD`. Integers must be JSON integers (not booleans), strings must be non-empty without control characters, and duplicate keys, `NaN` or `Infinity` give `BAD_JSON`. String lengths are in Table 10 (Appendix B), and the remaining rules are in the codec (`common/protocol.py`).

The receiver also checks direction. `HELLO` and `AUTH` come only from a client; `HELLO_ACK`, `AUTH_OK`, `ERROR` and `CHAT_ACK` only from the server; `DISCOVER` and `SERVER_INFO` only over UDP. A type in the wrong direction gives `INVALID_FIELD`.

## 2.4 Message catalogue

There are 21 message types, 19 on TCP and 2 on UDP, listed by group in Tables 3 to 7. "opt." marks an optional field; every other listed field is required. A type without a listed payload must have an empty payload. Section 3 shows the order of messages in a conversation.

### 2.4.1 Session

Table 3. Session messages.

| Type | Direction | Header fields (besides common) | Payload | Reply |
|---|---|---|---|---|
| `HELLO` | C→S | `client_name` (≤64), `client_version` (≤32) | none | `HELLO_ACK` |
| `HELLO_ACK` | S→C | `ref_request_id`, `server_name` (≤64), `limits` (object with exactly the 8 limit keys listed in Appendix B) | none | none |
| `AUTH` | C→S | `username` (≤32, no whitespace) | password, UTF-8, 1–256 B | `AUTH_OK`, or `ERROR` |
| `AUTH_OK` | S→C | `ref_request_id`, `username` | none | none |
| `USER_LIST` request | C→S | none | must be empty | `USER_LIST` reply, or `ERROR` |
| `USER_LIST` reply | S→C | `ref_request_id` | JSON `{"users":[{"username","online","ip","port"}]}`, 1–65 536 B | none |
| `LOGOUT` | C→S | opt. `reason` (≤128) | none | After login: none; the server closes the connection. Before login: `ERROR NOT_AUTHENTICATED`, and the connection stays open |
| `PING` | either (unused) | none | none | `PONG` (never sent) |
| `PONG` | either (unused) | `ref_request_id` | none | none |

The client checks that the `limits` object in `HELLO_ACK` is valid (an invalid one fails the handshake) but never applies the values (see 1.6). In the `USER_LIST` reply, `ip` and `port` may be `null`. `PING` and `PONG` have no handler: a client that sends `PING` gets an `ERROR` and is disconnected (Table 9; see 1.6).

### 2.4.2 Chat

Table 4. Chat messages.

| Type | Direction | Header fields (besides common) | Payload | Reply |
|---|---|---|---|---|
| `CHAT_PUBLIC` | C→S | none | UTF-8 text, 1–8 192 B | `CHAT_ACK` |
| `CHAT_PUBLIC` forwarded | S→C | `sender`, `message_id`, `timestamp_ms` | same text | none |
| `CHAT_PRIVATE` | C→S | `recipient` (≤32) | UTF-8 text, 1–8 192 B | `CHAT_ACK`, or `ERROR` |
| `CHAT_PRIVATE` forwarded | S→C | `recipient`, `sender`, `message_id`, `timestamp_ms` | same text | none |
| `CHAT_ACK` | S→C | `ref_request_id`, `message_id`, `timestamp_ms`, `status` (must be `"accepted"`) | none | none |

The forwarded copy has a new `request_id` and the server-stamped `sender`, `message_id` and `timestamp_ms` (Unix milliseconds; 2.6.3). The same `message_id` and `timestamp_ms` return in `CHAT_ACK`, which means "accepted by the server", not "delivered": a public message is acknowledged even if there were no recipients or some deliveries failed.

### 2.4.3 File transfer

Table 5. File-transfer messages. "Sender" and "recipient" are the two clients of a transfer. Every message passes through the server.

| Type | Sent by | Header fields (besides common) | Payload | Server action |
|---|---|---|---|---|
| `FILE_OFFER` | sender | `transfer_id`, `recipient`, `filename`, `size`, `sha256`, `chunk_size`; the forwarded copy adds `sender` | none | Creates an `OFFERED` session and forwards the offer to the recipient, or replies `ERROR` |
| `FILE_ACCEPT` | recipient | `transfer_id` | none | Sets `ACCEPTED`, forwards to the sender, then echoes back to the recipient |
| `FILE_REJECT` | recipient | `transfer_id`; opt. `reason` (≤128, free text) | none | Sets `REJECTED`, forwards to the sender, then echoes back to the recipient |
| `FILE_CHUNK` | sender | `transfer_id`, `seq` | file bytes, 1–65 536 B | Relays it, sets `TRANSFERRING` |
| `FILE_END` | sender | `transfer_id`, `total_chunks`, `total_bytes` | none | Relays it, sets `VERIFYING` |
| `FILE_RESULT` | recipient | `transfer_id`, `ok`, `received_bytes`; opt. `sha256`, opt. `error_code` | none | Relays it, sets `COMPLETED` or `FAILED` |
| `FILE_CANCEL` | either | `transfer_id`; opt. `reason` (must be an error code) | none | Relays it, sets `CANCELLED` |

The decoder rejects a `filename` that is not 1–255 characters and at most 255 UTF-8 bytes, is `.` or `..`, or contains `/` or `\`, with `UNSAFE_FILENAME`, and any size above 100 MiB with `FILE_TOO_LARGE`. `chunk_size` must be 1–65 536, and `FILE_RESULT` carries `error_code` exactly when `ok` is `false`.

The server does not check `seq`, sizes or hashes; the receiving client does. `seq` must be the next chunk number (`SEQ_MISMATCH`). Every chunk but the last must be `chunk_size` bytes, and the totals in `FILE_END` must match (`SIZE_MISMATCH`). The SHA-256 of the received bytes must match the offer (`HASH_MISMATCH`). The receiver always puts that hash in `FILE_RESULT`, and the sender accepts `ok: true` only if `received_bytes` and the hash match its own file.

### 2.4.4 Discovery

Table 6. Discovery messages (UDP only; the datagram format is in 2.8).

| Type | Direction | Header fields (besides common) | Payload | Reply |
|---|---|---|---|---|
| `DISCOVER` | client → broadcast | none | none | `SERVER_INFO` |
| `SERVER_INFO` | server → client (unicast) | `ref_request_id`, `server_name` (≤64), `port` (1–65535), `tls` (bool); opt. `host` (≤255) | none | none |

The server never sets `host`; the client takes the server's IP from the datagram's source address. Either type sent over TCP gives `INVALID_FIELD`.

### 2.4.5 Error

Table 7. The error message.

| Type | Direction | Header fields (besides common) | Payload | Reply |
|---|---|---|---|---|
| `ERROR` | S→C only | `code` (an error code, Table 9), `message` (≤256); opt. `ref_request_id`, opt. `field` (≤64), opt. `transfer_id` | none | none |

`message` is user-facing text (Vietnamese, except one English text in the HELLO phase). `ref_request_id` is present when the error answers a request. It is absent from the HELLO-phase error and from transfer notices (`TIMEOUT`, `CONNECTION_LOST`), including the one the recipient gets when its `FILE_REJECT` cannot be forwarded. The server never sets `field`.

## 2.5 Error codes

There are 35 error codes. A code reaches the peer in an `ERROR`, as the `error_code` of a `FILE_RESULT`, or as the `reason` of a `FILE_CANCEL`. Decoder errors close the connection without a reply (2.6.2), and some codes are local only. Table 9 in Appendix A lists every code and when it is used.

## 2.6 Limits and validation

### 2.6.1 Key limits

The largest frame is 81 924 B (4 + 16 384 + 65 536). A file chunk carries at most 64 KiB (65 536 B), a chat message at most 8 KiB (8 192 B), and a file may be at most 100 MiB. An idle connection is closed after 45 s with the server's built-in default, or 120 s when the server is started from the command line. A new connection has 5 s for the TLS handshake and then a fresh 5 s to send `HELLO`. Table 10 in Appendix B lists all limits and timeouts.

### 2.6.2 Malformed frame vs. bad request

**Malformed frame: the connection is closed.** Any decoder error (framing, JSON, schema, direction or a spoofed field) poisons the stream (2.2.2), so the server closes the socket without a reply. The only exception is a well-formed frame other than `HELLO` during the HELLO phase: it gets `ERROR INVALID_FIELD` without `ref_request_id`, and then the connection closes. A handshake timeout closes silently.

**Valid frame, bad request: an `ERROR` reply.** A well-formed message that fails a routing, state or login check gets one `ERROR` with the code, `ref_request_id` and, if present, `transfer_id`. The connection stays open, except after the third `AUTH_FAILED` and after an unhandled type (`HELLO` after the handshake, `PING`, `PONG`). Table 9 gives the codes. Invalid UDP datagrams are ignored silently.

### 2.6.3 Server-stamped fields

`sender`, `message_id` and `timestamp_ms` are set only by the server. If a client header contains any of them, the frame fails with `SPOOFED_FIELD` and the connection closes. The server stamps `sender` (the username logged in on the sending connection) on every forwarded chat message and `FILE_OFFER`, and a fresh `message_id` and `timestamp_ms` on chat messages. A client therefore cannot claim to be another user.

## 2.7 State diagrams

### 2.7.1 Server-side connection

The server keeps one record per accepted socket; Figure 3 shows its states.

- **`CONNECTING`**: created after `accept()` if fewer than the maximum number of clients (64 by default) are connected; TLS handshake, then `HELLO`, 5 s each.
- **`HELLO_ACCEPTED`**: `HELLO_ACK` sent; the server waits for `AUTH`.
- **`AUTHENTICATED`**: user registered, then `AUTH_OK` sent; other users' messages are forwarded only after that `AUTH_OK` (waiting up to 1 s).
- **`CLOSING`**: server shutdown, or a failed write to this peer (a partial frame may be on the wire); nothing more is sent, and the worker notices within 0.5 s.
- **`CLOSED`**: every exit path; the user is removed and its transfers fail with `CONNECTION_LOST`.

End of stream, an idle timeout, a malformed frame, the third failed login, an unhandled type and `LOGOUT` after login go straight to `CLOSED`. Before login, `LOGOUT` gets `ERROR NOT_AUTHENTICATED` and the state does not change.

![Figure 3. Server-side connection states (`ConnectionState`). "Fatal error" means a malformed frame, an idle timeout, or an unhandled type after its `ERROR` reply.](../diagrams/state-connection.svg)

### 2.7.2 File transfer

A transfer has eight statuses, four of them final (`COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`). The sender, the server and the recipient each track it. Figure 4 shows the allowed transitions; every non-final state may also move to `CANCELLED` or `FAILED`.

- **`OFFERED`**: the offer is made (sender, server); the recipient's record starts here when its user accepts.
- **`ACCEPTED`**: the recipient has opened its `.part` file and sends `FILE_ACCEPT`.
- **`TRANSFERRING`**: chunks are flowing (from the first `FILE_CHUNK`).
- **`VERIFYING`**: the last chunk is sent and `FILE_END` follows.
- **`COMPLETED`**: counts and SHA-256 match and the `.part` file is renamed; `FILE_RESULT` with `ok: true`.
- **`REJECTED`** / **`CANCELLED`**: after `FILE_REJECT` / `FILE_CANCEL`.
- **`FAILED`**: `ERROR TIMEOUT` or `CONNECTION_LOST`, `FILE_RESULT` with `ok: false`, or a local error.

The server also checks roles. Only the recipient may send `FILE_ACCEPT` or `FILE_REJECT` (in `OFFERED`) and `FILE_RESULT` (after `OFFERED`; `ok: true` only in `VERIFYING`). Only the sender may send `FILE_CHUNK` or `FILE_END` (in `ACCEPTED` or `TRANSFERRING`). Either party may send `FILE_CANCEL` before a final state. Anything else gets `ERROR TRANSFER_STATE`, and a finished session is removed at once.

Unlike the two clients, the server does not check transitions on its relay path. Its record therefore also moves `TRANSFERRING` → `TRANSFERRING` on each further chunk, and `ACCEPTED` → `VERIFYING` for an empty file (marked in Figure 4).

![Figure 4. File-transfer states (`TransferStatus`). The note marks the two moves that only the server's relay path makes.](../diagrams/state-transfer.svg)

## 2.8 UDP discovery

A client finds a server on the LAN by UDP broadcast. Each datagram holds exactly one frame in the format of Figure 2, with no payload and no trailing bytes, and is at most 1 024 B long; an invalid datagram is ignored.

The server binds its UDP socket to the same address and port as its TCP listener (default port 5000; with the default host `127.0.0.1` this is loopback). If the bind fails, the server runs without discovery. The client broadcasts one `DISCOVER` to `255.255.255.255` on that port and collects replies for 2 s. The server answers with a unicast `SERVER_INFO` to the source address and port of the request, carrying `ref_request_id`, `server_name`, the TCP `port` and the `tls` flag.

The client keeps the replies that match its `request_id`, takes each server's IP from the source address, prints the list and exits. The user then connects with that host and port; TLS is chosen from the client's own options, not from `tls`. Discovery is plaintext and finds servers, not users (see 1.6); a client learns who is online through `USER_LIST` (Table 3).
