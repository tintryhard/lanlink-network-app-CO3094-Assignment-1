# tests/

Run everything with `python -m pytest -q tests`. It takes about 70 s. The integration tests start real servers on `127.0.0.1` with port 0, so the OS picks a free port.

| Folder | What it covers |
|---|---|
| `protocol/` | The codec: golden frames, hostile or fragmented input, field rules, UDP datagrams. `test_layering.py` checks that `common/` and `transfer/` never import socket or ssl code |
| `transfer/` | The file engine on disk: state graph, chunking, hashing, `.part` cleanup, cancellation, odd file names |
| `integration/` | Real sockets, end to end. One file per stage: handshake, concurrency (20 clients), auth, private and public chat, file negotiation and transfer, TLS, discovery, CLI output |
| `fakes/` | `FakeRelay`, an in-memory relay used by the transfer tests (test code only) |
| `fixtures/` | `protocol_cases.json`: example frames with their exact bytes |

## Worth knowing

- `tests/integration/test_concurrency.py::running(...)` starts a `TCPServer` in a background thread and shuts it down after the `with` block. Most integration tests use it.
- The TLS tests create a short-lived certificate with `openssl`. They are skipped if `openssl` is not installed.
- The CLI tests replace `client.__main__._read_command` and `getpass.getpass` to type commands and passwords automatically.
- There is one known failure: `test_public_chat.py::test_cli_sends_and_receives_public_while_preserving_private_chat` expects the text "best-effort broadcast", which the CLI never prints.
