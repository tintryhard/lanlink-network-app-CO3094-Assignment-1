"""Streaming receive to an exclusive .part; publish only after counts/hash/fsync.

One worker writes. A lock serialises write/finish with cross-thread abort and
connection loss. Reservations and partial files belong to this receiver only.
"""
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Callable

from common.errors import ErrorCode, ProtocolError, TransferError
from common.models import ErrorInfo, Message, TransferDirection, TransferSnapshot, TransferStatus, check_transition
from common.protocol import (DEFAULT_LIMITS, MessageType, build_file_accept, build_file_reject,
                             build_file_result, build_file_cancel, validate_message)
from transfer.paths import fit_filename, io_error, reserve_destination, sanitize_filename
from transfer.progress import ProgressThrottle, UpdatePublisher


@dataclass(frozen=True)
class IncomingOffer:
    transfer_id: str
    sender: str
    filename: str
    size: int
    sha256: str
    chunk_size: int

    @classmethod
    def from_message(cls, msg: Message, *, limits=DEFAULT_LIMITS) -> 'IncomingOffer':
        from common.protocol import Direction
        checked = validate_message(msg.header, msg.payload, limits, Direction.SERVER_TO_CLIENT)
        if checked.type != MessageType.FILE_OFFER:
            raise TransferError(ErrorCode.TRANSFER_STATE)
        return cls(*(checked.header[name] for name in cls.__dataclass_fields__))


