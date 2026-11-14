# LanLink

A client-server chat and file-transfer application for a local network (LAN),
built on raw Python sockets (`socket`, `ssl`, `threading`). Course project CO3094.
It has no HTTP framework, no WebSockets and no third-party runtime packages.

## Features

| Feature | How |
|---|---|
| Custom protocol | Length-prefixed JSON frames over TCP ([docs/architecture.md](docs/architecture.md)) |
| Handshake | `HELLO` → `HELLO_ACK` |
| Many clients at once | One server thread per client (default limit 64) |
| Login and user list | Salted PBKDF2 passwords; the server tracks each user's IP and port |
| Chat | Private `/msg USER TEXT` and public `/all TEXT`, each confirmed by `CHAT_ACK` |
| File transfer | Offer → accept → 64 KiB chunks → SHA-256 check; any file type, up to 100 MiB |
| Resilience | Timeouts, disconnects and broken pipes end only that client's session and transfers |
| Security | TLS 1.2+ (stdlib `ssl`); without TLS the app only runs on localhost |
| CLI | User list, incoming messages and a text progress bar for transfers |
| Discovery | UDP broadcast `DISCOVER` → `SERVER_INFO`, so clients can find the server |

## Architecture

```
 Client (python -m client)                          Server (python -m server)
┌──────────────────────────────┐                  ┌──────────────────────────────────┐
│ main thread: CLI loop        │   TCP (+TLS)     │ accept thread                     │
│ input thread: reads stdin    │◄────────────────►│  └─ one worker thread per client  │
│ reader thread: socket → chat,│  frames          │     HELLO → AUTH → message loop   │
│   replies, file messages     │                  │     routes chat, relays files     │
│ sender thread: file chunks   │   UDP broadcast  │ discovery thread: DISCOVER →      │
│                              │─────────────────►│   SERVER_INFO (same port number)  │
└──────────────────────────────┘                  └──────────────────────────────────┘
```

Every message on the wire is a frame:

```
| 4-byte header length (big-endian) | JSON header | payload (payload_length bytes) |
```

The layers are kept apart:
- **Network:** `common/transport.py`, `server/app.py` and `client/private_chat.py`.
- **Data processing:** `common/protocol.py`, `common/builders.py` and `transfer/`, which have no socket code.
- **User interface:** `client/__main__.py` and `client/display.py`.

The full protocol, the state diagrams and the message flows are in [docs/architecture.md](docs/architecture.md).

## Project layout

| Folder | Contents |
|---|---|
| [`common/`](common/README.md) | Code shared by client and server: frame codec, message builders, socket transport, TLS |
| [`server/`](server/README.md) | Threaded server, password store, UDP discovery responder |
| [`client/`](client/README.md) | CLI, client session (reader thread), discovery, text formatting |
| [`transfer/`](transfer/README.md) | File engine: chunking, SHA-256, `.part` files, progress |
| [`tests/`](tests/README.md) | Unit and real-socket integration tests |
| [`scripts/`](scripts/README.md) | Dev certificate script |
| `docs/` | Architecture, protocol and state diagrams |

## Setup

You need Python 3.10 or newer. For TLS you also need the `openssl` command.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt   # only pytest; the app itself needs nothing
```

## Run on one machine (localhost)

Create accounts (the password is typed in hidden):

```bash
python -m server.auth --file data/users.json --username alice
python -m server.auth --file data/users.json --username bob
```

Start the server and two clients in separate terminals:

```bash
python -m server --host 127.0.0.1 --port 5000 --users data/users.json
python -m client --host 127.0.0.1 --port 5000 --username alice
python -m client --host 127.0.0.1 --port 5000 --username bob
```

## Run on a LAN (TLS)

Plaintext is refused for any address other than localhost, so a LAN needs TLS.

1. On the server machine, create a self-signed certificate and start the server:
   ```bash
   sh scripts/make_dev_cert.sh            # writes certs/server.crt and certs/server.key
   python -m server --host 0.0.0.0 --port 5000 --users data/users.json \
       --cert certs/server.crt --key certs/server.key
   ```
2. Copy **only** `certs/server.crt` to each client machine. Never copy the key.
3. On each client machine:
   ```bash
   python -m client --discover --port 5000   # prints e.g. "LanLink at 192.168.1.9:5000 (TLS)"
   python -m client --host 192.168.1.9 --port 5000 --username alice --cafile server.crt
   ```

If the network blocks broadcast, `--discover` finds nothing. Use the server's IP directly.

## Client commands

| Command | Action |
|---|---|
| `/msg USER TEXT` | Private message |
| `/all TEXT` | Message to every other online user |
| `/offer USER PATH` | Offer a file; it is sent once the user accepts |
| `/accept ID` / `/reject ID [REASON]` | Answer an incoming offer; received files go to `downloads/` |
| `/users` or Enter | Show the online users |
| `/logout` or Ctrl+D | Leave |

## Server options

| Option | Default | Meaning |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `5000` | TCP address; UDP discovery uses the same port number |
| `--users` | none | Password file; without it every login is refused |
| `--cert`, `--key` | none | Turn on TLS (both are required together) |
| `--max-clients` | 64 | Further connections are closed at once |
| `--timeout` | 5 s | Time limit for the TLS + HELLO handshake |
| `--idle-timeout` | 120 s | A silent client is disconnected, unless it has a running transfer |
| `--transfer-timeout` | 60 s | Time limit for each phase of a file transfer |

## Tests

```bash
python -m pytest -q tests
```

The tests start real servers on localhost. The TLS tests need `openssl` on PATH
(Git for Windows ships one) and are skipped without it.

## Team

- **A:** `server/`, `common/transport.py`, `common/tls.py`
- **B:** `client/`
- **C:** `common/models.py`, `common/errors.py`, `common/protocol.py`, `common/builders.py`, `transfer/`
