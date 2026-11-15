# 4 Challenges & Solutions

Table 13 in Appendix E lists the commits and tests behind each challenge.

## 4.1 Frame boundaries on a TCP stream

<!-- TODO: team to confirm whether this was observed (e.g. in Wireshark/terminal) or designed against from the start -->

**Problem.** One read from a TCP socket can return half a frame or several frames, and several threads may write to one socket at once. **Cause.** TCP delivers a byte stream with no message boundaries, and two unsynchronised writes can interleave their bytes. **Solution.** Every frame starts with a 4-byte header length, and the header gives the payload length (Section 2.2). The decoder keeps unfinished bytes between reads and rejects the rest of the stream after a corrupt frame, and each socket has one write lock, so a frame is always written whole. **Verified.** `test_all_splits_and_coalescing` cuts a three-frame stream at every byte offset and checks that the same frames come out.

## 4.2 Avoiding deadlock

<!-- TODO: team to confirm whether this was observed (e.g. in Wireshark/terminal) or designed against from the start -->

**Problem.** All server threads share one registry lock, and each file transfer has its own lock. **Cause.** Two threads that take two locks in opposite orders can wait for each other forever, and a send to a slow peer blocks, so a send under a shared lock stalls every client. **Solution.** We use one fixed order, the transfer lock before the registry lock (Table 12 in Appendix D), and no thread reads or writes a socket while it holds the registry lock. **Verified.** `test_cleanup_releases_shared_lock_during_socket_operations` checks the same rule on the client: its shared lock stays free during socket shutdown.

## 4.3 Detecting closed connections

**Problem.** An early client reader thread kept running after the server had shut down its socket, and on Windows the test suite did not pass until the server could drop a peer whose write had failed. **Cause.** Only a thread that reads or writes a socket learns that the connection is gone; the old client error path never closed its own socket, and on Windows shutting a socket down from another thread does not wake a thread blocked reading it. **Solution.** The client now shuts down and closes its socket once, on the first failure; the server marks a peer `CLOSING` after a failed write, and every worker checks for that state every 0.5 s. **Verified.** `test_forward_write_failure_returns_connection_lost` injects a write failure and checks that the sender gets `CONNECTION_LOST` and the peer leaves the user list within 3 s.

## 4.4 Timeouts vs. blocking public-chat fan-out

<!-- TODO: team to confirm whether this was observed (e.g. in Wireshark/terminal) or designed against from the start -->

**Problem.** The server delivers a public message to each recipient one by one before it acknowledges the sender, so the sender could reach its 5 s request timeout first. **Cause.** Before forwarding, the server waits until the recipient's own `AUTH_OK` has been sent, and this wait was bounded only by the idle timeout (45 s in the code, 120 s from the command line). **Solution.** The wait is now at most 1 s per recipient on every route. **Verified.** `test_chat_ack_arrives_before_client_timeout` makes the server treat a recipient's `AUTH_OK` as still pending and checks that the sender's private message gets `ERROR CONNECTION_LOST` before the test client's 3 s request timeout; no test times the public route.
