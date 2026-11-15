# Outline: 4 Challenges & Solutions (one A4 page)

## 1. Layout and budget
A4, 2 cm margins, 11 pt serif: one page holds about 550 words with headings, so the target is about 500 words of prose (placeholders not counted).
- `## 4 Challenges & Solutions` plus a 1–2 sentence lead-in (~30 words: "four problems, taken from the commit history and tests").
- `### 4.1` to `### 4.4`: **4 challenges at about 95–105 words each** (~400 words).
- `### 4.5 Lessons learned and remaining limitations`: 2–3 sentences (~70 words).
- No figures. A table is optional (see §4 below). If the page overflows, shorten 4.4 before removing a challenge.

## 2. Candidates (evidence: S = strong, W = weak)
| # | Problem (one line) | Evidence | Solution in code | Strength |
|---|---|---|---|---|
| C1 | TCP is a byte stream: one `recv()` can hold part of a frame or several frames. | `aa72c1b`; `common/protocol.py:412-471` (`FrameDecoder`), `common/transport.py:46`; tests `test_all_splits_and_coalescing`, `test_fragmented_hello_and_server_closes_after_ack`, `test_fragmented_and_coalesced_auth_requests`, `test_transport_retains_coalesced_messages` | 4-byte length prefix + header `payload_length`; the decoder keeps unfinished bytes and is poisoned after a bad frame (`protocol.py:454-458`), so it never tries to resync. | S in code/tests; W as a story (designed in from the start, no bug commit) |
| C2 | Several threads write to one socket (reader, file sender, server routing threads), so frames could interleave. | `81ba8d6` ("20 clients tested"); `common/transport.py:27,33` `_write_lock`; tests `test_concurrent_writes_preserve_frames` (4 threads, 400 frames, `SO_SNDBUF`=1024), `test_concurrent_public_private_and_user_list_writes_remain_intact` | A per-socket write lock around `sendall`. A failed or partial write shuts down that recipient's socket only (`server/app.py:305-325`, `507-514`). | S in code/tests; W as a story |
| C3 | Deadlock risk between the registry lock and per-transfer locks, and blocking I/O while holding a lock. | `4c0df38`; `server/app.py:36-39` docstring, `:352` "Never acquire a session lock while holding the registry lock"; `docs/architecture.md` §6 rules 1–2; test `test_cleanup_releases_shared_lock_during_socket_operations` (`81c0068`) | Fixed order: `TransferSession.lock` before `_lock`. Take a snapshot under `_lock`, then do socket I/O outside it. | S in code; W as a story |
| C4 | The client reader thread did not exit when the server shut the socket down. | `81c0068` message + diff (`client/private_chat.py` `_fail`, ~l.74-110); tests `test_shutdown_terminates_client_readers_and_cleans_sessions`, `test_disconnect_closes_client_socket_before_notification`, `test_disconnect_wakes_pending_ack_waiter` | The first `_fail()` call does `shutdown(SHUT_RDWR)` + `close()` exactly once, wakes all pending requests, then notifies. The CLI reads stdin with raw `os.read`, so interpreter exit is not blocked. | **S** (bug named in commit) |
| C5 | Public chat ACK is sent only after looping over all recipients, so the sender can hit its 5 s timeout. | `2e18eb0` message ("blocking `_route_public()` ... client is vulnerable to timeout"); fix `a9aae74`; `server/app.py:30-32` `AUTH_READY_WAIT_S = 1.0` vs `client/private_chat.py:39` `timeout=5.0`; test `test_chat_ack_arrives_before_client_timeout` | The wait for each recipient's `AUTH_OK` dropped from `idle_timeout` to 1 s. The loop itself is **still sequential** (`app.py:486-517`). | **S** (issue + fix both in commits) |
| C6 | A file receiver sends nothing until `FILE_RESULT`, so the idle timeout dropped it mid-transfer. | `a9aae74` message + diff `server/app.py:293-303`; test `test_quiet_receiver_is_not_dropped_by_idle_timeout`, which **FAILS** in the 2026-11-14 run (see §5) | A live transfer counts as activity and resets `idle_deadline`; stalled transfers still expire. | S commit; test currently red |
| C7 | Partial or corrupted files after a disconnect. | `aa72c1b`; `transfer/receiver.py:1,240-247` (SHA-256, `os.fsync`, `os.replace` of `.part`); tests `test_sender_disconnect_mid_transfer_removes_partial_file` (`3b342f6`), `test_extra_bytes_and_short_end` | Write to `.part`, check byte count + hash, fsync, then atomic rename; delete `.part` on failure. | S code; W story; overlaps §3 |
| C8 | TLS with a self-signed certificate on a LAN where the server IP changes. | `9d3d086`; `common/tls.py:17-24` (pinned `--cafile`, `check_hostname=False`); tests `test_client_rejects_an_untrusted_certificate`, `test_lan_bind_and_connect_require_tls` | Pin the exact certificate and skip the hostname check; plaintext only on loopback. | S code; W as a "challenge" |

