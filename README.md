# LanLink — Distributed Client-Server Collaboration Application

Course project skeleton for Python, TCP/UDP sockets, TLS and PySide6.

**Status:** Stage 4B supports concurrent TCP handshakes, local password
verification, authenticated user lists, private/public chat and logout. The server retains the bounded
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

## Stage 4B: Local authentication, private and public chat

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
Use `/all Hello everyone` to broadcast to other authenticated online users.
Private and public messages appear even while the recipient waits at the input prompt;
public messages display as `[sender -> all]`.
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
  Repeated AUTH preserves the original session. USER_LIST/LOGOUT/CHAT_PRIVATE/CHAT_PUBLIC before AUTH
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
state, file transfer, UDP discovery or Qt integration in Stage 4B.

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
errors preserve the sender session. Invalid schema/framing closes that connection.

### Public broadcast and ACK semantics

CHAT_PUBLIC uses the existing codec and schema. The server snapshots authenticated
online sessions except the sender, stamps one message ID and timestamp for the
whole broadcast, waits for each recipient's AUTH_OK, and rechecks session identity
before writing through its registered transport/send lock. Newly logged-in or
replacement sessions are not added to an in-progress broadcast. No registry lock
is held while waiting or sending.

The server attempts each recipient independently. A failed write shuts down that
recipient's socket for cleanup and does not stop attempts to other recipients.
After the attempts, CHAT_ACK references the original request ID and shares the
broadcast message ID and timestamp. `accepted` means server acceptance and a
best-effort broadcast attempt, **not delivery to every recipient or a read receipt**.
It is also returned when there are zero recipients or all recipient writes fail.
The existing ACK has no delivery counts; partial failures do not produce a second
ERROR or roll back messages already sent. There is no offline storage, retry,
deduplication or global ordering across simultaneous senders. Losing the sender's
connection before its ACK does not undo completed recipient writes.

After synchronous HELLO/AUTH, `PrivateChatClient` owns the connection's sole socket
reader thread. A daemon input worker reads commands; the main CLI thread handles
commands and disconnect notifications through a queue and sends requests. The reader
prints incoming private/public messages through a callback and puts ACK/ERROR/USER_LIST
responses in bounded per-request queues keyed by `ref_request_id`. Multiple
USER_LIST requests may be outstanding on the same connection (within the shared
64-request limit). Main-thread requests never call receive once the reader starts. Callback
functions run on the reader and must return promptly without making synchronous
client requests. Disconnects wake pending requests and promptly shut down and close
the socket. Cleanup is idempotent; close joins the reader unless called by that
reader itself. A surrounding socket context manager may safely close it again.

A correlated protocol ERROR fails only its request, including codes such as
RECIPIENT_OFFLINE, AUTH_FAILED, TIMEOUT and CONNECTION_LOST. Local validation
failures before writing also leave the connection usable and remove any pending
request entry. The CLI displays these request errors and accepts another command.
The authentication helper permits retries on the same socket; the CLI's existing
single login attempt still exits on authentication failure.

EOF, socket errors, malformed incoming frames, mismatched response types and
unknown/duplicate response references terminate the session. Idle socket read
timeouts merely retry; a request's response deadline or send timeout still closes
the session and fails all pending requests. This preserves the existing timeout
policy: operations may already have reached the server, and late responses cannot
be reused by another request. Retrying requires a new connection. Cleanup claims
ownership under the lock, performs socket operations outside it, and makes other
cleanup callers wait for descriptor closure before notifying disconnect.

USER_LIST requests retain their empty payload and existing header schema. Each
server response now requires `ref_request_id` equal to the original request's
`request_id`; its own `request_id` remains independent. This is a wire compatibility
change within protocol version 1: update client and server together. Older
responses without the reference and responses with unknown or already-completed
references are rejected. As with other requests, a response timeout closes the
client session and wakes all pending callers; retry requires a new connection.

Terminal output may interrupt the current prompt. A server disconnect exits the
CLI without another keystroke. Python's blocking input cannot be portably cancelled;
the input worker is a daemon and is never joined while waiting for a line.
Direct forwarding is blocking and bounded by the socket timeout; there is no
outbound queue or slow-reader fairness policy yet. Session inactivity is based
on client requests, so incoming chat alone does not reset the server idle deadline.

### Tests

```bash
# Stage 4A/4B chat and CLI tests
python -m pytest -q tests/integration/test_private_chat.py tests/integration/test_public_chat.py
# Stage 1/2/3/4A/4B and existing non-Qt regressions
python -m pytest -q tests/integration tests/protocol tests/transfer tests/scripts
```

The integration tests use real localhost sockets and generated passwords/hash
files in temporary directories. They cover 20 simultaneous authenticated users,
duplicate races, user-list metadata, unauthorized sequences, logout, disconnect,
timeout and shutdown cleanup. Private-chat tests also cover metadata/ACK matching,
third-party isolation, offline recipients, concurrent forwarding, authentication
reply ordering and unsolicited reception during CLI input. Public-chat tests cover
authenticated audiences, no sender echo, concurrent mixed traffic, maximum UTF-8
payloads, partial/all recipient failures, snapshot/login races and in-flight shutdown.
Broadcasts use sequential blocking writes bounded by each socket's timeout; a slow
recipient can delay later recipients and the sender's ACK. Stage 1–3 tests remain in place.

## Workflow
1. Agree `docs/protocol.md` and module interfaces first.
2. Create a feature branch: `git switch -c feat/a-server` (example).
3. Implement your owned module and tests; commit small changes.
4. Push and open a pull request into `main`; request review.
5. Never commit credentials, private keys or real user data.

## Submission
Add run instructions, protocol/state/sequence diagrams, test evidence, and PDF report before submission.
