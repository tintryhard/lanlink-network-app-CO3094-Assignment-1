"""Stage 8: CLI progress bar, user list and connection status."""
from contextlib import ExitStack
import queue

from client.__main__ import format_progress, format_users
from client.private_chat import PrivateChatClient
from common.models import MessageType, TransferDirection, TransferSnapshot, TransferStatus
from tests.integration.test_concurrency import running
from tests.integration.test_file_negotiation import event
from tests.integration.test_private_chat import accounts, async_login, login  # noqa: F401


def test_progress_bar_text():
    snapshot = TransferSnapshot('t1', TransferDirection.SEND, 'bob', 'a.txt', 1000, 250,
                                TransferStatus.TRANSFERRING)
    assert format_progress(snapshot, width=8) == '[##......]  25% a.txt to bob 250/1000 B'


def test_user_list_marks_current_user():
    users = [{'username': 'alice', 'ip': '127.0.0.1', 'port': 5001},
             {'username': 'bob', 'ip': '127.0.0.1', 'port': 5002}]
    text = format_users(users, 'alice')
    assert text.splitlines()[0] == 'Online users (2):'
    assert 'alice' in text.splitlines()[1] and text.splitlines()[1].endswith('(you)')
    assert not text.splitlines()[2].endswith('(you)')


def test_sender_reports_progress_before_completion(accounts, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'big.bin'
    source.write_bytes(b'x' * 2_000_000)
    progress = queue.Queue()
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, transport = login(stack, server, accounts, 'alice')
        alice = PrivateChatClient(transport, timeout=3, on_message=lambda m: None,
                                  on_progress=progress.put_nowait)
        stack.callback(alice.close)
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = alice.offer_file('bob', source)
        event(bob, MessageType.FILE_OFFER, transfer_id)
        bob.accept_file(transfer_id)
        event(alice, MessageType.FILE_ACCEPT, transfer_id)
        assert alice.transfer_events.get(timeout=10).status == TransferStatus.COMPLETED
    snapshots = list(progress.queue)
    assert snapshots and all(not s.is_terminal for s in snapshots)
    assert all(s.transfer_id == transfer_id for s in snapshots)
