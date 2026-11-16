# 4 Challenges & Solutions

LanLink required careful handling of TCP framing, concurrent access, failed
connections and forwarding delays. These challenges led to the following
design decisions and failure-handling improvements.

## 4.1 Preserving message boundaries and write integrity

TCP delivers a byte stream: one read may contain part of a frame or several
frames. Concurrent writers also need coordination to avoid mixing frame
bytes. LanLink uses the length-prefixed format in Section 2.2, retains
incomplete data between reads and stops decoding after a corrupt frame.
A per-connection write lock protects each complete send. Tests check decoding
at every split point of a three-frame stream and verify that 400 frames
written by four threads arrive intact.

## 4.2 Coordinating shared state without deadlock

Server workers share registries and per-transfer locks. Inconsistent lock
ordering could cause deadlock; socket I/O under the registry lock could
delay unrelated clients. When locks are nested, the transfer lock comes
before the registry lock, as described in Section 1.3.2. Workers release
the registry lock before socket reads or writes. A client cleanup test
checks the related rule that its shared lock stays available during
socket shutdown; it does not establish that all server deadlocks are impossible.

## 4.3 Cleaning up failed connections

An earlier client failure path could leave its reader thread running after
disconnection. Windows testing also exposed delayed removal of a peer after
a forwarding failure. The client now closes its socket on the first failure
and wakes pending requests. The server marks a failed recipient `CLOSING`
and shuts down its socket. Workers check state using read-polling intervals
of at most 0.5 seconds, without guaranteeing cleanup within that time.
The forwarding-failure test checks `CONNECTION_LOST` and peer removal within three seconds.

## 4.4 Reducing delays before chat acknowledgment

Public chat forwards to recipients sequentially before acknowledging the
sender. Waiting for a recipient's authentication response could therefore
contribute to a sender timeout. The authentication-readiness wait is now
limited to one second per recipient instead of the longer idle timeout.
This reduces one source of delay, but waits and blocking writes can still
accumulate. The existing timeout test covers the private-message route,
not public-broadcast timing. The change is therefore a partial improvement;
slow-recipient isolation remains future work.
