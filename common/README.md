# common/

Code shared by the client and the server. Only `transport.py` and `tls.py` touch sockets. The other files are pure data and logic, so they can be tested without a network.

| File | What it does |
|---|---|
| `models.py` | `MessageType`, the frozen `Message` record, `TransferStatus` with its legal transitions (`ALLOWED_TRANSITIONS`, `check_transition`), `TransferSnapshot`, `ConnectionState`, `UserInfo` |
| `errors.py` | `ErrorCode` (the codes sent in `ERROR` messages), Vietnamese user messages, and the exceptions `LanLinkError` > `ProtocolError` / `TransferError` |
| `protocol.py` | The codec. It encodes and decodes frames, holds the schema of every message type, validates each field, checks which message types may travel in which `Direction`, and has `FrameDecoder` for TCP streams and `decode_datagram` for UDP |
| `builders.py` | One `build_*` function per message type. Each one fills in the fields and validates the result through `protocol.py` |
| `transport.py` | `SocketTransport`: send and receive whole `Message`s over one socket |
| `handshake.py` | `initiate_hello` (client) and `accept_hello` (server) |
| `tls.py` | `server_context` / `client_context`, the TLS settings |

## Worth knowing

- **Frame format:** a 4-byte big-endian length (`struct "!I"`), then a JSON header, then `payload_length` bytes of payload. Binary data (file chunks) travels in the payload and is never base64-encoded.
- **TCP is a byte stream.** One `recv()` can return half a frame or several frames. `FrameDecoder.feed()` buffers the bytes and returns only complete messages. After a bad frame it fails permanently, because a stream cannot resync.
- **UDP keeps message boundaries.** A datagram must contain exactly one frame of at most 1024 bytes.
- **Validation is strict:**
  - unknown or missing fields are rejected, and so are duplicate JSON keys and NaN values;
  - a client cannot send server-stamped fields (`sender`, `message_id`, `timestamp_ms`), so it cannot pretend to be another user;
  - a filename cannot contain `/`, `\` or `..`.
- **Thread safety:**
  - `SocketTransport.send` holds a write lock, so two threads (for example the reader replying while the file sender pumps chunks) never interleave bytes of two frames;
  - `receive` is used by one thread only.
- **TLS:** the client trusts exactly the server's self-signed certificate (`--cafile`). The host name is not checked, because a LAN server's IP may change.
