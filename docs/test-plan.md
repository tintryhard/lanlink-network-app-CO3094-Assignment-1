# Member C test plan and measured execution record

## Scope and environment

Protocol/models, streaming disk engine, two widgets and load-driver logic only.
FakeRelay is explicitly a fixture; FakeConnection scripts replies and does not
validate server behaviour. No actual server or B client/service exists here.
The intended application capacity is 20 clients; this is a target, not a result.

Execution date: **2026-11-04 (Asia/Bangkok)**. Environment E1: Windows
10.0.19044, Python 3.10.11, PySide6 6.12.0, pytest 9.2.0, offscreen Qt.
The existing .venv is used. Dependency installation uses requirements-dev.txt;
requirements files are unchanged. The host's unrelated anyio pytest plugin
produces one CallSpec2 deprecation warning. No C tests require that plugin.
Qt offscreen discovered no default fonts on this host; the visual inspection
explicitly loaded the local Windows Segoe UI font. It introduced no dependency
or font/file access in widgets.

The 50 MiB input is generated in 64 KiB random blocks into tmp_path. Hash
comparisons stream the output. The fake relay never retains chunk payloads;
tracemalloc measures the transfer, including codec/relay/receiver allocations.
The final memory check also includes source generation and inspection. Every
engine test has teardown asserting no remaining .part. Tests additionally
check closed source/receiver handles, placeholders and terminal idempotency.

## Historical author commands and observed results

PowerShell setup for the exact root commands:

```powershell
$env:PATH = (Join-Path (Get-Location) '.venv\Scripts') + ';' + $env:PATH
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest -q
python -m pytest -q tests/ui
python scripts/load_test.py --help
```

At commit cef7d88, `python -m pytest -q`: **352 passed, 1 skipped, 1 warning in
6.84s**. Earlier stage commands and their real observations are in the matrix.
That run's single skip is symlink creation: this Windows host requires privileges.
There are zero test failures in that full run. An earlier transfer run found
five saved-hash failures from Windows text-mode low-level writes; O_BINARY fixed
them and the complete suite was rerun. No failed run is presented as success.

The unreachable-server check set LANLINK_LOAD_PASSWORD to a generated ephemeral
value, then ran:

```powershell
python scripts/load_test.py --host 127.0.0.1 --port 1 --users 1 --duration 0.1 --messages-per-client 0 --report-json "$env:TEMP\lanlink-unreachable.json" --report-md "$env:TEMP\lanlink-unreachable.md"
```

Observed exit **1**, one ConnectionRefusedError, zero sends/ACKs and a clear
"No verified TLS connection reached the server" diagnostic. Reports went to
the OS temporary directory, not source control. This verifies the failure path;
it says nothing about a running server's TLS behaviour or capacity.

## Historical author matrix

Historical rows below are author-reported observations; the independent review reruns are recorded separately. Actual results are NOT RUN until executed. Commit identifies the tested source
or the commit containing the subsequently tested change. E1 is defined above.

