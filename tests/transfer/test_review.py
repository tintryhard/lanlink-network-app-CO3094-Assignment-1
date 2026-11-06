"""Adversarial lifecycle, callback and filesystem regressions."""
from dataclasses import replace
from pathlib import Path
import threading
import os
import gc
import hashlib
import random
import time

import pytest

from common.models import TransferStatus as S
from common.models import MessageType
from transfer.paths import sanitize_filename
from transfer.progress import ProgressThrottle
from common.errors import ErrorCode as E, TransferError
from common.protocol import (build_file_accept, build_file_end, build_file_result,
                             build_error, build_file_chunk)
from tests.fakes.fake_transport import FakeRelay
from tests.transfer.test_engine import session


@pytest.mark.parametrize('endpoint', ['sender', 'receiver'])
def test_raising_callback_cannot_break_transfer(tmp_path, endpoint):
    sender = session(tmp_path, 1)
    relay = FakeRelay(sender, tmp_path / 'd')
    engine = sender if endpoint == 'sender' else relay.receiver
    def broken(snapshot):
        raise RuntimeError('observer failure must not break disk cleanup')
    engine.on_update = broken
    try:
        relay.run()
        assert sender.snapshot.status == relay.receiver.snapshot.status == S.COMPLETED
    finally:
        sender.connection_lost()
        relay.receiver.connection_lost()


@pytest.mark.parametrize('endpoint', ['sender', 'receiver'])
def test_callback_can_wait_for_cross_thread_cancel(tmp_path, endpoint):
    sender = session(tmp_path, 1)
    relay = FakeRelay(sender, tmp_path / 'd')
    engine = sender if endpoint == 'sender' else relay.receiver
    blocked = []
    workers = []
    def callback(snapshot):
        if snapshot.status == S.ACCEPTED:
            thread = threading.Thread(target=engine.cancel)
            workers.append(thread)
            thread.start()
            thread.join(1)
            blocked.append(thread.is_alive())
    engine.on_update = callback
    try:
        if endpoint == 'sender':
            sender.handle_message(build_file_accept(sender.transfer_id))
        else:
            relay.receiver.accept()
        for worker in workers:
            worker.join(2)
        assert blocked == [False], 'callback ran while holding engine lock'
        assert engine.snapshot.status == S.CANCELLED
        assert not list((tmp_path / 'd').glob('*'))
    finally:
        sender.connection_lost()
        relay.receiver.connection_lost()


def test_reentrant_receiver_cancel_does_not_write_closed_fd(tmp_path):
    sender = session(tmp_path, 1)
    relay = FakeRelay(sender, tmp_path / 'd')
    receiver = relay.receiver
    receiver.accept()
    receiver.on_update = lambda snap: receiver.cancel() if snap.status == S.TRANSFERRING else None
    receiver.write(0, b'x')
    assert receiver.snapshot.status == S.CANCELLED
    assert receiver._fd is None and not list((tmp_path / 'd').iterdir())


@pytest.mark.parametrize('name', ['safe\u202etxt.exe', 'zero\u200bwidth.txt', 'COM\u00b9.log', 'LPT\u00b2.txt'])
def test_names_cannot_disguise_extensions_or_target_windows_devices(name):
    safe = sanitize_filename(name)
    assert '\u202e' not in safe and '\u200b' not in safe
    assert not safe.upper().startswith(('COM\u00b9', 'LPT\u00b2'))


def test_throttle_never_regresses_after_terminal(tmp_path):
    snap = session(tmp_path, 1).snapshot
    throttle = ProgressThrottle(clock=lambda: 0)
    done = replace(snap, status=S.CANCELLED)
    assert throttle.should_emit(snap)
    assert throttle.should_emit(done)
    assert not throttle.should_emit(snap)


