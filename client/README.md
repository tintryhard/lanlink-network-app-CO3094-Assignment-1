# client/

| File | What it does |
|---|---|
| `__main__.py` | `python -m client ...`: options, login, and the interactive command loop (`login_cli`) |
| `private_chat.py` | `PrivateChatClient`: the logged-in session. It owns the reader thread, request/reply matching, chat and file transfers |
| `connection.py` | `open_connection` (TCP or TLS), `connect_hello`, `authenticate`, `request_users` |
| `discovery.py` | `discover_servers`: UDP broadcast `DISCOVER`, collects `SERVER_INFO` replies for 2 s |
| `display.py` | Text output: user list, progress bar, file event lines |
| `gui.py` | `python -m client.gui`: Tkinter login window and a dark chat window (sidebar of conversations, history per conversation), built on the same `PrivateChatClient` |

## Threads in a CLI session

| Thread | Job |
|---|---|
| main | Takes items from the `commands` queue one by one: typed lines and file events |
| input (`lanlink-cli-input`) | Reads a line from stdin each time the main loop is ready for one |
| reader (`lanlink-client-reader`) | The only thread that reads the socket. It prints chat, puts file events in the queue, and hands replies to the waiting request |
| sender (`lanlink-file-sender`) | Started after `FILE_ACCEPT`; sends the chunks and `FILE_END`, so the reader keeps reading meanwhile |

## Worth knowing

- **Request/reply matching.** `_request` registers the request's `request_id`, sends it and waits up to `--timeout` (5 s). The reader delivers the reply whose `ref_request_id` matches. A timeout closes the session, because a late reply could otherwise be taken for the next one.
- **One socket, two writers.** The reader (sending `FILE_RESULT`) and the sender thread (sending chunks) share the connection. The transport's write lock keeps frames whole.
- **File events.** Offers, decisions and errors (Messages), plus one final `TransferSnapshot` per transfer, go to `transfer_events`. The queue is bounded (128); an overflow closes the connection rather than losing an event silently. Progress snapshots go to `on_progress` instead. The CLI draws a bar such as `[#####...............]  25% a.txt to bob 256/1024 B`.
- **Disconnect.** `_close_with_error` runs its cleanup once:
  - it closes the socket;
  - it wakes every waiting request with the error;
  - it calls `connection_lost()` on every live transfer, which deletes partial downloads.
- **Plaintext only to localhost.** `open_connection` refuses a non-loopback address unless `--cafile` is given.
- **stdin** is read with `os.read`, one byte at a time, so the program can exit cleanly while the input thread waits for a line.
- **GUI threads.** Tk widgets may only be touched by the Tk thread. `ChatWindow` passes `events.put` as every `PrivateChatClient` callback. `poll()` drains that queue every 100 ms. Requests such as sending a message run on the Tk thread and wait at most `timeout` (5 s).
- **GUI transfer bar.** It appears once the peer accepts and stays at 100 % for 1 s after the end. A `FILE_REJECT` or transfer `ERROR` has no final snapshot, so the bar is hidden when that message arrives.
- **GUI user list.** The server never pushes user changes, so the GUI asks for `USER_LIST` every 3 s.
