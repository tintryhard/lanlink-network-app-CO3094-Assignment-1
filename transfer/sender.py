"""Streaming source inspection and sender state machine. No network or GUI work.

One worker drives outgoing()/handle_message(); cancellation may cross threads.
Only a validated FILE_RESULT can complete a sender. Live generators are closed
under the same lock that protects next(), so cancellation never leaks a handle.
"""
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import stat
import threading
import time
from typing import Callable, Iterator
from uuid import uuid4

from common.errors import ErrorCode, TransferError, ProtocolError
from common.models import ErrorInfo, Message, TransferDirection, TransferSnapshot, TransferStatus, check_transition
from common.protocol import DEFAULT_LIMITS, ProtocolLimits, MessageType, validate_message
from common.builders import build_file_offer, build_file_chunk, build_file_end, build_file_cancel
from transfer.paths import io_error, sanitize_filename
from transfer.progress import ProgressThrottle, UpdatePublisher


@dataclass(frozen=True)
class FileInfo:
    """Result of inspecting a file to send: path, safe name, size, SHA-256 and modification time."""
    path: Path
    name: str
    size: int
    sha256: str
    mtime_ns: int


@dataclass(frozen=True)
class Chunk:
    """One numbered piece of file content."""
    seq: int
    data: bytes


def _is_cancel_requested(cancel) -> bool:
    """True if cancel (an Event or a callable) asks to stop; None means never."""
    # Duck typing: a threading.Event has is_set(); anything else is called as a function.
    return bool(cancel.is_set() if hasattr(cancel, 'is_set') else cancel()) if cancel is not None else False


def _raise_if_cancelled(cancel) -> None:
    """Raise CANCELLED_LOCAL if cancellation was requested."""
    if _is_cancel_requested(cancel):
        raise TransferError(ErrorCode.CANCELLED_LOCAL)


def _stat_regular_file(path: Path) -> os.stat_result:
    """Return the file's stat, or raise FILE_NOT_FOUND if it is not a regular file."""
    result = path.stat()
    if not stat.S_ISREG(result.st_mode):
        raise TransferError(ErrorCode.FILE_NOT_FOUND)
    return result


def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    """True if two stats describe the same, unmodified file (size, mtime, inode, device)."""
    return (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) == (
        after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev)


def inspect_file(path: Path | str, *, limits: ProtocolLimits = DEFAULT_LIMITS,
                 cancel=None, progress: Callable[[int], None] | None = None) -> FileInfo:
    """Reject oversize before reading, then hash bounded chunks and check stat again."""
    path = Path(path)
    try:
        _raise_if_cancelled(cancel)
        before = _stat_regular_file(path)
        if before.st_size > limits.max_file_bytes:
            raise TransferError(ErrorCode.FILE_TOO_LARGE)
        digest, count = hashlib.sha256(), 0
        with path.open('rb') as source:
            if not _same_file_state(before, os.fstat(source.fileno())):
                raise TransferError(ErrorCode.SOURCE_CHANGED)
            while True:
                _raise_if_cancelled(cancel)
                data = source.read(limits.max_chunk_bytes)
                if not data:
                    break
                digest.update(data)
                count += len(data)
                if count > before.st_size:
                    raise TransferError(ErrorCode.SOURCE_CHANGED)
                if progress:
                    progress(count)
            if not _same_file_state(before, os.fstat(source.fileno())):
                raise TransferError(ErrorCode.SOURCE_CHANGED)
        if count != before.st_size or not _same_file_state(before, _stat_regular_file(path)):
            raise TransferError(ErrorCode.SOURCE_CHANGED)
        return FileInfo(path, sanitize_filename(path.name), count, digest.hexdigest(), before.st_mtime_ns)
    except OSError as error:
        raise io_error(error) from None


def iter_chunks(path: Path | str, *, chunk_size: int, expected: FileInfo | None = None,
                cancel=None) -> Iterator[Chunk]:
    """Lazily read one chunk; verify the source before normal exhaustion/FILE_END."""
    if type(chunk_size) is not int or not 1 <= chunk_size <= 1048576:
        raise TransferError(ErrorCode.INVALID_FIELD)
    path = Path(path)
    try:
        _raise_if_cancelled(cancel)
        before = _stat_regular_file(path)
        if expected and (before.st_size != expected.size or before.st_mtime_ns != expected.mtime_ns):
            raise TransferError(ErrorCode.SOURCE_CHANGED)
        digest, count, seq = hashlib.sha256(), 0, 0
        with path.open('rb') as source:
            if not _same_file_state(before, os.fstat(source.fileno())):
                raise TransferError(ErrorCode.SOURCE_CHANGED)
            while True:
                _raise_if_cancelled(cancel)
                data = source.read(chunk_size)
                if not data:
                    break
                count += len(data)
                if count > before.st_size:
                    raise TransferError(ErrorCode.SOURCE_CHANGED)
                digest.update(data)
                yield Chunk(seq, data)  # generator: pauses here until the caller asks for the next chunk
                seq += 1
            if not _same_file_state(before, os.fstat(source.fileno())):
                raise TransferError(ErrorCode.SOURCE_CHANGED)
        if count != before.st_size or not _same_file_state(before, _stat_regular_file(path)):
            raise TransferError(ErrorCode.SOURCE_CHANGED)
        if expected and digest.hexdigest() != expected.sha256:
            raise TransferError(ErrorCode.SOURCE_CHANGED)
    except OSError as error:
        raise io_error(error, missing=ErrorCode.SOURCE_CHANGED if expected else ErrorCode.FILE_NOT_FOUND) from None


