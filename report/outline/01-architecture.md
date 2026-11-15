# Outline: 1 System Architecture (target 1–1.5 pages)

## Heading tree
- **1.1 Overview and deployment model**
  - LanLink = one TCP server + CLI clients on one LAN; every chat and file byte is relayed by the server (no peer-to-peer). Entry points `python -m server`, `python -m client`. Refer to Figure 1 here. Sources: `server/__main__.py`, `client/__main__.py`, `server/app.py` (module docstring). ~8 lines.
  - Packages: `common/` (codec, models, transport, TLS), `server/`, `client/`, `transfer/` (file engine). ~4 lines.
- **1.2 Components and data paths** (Figure 1 sits here)
  - Server: accept loop (`serve_forever`), one worker per client (`_serve_client`), registries `_connections`/`_users`/`_transfers` under `_lock`, `lanlink-discovery` UDP thread bound to the TCP port number. Sources: `server/app.py`, `server/sessions.py`, `server/discovery.py`. ~6 lines.
  - Client: main loop, `lanlink-cli-input`, `lanlink-client-reader`, `lanlink-file-sender`; `--discover` broadcasts DISCOVER to 255.255.255.255, server unicasts SERVER_INFO. Sources: `client/__main__.py`, `client/private_chat.py`, `client/discovery.py`. ~6 lines.
- **1.3 Layer separation** (Table 1)
  - Map UI / network / data processing to modules; state the grep result and that `tests/protocol/test_layering.py` enforces it by AST. Sources: all packages. ~6 lines + table.
- **1.4 Concurrency model**
  - 1.4.1 Threads: server = 1 accept + 1 discovery + N workers (N <= `--max-clients`, default 64; excess accepts are closed); client = 3 + 1 per outgoing transfer. Why: blocking sockets, slow peer isolated (TLS handshake done in worker, not accept loop). `server/app.py:112-197`. ~6 lines.
  - 1.4.2 Locks and ordering (Table 2): `TCPServer._lock` (registries, never held during socket I/O), `TransferSession.lock` (orders one transfer's messages; always taken before `_lock`), `SocketTransport._write_lock` (one frame per `sendall`), client `PrivateChatClient._lock`, engine `RLock`s. `server/app.py:345-404`, `common/transport.py`, `transfer/sender.py:154`, `transfer/receiver.py:53`. ~8 lines.
  - 1.4.3 Scalability evidence: thread-per-client + no I/O under lock; `tests/integration/test_concurrency.py::test_twenty_simultaneously_connected_clients` and `test_concurrent_writes_preserve_frames`. ~4 lines.
- **1.5 Transport and security choices**
  - 1.5.1 TCP for chat/files, UDP only for DISCOVER/SERVER_INFO. `server/app.py:70-81`, `server/discovery.py`. ~3 lines.
  - 1.5.2 TLS via stdlib `ssl`: TLS >= 1.2, self-signed cert pinned with `--cafile`, `check_hostname=False` (why: LAN IP changes); protects confidentiality/integrity of every TCP frame; UDP discovery is NOT encrypted. `common/tls.py`. ~4 lines.
  - 1.5.3 Plaintext only on loopback, enforced on both sides. `server/app.py:56-58`, `client/connection.py:19-20`. ~2 lines.
  - 1.5.4 Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations, 16-byte salt, `hmac.compare_digest`, dummy record for unknown users; 3 failed AUTH closes the connection. `server/auth.py`, `server/app.py:269-276`. ~3 lines.

## Figures and tables
| # | Working caption | Shows | Type | Source |
|---|---|---|---|---|
| Fig 1 | LanLink runtime architecture: server threads, shared registries and three clients | Server process (accept, workers, `_lock` + 3 registries, discovery thread); clients A–C with main/input/reader/file-sender threads; edges labelled "TCP + TLS" and "UDP DISCOVER broadcast / SERVER_INFO unicast"; ~18 nodes (client C collapsed to one node) | Mermaid flowchart LR with subgraphs | `report/diagrams/architecture.mmd` |
| Table 1 | Layer separation: modules per layer | UI / network / data processing / orchestration rows, with modules and the import check result | Markdown table | in section |
| Table 2 | Locks, what they protect and acquisition order | lock name, file, guarded data, held during I/O? | Markdown table | in section |

No second diagram proposed; Table 2 replaces a lock diagram to stay within 1.5 pages.

## Rubric evidence (Spec.md §9)
- Step 3 (concurrency) — primary: 1.4. Step 8 (security layer) — 1.5.2–1.5.4.
- Step 4 (track users, IP, port) — `UserSession(username, connection_id, ip, port)` in 1.2, partial.
- Step 2 (handshake), step 7 (resilience: per-worker try/except, shutdown) — touched only; detail belongs to 3 and 4.
- Also §7 design requirements: layer separation (1.3), scalability 10–20 clients (1.4.3).

## Open questions and gaps
1. **Layer claim is partly false.** `transfer/sender.py:12` and `transfer/receiver.py:11` import `threading` (RLock, Event; they start no threads). No data-processing module imports `socket` or `ssl`. `tests/protocol/test_layering.py` forbids `socket`, `ssl`, `client`, `server` but not `threading`. Proposal: state this exactly; docs label `transfer/` "no sockets", which is true.
2. `common/handshake.py` imports `SocketTransport`, so it is network-layer, not pure codec; `client/private_chat.py` mixes network I/O with transfer orchestration. Classify as "network/orchestration"?
3. Default mismatch: `TCPServer(idle_timeout=45.0)` vs CLI `--idle-timeout` default 120.0 (`server/__main__.py:18`). Report the CLI value; flag for section 2.
4. Spec step 8 asks to "encrypt the data payloads ... before sending"; LanLink encrypts the whole TCP stream with TLS (Spec §6 allows TLS). TLS is optional (off on loopback), and UDP discovery is plaintext. Say so plainly.
5. Spec §5 says "peer-to-peer"; LanLink is strictly server-relayed. State the deviation.
6. Spec §4 "broadcast their presence": discovery finds the *server*, not peers; presence is via USER_LIST polling (`/users`). No UDP heartbeat exists. Confirm with section 2.
7. Boundaries: frame format, message fields, limits, state machines → section 2 (1.x names message types only). Login/chat/file flows → section 3 (1.x shows no message order). Lock-ordering/deadlock and partial-write shutdown stories → section 4 (1.x states the rule, 4 tells the bug). Who owns "max-clients rejection by close() without ERROR"? Suggest 1.4.1.
8. Is Figure 1 to be redrawn in Lucid (`report/diagrams/lucid/`)? Keep node names identical to code thread names to ease that.
