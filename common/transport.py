"""Blocking socket I/O using the existing protocol codec.

The caller owns the socket and its timeout/cleanup. One transport per connection.
"""
from collections import deque
import socket
import threading
import time

from common.models import Message
from common.protocol import DEFAULT_LIMITS, Direction, FrameDecoder, ProtocolLimits, encode_message


class SocketTransport:
    """Send and receive whole Messages over one (TLS or plain) socket."""
    def __init__(self, connection: socket.socket, *, direction: Direction,
                 limits: ProtocolLimits = DEFAULT_LIMITS) -> None:
        """Wrap `connection`; `direction` tells the decoder which message types are legal.

        `read_deadline` (a time.monotonic() value) optionally bounds receive() during the handshake.
        """
        self.connection = connection
        self.limits = limits
        self.decoder = FrameDecoder(limits, direction)
        self._pending = deque()  # decoded messages not yet returned by receive()
        self._eof = False
        self._write_lock = threading.Lock()
        self.read_deadline = None

    def send(self, message: Message) -> None:
        """Encode and send one message. The lock keeps frames from two threads apart."""
        frame = encode_message(message, self.limits)
        with self._write_lock:
            self.connection.sendall(frame)

    def receive(self) -> Message | None:
        """Return one message, or None for clean EOF; truncated EOF raises."""
        while not self._pending:
            if self._eof:
                return None
            if self.read_deadline is not None:
                remaining = self.read_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Handshake deadline expired")
                self.connection.settimeout(remaining)
            data = self.connection.recv(65536)  # up to 64 KiB; may hold part of a frame or several
            if not data:
                self._eof = True
                self.decoder.feed_eof()
                return None
            self._pending.extend(self.decoder.feed(data))
        return self._pending.popleft()
