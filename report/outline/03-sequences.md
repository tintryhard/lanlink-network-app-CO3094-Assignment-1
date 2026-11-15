# Outline: 3 Sequence Diagrams (owner: report-sequences)

## Heading tree
- **3 Sequence Diagrams** (intro, ~4 lines): what a lifeline is here (one thread, not one host); every
  arrow is a real `MessageType` or a named function. Answers Spec §10 "lifecycle of a message and a file transfer".
- **3.1 Login and user list** (`seq-login`, ~6 sentences + figure). Traces `client/__main__.py:login_cli`,
  `client/connection.py:open_connection/authenticate`, `common/handshake.py:initiate_hello/accept_hello`,
  `server/app.py:serve_forever/_serve_client/_dispatch_client_messages`, `server/auth.py:CredentialStore.verify`.
- **3.2 Chat message lifecycle** (`seq-chat`, ~6 sentences + figure). Traces `__main__.login_cli` (`/msg`, `/all`),
  `client/private_chat.py:send_private/_request/_read_loop`, `server/app.py:_route_private/_route_public`.
  Short paragraph on public fan-out (no extra figure).
- **3.3 File transfer: happy path** (`seq-file`, ~6 sentences + figure). Traces `private_chat.offer_file/accept_file/
  _dispatch_file_message/_send_chunks_until_done`, `transfer/sender.py:inspect_file/SendSession.outgoing/handle_message`,
  `transfer/receiver.py:Receiver.start/write/finish/handle_message`, `server/app.py:_negotiate_file_transfer/_relay_file_content`.
- **3.4 File transfer: failure paths** (`seq-file-failures`, may split in two figures, ~6 sentences each). Same files plus
  `server/app.py:_fail_transfers_of/_fail_transfer_and_notify/_receive_until_deadline`, `Receiver._fail_and_cleanup`.
- **3.5 Matching replies to requests** (~5 sentences, no figure): `request_id`/`ref_request_id`;
  `PrivateChatClient._pending` (one `queue.Queue(maxsize=1)` per request, cap 64 -> `QUEUE_FULL`, 5 s timeout closes
  the client); unsolicited frames (chat, file types, ERROR with `transfer_id`) bypass `_pending`.

## Diagrams
**seq-login** "Figure N. Login: TCP/TLS connect, HELLO, AUTH and the first USER_LIST."
- Lifelines: User, Alice CLI (main thread), Server accept loop (`serve_forever`), Server worker (Alice), Alice reader thread.
- Order: TCP connect -> accept, spawn worker `lanlink-<id>` -> Note: TLS handshake in worker, bounded by
  `handshake_timeout` (5.0 s) -> `HELLO` -> `HELLO_ACK` (ref_request_id) -> getpass -> `AUTH` (password = payload)
  -> PBKDF2 verify, `UserSession(ip, port)` registered, state AUTHENTICATED -> `AUTH_OK` -> reader thread started
  (`PrivateChatClient.__init__`) -> `USER_LIST` request -> `USER_LIST` reply via `_pending`.
- alt: bad password -> `ERROR AUTH_FAILED` (CLI exits; server closes after 3 failures); username taken ->
  `ERROR ALREADY_LOGGED_IN`; handshake timeout -> `socket.timeout` caught, socket closed, **no ERROR sent**;
  first frame not HELLO -> `ERROR INVALID_FIELD`, close. opt: max_clients (64) reached -> accept then close.

**seq-chat** "Figure N. Lifecycle of a private message."
- Lifelines: Alice input thread (`lanlink-cli-input`), Alice CLI main thread, Server worker (Alice), Bob reader thread,
  Alice reader thread. (No "Server worker (Bob)": Alice's worker writes straight into Bob's transport.)
- Order: `/msg bob text` -> `commands` queue -> `send_private` registers `_pending[request_id]` -> `CHAT_PRIVATE(recipient)`
  -> `_route_private`: lookup `_users`, `auth_ready.wait(1.0)`, recheck -> stamp `sender`, `message_id`, `timestamp_ms`
  -> `CHAT_PRIVATE` to Bob -> `display()` prints `[alice -> bob]` -> `CHAT_ACK(ref_request_id, message_id)` to Alice
  -> Alice reader pops `_pending` -> CLI prints "Accepted".
