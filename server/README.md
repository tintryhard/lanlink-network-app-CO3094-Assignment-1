# server/

| File | What it does |
|---|---|
| `__main__.py` | `python -m server ...`: parses options, builds `TCPServer`, runs it until Ctrl+C |
| `app.py` | `TCPServer`: accepts clients, runs one worker thread per client, routes chat, negotiates and relays file transfers |
| `sessions.py` | Records kept in the registries: `Connection`, `UserSession`, `TransferSession` |
| `discovery.py` | `answer_discovery`: UDP thread answering `DISCOVER` with `SERVER_INFO` |
| `auth.py` | `CredentialStore` (password file) and `python -m server.auth` to create accounts |

## How a client is served (`app.py`)

1. `serve_forever` accepts the socket and starts a worker thread `_serve_client`. Extra clients beyond `max_clients` are closed immediately.
2. The worker does the TLS handshake (if on) and `HELLO`, within `--timeout`.
3. `_dispatch_client_messages` reads one message at a time:
   - `AUTH` logs the client in;
   - `USER_LIST` returns the online users;
   - chat is forwarded and confirmed with `CHAT_ACK`;
   - file messages are negotiated or relayed;
   - `LOGOUT` ends the session.

   A bad request gets one `ERROR` reply that carries its `ref_request_id`.
4. On exit, for any reason, the worker removes the user, fails that client's transfers (`CONNECTION_LOST`, sent to the other party) and unregisters the connection.

## Worth knowing

- **One lock, no I/O under it.** `_lock` guards `_connections`, `_users` and `_transfers`. Code copies what it needs under the lock and sends after releasing it, so one slow client cannot freeze the others.
- **Per-transfer lock.** `TransferSession.lock` keeps the messages of one transfer in order. It is always taken *before* `_lock`, never after, which avoids deadlock.
- **The server does not read files.** It relays chunks between the two clients and checks only *who* may send *what* and *when* (`_status_after_message`). Hashing and size checks happen in the receiving client.
- **Timeouts:**
  - `idle_timeout` disconnects a silent client, but a client in a running transfer counts as active;
  - `transfer_timeout` expires each transfer phase;
  - `AUTH_READY_WAIT_S` (1 s) bounds how long a route waits for a recipient's `AUTH_OK`, so the sender's `CHAT_ACK` always arrives before the client's 5 s timeout.
- **Security:**
  - plaintext binds only to loopback; any other address needs `--cert/--key`;
  - passwords are stored as PBKDF2-HMAC-SHA256 hashes with a random salt (600,000 rounds);
  - unknown users are checked against a dummy hash, so a wrong name and a wrong password take the same time.
- **Discovery is optional.** If the UDP port cannot be bound (busy, or reserved on Windows), the server prints `Discovery disabled: ...` and still serves TCP.
- **Worker wake-up.** Each worker's `receive()` wakes at least every `WORKER_WAKEUP_S` (0.5 s) to expire transfers and to notice a shutdown.
- **Shutdown** stops accepting, closes the UDP socket and calls `socket.shutdown()` on every client, which wakes blocked `recv()` calls. It then joins all worker threads.
- **Public chat** is written to each recipient in turn on the sender's thread, so one slow recipient delays the `CHAT_ACK`.
