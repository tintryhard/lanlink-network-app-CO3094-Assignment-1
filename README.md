# LanLink — Distributed Client-Server Collaboration Application

Course project skeleton for Python, TCP/UDP sockets, TLS and PySide6.

**Status:** Stage 4A supports concurrent TCP handshakes, local password
verification, authenticated user lists, private chat and logout. The server retains the bounded
thread-per-client architecture. Existing file-engine and file-widget components
are not integrated into a complete application.

## Team ownership
- **A:** `server/`, `common/transport.py`, `common/tls.py`; architecture and network tests.
- **B:** `client/` except `client/ui/file_card.py` and `client/ui/transfers_panel.py`; GUI and ClientService.
- **C:** `common/models.py`, `common/errors.py`, `common/protocol.py`, `transfer/`, and file widgets.
- **All:** integration tests, reviews and demonstration.

Only create feature-specific modules when implementing the corresponding task. The initial repo deliberately does not contain empty placeholder modules.

## Setup
Use a team-agreed Python version. From the repository root:

```bash
python -m venv .venv
```

Activate the environment on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Or on macOS/Linux:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

## Stage 4A: Local authentication and private chat

Create accounts interactively (password input is hidden):

```bash
python -m server.auth --file data/users.json --username alice
python -m server.auth --file data/users.json --username bob
```

The command creates or updates one account, retaining the others. The version 1
JSON file contains a separate random 16-byte salt and PBKDF2-HMAC-SHA256 digest
(600,000 iterations) for each password. It contains no plaintext passwords.
Use the command offline, one instance at a time; restart the server after edits.
`data/` is already ignored by Git. No accounts or default passwords are bundled.

```bash
# Terminal 1
python -m server --host 127.0.0.1 --port 5000 --users data/users.json --max-clients 64 --timeout 5 --idle-timeout 45
# Terminal 2 (prompts for the password)
python -m client --host 127.0.0.1 --port 5000 --username alice
# Terminal 3
python -m client --host 127.0.0.1 --port 5000 --username bob
```

The client logs in, prints the current user list, and remains connected. In
Alice's terminal, send `/msg bob Hello Bob`; in Bob's, reply `/msg alice Hi Alice`.
Private messages appear even while the recipient waits at the input prompt.
Use `/users` (or Enter) to refresh users and `/logout` (or `logout`) to leave.
EOF on terminal input logs out. Usernames and message text preserve their case.
User lists are requested, not broadcast. They contain authenticated users only,
including the requester, with username, online=true, peer IP and peer TCP port.

**Passwords and chat travel in plaintext TCP in this stage.** This is a localhost
demo, not secure deployment authentication. The server requires a loopback bind
even without credentials; the CLI checks the connected peer is loopback. TLS is not implemented.

Omit `--username` to run the original handshake-only client. Starting the server
without `--users` preserves handshake functionality but rejects every AUTH.

### Connection and session lifecycle

- Every accepted connection owns its socket, decoder, worker and transport write
  lock. A server lock protects connection and authenticated-user registries.
- HELLO is required first. The absolute HELLO read deadline is `--timeout`
  (default 5 seconds). After HELLO, each request must finish within
  `--idle-timeout` (default 45 seconds), including a request sent in fragments.
- AUTH checks the username/password against immutable loaded hashes. Password
  hashing and socket I/O happen outside the registry lock. Duplicate username
  check-and-insert is atomic; one active session per exact, case-sensitive name.
- AUTH_OK confirms login and the connection state becomes AUTHENTICATED. The
  registry stores username, connection ID, IP, port and online state. Connection
  IDs and credentials are not included in USER_LIST responses.
- Invalid credentials return ERROR(AUTH_FAILED), without distinguishing unknown
  names from wrong passwords. Three credential failures close that connection;
  failed attempts can otherwise be retried on the same connection.
