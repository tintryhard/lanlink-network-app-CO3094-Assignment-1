# Appendix

This appendix collects reference tables that the main text cites but does not need in full.

## A Error codes

Table 9 lists all 35 members of `ErrorCode` (`common/errors.py`), where each can appear (`ERROR.code`, `FILE_RESULT.error_code`, `FILE_CANCEL.reason`, a closed connection, or a local outcome only) and when it is produced.

Table 9. Error codes and where each appears on the wire.

| Code | Where it appears | When |
|---|---|---|
| `BAD_FRAME` | connection closed; datagram ignored (UDP) | Header length 0; a UDP datagram with bytes after its frame |
| `HEADER_TOO_LARGE` | connection closed | Header length above 16 384 B |
| `PAYLOAD_TOO_LARGE` | connection closed; datagram ignored (UDP) | `payload_length` above the ceiling for the type; datagram above 1 024 B |
| `PAYLOAD_LENGTH_MISMATCH` | local only | A `Message` whose payload size differs from `payload_length` (only at build or encode time, because the decoder always reads exactly `payload_length` bytes) |
| `UNEXPECTED_EOF` | connection closed; datagram ignored (UDP) | Stream ends inside a frame; datagram shorter than its prefix says |
| `BAD_JSON` | connection closed; local | Invalid UTF-8 or JSON, header not an object, duplicate key, `NaN`/`Infinity`, or nesting too deep for the JSON parser. Local: a header nested deeper than 32 at build or encode time (a received header nested that deep fails the field checks first, with `INVALID_FIELD`) |
| `UNSUPPORTED_VERSION` | connection closed | `version` is not 1 |
| `UNKNOWN_TYPE` | connection closed | `type` not in `MessageType` |
| `MISSING_FIELD` | connection closed | A required field is absent |
| `INVALID_FIELD` | connection closed; `ERROR` | Decoder: bad value, unknown field or wrong direction (closed). `ERROR`: first message is not `HELLO`; `HELLO`, `PING` or `PONG` after login (then closed; before login these get `NOT_AUTHENTICATED`, then closed); `FILE_OFFER` to oneself |
| `SPOOFED_FIELD` | connection closed | A client header contains `sender`, `message_id` or `timestamp_ms` |
| `BAD_PAYLOAD` | connection closed | Empty `AUTH`, chat or `FILE_CHUNK` payload; text that is not UTF-8; malformed `USER_LIST` JSON |
| `NOT_AUTHENTICATED` | `ERROR` | Any message other than `AUTH` before login |
| `AUTH_FAILED` | `ERROR` | Wrong username or password, or the server was started without `--users`. The third failure also closes the connection |
| `ALREADY_LOGGED_IN` | `ERROR` | `AUTH` on a connection that is already logged in, or for a username that has a session |
| `RECIPIENT_OFFLINE` | `ERROR` | `CHAT_PRIVATE` or `FILE_OFFER` to a username with no active session, whether or not the account exists |
| `RECIPIENT_UNKNOWN` | never raised | Defined, but no code path uses it |
| `QUEUE_FULL` | `ERROR`; local | `ERROR`: the connection has already used 64 transfer IDs. Local: the client already has 64 requests waiting for replies; or the client's `transfer_events` queue is full, which closes the client's connection (`_push_transfer_event`). That queue defaults to 128 entries, but the CLI passes its own unbounded queue, so this case cannot occur in the CLI |
| `TIMEOUT` | `ERROR` | A transfer phase exceeded `--transfer-timeout`; sent with `transfer_id` to both parties |
| `CONNECTION_LOST` | `ERROR`; local | `ERROR` reply to `CHAT_PRIVATE` when the write to the recipient failed, and to `CHAT_PRIVATE`/`FILE_OFFER` when the recipient's `AUTH_OK` was not sent within 1 s. `ERROR` notice with `transfer_id` and no `ref_request_id`, sent to both parties, when the other party of a transfer disconnected or a transfer message (including the `FILE_OFFER` itself) could not be forwarded. The exception is a `FILE_RESULT` or `FILE_CANCEL` that ends the transfer: if it cannot be forwarded, it is dropped without a notice. If a `FILE_REJECT` could not be forwarded, the notice goes to the recipient only. Local: the client lost the server |
| `FILE_TOO_LARGE` | connection closed; local | Decoder: a size field above 100 MiB. Local: `inspect_file` refuses to offer the file |
| `FILE_NOT_FOUND` | local | `inspect_file`: the path is missing or is not a regular file |
| `SOURCE_CHANGED` | `FILE_CANCEL.reason`; local | Local: the file changed while `inspect_file` hashed it, before any offer. `FILE_CANCEL.reason`: it changed while the chunks were being sent |
| `UNSAFE_FILENAME` | connection closed; local | Decoder: bad `filename`. Local: `sanitize_filename` |
| `TRANSFER_UNKNOWN` | `ERROR` | `transfer_id` not registered, already finished, or expired on accept or reject |
| `TRANSFER_STATE` | `ERROR`; `FILE_RESULT.error_code` | `ERROR`: duplicate `transfer_id`, wrong party, or a message not allowed in the current status. Result: the receiver got an unexpected message |
| `SEQ_MISMATCH` | `FILE_RESULT.error_code` | Chunk `seq` is not the next expected number |
| `SIZE_MISMATCH` | `FILE_RESULT.error_code`; local | A chunk or total size is wrong. The sender also fails locally if `received_bytes` differs from the file size |
| `HASH_MISMATCH` | `FILE_RESULT.error_code`; local | The SHA-256 of the received bytes differs from the offer, or the result hash differs from the sender's |
| `DISK_FULL` | `FILE_RESULT.error_code` | No space while writing. At accept time, the client sends `FILE_REJECT` (no reason) instead |
| `PERMISSION_DENIED` | `FILE_RESULT.error_code` | No permission while writing or renaming. At accept time, the client sends `FILE_REJECT` instead |
| `REJECTED_BY_PEER` | local | The sender's outcome after `FILE_REJECT` |
| `CANCELLED_BY_PEER` | local | The outcome after receiving `FILE_CANCEL` |
| `CANCELLED_LOCAL` | local; `FILE_CANCEL.reason` | `SendSession.cancel()`/`Receiver.cancel()`. The CLI never calls them |
| `INTERNAL` | `FILE_RESULT.error_code`; local | Any other OS error; fallback text for an unknown code |

`RECIPIENT_UNKNOWN` is never raised: the server looks only for an active session, so a message to an unknown username gets `RECIPIENT_OFFLINE`. `FILE_CANCEL.reason` carries whichever error stopped the sender while it was reading the file; in practice `SOURCE_CHANGED` or a translated OS error.
