# 6 Conclusion

LanLink provides authenticated private/public chat, an online user list and file
transfer through a central server. CLI and optional Tkinter interfaces share its
session and transfer engine. Clients keep a TCP connection, use TLS across the LAN
and discover servers through UDP. File transfers require recipient approval and
verify size and SHA-256 before confirming completion.

The work showed the coordination required above the transport layer: message
boundaries, request/reply matching, shared-state protection and cleanup after failure.
Separating interface, networking and transfer logic made these behaviours easier to
test. Automated tests cover framing, concurrency, authentication, integrity and failure
handling; CLI and GUI demos show chat and completed transfers. GUI conversation
history, unread counts and dialogs still lack automated widget coverage.

The current version serves online users: public forwarding is sequential, discovery
has no retry, and neither heartbeat nor offline message storage is implemented.
Future work should improve connection monitoring, discovery reliability and
slow-recipient isolation, and add GUI tests while moving blocking requests off the
Tk thread. These changes retain the client-server model.
