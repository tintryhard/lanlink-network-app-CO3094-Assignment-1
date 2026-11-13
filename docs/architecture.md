# LanLink architecture and protocol

## 1. Components

```
                 ┌──────────────────────── Server process ─────────────────────────┐
                 │  accept loop (serve_forever)                                    │
 Client A ──TCP/TLS──► worker thread A ─┐                                          │
 Client B ──TCP/TLS──► worker thread B ─┼─► registries under one _lock:            │
 Client C ──TCP/TLS──► worker thread C ─┘     _connections, _users, _transfers    │
                 │                                                                 │
 any client ──UDP──► discovery thread (same port number) ──► SERVER_INFO           │
                 └─────────────────────────────────────────────────────────────────┘

 Client process: main (CLI loop) · input (stdin) · reader (socket) · file sender (chunks)
```

| Layer | Modules |
|---|---|
| User interface | `client/__main__.py`, `client/display.py` |
| Network | `client/private_chat.py`, `client/connection.py`, `client/discovery.py`, `server/app.py`, `server/discovery.py`, `common/transport.py`, `common/tls.py` |
| Data processing (no sockets) | `common/protocol.py`, `common/builders.py`, `common/models.py`, `transfer/` |

All chat and file data goes **through the server**: clients never connect to each other.

## 2. Frame format

```
+---------------------+------------------------------+---------------------------+
| header length (4 B) | JSON header (UTF-8)          | payload (payload_length B)|
| unsigned, big-endian| {"version":1,"type":...,...} | text or raw file bytes    |
+---------------------+------------------------------+---------------------------+
```

- **Every header contains** `version` (1), `type`, `request_id` (a random hex ID) and `payload_length`.
- **Replies carry** `ref_request_id`, set to the `request_id` they answer. This is how a client matches a reply to its request.
- **Limits:**

  | Item | Limit |
  |---|---|
  | header | 16 KiB |
  | chat | 8 KiB |
  | chunk | 64 KiB |
  | file | 100 MiB |
  | UDP datagram | 1024 B |

- **A bad frame closes the connection.** A valid frame with a wrong request gets an `ERROR` reply.

## 3. Message types

| Type | Direction | Header fields (besides the common ones) | Payload |
|---|---|---|---|
| `HELLO` | C→S | `client_name`, `client_version` | – |
| `HELLO_ACK` | S→C | `ref_request_id`, `server_name`, `limits` | – |
| `AUTH` | C→S | `username` | password (UTF-8) |
| `AUTH_OK` | S→C | `ref_request_id`, `username` | – |
| `USER_LIST` | C→S / S→C | reply: `ref_request_id` | reply: `{"users":[{username, online, ip, port}]}` |
| `CHAT_PRIVATE` | C→S / S→C | `recipient`; forwarded copy adds `sender`, `message_id`, `timestamp_ms` | text |
| `CHAT_PUBLIC` | C→S / S→C | forwarded copy adds `sender`, `message_id`, `timestamp_ms` | text |
| `CHAT_ACK` | S→C | `ref_request_id`, `message_id`, `timestamp_ms`, `status` = `accepted` | – |
| `FILE_OFFER` | C→S→C | `transfer_id`, `recipient`, `filename`, `size`, `sha256`, `chunk_size`; forwarded copy adds `sender` | – |
| `FILE_ACCEPT` / `FILE_REJECT` | C→S→C | `transfer_id` (reject: optional `reason`) | – |
| `FILE_CHUNK` | C→S→C | `transfer_id`, `seq` | file bytes |
| `FILE_END` | C→S→C | `transfer_id`, `total_chunks`, `total_bytes` | – |
| `FILE_RESULT` | C→S→C | `transfer_id`, `ok`, `received_bytes`, `sha256`; `error_code` when not ok | – |
| `FILE_CANCEL` | C→S→C | `transfer_id`, optional `reason` | – |
| `LOGOUT` | C→S | optional `reason` | – |
| `ERROR` | S→C | `code`, `message`, optional `ref_request_id`, `transfer_id` | – |
| `DISCOVER` | UDP C→S | – | – |
| `SERVER_INFO` | UDP S→C | `ref_request_id`, `server_name`, `port`, `tls` | – |

