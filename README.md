# LanLink — Distributed Client-Server Collaboration Application

Course project skeleton for Python, TCP/UDP sockets, TLS and PySide6.

**Status:** Stage 3 supports concurrent TCP handshakes, local password
verification, authenticated user lists and logout. The server retains the bounded
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

## Stage 3: Local authentication and user management

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

The client logs in, prints the current user list, and remains connected. Press
Enter to refresh the list or type `logout` to terminate the session. Refresh
Alice's list after Bob logs in to see both users. EOF on terminal input logs out.
User lists are requested, not broadcast. They contain authenticated users only,
including the requester, with username, online=true, peer IP and peer TCP port.

**Passwords travel in plaintext TCP in this stage.** This is a localhost demo,
not secure deployment authentication. Configuring credentials requires a loopback
server bind; the login CLI also checks the connected peer is loopback before
prompting for or sending a password. TLS is not implemented.

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
  Repeated AUTH preserves the original session. USER_LIST/LOGOUT before AUTH
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
state, chat routing, file transfer, UDP discovery or Qt integration in Stage 3.

### Tests

```bash
# Stage 3 local authentication and CLI tests
python -m pytest -q tests/integration/test_auth.py
# Stage 1/2/3 and existing non-Qt regressions
python -m pytest -q tests/integration tests/protocol tests/transfer tests/scripts
```

The integration tests use real localhost sockets and generated passwords/hash
files in temporary directories. They cover 20 simultaneous authenticated users,
duplicate races, user-list metadata, unauthorized sequences, logout, disconnect,
timeout and shutdown cleanup. Existing Stage 1/2 tests remain in place.

## Workflow
1. Agree `docs/protocol.md` and module interfaces first.
2. Create a feature branch: `git switch -c feat/a-server` (example).
3. Implement your owned module and tests; commit small changes.
4. Push and open a pull request into `main`; request review.
5. Never commit credentials, private keys or real user data.

## Submission
Add run instructions, protocol/state/sequence diagrams, test evidence, and PDF report before submission.
