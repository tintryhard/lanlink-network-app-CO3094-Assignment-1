## E Evidence

Table 13 links each challenge in Section 4 to the commits that introduced or fixed the behaviour and to the tests (under `tests/`) that check it.

Table 13. Evidence for Section 4.

| Challenge | Commit(s) | Test(s) |
|---|---|---|
| 4.1 Frame boundaries on a TCP stream | `aa72c1b` frame decoder; `81ba8d6` per-socket write lock | `test_all_splits_and_coalescing`; `test_concurrent_writes_preserve_frames` |
| 4.2 Avoiding deadlock | `00c5897` no I/O or hashing under the registry lock; `4c0df38` per-transfer lock taken before the registry lock; `81c0068` client cleanup outside its shared lock | `test_cleanup_releases_shared_lock_during_socket_operations` |
| 4.3 Detecting closed connections | `81c0068` client reader closes its socket; `0bf8895` peer marked `CLOSING`, 0.5 s worker wakeup on Windows | `test_forward_write_failure_returns_connection_lost`; `test_shutdown_terminates_client_readers_and_cleans_sessions` |
| 4.4 Timeouts vs. blocking public-chat fan-out | `2e18eb0` issue recorded; `a9aae74` 1 s bound on the `AUTH_OK` wait | `test_chat_ack_arrives_before_client_timeout`; `test_public_waits_for_auth_ok` |