class SendSession:
    """Single-worker session with idempotent, thread-safe cancellation and failure."""
    def __init__(self, info: FileInfo, peer: str, *, limits=DEFAULT_LIMITS,
                 chunk_size: int | None = None, on_update: Callable[[TransferSnapshot], None] = lambda s: None,
                 clock: Callable[[], float] = time.monotonic, accept_timeout_s: float = 60,
                 result_timeout_s: float = 30):
        """Prepare to send `info` to `peer`; the session starts in the OFFERED state."""
        requested = limits.max_chunk_bytes if chunk_size is None else chunk_size
        if type(requested) is not int or requested < 1:
            raise TransferError(ErrorCode.INVALID_FIELD)
        if info.size > limits.max_file_bytes:
            raise TransferError(ErrorCode.FILE_TOO_LARGE)
        self.info, self.peer, self.limits = info, peer, limits
        self.chunk_size = min(requested, limits.max_chunk_bytes)
        self.transfer_id = uuid4().hex
        self.clock, self.on_update = clock, on_update
        self.accept_timeout_s, self.result_timeout_s = accept_timeout_s, result_timeout_s
        self._lock = threading.RLock()  # reentrant: an engine method may call another under the lock
        # The lambda looks up self.on_update at call time, so the callback can be replaced later.
        self._updates = UpdatePublisher(self._lock, lambda snap: self.on_update(snap))
        self._cancel_event = threading.Event()
        self._chunks = None
        self._started = False
        self._last_activity = clock()
        self._throttle = ProgressThrottle(clock=clock)
        self._snapshot = TransferSnapshot(self.transfer_id, TransferDirection.SEND, peer,
                                           info.name, info.size, 0, TransferStatus.OFFERED)
        with self._updates.operation():
            self._queue_snapshot()

    @property
    def snapshot(self) -> TransferSnapshot:
        """Current TransferSnapshot (read under the lock)."""
        with self._lock:
            return self._snapshot

    def _queue_snapshot(self) -> None:
        """Queue the current snapshot for on_update if the throttle lets it through."""
        if self._throttle.should_emit(self._snapshot):
            self._updates.queue(self._snapshot)

    def _change_status(self, status: TransferStatus, **fields) -> None:
        """Validate and apply a status change, then queue the new snapshot."""
        check_transition(self._snapshot.status, status)
        self._snapshot = replace(self._snapshot, status=status, **fields)
        self._last_activity = self.clock()
        self._queue_snapshot()

    def _stop_with_error(self, code: ErrorCode, status: TransferStatus = TransferStatus.FAILED) -> None:
        """Move to a terminal status with an error and stop reading the file. Idempotent."""
        if self._snapshot.is_terminal:
            return
        self._cancel_event.set()
        if self._chunks is not None:
            self._chunks.close()
            self._chunks = None
        self._change_status(status, error=ErrorInfo(code, TransferError(code).message))

    def offer_message(self) -> Message:
        """Build the FILE_OFFER for this transfer (only while OFFERED)."""
        with self._updates.operation():
            if self._snapshot.status != TransferStatus.OFFERED:
                raise TransferError(ErrorCode.TRANSFER_STATE)
            return build_file_offer(self.transfer_id, self.peer, self.info.name, self.info.size,
                                     self.info.sha256, self.chunk_size, limits=self.limits)

    def handle_message(self, msg: Message) -> None:
        """Apply a message from the receiver side: accept, reject, cancel, error or result.

        Only a FILE_RESULT whose size and hash match the source completes the transfer.
        """
        with self._updates.operation():
            if msg.header.get('transfer_id') != self.transfer_id:
                raise TransferError(ErrorCode.TRANSFER_UNKNOWN)
            if self._snapshot.is_terminal:
                return
            try:
                msg = validate_message(msg.header, msg.payload, self.limits)
            except ProtocolError as error:
                self._stop_with_error(error.code)
                raise TransferError(error.code) from None
            h = msg.header
            if msg.type == MessageType.FILE_CANCEL:
                self._stop_with_error(ErrorCode.CANCELLED_BY_PEER, TransferStatus.CANCELLED)
            elif msg.type == MessageType.ERROR:
                self._stop_with_error(ErrorCode(h['code']))
            elif msg.type == MessageType.FILE_ACCEPT and self._snapshot.status == TransferStatus.OFFERED:
                self._change_status(TransferStatus.ACCEPTED)
            elif msg.type == MessageType.FILE_REJECT and self._snapshot.status == TransferStatus.OFFERED:
                self._stop_with_error(ErrorCode.REJECTED_BY_PEER, TransferStatus.REJECTED)
            elif msg.type == MessageType.FILE_RESULT and not h['ok']:
                self._stop_with_error(ErrorCode(h['error_code']))
            elif msg.type == MessageType.FILE_RESULT and self._snapshot.status == TransferStatus.VERIFYING:
                if h['received_bytes'] != self.info.size:
                    self._stop_with_error(ErrorCode.SIZE_MISMATCH)
                elif h.get('sha256', self.info.sha256) != self.info.sha256:
                    self._stop_with_error(ErrorCode.HASH_MISMATCH)
                else:
                    self._change_status(TransferStatus.COMPLETED)
            else:
                self._stop_with_error(ErrorCode.TRANSFER_STATE)
                raise TransferError(ErrorCode.TRANSFER_STATE)

    def outgoing(self) -> Iterator[Message]:
        """Pump incrementally; caller sends each yield before requesting the next."""
        with self._updates.operation():
            if self._snapshot.status != TransferStatus.ACCEPTED or self._started:
                return
            self._started = True
            self._change_status(TransferStatus.TRANSFERRING)
            self._chunks = iter_chunks(self.info.path, chunk_size=self.chunk_size,
                                       expected=self.info, cancel=self._cancel_event)
        count = 0
        try:
            while True:
                with self._updates.operation():
                    if self._snapshot.is_terminal:
                        return
                    try:
                        chunk = next(self._chunks)
                    except StopIteration:
                        self._chunks = None
                        self._change_status(TransferStatus.VERIFYING)
                        message = build_file_end(self.transfer_id, count, self.info.size, limits=self.limits)
                        break
                    self._snapshot = replace(self._snapshot, processed_bytes=self._snapshot.processed_bytes + len(chunk.data))
                    self._last_activity = self.clock()
                    self._queue_snapshot()
                    message = build_file_chunk(self.transfer_id, chunk.seq, chunk.data, limits=self.limits)
                    count += 1
                # An observer may have cancelled when the operation released
                # its lock. Do not hand that pending frame to the transport.
                with self._lock:
                    if self._snapshot.is_terminal:
                        return
                yield message
            with self._lock:
                if self._snapshot.is_terminal:
                    return
            yield message
        except TransferError as error:
            with self._updates.operation():
                if not self._snapshot.is_terminal:
                    self._stop_with_error(error.code)
            yield build_file_cancel(self.transfer_id, error.code)
        finally:
            with self._updates.operation():
                if self._chunks is not None:
                    self._chunks.close()
                    self._chunks = None
                    if not self._snapshot.is_terminal:
                        self._stop_with_error(ErrorCode.CANCELLED_LOCAL, TransferStatus.CANCELLED)

    def cancel(self) -> Message | None:
        """Cancel locally and return the FILE_CANCEL to send, or None if already finished."""
        with self._updates.operation():
            if self._snapshot.is_terminal:
                return None
            self._stop_with_error(ErrorCode.CANCELLED_LOCAL, TransferStatus.CANCELLED)
            return build_file_cancel(self.transfer_id, ErrorCode.CANCELLED_LOCAL)

    def abort(self, error: TransferError | None = None) -> None:
        """Fail the transfer locally without sending anything."""
        with self._updates.operation():
            self._stop_with_error(error.code if error else ErrorCode.INTERNAL)

    def connection_lost(self) -> None:
        """Fail the transfer because the connection to the server dropped."""
        with self._updates.operation():
            self._stop_with_error(ErrorCode.CONNECTION_LOST)

    def check_timeout(self) -> None:
        """Fail with TIMEOUT if there was no activity for too long."""
        with self._updates.operation():
            status = self._snapshot.status
            timeout = self.accept_timeout_s if status == TransferStatus.OFFERED else self.result_timeout_s
            if not self._snapshot.is_terminal and self.clock() - self._last_activity >= timeout:
                self._stop_with_error(ErrorCode.TIMEOUT)