@pytest.mark.parametrize('stage', ['offered', 'accepted', 'transferring', 'verifying'])
@pytest.mark.parametrize('action', ['cancel', 'connection_lost', 'timeout', 'error'])
def test_sender_all_live_stages_terminate_once(tmp_path, stage, action):
    now = [0.0]
    updates = []
    sender = session(tmp_path, 3, chunk_size=1, clock=lambda: now[0], on_update=updates.append)
    outgoing = None
    if stage != 'offered':
        sender.handle_message(build_file_accept(sender.transfer_id))
    if stage in {'transferring', 'verifying'}:
        outgoing = sender.outgoing()
        next(outgoing)
    if stage == 'verifying':
        list(outgoing)
    if action == 'error':
        sender.handle_message(build_error(E.CONNECTION_LOST, 'lost', transfer_id=sender.transfer_id))
    elif action == 'timeout':
        now[0] = 100
        sender.check_timeout()
    else:
        getattr(sender, action)()
    terminal = sender.snapshot
    sender.connection_lost(); sender.cancel(); sender.check_timeout()
    sender.handle_message(build_file_result(sender.transfer_id, True, 3, sender.info.sha256))
    if outgoing:
        outgoing.close()
    assert sender.snapshot == terminal and terminal.is_terminal
    assert terminal.status != S.COMPLETED
    assert sum(s.is_terminal for s in updates) == 1 and sender._chunks is None
    sender.info.path.unlink()  # also proves immediate handle closure on Windows


@pytest.mark.parametrize('endpoint', ['sender', 'receiver'])
def test_250_cancel_loss_timeout_message_races(tmp_path, endpoint):
    rng = random.Random(3094)
    for iteration in range(250):
        root = tmp_path / str(iteration)
        root.mkdir()
        sender = session(root, 4, chunk_size=1)
        relay = FakeRelay(sender, root / 'd')
        updates = []
        engine = sender if endpoint == 'sender' else relay.receiver
        engine.on_update = updates.append
        sender.handle_message(relay.receiver.accept())
        engine.clock = lambda: 1000000000000
        message = (build_file_result(sender.transfer_id, False, 0, error_code=E.CONNECTION_LOST)
                   if endpoint == 'sender' else build_file_chunk(sender.transfer_id, 0, b'x'))
        errors = []
        operations = [engine.cancel, engine.connection_lost, engine.check_timeout,
                      lambda: engine.handle_message(message)]
        rng.shuffle(operations)
        gate = threading.Barrier(4)
        def run(operation, delay):
            try:
                gate.wait(timeout=3)
                time.sleep(delay)
                operation()
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run, args=(operation, rng.random() / 10000))
                   for operation in operations]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        assert not errors and not any(t.is_alive() for t in threads)
        assert engine.snapshot.is_terminal and engine.snapshot.status != S.COMPLETED
        assert sum(s.is_terminal for s in updates) == 1
        sender.connection_lost(); relay.receiver.connection_lost()
        assert sender._chunks is None and relay.receiver._fd is None
        assert not list((root / 'd').iterdir())
    gc.collect()


