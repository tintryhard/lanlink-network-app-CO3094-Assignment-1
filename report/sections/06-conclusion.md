# 6 Conclusion

The main lesson is that socket behaviour differs between operating systems, so the code must not depend on one platform's details. On Windows, shutting a socket down from another thread does not wake a thread blocked reading it, so server workers now wake every 0.5 s (Section 4.3). Windows can also reserve UDP port ranges (WinError 10013), so the server now starts without discovery when its UDP port is reserved or in use. Finally, the CLI now writes UTF-8, so non-ASCII chat text cannot crash it when its output goes to a pipe with a Windows code page.

The following limitations remain; the deviations from the specification are listed in Section 1.6.

- Public chat is delivered to recipients one by one before `CHAT_ACK`. Each recipient that is not ready yet can add up to 1 s, and `CHAT_ACK` confirms that the server accepted the message, not that it was delivered.
- There is no heartbeat (see 1.6).
- Discovery sends a single `DISCOVER` broadcast with no retry, so one lost datagram means no server is found.
- Messages are not stored: a user who is offline never receives a message sent in the meantime.
