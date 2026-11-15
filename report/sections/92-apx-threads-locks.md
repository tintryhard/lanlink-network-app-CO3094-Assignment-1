## C Threads

Table 11 lists every thread that the server and the client start, with the name given
in the code. With N open connections and discovery enabled, the server runs N + 2
threads. A logged-in client runs 3 threads, plus 1 for each outgoing transfer that is
sending.

Table 11. Threads on the server and the client.

| Thread (name in code) | Started in | What it does | How many |
|---|---|---|---|
| Server main thread | `server/__main__.py` calls `TCPServer.serve_forever()` | Accept loop (0.1 s accept timeout); starts discovery and one worker per accepted socket; closes sockets above `--max-clients`; on exit `shutdown()` joins all workers | 1 |
| `lanlink-discovery` | created in `TCPServer.__init__`, started in `serve_forever()` (`server/app.py:89`, `:137`) | `answer_discovery()` in `server/discovery.py`: answers each valid `DISCOVER` with a unicast `SERVER_INFO`; daemon | 0 or 1 (0 if the UDP bind fails) |
| `lanlink-<connection_id>` (worker) | `serve_forever()` (`server/app.py:153`) | `_serve_client()`: TLS handshake, `HELLO`, login, request loop, cleanup; also writes forwarded chat and file messages onto other clients' connections; after `HELLO`, wakes at least every 0.5 s | 1 per connection, at most `--max-clients` (default 64) |
| Client main thread (login run) | `login_cli()` in `client/__main__.py` | Command loop: takes one item at a time from the `commands` queue (typed line, file event, final transfer snapshot); sends requests and waits for their replies | 1 |
| `lanlink-cli-input` | `login_cli()` (`client/__main__.py:119`) | Reads one stdin line per prompt and queues it; end of input becomes `/logout`; daemon | 1 |
| `lanlink-client-reader` | `PrivateChatClient.__init__` (`client/private_chat.py:70`) | Only reader of the socket: prints chats, hands replies to waiting requests by `ref_request_id`, feeds file messages to the engines and sends their replies (e.g. `FILE_RESULT`), queues file events; not a daemon, joined by `close()` | 1 |
| `lanlink-file-sender` | `_dispatch_file_message()` on `FILE_ACCEPT` for an own offer (`client/private_chat.py:237`) | `_send_chunks_until_done()`: sends every `FILE_CHUNK` and the `FILE_END`; daemon | 1 per accepted outgoing transfer |
| Client main thread (`--discover` or handshake-only run) | `main()` in `client/__main__.py` | `discover_servers()`: one broadcast, collects `SERVER_INFO` replies for 2 s, exits; or one `HELLO` exchange, then exit | 1 |

## D Locks and lock order

Table 12 lists the locks, what each protects and whether it is held during socket I/O.

Table 12. Locks, what they protect and whether they are held during socket I/O.

| Lock | Defined in | Protects | Held during socket I/O |
|---|---|---|---|
| `TCPServer._lock` | `server/app.py:72` | the registries `_connections`, `_users`, `_transfers`; connection state; transfer status and deadline; the atomic check-and-insert at login | never while a thread reads or writes a socket or hashes a password (the accept loop does close an over-cap socket and start a worker under it, `server/app.py:148-161`) |
| `TransferSession.lock` | `server/sessions.py:41` | the order in which one transfer's messages are checked and forwarded | yes, while forwarding to the other party or sending `ERROR` to both |
| `SocketTransport._write_lock` | `common/transport.py:27` | one `sendall()` of one whole frame | yes (its purpose) |
| `PrivateChatClient._lock` | `client/private_chat.py:63` | pending requests, incoming offers, live transfers, the stored connection error | never |
| engine `RLock` | `transfer/sender.py:154`, `transfer/receiver.py:53` | one transfer's client-side state | never; callbacks run after it is released |

Order on the server: a thread may hold a `TransferSession.lock` while it takes
`TCPServer._lock` or a `_write_lock`, never the reverse. `TCPServer._lock` is never held
while a transfer lock or a write lock is taken (`server/app.py:393`), and `_write_lock`
is innermost: under it the thread only calls `sendall()`. On the client, the shared
lock (`PrivateChatClient._lock`) and the engine `RLock` are never nested; engine callbacks
run only after the engine lock is released (see `transfer/progress.py`).
Apart from taking those two inner locks and writing, a thread that holds a transfer
lock blocks only on the event `Connection.auth_ready`, for at most 1 s
(`AUTH_READY_WAIT_S`), until the recipient's `AUTH_OK` has been sent.