| ID | Area | Steps | Expected result | Actual result | Environment | Commit | Status |
|---|---|---|---|---|---|---|---|
| C1-01 | Protocol golden catalogue | python -m pytest -q tests/protocol | All message types/directions reproduce independent hex | 163 passed, 1 warning; 2026-11-04 | E1 | 9084c8d | PASS |
| C1-02 | Framing and hostile headers | Split at every byte, coalesce, inject invalid JSON/types/lengths/spoofing/UTF-8 | Typed rejection; bounded lengths; decoder poisoned | Included in C1-01 execution | E1 | 9084c8d | PASS |
| C1-03 | UDP | One exact datagram, source-IP policy, wrong directions/trailing bytes | Only DISCOVER/SERVER_INFO | Included in C1-01 execution | E1 | 9084c8d | PASS |
| C2-01 | Models and state graph | Enumerate every old/new status pair, inspect fields/events | Exact nine snapshot fields, no terminal exits, frozen events | Included in 289 passed, 1 skipped; 2026-11-04 | E1 | 42b5b5c | PASS |
| C3-01 | File sizes and bounded memory | python -m pytest -q -s tests/protocol tests/transfer; stream 0 B..50 MiB | Equal byte counts and streamed SHA-256; peak below 8 MiB | 289 passed, 1 skipped; 50 MiB peak 375863 bytes; 2026-11-04 | E1 | 42b5b5c | PASS |
| C3-02 | Corruption/sequence/size | Flip/duplicate/skip/reorder, short END, excess bytes | HASH/SEQ/SIZE_MISMATCH; cleanup; no sender success | Included in C3-01 execution | E1 | 42b5b5c | PASS |
| C3-03 | Cancellation/loss/timeouts | Both cancel directions, cross-thread cancellation, drop link, injected clock | One terminal state, no part/handle leaks | Included in C3-01 execution | E1 | 42b5b5c | PASS |
| C3-04 | Disk/path failures | Inject ENOSPC/EACCES at open/write/fsync/replace; unsafe names, collisions | Typed error, original files untouched | Included in C3-01 execution | E1 | 42b5b5c | PASS |
| C3-05 | Symlink destination collision | Reserve existing symlink name without following target | Unique alternate name; target unchanged | SKIPPED: Windows symlink creation privilege unavailable; 2026-11-04 | E1 | 42b5b5c | SKIP |
| C3-06 | Source mutation | Modify/truncate during send, delete before send | SOURCE_CHANGED/FILE_NOT_FOUND; no END success | Included in C3-01 execution | E1 | 42b5b5c | PASS |
| C4-01 | Widget states/actions | QT_QPA_PLATFORM=offscreen python -m pytest -q tests/ui | Every direction/state, exactly-once signals, sticky state | 32 passed, 1 warning; 2026-11-04 | E1 | 8e1b8ba | PASS |
| C4-02 | Widget layout/QSS/list | Long text, zero size, 280/900 px, keyboard, 220 rows with repeated updates | Buttons contained, valid QSS, stable row identities/counts | Included in C4-01 execution; widgets+layering rerun 33 passed | E1 | 8e1b8ba | PASS |
| C4-03 | Fake-data visual demo | Offscreen launch, process events, timed quit, inspect 300 px panel PNG | Visible fake banner; readable states/layout | Demo exit 0; manually inspected rendered PNG with Segoe UI; 2026-11-04 | E1 | 8e1b8ba | PASS |
| C5-01 | Load logic | python -m pytest -q tests/scripts | Correct statistics/CLI/reports, fake loop, real engine pumping, TLS flags | 31 passed, 1 warning; 2026-11-04 | E1 | cef7d88 | PASS |
| C5-02 | Load CLI | python scripts/load_test.py --help | CLI help, no insecure flag | Exit 0; 2026-11-04 | E1 | cef7d88 | PASS |
| C5-03 | No server | Run closed local port command above | Clear diagnostic, nonzero, zero fabricated observations | Exit 1, ConnectionRefusedError:1, sends:0, ACKs:0; 2026-11-04 | E1 | cef7d88 | PASS |
| C-ALL | All C tests | python -m pytest -q | All applicable tests pass | 352 passed, 1 skipped, 1 warning in 6.84s; 2026-11-04 | E1 | cef7d88 | PASS |
| C-FINAL | Self-review regressions and full suite | python -m pytest -q -ra | Strict numeric fields, negotiated limits, independent state expectations, TLS failure/interrupt cleanup, all tests pass | 394 passed, 2 skipped, 1 warning in 6.46s; 2026-11-04 | E1 | c84f350 | PASS |
| C-MEM-FINAL | Full source/transfer memory | python -m pytest -q -s tests/transfer/test_engine.py::test_sizes_streaming_and_memory[52428800] | Peak under 8 MiB including generation/inspection/relay/disk | 1 passed; peak 440688 bytes; 2026-11-04 | E1 | c84f350 | PASS |
| C-UI-FINAL | Offscreen rerun | QT_QPA_PLATFORM=offscreen python -m pytest -q tests/ui | All states/layout/signals remain correct | 32 passed, 1 warning in 1.68s; 2026-11-04 | E1 | c84f350 | PASS |
| C-POSIX-UNLINK | Delete source after first chunk | Unlink an open source while the relay pumps chunks | SOURCE_CHANGED, peer cancellation, clean parts | SKIPPED: Windows disallows unlinking an open source; deletion before send separately passes; 2026-11-04 | E1 | c84f350 | SKIP |
| INT-01 | LAN/TLS two machines | A starts real server; B connects via LAN/VPN with trusted certificate | Verify valid CA/hostname and reject invalid trust/hostname | NOT RUN | Two machines required | — | PENDING A/B |
| INT-02 | 20-client load | Create real accounts; run 20 users for 300s, simultaneous/staggered | Observe actual logins/ACKs/deliveries/latencies | NOT RUN | Real A server required | — | PENDING A |
| INT-03 | Chat during 50 MiB relay | --file-mib 50 with sufficient duration and real clients | Measured baseline/during-file samples and verified FILE_RESULT | NOT RUN | Real A server required | — | PENDING A/B |
| INT-04 | Slow-reader isolation | Raise message volume, --slow-readers N, inspect reports/server logs | Controlled QUEUE_FULL/close; other clients' metrics remain healthy | NOT RUN | Real A server required | — | PENDING A |
| INT-05 | GUI responsiveness | B bridge queues worker snapshots into complete app while hashing/transferring | No disk/network/hash work in GUI thread; responsive scrolling/input | NOT RUN | Integrated B app required | — | PENDING B |
| INT-06 | POSIX/macOS portability | Run complete suite on Linux/macOS with native Qt and symlink privileges | Same wire bytes and state/error behaviour | NOT RUN | Linux/macOS required | — | PENDING |
| INT-07 | UDP discovery LAN | A responder/B requester on broadcast-capable LAN | Source-IP selection, bounded datagrams, verified TLS follow-up | NOT RUN | LAN A/B implementations required | — | PENDING A/B |

## Rubric coverage and remaining ownership

- Milestone 1: protocol contract, limits, golden examples, models and diagrams.
- Milestone 6: streaming sender/receiver, byte/hash verification, progress and views.
- Milestone 7: negative cases, safe paths, disk errors, disconnects and cancellation.
- Milestone 9: executable measured load infrastructure; actual 20-client,
  two-machine, TLS/UDP and server queue experiments remain A/B integration work.
- Milestone 10: explainable implementation, protocol/test documentation and demo.

A owns the TLS transport/read loop, auth/session rules, queue bounds, relay role
checks, server heartbeat/router and real server tests. B owns queued event
delivery, ClientService message pumping, GUI shell/theme and actual application
responsiveness. No C implementation is a replacement for those components.

## Historical author self-review and changed-file manifest

The final tests add independent expected transitions (rather than using the
implementation table as the test oracle), negotiated UDP encoding bounds,
pre-buffer payload rejection, every numeric header field, duplicate user lists,
documentation/golden agreement, TLS handshake socket cleanup, scripted Ctrl-C,
crash aggregation, CSV credentials and explicit slow-reader observations. The
report includes zero counts for unobserved connection stages, per-stage failures,
reading clients' errors/missing traffic, and all min/p50/p95/p99/max latencies in
both JSON and Markdown. An early intentional Ctrl-C test interrupted pytest
because its fake thread also affected Windows subprocess pipe readers; its
environment lookup is now isolated, and the full suite above was rerun.