class Receiver:
    def __init__(self, offer: IncomingOffer, download_dir: Path | str, *, limits=DEFAULT_LIMITS,
                 on_update: Callable[[TransferSnapshot], None] = lambda s: None,
                 clock: Callable[[], float] = time.monotonic, idle_timeout_s: float = 30):
        self.offer, self.download_dir, self.limits = offer, Path(download_dir), limits
        self.clock, self.on_update, self.idle_timeout_s = clock, on_update, idle_timeout_s
        self._lock = threading.RLock()
        self._updates = UpdatePublisher(self._lock, lambda snap: self.on_update(snap))
        self._fd = None
        self._part = None
        self._reserved = None
        self._reservation_identity = None
        self._hash = hashlib.sha256()
        self._seq = 0
        self._last_activity = clock()
        self._throttle = ProgressThrottle(clock=clock)
        self._snapshot = TransferSnapshot(offer.transfer_id, TransferDirection.RECEIVE, offer.sender,
                                           offer.filename, offer.size, 0, TransferStatus.OFFERED)
        with self._updates.operation():
            self._publish()

    @property
    def snapshot(self) -> TransferSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def part_path(self) -> Path | None:
        return self._part

    def _publish(self) -> None:
        if self._throttle.should_emit(self._snapshot):
            self._updates.queue(self._snapshot)

    def _set(self, status: TransferStatus, **fields) -> None:
        check_transition(self._snapshot.status, status)
        self._snapshot = replace(self._snapshot, status=status, **fields)
        self._last_activity = self.clock()
        self._publish()

    def _owns_reservation(self) -> bool:
        if self._reserved is None:
            return False
        try:
            st = self._reserved.lstat()
            return (st.st_dev, st.st_ino) == self._reservation_identity
        except OSError:
            return False

    def _cleanup(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        paths = [self._part]
        if self._owns_reservation():
            paths.append(self._reserved)
        for path in paths:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # Best effort if the directory itself became inaccessible.
        self._part = self._reserved = None

    def _fail(self, error: TransferError, status: TransferStatus = TransferStatus.FAILED) -> None:
        if self._snapshot.is_terminal:
            return
        self._cleanup()
        self._set(status, error=ErrorInfo(error.code, error.message))

    def start(self) -> None:
        """Create the directory, reserve final name and exclusively open a private part."""
        with self._updates.operation():
            if self._snapshot.status != TransferStatus.OFFERED:
                raise TransferError(ErrorCode.TRANSFER_STATE)
            try:
                if type(self.offer.size) is not int or self.offer.size < 0:
                    raise TransferError(ErrorCode.INVALID_FIELD)
                if self.offer.size > self.limits.max_file_bytes:
                    raise TransferError(ErrorCode.FILE_TOO_LARGE)
                if type(self.offer.chunk_size) is not int or not 1 <= self.offer.chunk_size <= self.limits.max_chunk_bytes:
                    raise TransferError(ErrorCode.INVALID_FIELD)
                # Directly constructed offers are checked too; no path work before this.
                from common.protocol import HASH_RE, TRANSFER_RE
                if not isinstance(self.offer.transfer_id, str) or not TRANSFER_RE.fullmatch(self.offer.transfer_id):
                    raise TransferError(ErrorCode.INVALID_FIELD)
                if not isinstance(self.offer.sha256, str) or not HASH_RE.fullmatch(self.offer.sha256):
                    raise TransferError(ErrorCode.INVALID_FIELD)
                name = sanitize_filename(self.offer.filename)
                try:
                    self.download_dir.mkdir(parents=True, exist_ok=True)
                    self.download_dir = self.download_dir.resolve()
                except OSError:
                    raise TransferError(ErrorCode.PERMISSION_DENIED) from None
                try:
                    if shutil.disk_usage(self.download_dir).free < self.offer.size:
                        raise TransferError(ErrorCode.DISK_FULL)
                except OSError:
                    pass  # Space check is advisory; every write is still checked.
                self._reserved, fd = reserve_destination(self.download_dir, name)
                try:
                    st = os.fstat(fd)
                    self._reservation_identity = (st.st_dev, st.st_ino)
                finally:
                    os.close(fd)
                part_name = fit_filename(self._reserved.name, 255 - len(f'.{self.offer.transfer_id[:8]}.part'.encode()))
                part = self.download_dir / f'{part_name}.{self.offer.transfer_id[:8]}.part'
                self._fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                   | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                self._part = part
                self._set(TransferStatus.ACCEPTED)
            except (OSError, TransferError) as error:
                typed = io_error(error, missing=ErrorCode.PERMISSION_DENIED) if isinstance(error, OSError) else error
                self._fail(typed)
                raise typed from None

    def accept(self) -> Message:
        self.start()
        return build_file_accept(self.offer.transfer_id)

    def reject(self, reason: str | None = None) -> Message:
        with self._updates.operation():
            if self._snapshot.status != TransferStatus.OFFERED:
                raise TransferError(ErrorCode.TRANSFER_STATE)
            message = build_file_reject(self.offer.transfer_id, reason)
            self._set(TransferStatus.REJECTED)
            return message

    def write(self, seq: int, data: bytes) -> None:
        with self._updates.operation():
            if self._snapshot.status not in {TransferStatus.ACCEPTED, TransferStatus.TRANSFERRING}:
                raise TransferError(ErrorCode.TRANSFER_STATE)
            try:
                if type(seq) is not int or seq != self._seq:
                    raise TransferError(ErrorCode.SEQ_MISMATCH)
                if not isinstance(data, bytes) or not data:
                    raise TransferError(ErrorCode.SIZE_MISMATCH)
                size = self._snapshot.processed_bytes + len(data)
                if (size > self.offer.size or len(data) > self.offer.chunk_size
                        or (len(data) != self.offer.chunk_size and size != self.offer.size)):
                    raise TransferError(ErrorCode.SIZE_MISMATCH)
                if self._snapshot.status == TransferStatus.ACCEPTED:
                    self._set(TransferStatus.TRANSFERRING)
                view = memoryview(data)
                while view:
                    written = os.write(self._fd, view)
                    if written <= 0:
                        raise TransferError(ErrorCode.INTERNAL)
                    view = view[written:]
                self._hash.update(data)
                self._seq += 1
                self._snapshot = replace(self._snapshot, processed_bytes=size)
                self._last_activity = self.clock()
                self._publish()
            except (TransferError, OSError) as error:
                typed = io_error(error) if isinstance(error, OSError) else error
                self._fail(typed)
                raise typed from None

    def _result(self) -> Message:
        error = self._snapshot.error
        return build_file_result(self.offer.transfer_id, self._snapshot.status == TransferStatus.COMPLETED,
                                  self._snapshot.processed_bytes, self._hash.hexdigest(),
                                  error.code if error else None, limits=self.limits)

    def finish(self, total_chunks: int, total_bytes: int) -> Message:
        """No success before exact counts, digest, durable close and atomic replace."""
        with self._updates.operation():
            if self._snapshot.status not in {TransferStatus.ACCEPTED, TransferStatus.TRANSFERRING}:
                raise TransferError(ErrorCode.TRANSFER_STATE)
            if self._snapshot.status == TransferStatus.ACCEPTED:
                self._set(TransferStatus.TRANSFERRING)
            self._set(TransferStatus.VERIFYING)
            try:
                if (type(total_chunks) is not int or type(total_bytes) is not int
                        or total_chunks != self._seq or total_bytes != self.offer.size
                        or total_bytes != self._snapshot.processed_bytes):
                    raise TransferError(ErrorCode.SIZE_MISMATCH)
                if self._hash.hexdigest() != self.offer.sha256:
                    raise TransferError(ErrorCode.HASH_MISMATCH)
                os.fsync(self._fd)  # os.write is unbuffered, so no Python flush is needed.
                os.close(self._fd)
                self._fd = None
                if not self._owns_reservation():
                    raise TransferError(ErrorCode.PERMISSION_DENIED)
                os.replace(self._part, self._reserved)
                saved_path = str(self._reserved)
                self._part = self._reserved = None
                self._set(TransferStatus.COMPLETED, saved_path=saved_path)
            except (TransferError, OSError) as error:
                self._fail(io_error(error) if isinstance(error, OSError) else error)
            return self._result()

    def handle_message(self, msg: Message) -> list[Message]:
        with self._updates.operation():
            if msg.header.get('transfer_id') != self.offer.transfer_id:
                raise TransferError(ErrorCode.TRANSFER_UNKNOWN)
            if self._snapshot.is_terminal:
                return []
            try:
                msg = validate_message(msg.header, msg.payload, self.limits)
                h = msg.header
                if msg.type == MessageType.FILE_CHUNK:
                    self.write(h['seq'], msg.payload)
                elif msg.type == MessageType.FILE_END:
                    return [self.finish(h['total_chunks'], h['total_bytes'])]
                elif msg.type == MessageType.FILE_CANCEL:
                    self._fail(TransferError(ErrorCode.CANCELLED_BY_PEER), TransferStatus.CANCELLED)
                elif msg.type == MessageType.ERROR:
                    self._fail(TransferError(ErrorCode(h['code'])))
                else:
                    raise TransferError(ErrorCode.TRANSFER_STATE)
            except (TransferError, ProtocolError) as error:
                self._fail(TransferError(error.code))
                return [self._result()]
            return []

    def abort(self, error: TransferError | None = None) -> None:
        with self._updates.operation():
            error = error or TransferError(ErrorCode.CANCELLED_LOCAL)
            status = TransferStatus.CANCELLED if error.code in {ErrorCode.CANCELLED_LOCAL, ErrorCode.CANCELLED_BY_PEER} else TransferStatus.FAILED
            self._fail(error, status)

    def cancel(self) -> Message | None:
        with self._updates.operation():
            if self._snapshot.is_terminal:
                return None
            self.abort()
            return build_file_cancel(self.offer.transfer_id, ErrorCode.CANCELLED_LOCAL)

    def connection_lost(self) -> None:
        self.abort(TransferError(ErrorCode.CONNECTION_LOST))

    def check_timeout(self) -> None:
        with self._updates.operation():
            if not self._snapshot.is_terminal and self._snapshot.status != TransferStatus.OFFERED and self.clock() - self._last_activity >= self.idle_timeout_s:
                self._fail(TransferError(ErrorCode.TIMEOUT))