## 3. Recommended selection (in this order)
1. **C1 + C2 merged, "Keeping frames intact on a TCP stream"**: this is the spec's own "packet fragmentation" example, and reads and writes are two sides of the same issue.
2. **C3, "Avoiding deadlock"**: the spec's other example. Keep it short and refer to Table 2 in §1.4.2.
3. **C4, "Detecting a closed connection"**: the strongest real-bug evidence (`81c0068`).
4. **C5 + C6 merged, "Timeouts vs blocking fan-out"**: a real issue and fix across two commits (`2e18eb0` → `a9aae74`). Mention the red test honestly.
C7, C8 and discovery go in 4.5 only if there is space (C7 is already in §3).

## 4. Format per challenge
Bold run-in title, then **Problem** (symptom) · **Cause** (network reason) · **Solution** (`file.py` + commit) · **Result** (test name and its current status), in 3–5 sentences. Add one `[STUDENT: how you discovered this ...]` per challenge, inline after the Problem sentence. No summary table: it would cost about 80 words, duplicate the prose, and push 4.5 off the page.

## 5. Open questions
- **Real or designed-in?** C1, C2, C3, C7 and C8 have no bug commit. The team must say whether they actually saw interleaved or partial frames, or a deadlock (in Wireshark or the terminal), or designed against them up front. The wording will follow their answer.
- **Did `2e18eb0`'s timeout really happen,** or was it found by reading the code?
- **Failing tests (local run 2026-11-14, Windows: 4 failed / 544 passed / 2 skipped):** `test_quiet_receiver_is_not_dropped_by_idle_timeout` (alice's session is cancelled before the offer, `CANCELLED_LOCAL` at `transfer/sender.py:73`, probably the 0.5 s idle timeout firing during setup; not diagnosed), `test_forward_write_failure_returns_connection_lost` (bob not removed within 3 s after the injected write failure), `test_cli_termination_and_cleanup[request-error]` and `test_cli_sends_and_receives_public_while_preserving_private_chat`. Do they also fail on the team's machines or on Linux? C6 cannot be called "verified" until this is answered.
- **Limitations to state in 4.5 (checked in code):** the public broadcast is still a sequential loop before `CHAT_ACK` (`app.py:497-517`). `PING`/`PONG` are defined but never sent, and `heartbeat_interval_s` is only advertised (`common/protocol.py:50`). Discovery sends one `DISCOVER` to `255.255.255.255` with no retry, so it returns `[]` where broadcast is blocked or the datagram is lost (`client/discovery.py:11-37`, `test_no_server_returns_empty_list`).
- **Overlaps:** locks → §1.4.2 Table 2 (refer, don't repeat). Frame format → §2 (describe only how the decoder behaves). `auth_ready.wait(1.0)` and `.part`/fsync appear in the §3 sequences, which is why C7 is dropped.