- Duplicate active names and repeated AUTH return ERROR(ALREADY_LOGGED_IN).
  Repeated AUTH preserves the original session. USER_LIST/LOGOUT/CHAT_PRIVATE before AUTH
  return ERROR(NOT_AUTHENTICATED); unsupported operations are rejected and closed.
  Errors after HELLO reference the rejected request ID. AUTH before HELLO gets
  ERROR(INVALID_FIELD) followed by close; corrupt frames are closed directly.
- Authenticated USER_LIST requests return a locked snapshot. LOGOUT terminates
  the connection with EOF (there is no separate logout-ACK message type).
- EOF, malformed frames, reset, request timeout and shutdown close sockets and
  remove both connection and user records. Ctrl+C stops admissions, interrupts
  socket I/O and joins workers. No credential hashing occurs under the lock.

The default connection cap is 64; excess connections are closed. There is no
cross-connection rate limiter, account registration API, persistence of online
state, public broadcast, file transfer, UDP discovery or Qt integration in Stage 4A.

### Private routing and client workers

A CHAT_PRIVATE request contains a recipient and UTF-8 payload. The server takes
sender identity from the authenticated session, resolves the recipient through
`_users` and `_connections`, and stamps a new message_id and timestamp_ms. Client
supplied sender/message_id/timestamp_ms fields are rejected by the existing codec.
The server forwards only to that recipient, using its existing SocketTransport
and write lock. It waits for AUTH_OK to be sent before any unsolicited chat.
No registry lock is held during network I/O. Sending to yourself is supported.

After a successful forward write, CHAT_ACK references the sender's original
request_id and carries the forwarded message_id and timestamp_ms. Its `accepted`
status means server acceptance after the write, **not a read receipt or durable
recipient delivery**. A missing ACK after disconnect does not prove that the
recipient did not receive the message; there is no automatic retry/deduplication.

A missing active recipient (including an unknown account) returns
ERROR(RECIPIENT_OFFLINE). Forward-write failure returns ERROR(CONNECTION_LOST)
and shuts down the failed recipient socket to trigger session cleanup. Business
errors preserve the sender session. Invalid schema/framing closes that connection;
unsupported public chat returns ERROR(INVALID_FIELD) and closes it.

After synchronous HELLO/AUTH, `PrivateChatClient` owns the connection's sole socket
reader thread. The main CLI thread reads commands and sends requests. The reader
prints incoming private messages through a callback and puts ACK/ERROR responses
in bounded per-request queues keyed by ref_request_id. USER_LIST has no response
reference field in the existing schema, so only one list request is allowed at a
time. Main-thread requests never call receive once the reader starts. Callback
functions run on the reader and must return promptly without making synchronous
client requests. Disconnects wake pending requests; close shuts down the socket
and joins the reader. The socket is closed by its owning context manager.

Terminal output may interrupt the current prompt. A server disconnect is printed
while input waits; press Enter to let the main input loop observe the failure.
Direct forwarding is blocking and bounded by the socket timeout; there is no
outbound queue or slow-reader fairness policy yet. Session inactivity is based
on client requests, so incoming chat alone does not reset the server idle deadline.

### Tests

```bash
# Stage 4A private chat and CLI tests
python -m pytest -q tests/integration/test_private_chat.py
# Stage 1/2/3/4A and existing non-Qt regressions
python -m pytest -q tests/integration tests/protocol tests/transfer tests/scripts
```

The integration tests use real localhost sockets and generated passwords/hash
files in temporary directories. They cover 20 simultaneous authenticated users,
duplicate races, user-list metadata, unauthorized sequences, logout, disconnect,
timeout and shutdown cleanup. Private-chat tests also cover metadata/ACK matching,
third-party isolation, offline recipients, concurrent forwarding, authentication
reply ordering and unsolicited reception during CLI input. Stage 1–3 tests remain
in place.

## Workflow
1. Agree `docs/protocol.md` and module interfaces first.
2. Create a feature branch: `git switch -c feat/a-server` (example).
3. Implement your owned module and tests; commit small changes.
4. Push and open a pull request into `main`; request review.
5. Never commit credentials, private keys or real user data.

## Submission
Add run instructions, protocol/state/sequence diagrams, test evidence, and PDF report before submission.
