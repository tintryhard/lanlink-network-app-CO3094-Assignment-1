# LanLink report: merged structure

Merged from the four section outlines in this folder (`01`–`04`). Those files hold the
detail (what each heading covers, which code files it draws from); this file fixes the
order, the numbering, the section boundaries and the page budget. Brief: `Task.md`.

## Heading tree

**Front matter:** title page · contents · list of figures · list of tables

**1 System Architecture** (~1.5 pages) — `01-architecture.md`
- 1.1 Overview and deployment model
- 1.2 Components and data paths
- 1.3 Layer separation
- 1.4 Concurrency model
  - 1.4.1 Threads
  - 1.4.2 Locks and ordering
  - 1.4.3 Scalability evidence
- 1.5 Transport and security choices
  - 1.5.1 TCP for chat and files, UDP for discovery
  - 1.5.2 TLS
  - 1.5.3 Plaintext on loopback only
  - 1.5.4 Password storage and login limits

**2 Protocol Specification** (~3.5–4 pages) — `02-protocol.md`
- 2.1 Design rationale
- 2.2 Frame format
  - 2.2.1 Byte layout and byte order
  - 2.2.2 How a receiver reads a frame
  - 2.2.3 Worked example: a `CHAT_PRIVATE` frame
- 2.3 Common header fields and identifier formats
- 2.4 Message catalogue
  - 2.4.1 Session · 2.4.2 Chat · 2.4.3 File transfer · 2.4.4 Discovery · 2.4.5 Error
- 2.5 Error codes
- 2.6 Limits and validation
  - 2.6.1 Size limits and timeouts
  - 2.6.2 Malformed frame vs. bad request
  - 2.6.3 Server-stamped fields
- 2.7 State diagrams
  - 2.7.1 Server-side connection
  - 2.7.2 File transfer
- 2.8 UDP discovery

**3 Sequence Diagrams** (~4–5 pages) — `03-sequences.md`
- 3.1 Login and user list
- 3.2 Chat message lifecycle
- 3.3 File transfer: happy path
- 3.4 File transfer: failure paths
- 3.5 Matching replies to requests

**4 Challenges & Solutions** (exactly 1 page, ~500 words) — `04-challenges.md`
- 4.1 Keeping frames intact on a TCP stream
- 4.2 Avoiding deadlock
- 4.3 Detecting a closed connection
- 4.4 Timeouts vs. blocking fan-out
- 4.5 Lessons learned and remaining limitations

**Use of Generative AI** (unnumbered, from `Task.md` §7)

Estimated total: 14–16 pages including front matter.

## Figures (numbered through the whole report)

| # | Section | Caption (working) | Source |
|---|---|---|---|
| 1 | 1.2 | LanLink runtime architecture: server threads, shared registries and clients | `report/diagrams/architecture.mmd` |
| 2 | 2.2.1 | Frame layout: length prefix, JSON header, payload | in the section (text block) |
| 3 | 2.7.1 | Server-side connection states | `report/diagrams/state-connection.mmd` |
| 4 | 2.7.2 | File-transfer states | `report/diagrams/state-transfer.mmd` |
| 5 | 3.1 | Login and user list | `report/diagrams/seq-login.mmd` |
| 6 | 3.2 | Lifecycle of a private chat message | `report/diagrams/seq-chat.mmd` |
| 7 | 3.3 | File transfer, happy path | `report/diagrams/seq-file.mmd` |
| 8 (–9) | 3.4 | File transfer, failure paths (split in two if it does not fit A4) | `report/diagrams/seq-file-failures.mmd` |

## Tables

| # | Section | Caption (working) |
|---|---|---|
| 1 | 1.3 | Layer separation: modules per layer |
| 2 | 1.4.2 | Locks, what they protect and acquisition order |
| 3 | 2.3 | Common header fields |
| 4–8 | 2.4.1–2.4.5 | Messages: session, chat, file transfer, discovery, error |
| 9 | 2.5 | Error codes and where each appears on the wire |
| 10 | 2.6.1 | Size limits and timeouts |

Listing 1 (2.2.3): the example `CHAT_PRIVATE` frame in hex.

## Section boundaries

- **Section 1** owns threads, locks, TLS and password storage. It shows the UDP
  discovery path in Figure 1 but does not describe the messages.
- **Section 2** owns the frame format, every field table, error codes, limits, both
  state diagrams and the discovery messages (2.8).
- **Section 3** owns message order and failure paths. It names states and timeouts only
  as notes with a cross-reference to section 2. No separate discovery sequence.
- **Section 4** owns the stories (what went wrong, how it was fixed). It refers to
  Table 2 for locks and to section 2 for the frame format instead of repeating them.
- In sequence diagrams, one lifeline is one thread, and the names match the thread
  names used in Figure 1.

## Gaps between the spec and the code, to state plainly

Each is stated once, in the section named, and listed again in 4.5 if it is a
limitation. If the code is fixed before the report is written, the entry is dropped.

| Gap | Stated in |
|---|---|
| No heartbeat: `PING`/`PONG` are defined but never sent, and a client that sends `PING` is disconnected | 2.4.1, 4.5 |
| Users do not broadcast presence; discovery finds the server only, and the user list is requested | 1.2, 2.8 |
| All traffic is relayed by the server; there is no peer-to-peer path | 1.1 |
| Encryption is TLS on the whole TCP stream; UDP discovery is plaintext; TLS is optional on loopback | 1.5.2, 1.5.3 |
| Idle timeout: advertised as 45 s, CLI default 120 s | 2.6.1 |
| The server's transfer path skips the transition check (empty file: ACCEPTED → VERIFYING) | 2.7.2 |
| Most framing and schema errors close the connection without an `ERROR` reply | 2.6.2 |
| `RECIPIENT_UNKNOWN` is never raised; an unknown user gets `RECIPIENT_OFFLINE` | 2.5 |
| No user-facing cancel for a file transfer | 3.4 |
| A handshake timeout closes silently | 3.1 |
| `transfer/` imports `threading` (locks only); `common/handshake.py` and `client/private_chat.py` span two layers | 1.3 |
| Public broadcast delivers to recipients one by one before the ACK | 4.5 |

## Needed from the team before writing

1. `Task.md` §7: names, IDs, class, instructor, AI-use statement.
2. For challenges 4.1 and 4.2: did you actually see split or interleaved frames or a
   deadlock, or did you design against them from the start? The wording follows the
   answer.
3. For 4.4: did the public-chat timeout in commit `2e18eb0` really happen, or was it
   found by reading the code?
4. Whether the code-review fixes land before the report is written (it changes the gap
   table above and the test results quoted in section 4).