- alt: recipient offline -> `ERROR RECIPIENT_OFFLINE`; write to Bob fails -> Bob socket shut down, `ERROR CONNECTION_LOST`.
- Note: `CHAT_PUBLIC` is sent to every other online user, write failures skipped, `CHAT_ACK` always sent; ACK is not a read receipt.

**seq-file** "Figure N. File transfer: offer, accept, chunks, verification."
- Lifelines: Alice CLI, Alice reader thread, Alice sender thread (`lanlink-file-sender`), Server worker (Alice),
  Server worker (Bob), Bob reader thread, Bob CLI.
- Order: `/offer` -> `inspect_file` (SHA-256) -> `FILE_OFFER` -> TransferSession OFFERED -> `FILE_OFFER(+sender)` -> Bob CLI
  `/accept` -> `Receiver.start` (reserve name, open `<name>.<id8>.part`) -> `FILE_ACCEPT` -> ACCEPTED -> `FILE_ACCEPT` to
  Alice, echo to Bob -> Alice reader starts sender thread -> loop `FILE_CHUNK(seq 0..n-1)` relayed by Server worker (Alice),
  `Receiver.write` checks seq/size, hashes, progress bar -> `FILE_END(total_chunks, total_bytes)` (VERIFYING) -> `finish`:
  counts + SHA-256, fsync, `os.replace` .part -> final -> `FILE_RESULT(ok, sha256)` from Bob reader -> COMPLETED, session
  removed -> `SendSession.handle_message` checks size + hash.
- Note: no per-chunk ACK; TCP flow control only; server never inspects content.

**seq-file-failures** (likely split into `seq-file-failures` + `seq-file-failures-2` for A4)
- Lifelines: Alice reader/sender threads, Server worker (Alice), Server worker (Bob), Bob reader thread.
- (a) reject: `FILE_REJECT` -> REJECTED, session deleted, forwarded to Alice, echoed to Bob.
- (b) cancel: `FILE_CANCEL(reason)` from `SendSession.outgoing` on `TransferError` (e.g. `SOURCE_CHANGED`) -> CANCELLED ->
  Bob `CANCELLED_BY_PEER`, .part deleted.
- (c) sender disconnect: worker EOF -> `_serve_client` finally -> `_fail_transfers_of(CONNECTION_LOST)` ->
  `ERROR CONNECTION_LOST (transfer_id)` to Bob -> `_fail_and_cleanup` deletes .part.
- (d) hash mismatch: `finish` -> `HASH_MISMATCH` -> `FILE_RESULT ok=false error_code` -> FAILED -> Alice FAILED.
- opt (e) timeout: `_receive_until_deadline` -> `ERROR TIMEOUT` to both (60 s `transfer_timeout`), if space allows.

## Rubric evidence (Spec §9)
Step 2 (HELLO/HELLO_ACK), 3 (worker per client, reader/sender/input threads), 4 (AUTH, UserSession ip/port), 5 (routing),
6 (chunking, seq, SHA-256, reassembly), 7 (timeouts, disconnect cleanup), 8 (TLS note only), 9 (display, progress bar).

## Open questions / gaps
1. **No user-initiated cancel.** `SendSession.cancel`/`Receiver.cancel` exist but the CLI has no `/cancel`; `FILE_CANCEL`
   is only sent on a local source error. Diagram (b) shows that path and the text says so. Report as a gap?
2. **Handshake timeout sends nothing**: the server just closes; the client sees `EOFError`. Docs say only "within --timeout".
3. Docs §5 shows one "Server" lifeline; code relays via the *sending* client's worker writing to the peer transport.
   Brief's suggested `Server worker (Bob)` only appears where Bob sends (FILE_ACCEPT/REJECT/RESULT).
4. CLI uses `client.users()` (reader-thread path) for the first USER_LIST; `connection.request_users` is test-only.
5. "Encrypt payloads" (step 8) is TLS on the whole stream; sequences show it as a note only, detail belongs to sections 1/2.
6. Boundary with section 2: section 2 owns field tables, error-code table and both state diagrams; here state names
   appear only as notes (OFFERED/ACCEPTED/...), with a cross-reference. UDP discovery (`DISCOVER`/`SERVER_INFO`) left to
   sections 1/2 unless the lead wants a `seq-discovery`.
