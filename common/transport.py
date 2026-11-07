"""Blocking socket I/O using the existing protocol codec.

The caller owns the socket and its timeout/cleanup. One transport per connection.
"""
from collections import deque
import socket

from common.models import Message
from common.protocol import DEFAULT_LIMITS, Direction, FrameDecoder, encode_message


class SocketTransport:
    def __init__(self, connection: socket.socket, *, direction: Direction,
                 limits=DEFAULT_LIMITS):
        self.connection = connection
        self.limits = limits
        self.decoder = FrameDecoder(limits, direction)
        self._pending = deque()
        self._eof = False

    def send(self, message: Message) -> None:
        self.connection.sendall(encode_message(message, self.limits))

    def receive(self) -> Message | None:
        """Return one message, or None for clean EOF; truncated EOF raises."""
        while not self._pending:
            if self._eof:
                return None
            data = self.connection.recv(65536)
            if not data:
                self._eof = True
                self.decoder.feed_eof()
                return None
            self._pending.extend(self.decoder.feed(data))
        return self._pending.popleft()
