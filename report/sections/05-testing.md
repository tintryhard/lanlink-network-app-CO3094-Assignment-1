# 5 Testing

## 5.1 Automated tests

The tests run under pytest and otherwise use only the Python standard library, like the application. They cover the frame codec, the file engine, and integration tests that start a real server on the loopback interface and connect real client sockets to it. A run of the 550 tests (counting parametrised cases) on Windows gave 548 passed and 2 skipped; both skipped tests need file-system features that host lacks. Table 12 summarizes the test coverage.

Table 12. Test areas.

| Test area | What it checks |
| --- | --- |
| Framing | A stream split at every byte offset decodes to the same frames. |
| Wire format | Golden frames match byte for byte; invalid ones fail with their error code. |
| Concurrency: server | 20 clients connect and send `HELLO` at once; each gets its own worker. |
| Concurrency: one socket | 400 frames written from 4 threads through one connection all arrive whole. |
| Resilience: disconnect | A client that resets its connection is removed; new clients are still accepted. |
| Resilience: forwarding | A failed forward gives the sender `CONNECTION_LOST`; the peer goes offline. |
| Resilience: shutdown | Server shutdown ends every client reader and leaves no users or connections. |
| File integrity | Bad or out-of-order chunks give `HASH_MISMATCH` or `SEQ_MISMATCH`; no file is left. |
| Auth and TLS | A client refuses a server certificate it does not trust. |
| Discovery | Junk datagrams are ignored and `DISCOVER` is still answered. |
| CLI | An incoming message is printed while the client waits for input. |

## 5.2 User interface

The client is a command-line program with no GUI toolkit (Figure 10). Incoming chat appears at once on its own line as `[sender -> recipient] text`, where the recipient is `all` for a public message. The user list shows `Online users (N):` and then one line per user with the name and the IP address and port, with `(you)` on the user's own line. During a transfer, both sides redraw one line in place with a bar, the percentage, the file name, the peer and the bytes done, for example `[#####...............]  25% a.txt to bob 256/1024 B`. The commands are:

- `/msg USER TEXT`: private message
- `/all TEXT`: public message
- `/offer USER PATH`: offer a file
- `/accept ID`, `/reject ID [REASON]`: answer an offer
- `/users` or Enter: show the online users
- `/logout` or Ctrl+D: log out

![Figure 10. The LanLink command-line client: incoming messages, the user list and a file-transfer progress bar.](../screenshots/cli-ui.png)

## 5.3 Manual demo

The demo in Figure 11 uses one host and four terminals: create accounts for `alice`, `bob` and `charlie`, start the server in one terminal, and start one client per user in the others (replace `NAME` with the username).

```
python -m server.auth --file data/users.json --username alice
python -m server --host 127.0.0.1 --port 5000 --users data/users.json
python -m client --host 127.0.0.1 --port 5000 --username NAME
```

![Figure 11. Multi-terminal demo: one server and three clients on one host.](../screenshots/demo.png)
