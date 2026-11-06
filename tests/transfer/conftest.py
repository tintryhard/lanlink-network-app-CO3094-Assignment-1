"""Every disk test must release owned parts, placeholders and descriptors."""
from pathlib import Path
import pytest
from transfer.receiver import Receiver


@pytest.fixture(autouse=True)
def no_partial_files(tmp_path, monkeypatch):
    receivers = []
    original = Receiver.__init__
    def record(receiver, *args, **kwargs):
        original(receiver, *args, **kwargs)
        receivers.append(receiver)
    monkeypatch.setattr(Receiver, '__init__', record)
    yield
    # A successfully received file may legitimately end in .part. Only exact
    # completed paths are exempt; leaked temporary files still fail this check.
    saved = {Path(r.snapshot.saved_path).resolve() for r in receivers if r.snapshot.saved_path}
    assert not [p for p in tmp_path.rglob('*.part') if p.resolve() not in saved]
    assert all(r._fd is None and r.part_path is None and r._reserved is None for r in receivers)
