"""Deterministic snapshot throttling; state changes and the final byte always pass."""
import time
import logging
from collections import deque
from contextlib import contextmanager
from typing import Callable
from common.models import TransferSnapshot


class UpdatePublisher:
    """Queue under the engine lock; invoke observers only after its outer operation.

    Nested engine calls share the operation depth. One drainer preserves order;
    reentrant/concurrent cancellation can queue a terminal update without waiting
    for a slow observer. Exceptions in observers cannot interrupt disk cleanup.
    """
    def __init__(self, lock, callback):
        """`lock` is the engine's lock; `callback` receives each delivered snapshot."""
        self.lock, self.callback = lock, callback
        self._depth = 0
        self._pending = deque()
        self._delivering = False
        self.callback_failures = 0

    # @contextmanager turns this generator into a `with` block: code before `yield`
    # runs on entry, code after it on exit.
    @contextmanager
    def operation(self):
        """Hold the engine lock for one state change; deliver queued updates after release.

        Nested operations share a depth counter, so only the outermost one flushes.
        """
        outer = False
        try:
            with self.lock:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                    outer = self._depth == 0
        finally:
            if outer:
                self.flush()

    def queue(self, snapshot):
        """Add a snapshot to deliver later; a newer one with the same status replaces it."""
        # Progress may be coalesced while an observer is busy; state changes stay.
        if self._pending and self._pending[-1].status == snapshot.status:
            self._pending[-1] = snapshot
        else:
            self._pending.append(snapshot)

    def flush(self):
        """Deliver queued snapshots in order, outside the lock, from a single thread.

        If another thread is already delivering, it will also deliver ours. Errors in
        the callback are counted and logged once, never raised into the engine.
        """
        with self.lock:
            if self._delivering:
                return
            self._delivering = True
        try:
            while True:
                with self.lock:
                    if not self._pending:
                        self._delivering = False
                        return
                    snapshot = self._pending.popleft()
                try:
                    self.callback(snapshot)
                except Exception:
                    self.callback_failures += 1
                    if self.callback_failures == 1:
                        logging.getLogger(__name__).error('Transfer update callback failed')
        except BaseException:
            with self.lock:
                self._delivering = False
            raise


class ProgressThrottle:
    """Limit how often progress snapshots reach the UI (status changes always pass)."""
    def __init__(self, min_interval_s: float = 0.1, min_delta_bytes: int = 1,
                 *, clock: Callable[[], float] = time.monotonic):
        """Report progress at most every `min_interval_s` and after `min_delta_bytes` new bytes."""
        if min_interval_s < 0 or min_delta_bytes < 0:
            raise ValueError('Progress thresholds must be nonnegative')
        self.min_interval_s = min_interval_s
        self.min_delta_bytes = min_delta_bytes
        self.clock = clock
        self._last = None
        self._time = 0.0

    def should_emit(self, snapshot: TransferSnapshot) -> bool:
        """Decide whether this progress snapshot is worth reporting.

        Status changes and the final byte always pass; other progress needs at least
        min_interval_s and min_delta_bytes since the last report. Nothing passes
        after a terminal snapshot.
        """
        now = self.clock()
        previous = self._last
        if previous and (previous.is_terminal or snapshot.transfer_id != previous.transfer_id
                         or snapshot.processed_bytes < previous.processed_bytes):
            return False
        force = (previous is None or previous.status != snapshot.status
                 or (snapshot.processed_bytes == snapshot.total_bytes
                     and previous.processed_bytes != snapshot.processed_bytes))
        changed = previous != snapshot
        enough = (now - self._time >= self.min_interval_s
                  and snapshot.processed_bytes - previous.processed_bytes >= self.min_delta_bytes) if previous else True
        if force or (changed and enough):
            self._last, self._time = snapshot, now
            return True
        return False
