# transfer/

The file engine. It reads, writes and verifies files, but has **no socket code**: it takes protocol messages in and returns messages to send. `client/private_chat.py` connects it to the network.

| File | What it does |
|---|---|
| `sender.py` | `inspect_file` hashes the file before the offer. `iter_chunks` reads it lazily, one chunk at a time. `SendSession` is the sender's state machine: offer, then chunks, then `FILE_END`, then waiting for `FILE_RESULT` |
| `receiver.py` | `IncomingOffer` holds a checked `FILE_OFFER`. `Receiver` writes chunks to a `.part` file, verifies the file and renames it |
| `paths.py` | Safe file names (`sanitize_filename`), reserving a unique destination name (`reserve_destination`), mapping OS errors to error codes |
| `progress.py` | `UpdatePublisher` delivers snapshots after the engine lock is released. `ProgressThrottle` limits progress updates to about one every 0.1 s |

## Transfer states

```
OFFERED ──accept──► ACCEPTED ──first chunk──► TRANSFERRING ──FILE_END──► VERIFYING ──ok──► COMPLETED
   │                   │                          │                         │
   └─reject─► REJECTED └──────── cancel / error / disconnect ──────────────┴──► CANCELLED / FAILED
```

Any state that is not final can also go to CANCELLED or FAILED. `check_transition` (in
`common/models.py`) refuses every other move.

## Worth knowing

- **Integrity:** the sender sends the SHA-256 in the offer. The receiver hashes every chunk as it arrives, then checks the chunk count, the byte total and the hash at `FILE_END`. If any check fails, the result is `FILE_RESULT ok=false` and the partial file is deleted.
- **No half files:**
  - data goes to `name.<id>.part`;
  - `finish()` calls `fsync`, then `os.replace` (an atomic rename) to the final name;
  - an existing file is never overwritten: a free name such as `a (1).txt` is reserved with `O_EXCL`.
- **Safe names:** the receiver rejects paths (`/`, `\`, `..`, `C:`), control characters and Windows device names, so a peer cannot write outside `downloads/`.
- **Source changes:** if the file being sent changes while it is read (different size, modification time or hash), the transfer fails with `SOURCE_CHANGED`.
- **Threads:**
  - each engine has an `RLock`;
  - callbacks (`on_update`) run only after the lock is released (`UpdatePublisher`), so UI code can never deadlock the engine;
  - an exception raised inside a callback is counted and ignored.
- **Timeouts:** the engines have a `check_timeout()` method, but the CLI does not call it. Stalled transfers are expired by the server's `--transfer-timeout` instead.