def test_preserved_size_mtime_edit_is_caught_by_streaming_digest(tmp_path):
    sender = session(tmp_path, 20000)
    before = sender.info.path.stat()
    with sender.info.path.open('r+b') as file:
        file.seek(10000); file.write(b'changed')
    os.utime(sender.info.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert sender.info.path.stat().st_mtime_ns == before.st_mtime_ns
    assert sender.info.path.stat().st_size == before.st_size
    receiver = FakeRelay(sender, tmp_path / 'd').run()
    assert sender.snapshot.error.code == E.SOURCE_CHANGED
    assert receiver.snapshot.status == S.CANCELLED


def test_replaced_source_with_same_size_mtime_is_caught(tmp_path):
    sender = session(tmp_path, 4)
    before = sender.info.path.stat()
    other = tmp_path / 'replacement'
    other.write_bytes(b'xxxx')
    os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(other, sender.info.path)
    receiver = FakeRelay(sender, tmp_path / 'd').run()
    assert sender.snapshot.error.code == E.SOURCE_CHANGED
    assert receiver.snapshot.status == S.CANCELLED


def test_foreign_and_early_messages_never_succeed(tmp_path):
    sender = session(tmp_path, 1)
    receiver = FakeRelay(sender, tmp_path / 'd').receiver
    with pytest.raises(TransferError): receiver.handle_message(build_file_end('f' * 32, 0, 0))
    receiver.accept()
    result = receiver.handle_message(build_file_end(sender.transfer_id, 0, 0))[0]
    assert not result.header['ok'] and receiver.snapshot.error.code == E.SIZE_MISMATCH
    with pytest.raises(TransferError):
        sender.handle_message(build_file_result(sender.transfer_id, True, 1, sender.info.sha256))
    assert sender.snapshot.status == S.FAILED


def test_reservation_replacement_and_preexisting_part_are_preserved(tmp_path):
    sender = session(tmp_path, 0)
    receiver = FakeRelay(sender, tmp_path / 'd').receiver
    receiver.accept()
    reserved = receiver._reserved
    # Keep original reservation allocated so filesystem inode reuse cannot
    # masquerade as the same file on platforms with immediate inode recycling.
    moved = reserved.with_name('original-reservation')
    reserved.rename(moved)
    reserved.write_bytes(b'foreign')
    result = receiver.finish(0, 0)
    assert not result.header['ok'] and reserved.read_bytes() == b'foreign'
    moved.unlink(); reserved.unlink()
    part = tmp_path / 'd' / f'{sender.info.name}.{sender.transfer_id[:8]}.part'
    part.write_bytes(b'foreign-part')
    receiver = FakeRelay(sender, tmp_path / 'd').receiver
    with pytest.raises(TransferError): receiver.accept()
    assert part.read_bytes() == b'foreign-part'
    part.unlink()


def test_successful_file_named_part_is_not_a_temporary_leak(tmp_path):
    sender = session(tmp_path, 0)
    relay = FakeRelay(sender, tmp_path / 'd')
    relay.receiver.offer = replace(relay.receiver.offer, filename='archive.part')
    receiver = relay.run()
    assert receiver.snapshot.status == S.COMPLETED
    assert receiver.snapshot.saved_path.endswith('archive.part')


def test_normalised_sanitised_names_collide_without_overwrite(tmp_path):
    from transfer.paths import reserve_destination
    directory = tmp_path / 'download space \u0111'
    for name in ['a\u0301.txt', '\u00e1.txt', 'x?.txt', 'x*.txt', 'z' * 256, '\u00e1' * 128]:
        path, fd = reserve_destination(directory, name)
        try: os.write(fd, b'keep')
        finally: os.close(fd)
        assert path.parent == directory.resolve() and len(path.name.encode()) <= 255
    paths = list(directory.iterdir())
    assert len(paths) == 6 and all(path.read_bytes() == b'keep' for path in paths)


def test_relative_download_directory_is_resolved(tmp_path, monkeypatch):
    sender = session(tmp_path, 0)
    monkeypatch.chdir(tmp_path)
    receiver = FakeRelay(sender, 'relative download \u0111').run()
    assert receiver.snapshot.status == S.COMPLETED
    assert Path(receiver.snapshot.saved_path).is_relative_to(tmp_path)


@pytest.mark.parametrize('cancel_at', ['last_chunk', 'verifying'])
def test_callback_cancellation_suppresses_pending_outgoing_frame(tmp_path, cancel_at):
    sender = session(tmp_path, 1)
    sender.handle_message(build_file_accept(sender.transfer_id))
    def cancel(snapshot):
        if ((cancel_at == 'last_chunk' and snapshot.status == S.TRANSFERRING
             and snapshot.processed_bytes == 1)
                or (cancel_at == 'verifying' and snapshot.status == S.VERIFYING)):
            sender.cancel()
    sender.on_update = cancel
    messages = list(sender.outgoing())
    assert sender.snapshot.status == S.CANCELLED and sender._chunks is None
    assert not any(m.type == MessageType.FILE_END for m in messages)
    if cancel_at == 'last_chunk':
        assert not messages
