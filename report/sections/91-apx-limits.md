## B Limits and timeouts

Table 10 collects every size limit and timeout. The size values are the protocol defaults (`DEFAULT_LIMITS` in `common/protocol.py`). A decoded `limits` object must contain exactly the eight keys below. Each value must stay within a ceiling (for example 1 MiB for the header and chunk limits, 65 507 B for datagrams), and the idle timeout must be greater than the heartbeat interval.

```
max_header_bytes      max_chunk_bytes       max_file_bytes     max_chat_bytes
max_user_list_bytes   max_datagram_bytes    heartbeat_interval_s   idle_timeout_s
```

Table 10. Size limits and timeouts.

| Item | Value | Source | Effect |
|---|---|---|---|
| Header length N | 1–16 384 B | `max_header_bytes` | `BAD_FRAME` / `HEADER_TOO_LARGE` |
| `AUTH` payload | 1–256 B | `max_payload_for` | `PAYLOAD_TOO_LARGE` / `BAD_PAYLOAD` |
| Chat payload | 1–8 192 B | `max_chat_bytes` | same |
| `FILE_CHUNK` payload, `chunk_size` | 1–65 536 B | `max_chunk_bytes` | same; `INVALID_FIELD` for `chunk_size` |
| `USER_LIST` payload | request 0 B; reply 1–65 536 B | `max_user_list_bytes` | same |
| Payload of every other type | 0 B | `max_payload_for` | `PAYLOAD_TOO_LARGE` |
| File size | 0–104 857 600 B (100 MiB) | `max_file_bytes` | `FILE_TOO_LARGE` |
| UDP datagram (whole frame) | ≤ 1 024 B | `max_datagram_bytes` | `PAYLOAD_TOO_LARGE`; datagram ignored |
| String fields (characters) | `username`, `sender`, `recipient` 32 (no whitespace); `client_name` 64; `client_version` 32; `server_name` 64; `host` 255; `message` 256; `field` 64; `status` 16; `reason` 128; `filename` 255 (and ≤255 UTF-8 bytes); `ip` 45 | `_validate_header`, `_validate_users` | `INVALID_FIELD` / `UNSAFE_FILENAME` / `BAD_PAYLOAD` |
| Integers | 0 to 2^63-1; `port` 1–65535 | `MAX_INT`, `_integer` | `INVALID_FIELD` |
| Transfer IDs per connection | 64 | `_negotiate_file_transfer` | `QUEUE_FULL` |
| Failed logins per connection | 3 | `_dispatch_client_messages` | connection closed |
| Concurrent connections | 64 | `max_clients`, `--max-clients` | extra connections closed at once |
| Handshake | 5.0 s for the TLS handshake, then a fresh 5.0 s for `HELLO` | `handshake_timeout`, `--timeout` | connection closed, no `ERROR` |
| Idle connection | 45.0 s (`TCPServer` default); 120.0 s (`--idle-timeout`) | `idle_timeout` | connection closed; extended while the connection has a live transfer |
| Transfer phase | 60.0 s | `transfer_timeout`, `--transfer-timeout` | `ERROR TIMEOUT` to both parties; reset by every accepted or relayed transfer message |
| Worker wake-up | 0.5 s | `WORKER_WAKEUP_S` | longest single wait in `recv()`; worker re-checks transfer deadlines and `CLOSING` |
| Wait for recipient's `AUTH_OK` | 1.0 s | `AUTH_READY_WAIT_S` | `ERROR CONNECTION_LOST` |
| Heartbeat interval | 15 s | `heartbeat_interval_s` | advertised only; nothing uses it |
| Client request | 5.0 s | `--timeout` (`client/__main__.py`) | waits for the matching reply; on expiry the client closes the session |
| Discovery wait | 2.0 s | `discover_servers(wait=2.0)` | collects `SERVER_INFO` replies until then |
| Transfer engine timeouts | sender 60 s (`OFFERED`) / 30 s (later); receiver 30 s | `accept_timeout_s`, `result_timeout_s`, `idle_timeout_s` | defined in `check_timeout()`, which the CLI never calls |

The advertised idle timeout differs from the real one: `HELLO_ACK` always advertises 45 s (the built-in server default), while the command-line server uses 120 s (`server/__main__.py`). The client checks that `limits` is valid (an invalid object fails the handshake) but never applies the values, so the wrong number has no effect at run time (`common/protocol.py:320-321`).