`git diff --check` passed. Source scans found no unfinished stubs, pickle, eval,
shell=True, disabled certificate checks, secrets or private keys. The baseline
working tree was clean; only the following 32 C-owned paths changed. Existing
README, requirements, architecture, A/B implementations and their placeholders
were left intact. All seven commits are local; no push was performed.

```text
client/ui/file_card.py
client/ui/file_format.py
client/ui/transfers_panel.py
common/errors.py
common/models.py
common/protocol.py
docs/protocol.md
docs/test-plan.md
scripts/demo_file_widgets.py
scripts/load_test.py
tests/__init__.py
tests/fakes/__init__.py
tests/fakes/fake_transport.py
tests/fixtures/__init__.py
tests/fixtures/protocol_cases.json
tests/protocol/__init__.py
tests/protocol/test_codec.py
tests/protocol/test_layering.py
tests/scripts/__init__.py
tests/scripts/test_load_test.py
tests/transfer/__init__.py
tests/transfer/conftest.py
tests/transfer/test_engine.py
tests/transfer/test_states.py
tests/ui/__init__.py
tests/ui/conftest.py
tests/ui/test_file_card.py
tests/ui/test_transfers_panel.py
transfer/paths.py
transfer/progress.py
transfer/receiver.py
transfer/sender.py
```

## Oral defence prompts (review corrections below)

- common/errors.py: one enum and translation table keep wire codes stable and UI
  wording central. Exceptions carry a code and safe message. Questions: why
  typed errors; why keep passwords out of exceptions?
- common/models.py: frozen snapshots/events are values crossing thread boundaries.
  One transition graph gives endpoints and relay the same terminal semantics.
  Questions: why exactly nine fields; why no terminal exits?
- common/protocol.py: canonical JSON and byte lengths make framing deterministic.
  Header validation bounds the payload before a transport allocates it. The
  incremental parser poisons corrupt streams. Questions: bytes vs characters;
  why never scan for a new prefix after corruption?
- transfer/paths.py: NFC preserves Vietnamese while platform-safe names neutralise
  path/device hazards. O_EXCL reserves a collision-free destination atomically.
  Questions: why sanitise twice; why not exists() then open()?
- transfer/progress.py: an injected clock makes 100 ms throttling testable.
  Status changes and the final byte bypass throttling. Questions: why time and
  delta thresholds; why force the terminal snapshot?
- transfer/sender.py: inspection and iteration each stream bounded chunks.
  Metadata and digest checks stop a modified source before normal exhaustion.
  FILE_RESULT alone can complete the sender. Questions: why inspect before
  offer; why is FILE_END insufficient?
- transfer/receiver.py: verified chunks go to an exclusive binary part file.
  Exact counts/hash, fsync and rename precede completion. A lock protects cleanup
  from concurrent cancellation; observer delivery occurs after releasing it. Questions: why .part first; why fsync before rename?
- client/ui/file_format.py: pure functions format binary sizes and snapshot text.
  Failure details use the central translation. Questions: KiB vs KB; zero-byte
  progress before and after completion?
- client/ui/file_card.py: a snapshot selects text, progress and actions.
  Clicks disable immediately and emit intent without inventing status. Questions:
  why sticky terminals; why can't a widget open a saved file itself?
- client/ui/transfers_panel.py: a dict keeps row identity stable during updates.
  Sorting happens only on insertion/terminal grouping changes. Questions: why
  reuse cards; how are active/finished counts maintained?
- scripts/demo_file_widgets.py: a visible fake banner separates examples from
  app state. Timer-driven snapshots cover success and each terminal branch.
  Questions: why new IDs per branch; why no simulated completion in product code?
- scripts/load_test.py: one verified TLS connection belongs to each worker.
  In-process markers correlate real send/ACK/delivery timestamps. Absent samples
  remain missing and fail verification. Questions: why verify hostname; why is
  no observed slow-reader close different from controlled isolation?
- tests/fakes/fake_transport.py: bounded fault injection still encodes and decodes
  every hop. It keeps no full-file message history. Questions: what can a fake
  prove; which claims require a real server?
- tests/fixtures: independent canonical frame hex catches accidental wire changes.
  Invalid cases document stable error decisions. Questions: why not generate
  expected bytes with the codec under test; how should contract changes land?

## Independent review baseline (2026-11-04)

Tested commit: c84f3503a19a1e4226555d0ef472933ecd181819, clean tree.
Windows 10.0.19044 / Python 3.10.11 / PySide6 6.12.0 / pytest 9.2.0.
The default Python could not collect UI tests (PySide6 absent); pip networking
was denied. Existing .venv requirements-dev installation succeeded with all
requirements already satisfied. Using .venv: `python -m pytest -q -ra`: 394
passed, 2 skipped, 1 warning, 8.19s. Offscreen `python -m pytest -q tests/ui`:
32 passed, 1 warning, 2.68s. The warning is the anyio CallSpec2 deprecation.
Skips: Windows symlink creation privilege; Windows unlink of open source.
All 35 valid golden frames independently recomputed with bare struct/json;
the four documented examples match. Original fixture generation method cannot
be established from committed files.

## Independent hardening review: evidence and checklist

The first complete reviewed suite source is **234d4b5d9306dbcab9b7abc14a65fc68f002fc06**.
A final cancellation follow-up is recorded below; earlier results retain their
actual source hashes.
The final documentation-only commit is necessarily newer; its full-suite rerun
and final HEAD are supplied in the review response. Rows below identify exact
tested commits instead of a self-referential description of an uncommitted tree.
All review fixes are local; no push, credentials, certificates, keys, load reports
or temporary harnesses are committed. A/B source, README, requirements and
architecture were not edited. Environment E1 above is unchanged. No Linux/macOS
run was available. Historical measurements remain explicitly historical, not
newly asserted observations.

