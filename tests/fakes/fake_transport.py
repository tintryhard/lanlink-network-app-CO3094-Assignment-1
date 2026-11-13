"""FIXTURE — NOT PART OF THE PRODUCT: bounded in-memory relay with wire faults."""
from dataclasses import replace
from typing import Callable

from common.models import Message
from common.protocol import Direction, FrameDecoder, MessageType, encode_message
from common.builders import build_file_result
from common.errors import TransferError
from transfer.receiver import IncomingOffer, Receiver


class FakeRelay:
    """Serialise every hop; retain type names only, never an entire file's chunks."""
    def __init__(self, sender, download_dir, *, drop_after: int | None = None,
                 flip_chunk: int | None = None, duplicate_chunk: int | None = None,
                 skip_chunk: int | None = None, reorder: bool = False,
                 cancel_at: int | None = None, cancel_side: str = 'sender',
                 delay: Callable[[Message], None] | None = None, receiver_updates=lambda s: None):
        self.sender = sender
        self.drop_after, self.flip_chunk = drop_after, flip_chunk
        self.duplicate_chunk, self.skip_chunk, self.reorder = duplicate_chunk, skip_chunk, reorder
        self.cancel_at, self.cancel_side, self.delay = cancel_at, cancel_side, delay
        self.messages = 0
        self.types = []
        self.dropped = False
        offer = self._wire(sender.offer_message(), Direction.CLIENT_TO_SERVER)
        offer = Message({**offer.header, 'sender': 'fixture_sender'}, offer.payload)
        offer = self._wire(offer, Direction.SERVER_TO_CLIENT)
        self.receiver = Receiver(IncomingOffer.from_message(offer, limits=sender.limits), download_dir,
                                 limits=sender.limits, clock=sender.clock, on_update=receiver_updates)

    def _wire(self, msg: Message, direction: Direction) -> Message:
        frame = encode_message(msg, self.sender.limits)
        decoder = FrameDecoder(self.sender.limits, direction)
        got = decoder.feed(frame[:3]) + decoder.feed(frame[3:])
        assert decoder.at_clean_boundary() and len(got) == 1
        return got[0]

    def _deliver(self, msg: Message) -> None:
        if self.drop_after is not None and self.messages >= self.drop_after:
            self.dropped = True
            self.sender.connection_lost()
            self.receiver.connection_lost()
            return
        self.messages += 1
        self.types.append(msg.type)
        if self.delay:
            self.delay(msg)
        if self.cancel_at == self.messages:
            local = self.sender if self.cancel_side == 'sender' else self.receiver
            remote = self.receiver if self.cancel_side == 'sender' else self.sender
            cancel = local.cancel()
            if cancel:
                remote.handle_message(self._wire(cancel, Direction.SERVER_TO_CLIENT))
            return
        incoming = self._wire(msg, Direction.SERVER_TO_CLIENT)
        for reply in self.receiver.handle_message(incoming):
            reply = self._wire(reply, Direction.CLIENT_TO_SERVER)
            self.sender.handle_message(self._wire(reply, Direction.SERVER_TO_CLIENT))

    def run(self, *, accept: bool = True):
        if not accept:
            self.sender.handle_message(self._wire(self.receiver.reject(), Direction.SERVER_TO_CLIENT))
            return self.receiver
        try:
            response = self.receiver.accept()
        except TransferError as error:
            response = build_file_result(self.sender.transfer_id, False, 0, error_code=error.code, limits=self.sender.limits)
        self.sender.handle_message(self._wire(response, Direction.SERVER_TO_CLIENT))
        held = None
        outgoing = self.sender.outgoing()
        try:
            for msg in outgoing:
                if msg.type == MessageType.FILE_CHUNK:
                    seq = msg.header['seq']
                    if seq == self.skip_chunk:
                        continue
                    if seq == self.flip_chunk:
                        msg = Message(msg.header, bytes([msg.payload[0] ^ 1]) + msg.payload[1:])
                    if self.reorder and seq == 0:
                        held = msg
                        continue
                    self._deliver(msg)
                    if held:
                        self._deliver(held)
                        held = None
                    if seq == self.duplicate_chunk:
                        self._deliver(msg)
                else:
                    self._deliver(msg)
                if self.dropped or self.sender.snapshot.is_terminal:
                    break
        finally:
            outgoing.close()
        return self.receiver
