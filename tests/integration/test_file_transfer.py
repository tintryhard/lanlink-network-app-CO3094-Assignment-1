"""Stage 5B: file contents are chunked, relayed by the server and verified."""
from contextlib import ExitStack
import hashlib
import os
from uuid import uuid4

import pytest

from common.errors import ErrorCode
from common.models import MessageType, TransferStatus
from common.protocol import DEFAULT_LIMITS, build_file_chunk, build_file_offer, build_file_result
from tests.integration.test_concurrency import running, wait_until
from tests.integration.test_file_negotiation import event, offer
from tests.integration.test_private_chat import accounts, async_login


@pytest.fixture(autouse=True)
def isolated_downloads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def finished(client):
    """Next event that is the final TransferSnapshot (skips decision messages)."""
    while True:
        item = client.transfer_events.get(timeout=5)
        if not hasattr(item, 'type'):
            return item


@pytest.mark.parametrize('size', [0, 1, 3 * DEFAULT_LIMITS.max_chunk_bytes + 123])
def test_binary_file_arrives_intact(accounts, tmp_path, size):
    source = tmp_path / 'data.bin'
    source.write_bytes(os.urandom(size))
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = alice.offer_file('bob', source)
        event(bob, MessageType.FILE_OFFER, transfer_id)
        bob.accept_file(transfer_id)
        sent, received = finished(alice), finished(bob)
        assert sent.status == received.status == TransferStatus.COMPLETED
        saved = tmp_path / 'downloads' / 'data.bin'
        assert received.saved_path == str(saved)
        assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
        assert not list((tmp_path / 'downloads').glob('*.part'))
        wait_until(lambda: not server.transfers())


def test_only_the_right_party_may_send_content(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        charlie, _ = async_login(stack, server, accounts, 'charlie')
        transfer_id = offer(alice).header['transfer_id']  # raw offer: no local engine
        event(bob, MessageType.FILE_OFFER)
        # Before acceptance, even the real sender may not send chunks.
        alice.transport.send(build_file_chunk(transfer_id, 0, b'hello'))
        event(alice, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        bob.accept_file(transfer_id)
        event(alice, MessageType.FILE_ACCEPT)
        # Recipient may not send chunks; a third user may not send anything.
        bob.transport.send(build_file_chunk(transfer_id, 0, b'hello'))
        event(bob, MessageType.FILE_ACCEPT)
        event(bob, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        charlie.transport.send(build_file_result(transfer_id, True, 5))
        event(charlie, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        # A successful result before FILE_END is refused as well.
        bob.transport.send(build_file_result(transfer_id, True, 5))
        event(bob, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        assert server.transfers()[transfer_id].status == TransferStatus.ACCEPTED


def test_sender_disconnect_mid_transfer_removes_partial_file(accounts, tmp_path):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = uuid4().hex
        # Raw offer of a 10-byte file in 5-byte chunks; only the first chunk is sent.
        alice.transport.send(build_file_offer(transfer_id, 'bob', 'half.bin', 10, '0' * 64, 5))
        event(bob, MessageType.FILE_OFFER)
        bob.accept_file(transfer_id)
        event(alice, MessageType.FILE_ACCEPT)
        alice.transport.send(build_file_chunk(transfer_id, 0, b'12345'))  # half of the file
        wait_until(lambda: server.transfers()[transfer_id].status == TransferStatus.TRANSFERRING)
        assert list((tmp_path / 'downloads').glob('*.part'))
        alice.close()
        event(bob, MessageType.FILE_ACCEPT)
        event(bob, MessageType.ERROR, transfer_id, ErrorCode.CONNECTION_LOST)
        wait_until(lambda: not list((tmp_path / 'downloads').iterdir()))
        assert not server.transfers()