### Reproduction commands and exact observations

All `python` commands here mean `.\.venv\Scripts\python.exe`, from repository
root. The first default-interpreter failure is recorded in the baseline above.

| ID | Steps / expected | Actual | Environment / exact tested commit |
|---|---|---|---|
| REV-ALL | `python -m pytest -q -ra -p no:randomly`; all applicable cases pass | 452 passed, 2 skipped, 1 warning, 17.97s | E1 / 234d4b5d9306dbcab9b7abc14a65fc68f002fc06 |
| REV-UI | `$env:QT_QPA_PLATFORM='offscreen'; python -m pytest -q tests/ui` | 41 passed, 1 warning, 2.53s | E1 / 234d4b5d9306dbcab9b7abc14a65fc68f002fc06 |
| REV-ORDER | Temporary pytest plugin `pytest_collection_modifyitems(self, items): items.reverse()`; `pytest.main(['-q','-ra'], plugins=[Reverse()])` | 452 passed, 2 skipped, 1 warning, 22.84s; exit 0 | E1 / 234d4b5d9306dbcab9b7abc14a65fc68f002fc06 |
| REV-REPEAT | Five separate `python -W error::ResourceWarning -m pytest -q tests/transfer -ra` runs | Each 162 passed, 2 skipped; 17.12 / 17.15 / 16.89 / 14.27 / 15.87s; no flaky failure | E1 / 234d4b5d9306dbcab9b7abc14a65fc68f002fc06 |
| REV-OPT | `python -O -m pytest -q tests/protocol tests/transfer -ra` | 369 passed, 2 skipped, 2 warnings, 18.36s | E1 / 234d4b5d9306dbcab9b7abc14a65fc68f002fc06 |

The normal warning is the host anyio plugin's CallSpec2 deprecation. Under -O,
pytest additionally warns that assertions outside rewritten test modules are
disabled; product validation uses typed exceptions, not runtime assert. The
FakeRelay diagnostic assertion is disabled under -O; independent test-module
assertions remain rewritten. Both skips have exact reasons: symlink creation
requires this Windows user's missing privilege; unlink of an open source is a
POSIX-only operation. A new same-size/same-mtime rename/replace regression runs
on Windows and covers source substitution without pretending to cover POSIX
unlink semantics. Neither skipped case was silently relabelled PASS.

Earlier incremental full suites after fixes: cdf0ff1 tree 398/2 skipped;
19d1450 tree 403/2; 7c24358 tree 408/2; 6be3254 tree 412/2; 0dc1a19 tree
417/2; 48d9d98 tree with new stress regressions 444/2; 313c203 tree with
new stress regressions 446/2; subsequent complete reviewed test tree 452/2.
These are development-tree observations, not extra executed matrix rows with
misleading commit identifiers. The exact clean-commit matrix above is the
reproducible result.

### Independent probes (temporary, never committed)

- Bare `struct.pack('!I', len(header))` + canonical `json.dumps` recomputed all
  35 valid golden frames; the four protocol.md hexes match their fixtures. The
  committed regression does not import encode_message. Original generation
  provenance cannot be recovered; reproducibility is established independently.
- Fuzz seed 3094: 1009 byte inputs through decode_header/decode_datagram/feed,
  including malformed/deep JSON, BOM, overlong UTF-8, surrogates and 10000-digit
  integers; 875 schema mutations through validate_message. A separate 10 MB
  poisoned-prefix probe retains no bytes. No exception other than ProtocolError
  escaped. Directly nested in-memory headers previously raised RecursionError
  and now produce BAD_JSON before recursion (regression committed).
- `python -m pytest -q -s tests/transfer/test_engine.py::test_sizes_streaming_and_memory[52428800] tests/transfer/test_engine.py::test_sizes_streaming_and_memory[104857600]`:
  2 passed, 1 warning, 4.31s; measured peaks 441830 / 389860 bytes, including
  generation, inspection, codec, relay and disk receiver. This probe used the
  48d9d98 implementation with the subsequently committed 100 MiB test expansion.
  The earlier author's 440688-byte peak is a historical observation; allocation
  peaks vary with interpreter/cache state and are not exact expected constants.
- Ten single mutations, restored byte-for-byte after each subprocess: skip
  receiver hash compare; skip partial-file unlink; remove O_EXCL; mark sender
  complete on FILE_END; skip payload_length comparison; allow path separators;
  bypass source size limit; permit terminal widget regression; reverse panel
  row order; inspect with source.read() of the whole file. **10 introduced,
  10 caught, zero survivors.** The last mutation failed the 8 MiB assertion with
  peak **52443024 bytes**. Every mutation selected an existing behavior test;
  no failure was treated as a successful product run. The tested implementation
  parent was 0dc1a19; changes made by the mutations were temporary only.
- 500 seeded four-way races per execution (250 sender + 250 receiver), with
  tiny random scheduling sleeps, cancellation/loss/timeout/inbound dispatch,
  terminal-count and exact directory cleanup assertions. A separate run under
  `-W error::ResourceWarning`, with gc.collect() and Win32
  GetProcessHandleCount, observed **144 -> 144 handles**, **1 -> 1 threads**.
  This verifies Windows handles rather than claiming a /proc fd measurement.
- Local real-socket TLS fixture generated a one-day localhost cert in
  TemporaryDirectory using OpenSSL; load driver used --cafile and
  --server-hostname localhost. 5 clients, --duration 20,
  --messages-per-client 10: **5 authenticated, 50 sends, 50 ACKs, 164
  deliveries, zero errors/missing observations, exit 0**. ACK samples=50,
  min/p50=0ms, p95/p99/max=16.00000000325963ms; Windows monotonic clock
  resolution permits observed zero latency. Wrong-hostname and untrusted-CA
  cases each exited 1 with SSLCertVerificationError, zero TLS sessions, zero
  sends/ACKs; absent latency values remained null / NOT OBSERVED. The fixture
  initially failed with two CONNECTION_LOST errors while using concurrent
  SSL operations; it was corrected to one I/O owner per connection before
  the successful run. This is fixture evidence, never A-server capacity data.
  Tested production implementation was 0dc1a19 (unchanged load networking
  since 6be3254); generated cert/key/reports were removed automatically.