`PING` and `PONG` exist in the codec but are not used by the CLI.

The server stamps `sender`, `message_id` and `timestamp_ms` itself. If a client sends them, the frame is rejected (`SPOOFED_FIELD`).

## 4. State diagrams

**Server-side connection** (`ConnectionState`):

```
CONNECTING ──TLS + HELLO ok──► HELLO_ACCEPTED ──AUTH ok──► AUTHENTICATED
     │                              │                            │
     └──────────── error / timeout / LOGOUT / disconnect ────────┴──► CLOSED
                         (server shutdown marks every connection CLOSING first)
```

**File transfer** (`TransferStatus`; the same graph on the sender, the server and the receiver):

```
OFFERED ──FILE_ACCEPT──► ACCEPTED ──FILE_CHUNK──► TRANSFERRING ──FILE_END──► VERIFYING ──FILE_RESULT ok──► COMPLETED
   │
   └──FILE_REJECT──► REJECTED
Any non-final state ──FILE_CANCEL──► CANCELLED
Any non-final state ──error / timeout / disconnect / FILE_RESULT not ok──► FAILED
```

## 5. Message flows

**Login and user list**

```
Client                         Server
  │── HELLO ──────────────────►│  (within --timeout)
  │◄──────────────── HELLO_ACK ─│
  │── AUTH(username, password)►│  PBKDF2 check, register user with IP and port
  │◄────────────────── AUTH_OK ─│
  │── USER_LIST ──────────────►│
  │◄──── USER_LIST(users JSON) ─│
```

**Private chat** (a public chat is the same, but the server forwards it to every other online user)

```
Alice                        Server                         Bob
  │── CHAT_PRIVATE(bob, text) ►│                              │
  │                            │── CHAT_PRIVATE(+sender,id) ─►│
  │◄──────── CHAT_ACK(id) ─────│                              │
```

If Bob is offline, Alice gets `ERROR RECIPIENT_OFFLINE` instead of `CHAT_ACK`.

**File transfer**

```
Alice (sender)                 Server                        Bob (receiver)
  │── FILE_OFFER(name,size,sha)►│ new session OFFERED          │
  │                             │── FILE_OFFER(+sender) ──────►│ /accept: open name.<id>.part
  │                             │◄──────────── FILE_ACCEPT ────│
  │◄──────────── FILE_ACCEPT ───│ ACCEPTED (echoed to Bob too) │
  │── FILE_CHUNK seq 0..n ─────►│── FILE_CHUNK ───────────────►│ write + hash each chunk
  │── FILE_END(chunks, bytes) ─►│── FILE_END ─────────────────►│ check counts + SHA-256,
  │                             │◄──── FILE_RESULT(ok, sha) ───│ fsync, rename .part → name
  │◄──── FILE_RESULT ───────────│ COMPLETED, session removed   │
```

If either side disconnects, the server sends `ERROR CONNECTION_LOST` (with `transfer_id`) to the other side. The receiver then deletes its `.part` file.

**Discovery**

```
Client ── DISCOVER (UDP broadcast 255.255.255.255:port) ──► every host on the LAN
Client ◄── SERVER_INFO(name, port, tls) (UDP unicast) ──── LanLink server
```

The client takes the server's IP from the reply's source address.

## 6. Concurrency rules

1. **Server:** one thread per client. The registries share one `_lock`, and socket I/O never happens while it is held.
2. **Lock order:** `TransferSession.lock` is taken before `_lock`, never the other way round, so the two locks cannot deadlock.
3. **One socket, many writers:** the reader, the sender thread and the server's routing threads may all write to the same socket. `SocketTransport` has a per-socket write lock, so frames are never interleaved.
4. **Client reads:** only the reader thread reads the socket. Requests wait on their own queue, matched by `ref_request_id`.
