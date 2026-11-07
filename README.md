# LanLink — Distributed Client-Server Collaboration Application

Course project skeleton for Python, TCP/UDP sockets, TLS and PySide6.

**Status:** Stage 1 TCP HELLO/HELLO_ACK is implemented. The server handles one connection at a time and closes it after the handshake. Existing protocol, file-engine and file-widget components are not integrated into a complete application.

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

## Stage 1: TCP handshake

```bash
# Terminal 1
python -m server --host 127.0.0.1 --port 5000
# Terminal 2
python -m client --host 127.0.0.1 --port 5000
```

The client prints `HELLO_ACK from LanLink; handshake complete` and exits.
The server stays available for the next sequential connection; stop it with Ctrl+C.
Both commands accept `--timeout` (default 5 seconds) for socket operations.

Run the focused localhost integration tests and existing protocol regressions:

```bash
python -m pytest -q tests/integration/test_handshake.py tests/protocol
```

`common/transport.py` delegates framing and validation to `common.protocol`.
`common/handshake.py` checks message order and correlates HELLO_ACK to HELLO.
Sockets are closed on completion, EOF, malformed input, timeout or disconnect.
There is no authentication, concurrent connection processing, chat, network file
transfer, TLS or Qt integration in this stage. This plaintext handshake sends
only application identification and protocol limits.

## Workflow
1. Agree `docs/protocol.md` and module interfaces first.
2. Create a feature branch: `git switch -c feat/a-server` (example).
3. Implement your owned module and tests; commit small changes.
4. Push and open a pull request into `main`; request review.
5. Never commit credentials, private keys or real user data.

## Submission
Add run instructions, protocol/state/sequence diagrams, test evidence, and PDF report before submission.