- A throwaway pseudo-relay/service used only public codec/engine APIs, two
  HELLO/AUTH handshakes, chat/ACK, and 262144 bytes with 17-byte frame splits.
  Both engines completed with equal content; it did not import A/B or FakeRelay.
- Offscreen demo main ran and exited 0; QWidget.grab PNGs at actual 300/900px
  were opened and inspected after explicitly loading Windows Segoe UI. FAKE
  banner, text, buttons, progress and borders are visible; no overlap in those
  two captures. Before the fix the subclass's background/border was not painted.
  A 220px card initially grew to 240px, and a first scaled-font fix still grew
  to 268px in the full suite; final responsive action layout passes at
  220/280/320/900px. Native monitor/high-DPI behavior is not claimed from
  offscreen captures. No accessibility contrast conformance certification is
  claimed; visual contrast was inspected, not measured to a standard.
- A 500-row panel with 20 updates each reused all identities and preserved
  order/counts: insertion 1.486s, 10000 updates 4.017s (diagnostic, no flaky
  timing assertion). Ordinary progress never calls _reorder.
- AST/rg scans: no socket/ssl/hashlib/transfer/server/os/pathlib/thread/QFile/
  QDesktopServices work in the three widgets; open_location_requested emits
  only. No pickle/eval/shell=True, disabled certificate checks, production
  runtime assert, hardcoded success count or secrets found. The only product
  subprocess is shell-free `git rev-parse HEAD` for report provenance. Broad
  callback catches count/log failures; load worker catches aggregate crashes;
  OS cleanup/space-check catches are explicitly best effort/advisory.

### Findings and fixes

Line references below refer to the reviewed implementation, not the historical
line numbers before the fixes. Each behavior regression was observed failing
before its associated change; full-suite checks followed the changes.

| ID | Severity | Area / file:line | Confirmed defect / evidence | Resolution |
|---|---|---|---|---|
| R01 | Major | docs/protocol.md error codes; docs/test-plan.md historical final rows | Emitters copy-pasted (including unimplemented auth/router); generic meanings; no carrier; Step-7 description instead of hash | d8a6597 plus this documentation update |
| R02 | Major | common/protocol.py:112 | validate_message with 1500 nested dicts raised RecursionError | cdf0ff1; test_deep_in_memory_header_is_a_protocol_error |
| R03 | Blocker | transfer/sender.py:163; transfer/receiver.py:71; transfer/progress.py:10 | Five callback regressions failed; cross-thread cancel blocked under callback lock; reentrant receive cancel wrote fd=None; raising callback interrupted cleanup | 19d1450; ordered queue outside lock and exception isolation |
| R04 | Blocker | scripts/load_test.py:199,209 | Wrong-connection ACK could satisfy a request; wrong sender/type/fan-out with same marker counted; untracked traffic did not fail verification | 6be3254; four attribution regressions failed before fix |
| R05 | Minor | client/ui/file_card.py:24 | Theme token semicolon/brace injection and None were interpolated unchecked | 7c24358; three invalid-token regressions |
| R06 | Major | client/ui/file_card.py:186; client/ui/transfers_panel.py:70 | Failed operation plus equal snapshot left actions disabled permanently; no retry hook | 7c24358; correlated operation_failed(id) local API; B must wire it |
| R07 | Major | transfer/paths.py:38 | Bidi/zero-width controls preserved and COM-superscript/LPT-superscript device names not neutralised | 0dc1a19; four hostile-name regressions |
| R08 | Minor | transfer/progress.py:81 | Standalone throttle emitted OFFERED after CANCELLED | 0dc1a19; terminal/progress/ID guard |
| R09 | Minor | client/ui/file_card.py:88,161 | Minimum width defeated 220px allocation; scaled actions could widen card; custom QWidget did not paint themed surface | 48d9d98; scaled-font geometry regression plus viewed PNGs |
| R10 | Major | scripts/load_test.py:664 | Ctrl-C during Thread.start led to RuntimeError joining unstarted thread instead of exit130 | 313c203; startup-interrupt regression |
| R11 | Minor | tests/transfer/conftest.py:8 | Autouse leak oracle rejected legitimate completed archive.part | d022eab; only completed paths exempt, plus owned FD/reservation assertions |
| R12 | Major | tests/protocol/test_review.py; tests/transfer/test_review.py | Baseline lacked seeded fuzz, callback/race, same-metadata replacement and 100 MiB/memory sensitivity coverage | cdf0ff1, d022eab, 234d4b5; adversarial regressions/probes above |
| R13 | Nit | common/protocol.py:189; scripts/load_test.py:551 | 102-line schema validator and 71-line report function exceed suggested oral-defence size; several widget statements are compressed | Documented clarity debt; no behavioral rewrite justified |
| R15 | Major | transfer/sender.py:250 | Cancellation from final-progress/VERIFYING callback still yielded pending CHUNK/END; two new regressions failed | 78382e4; terminal check before each yield |
| R14 | Blocker (integration) | server/__main__.py:1; missing ClientService/bridge | Authenticated relay roles, connection lifecycle, queue isolation and real GUI threading not implemented here | needs A/B; no prohibited files changed |

### Checklist dispositions (review request sections 3.1-3.9)

