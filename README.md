# LanLink — Distributed Client-Server Collaboration Application

Course project skeleton for Python, TCP/UDP sockets, TLS and PySide6.

**Status:** Stage 2 supports concurrent TCP HELLO/HELLO_ACK connections with
bounded thread-per-client workers. Existing protocol, file-engine and file-widget
components are not integrated into a complete application.

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

## Stage 2: Concurrent TCP handshake

```bash
# Terminal 1
python -m server --host 127.0.0.1 --port 5000 --max-clients 64 --timeout 5 --idle-timeout 45
# Terminal 2
python -m client --host 127.0.0.1 --port 5000
```

The client prints `HELLO_ACK from LanLink; handshake complete` and exits.
The server accepts clients independently and retains successful connections until
EOF, unsupported input, or idle timeout. The CLI client exits after HELLO_ACK, so
its connection is then removed. Stop the server with Ctrl+C to interrupt socket
I/O and join workers.

The default cap is 64 accepted clients; excess connections are closed immediately.
Each client owns a socket, decoder, and non-daemon worker. A lock protects the
connection ID/socket/worker/state registry; a per-transport lock serializes writes.
`--timeout` sets the server's absolute HELLO read deadline (5 seconds), including
partial reads. The ACK write also has a finite socket timeout. `--idle-timeout`
sets the post-handshake socket inactivity timeout (45 seconds). The client uses
`--timeout` as its socket-operation timeout.

Run the focused localhost integration tests and existing protocol regressions:

```bash
python -m pytest -q tests/integration tests/protocol
```

`common/transport.py` delegates framing and validation to `common.protocol`.
`common/handshake.py` checks message order and correlates HELLO_ACK to HELLO.
Sockets are closed on EOF, malformed input, timeout, disconnect or server shutdown.
There is no authentication, chat, network file transfer, TLS, UDP discovery or
Qt integration in this stage. Messages after HELLO are rejected by closing the
connection. This plaintext handshake sends
only application identification and protocol limits.

## Workflow
1. Agree `docs/protocol.md` and module interfaces first.
2. Create a feature branch: `git switch -c feat/a-server` (example).
3. Implement your owned module and tests; commit small changes.
4. Push and open a pull request into `main`; request review.
5. Never commit credentials, private keys or real user data.

## Submission
Add run instructions, protocol/state/sequence diagrams, test evidence, and PDF report before submission.