| Checklist target | Disposition / evidence / limits |
|---|---|
| 3.1 error meanings, emitters and carriers | R01 fixed; all ErrorCode values translated and catalogued; six router/auth codes explicitly unimplemented instead of falsely attributed to codec |
| 3.1 exact commits/counts and all four examples | Historical missing hashes corrected to c84f350; independently rerun 394/2 baseline; new clean-commit matrix; 35 independent struct/json golden comparisons |
| 3.1 names, limits, diagrams and stale text | All 21 types, eight limits, nine snapshot fields, eight events, eight B mapping signal names and four widget signals checked; all 13 transfer Mermaid edges equal ALLOWED_TRANSITIONS; invalid disconnect arrow corrected; symmetric disconnect documented. Mermaid CLI unavailable: no rendered-diagram claim |
| 3.2 seeded random/hostile inputs | Verified by seeded byte/header corpus, huge prefix, strict numeric tests and R02 regression; no untyped exception observed after fix |
| 3.2 direction, spoofing and server-only types | Every valid catalogue frame tested against all Direction values; server-stamped chat/offer cannot pass CLIENT_TO_SERVER; SERVER_INFO only UDP_REPLY |
| 3.2 decoder poison, feed empty, pre-buffer limits and threading | Verified by EOF/prefix/coalescing/negotiated payload tests; decoder remains poisoned; feed empty is not EOF; owner-thread-only documented |
| 3.2 negotiated limits, immutability, strings, codes | Strict missing/extra/bool/float limits and heartbeat inequality; source/session/receiver use negotiated file/chunk bounds; encoder/decoder use header/chat/UDP bounds. Header tree read-only, AUTH repr omits payload; Cc header controls rejected, chat newline/NFC/NFD preserved; receiver platform sanitizer intentionally stricter |
| 3.3 states, message order, cancel/reject/loss/errors/timeouts | Exact graph independently enumerated; live sender stages x four termination paths, both-side cancel/loss, early result/END, corruption/seq/size, terminal late messages and foreign IDs tested. Callback-driven VERIFYING cancellation races an atomic finish and cannot interrupt publication once it holds the lock; cancellation wins if it acquires the lock first |
| 3.3 receiver trust/chunk shape | Totals/hash/offer verified; exact nonfinal chunk, final1..chunk_size, zero chunks only for empty; oversize before disk; short/excess/early END fail. Sender clamps local requested chunk size; peer offers above negotiated max are rejected (silently clamping would contradict advertised chunk shape) |
| 3.3 hostile paths/collisions/reservations | Paths, drive names, device/superscript names, trailing dot/space, Unicode byte limits, Cf, NFC/sanitizer collisions, relative/Unicode/spaced directory, archive.part, preexisting part, replaced reservation and two concurrent receivers exercised. Symlink creation/directory targets remain platform gap on this host; local malicious directory-entry races outside remote-peer threat model |
| 3.3 resources/concurrency/callbacks | R03 fixed; 500 seeded races per run, gc/Win32 handles no growth, source immediately deletable after stop. Injected open/write/fsync/replace ENOSPC/EACCES verifies exact cleanup; OS removal after permission revocation still best effort |
| 3.3 source inspection/change/generator | Directory/missing/oversize before hashing; truncate/grow detection, preserved-size/mtime digest edit and replacement before pumping; empty file and generator closure covered. POSIX live unlink remains skipped; after-cancel Windows delete succeeds |
| 3.3 memory/progress/mutation | 50/100MiB bounded measurements, whole-read mutation fails; terminal throttle fixed; snapshot always current despite throttling; 10/10 deliberate mutations caught |
| 3.4 layering and all direction/status actions | AST checks strengthened; existing 16 state/direction cases, intent signals, open-location emit only, sticky terminals and monotonic bytes pass; R06 adds explicit retry recovery without changing snapshot |
| 3.4 invalid/large snapshots | Zero size, failed without error, unknown error text fallback, >total/negative progress, totals >=2^53 inspected/probed; fraction clamps and no success is fabricated. Contract snapshot inputs remain typed values; nonsensical types are not a supported wire path |
| 3.4 layout/QSS/accessibility | 220/280/320/900px scaled-font regression, keyboard tab order/accessibility names, long text/elision/plain text, viewed demo PNGs; QSS values validated; native high-DPI monitor not tested |
| 3.4 panel/update complexity | Dict lookup and reused rows; newest-within-group ordering on terminal flip; unknown removal/clear_finished/counts tests; 500x20 diagnostic 4.017s; no time threshold |
| 3.5 TLS and credentials | CERT_REQUIRED + check_hostname true, no insecure option/env fallback; real trusted/wrong-host/untrusted TLS runs; passwords env/file only, repr/report redaction tested; no cert/key/report committed |
| 3.5 stats, accounting and interruption | Percentiles empty/1/2/100 samples, actual ACK/delivery absence, duplicates/errors/file verification nonzero; R04 attribution and R10 startup Ctrl-C fixes; failed joins release barrier, threads joined. All load samples bounded by run plan; not a constant-memory online quantile algorithm |
| 3.5 five-minute sample storage estimate | Defaults20x10=200 chats, at most3800 expected public deliveries (~122kB for float+list entries before chat dictionaries). At min50ms pacing for300s an arbitrarily high message target can approach120000 chats/2.28million samples (~73MB numeric list storage plus chat/Counter dictionaries). Scale report retention accordingly; no spontaneous sample growth after planned sends |
| 3.6 oracles/order/flakiness/skips/artifacts | Independent golden and transition expectations, strengthened cleanup oracle R11, normal+reversed suite, five repeated transfer runs no failures; two explicit Windows skips, no unclaimed POSIX evidence. Tiny sleeps only in deliberate scheduling stress; logical timeouts use injected clocks |
| 3.7 security and -O | Scans and 369/2 optimized run above; shell-free git metadata subprocess only; no unsafe serialization, TLS bypass, payload logging or product runtime assert found |
| 3.8 explainability/oral claims | R13 clarity debt; poisoning, streaming, ownership, exact state/snapshot/translation claims checked; callback prompt corrected: observers outside lock, cleanup protected by lock, exceptions isolated |
| 3.9 integration readiness and public API | Pseudo-service proved composition, docs now specify timeout cancellation owner, failed accept reply, return/exception shapes, decoder thread ownership, callback semantics and one-frame pump scheduling; authenticated server/GUI behaviors remain needs A/B |

### Requirement -> code -> test traceability

| Plan requirement | Code | Independent/existing test | Status |
|---|---|---|---|
| TCP+TLS auth/chat/files; UDP discovery only | protocol Direction; load create_tls_connection | test_udp_exact_frame_and_direction, test_tls_factory_keeps_verification; actual TLS fixture | C verified; A transport pending |
| uint32BE + UTF8 JSON + exact binary payload | protocol encode_message/FrameDecoder | test_golden, independent golden bytes, all_splits_and_coalescing | verified OK |
| configurable published16KiB/64KiB/100MiB and all limits | ProtocolLimits; HELLO_ACK; engines | test_limits_and_hello_ack, payload_boundaries, negotiated_payload_bounds_before_buffering, oversize_refused_before_hash | verified OK; A/B must pass negotiated object |
| split/coalesce/EOF/invalid lengths; permanent poison | FrameDecoder | all_splits_and_coalescing, eof_and_poison, prefix_rejected_immediately, huge_input_prefix_and_empty_feeds | verified OK |
| strict UTF8 chat, raw binary file, no pickle | validate_message; iter_chunks; receiver.write | unicode_byte_lengths, unicode_rejected_in_payload_and_builders, sizes_streaming_and_memory, AST/rg | verified OK |
| offered/accepted/transferring/verifying and four terminals | models.ALLOWED_TRANSITIONS; both engines | all_transitions; sender_all_live_stages_terminate_once; wire_faults/cancel/rejected/timeouts | verified OK; relay pending |
| FILE_END never success; receiver exact size+SHA256; FILE_RESULT | Sender.handle_message; Receiver.finish | end_is_not_sender_success; wire_faults; extra_bytes_and_short_end; wrong_result_byte_count | verified OK |
| chunk I/O and bounded memory | inspect_file/iter_chunks/write; codec relay | sizes_streaming_and_memory50/100MiB plus whole-read mutation | verified OK within measured path |
| .part, safe final name, no traversal or overwrite | paths.reserve_destination/sanitize_filename; Receiver | reject_paths, hostile review names, collisions, reservations_replaced/preexisting_part | verified OK; symlink OS case skipped |
| cancel/error cleans temporary file and session | both engines; receiver._cleanup | no_partial_files fixture; cancel_both_directions, io_failure_cleanup, 500-race probe | verified OK except unrecoverable OS access revocation |
| ENOSPC/EACCES/source-change business errors | paths.io_error; source stat/hash checks | io_failure_cleanup, low_free_space_and_chunk_shape, preserved metadata edits/replacement | verified OK |
| hash/disk/network off GUI thread; widgets display only snapshots | widgets AST boundary; worker docs | test_layering; snapshot/action tests; B bridge absent | static C verified; real GUI pending B |
| accept/reject/cancel(id) intent only; no fake success | FileCard._request/set_snapshot | intent_emitted_once_without_fake_state, terminal_stickiness, all16 states | verified OK; operation_failed retry hook added |
| real measurements; absent observation never success | load Measurements/make_report | missing_ack_is_not_fabricated, attribution regressions, TLS fixture negative cases | verified OK for driver; capacity unmeasured |
| protocol no PySide/client/server; UI no sockets/files/threads | all C modules | strengthened test_layering | verified OK |
| event/signal/field catalogue complete | models events; protocol.md; widgets signals | exact_contract plus catalogue introspection and UI forwarding | verified declarations; B runtime mapping pending |

Every message has a golden frame and an explicit-direction probe. Behaviour
ownership is separate from codec syntax:

| Message names (complete catalogue) | Code/test | Runtime boundary |
|---|---|---|
| HELLO, HELLO_ACK, AUTH, AUTH_OK | builders; golden/direction tests; load.login; real TLS fixture | A authentication/session pending |
| USER_LIST, LOGOUT, ERROR | schema/payload/golden; user-list malformed tests; engine ERROR handling and loss tests | A list/logout/router pending |
| CHAT_PUBLIC, CHAT_PRIVATE, CHAT_ACK | builders/golden/direction; load accounting/real socket observations | A fan-out/authorization pending |
| FILE_OFFER, FILE_ACCEPT, FILE_REJECT | builders/golden; FakeRelay accept/reject; oversize/path tests | A authenticated transfer-role checks pending |
| FILE_CHUNK, FILE_END, FILE_RESULT, FILE_CANCEL | codec plus both engines; wire_faults/end_not_success/cancel/timeouts | A relay/backpressure pending |
| PING, PONG | builders/golden; fake login loop PONG; actual socket fixture responses | A/B periodic responsibility decision pending |
| DISCOVER, SERVER_INFO | golden plus exact-frame UDP/direction/limit tests | A/B real LAN UDP pending |

| Event/signal names (complete declared integration surface) | Code/test | Status |
|---|---|---|
| ConnectionChanged -> connection_changed; AuthResult -> auth_result | models; frozen-value inspection/test_exact_contract | declarations verified; B mapping pending |
| UsersChanged -> users_changed; MessageReceived -> message_received | models; protocol event field catalogue inspected | declarations verified; B mapping pending |
| MessageStatusChanged -> message_status | models; request/message/status/timestamp fields inspected | declaration verified; B mapping pending |
| FileOffered -> file_offered; TransferUpdated -> transfer_updated | models snapshots; engine callback tests | C values verified; B queued bridge pending |
| OperationFailed -> operation_failed | models optional request_id/field/transfer_id; new widget retry test | B must route correlated ID to the new retry slot |
| accept_requested, reject_requested, cancel_requested(str id) | both widgets; intent/forwarding tests | verified OK |
| open_location_requested(str saved_path) | FileCard emit; panel forwarding tests | verified OK; B owns actual folder opening |

The error-code table in protocol.md enumerates all 35 stable ErrorCode strings,
central Vietnamese messages and implemented versus planned producers. Router-only
NOT_AUTHENTICATED, AUTH_FAILED, ALREADY_LOGGED_IN, RECIPIENT_OFFLINE,
RECIPIENT_UNKNOWN and QUEUE_FULL have no C runtime raiser; builds/tests that
manufacture ERROR examples are not evidence of an implemented router.

### Remaining A/B decisions and unverified integration

Recommend no sender echo for public chat, JSON {users:[...]} demo credentials,
both transport owners performing PING/PONG, service-owned FILE_CANCEL(TIMEOUT),
and ERROR(CONNECTION_LOST, transfer_id) to the surviving peer on either-side
loss. Reject nonreceiver FILE_RESULT before forwarding. Use ERROR for valid
business failures and close poisoned framing; confirm rubric UDP requirement.
B must wire operation_failed(id), queue callbacks from arbitrary worker/cancel
threads, and budget one outgoing frame per worker tick under queue capacity.

No A server/B integrated app exists to verify real LAN TLS, two-machine
connectivity, actual20-client/300s load, slow-reader saturation/isolation,
chat during50MiB over LAN, authenticated role enforcement, actual UI response
under the real bridge, or native Linux/macOS/high-DPI/symlink semantics. INT-01
through INT-07 remain NOT RUN against real A/B implementations. The local TLS
fixture is explicitly not a replacement for those tests.

Additional final temporary probes (same 234d4b5 implementation): seven widget
snapshot cases (FAILED without error, unknown error code, >total and negative
progress/totals, zero total, total2^63-1 with progress>2^53) produced no crash or
fabricated status; NUL/COM1/con/trailing-space/Cf names sanitised as specified;
live source append produced SOURCE_CHANGED and peer CANCELLED with no part;
closing and dropping started iter_chunks generators released the source for
immediate Windows deletion. These are probe observations, not invented pytest
case counts. Native font scaling was exercised using widget fonts; only the
300/900px demo images were visually reviewed. No machine Mermaid renderer was
available, so edge equality and syntax inspection are the diagram evidence.

### Final cancellation follow-up

Commit **78382e40e5a138b2db512389f5468ddb0ed3e001** adds two regressions and
suppresses a pending outgoing chunk/end if observer delivery has already
cancelled the sender. Both tests failed before the final terminal checks; the
full suite then passed 454/2 skipped. A transport frame already handed off
before cancellation cannot be retracted; service-side queue cancellation remains
B/A's responsibility. No wire/schema/snapshot change was made.

One attempted final verification batch accidentally overlapped deliberate source
mutations with normal suite execution. That batch is excluded: a transfer
repetition loaded the intentional removed-size-check mutation and reported
1 failed, 163 passed, 2 skipped. This is an invalid verification setup, not a
flaky product observation. All source mutations were restored byte-for-byte,
product bytecode caches removed, and all final checks repeated with a stable
source tree and PYTHONDONTWRITEBYTECODE=1. Mutation results themselves remain
10/10 caught, zero survivors; whole-file peak again52443024 bytes. The new
unmutated 50/100MiB peaks were451489/392177 bytes (2 passed, 1 warning,7.85s).

Final stable execution matrix (same E1, exact implementation commit
**78382e40e5a138b2db512389f5468ddb0ed3e001**; only review documentation pending):

| ID | Command / steps | Expected | Actual | Environment | Tested commit |
|---|---|---|---|---|---|
| FINAL-ALL | `python -m pytest -q -ra -p no:randomly` | Applicable tests pass | 454 passed, 2 skipped, 1 warning,19.40s | E1 | 78382e40e5a138b2db512389f5468ddb0ed3e001 |
| FINAL-REVERSE | `python $env:TEMP\lanlink-review-order.py` (plugin reverses collected items) | No order dependency | 454 passed, 2 skipped, 1 warning,22.95s | E1 | 78382e40e5a138b2db512389f5468ddb0ed3e001 |
| FINAL-REPEAT | Five `python -W error::ResourceWarning -m pytest -q tests/transfer -ra` | No leaks/flaky failures | Each164 passed, 2 skipped;15.15/17.76/17.94/17.18/17.23s | E1 | 78382e40e5a138b2db512389f5468ddb0ed3e001 |
| FINAL-OPT | `python -O -m pytest -q tests/protocol tests/transfer -ra` | Validation unchanged | 371 passed, 2 skipped, 2 warnings,18.41s | E1 | 78382e40e5a138b2db512389f5468ddb0ed3e001 |
| FINAL-UI | QT_QPA_PLATFORM=offscreen; `python -m pytest -q tests/ui` | Widget regressions pass | 41 passed, 1 warning,2.97s | E1 | 78382e40e5a138b2db512389f5468ddb0ed3e001 |

All five final transfer repetitions and normal/reversed suites were run after
source restoration and bytecode-cache removal. No final source mutation runs
were concurrent with these measurements. The final documentation-only commit
will be tested once more with `python -m pytest -q -ra`; its actual hash/result
will be reported in the final response, avoiding a self-referential commit hash
inside its own tree.
