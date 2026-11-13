"""Stage 5A: real localhost metadata negotiation; no content transmission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import queue
import socket
import struct
import threading
import time
from uuid import uuid4

import pytest

from client.connection import authenticate
from client.__main__ import main as client_main
from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.models import MessageType, TransferStatus
from common.protocol import DEFAULT_LIMITS, encode_message
from common.builders import (build_file_accept, build_file_chunk, build_file_end, build_file_offer,
                             build_file_reject, build_user_list)
from tests.integration.test_auth import exchange_error, open_client
from tests.integration.test_concurrency import running, wait_until
from tests.integration.test_private_chat import accounts, async_login, login


@pytest.fixture(autouse=True)
def isolated_downloads(tmp_path, monkeypatch):
    # Clients save into ./downloads; keep that inside the test's temp folder.
    monkeypatch.chdir(tmp_path)


def offer(client, recipient='bob', transfer_id=None, **metadata):
    request = build_file_offer(transfer_id or uuid4().hex, recipient,
                              metadata.get('filename', 'hello.txt'), metadata.get('size', 5),
                              metadata.get('sha256', hashlib.sha256(b'hello').hexdigest()),
                              DEFAULT_LIMITS.max_chunk_bytes)
    client.transport.send(request)
    return request


def event(client, kind, transfer_id=None, code=None):
    message = client.transfer_events.get(timeout=3)
    assert message.type == kind
    if transfer_id is not None:
        assert message.header['transfer_id'] == transfer_id
    if code is not None:
        assert message.header['code'] == code
    return message


@pytest.mark.parametrize('accept', [True, False])
def test_offer_decision_metadata_and_chat_preserved(accounts, tmp_path, accept):
    path = tmp_path / 'xin chào.txt'
    path.write_bytes(b'hello')
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, alice_chat = async_login(stack, server, accounts, 'alice')
        bob, bob_chat = async_login(stack, server, accounts, 'bob')
        charlie, charlie_chat = async_login(stack, server, accounts, 'charlie')
        transfer_id = alice.offer_file('bob', path)
        message = event(bob, MessageType.FILE_OFFER, transfer_id)
        assert message.header['sender'] == 'alice' and message.header['recipient'] == 'bob'
        assert message.header['filename'] == path.name and message.header['size'] == 5
        assert message.header['sha256'] == hashlib.sha256(b'hello').hexdigest()
        assert message.header['chunk_size'] == DEFAULT_LIMITS.max_chunk_bytes and not message.payload
        assert server.transfers()[transfer_id].status == TransferStatus.OFFERED
        assert alice.transfer_events.empty() and charlie.transfer_events.empty()
        if accept:
            bob.accept_file(transfer_id)
            kind = MessageType.FILE_ACCEPT
        else:
            bob.reject_file(transfer_id, 'No thanks')
            kind = MessageType.FILE_REJECT
        decision = event(alice, kind, transfer_id)
        assert dict(event(bob, kind, transfer_id).header) == dict(decision.header)
        if accept:
            # Acceptance starts the real transfer; both sides report completion.
            for client in (alice, bob):
                done = client.transfer_events.get(timeout=3)
                assert done.status == TransferStatus.COMPLETED
            assert (tmp_path / 'downloads' / path.name).read_bytes() == b'hello'
            wait_until(lambda: not server.transfers())
        else:
            assert decision.header['reason'] == 'No thanks' and not server.transfers()
        assert alice.transfer_events.empty() and bob.transfer_events.empty()
        alice.send_private('bob', 'still private')
        assert bob_chat.get(timeout=2).payload == b'still private'
        bob.send_public('still public')
        assert alice_chat.get(timeout=2).payload == b'still public'
        assert charlie_chat.get(timeout=2).payload == b'still public'
        assert len(charlie.users()) == 3


@pytest.mark.parametrize('builder', [lambda: build_file_offer(uuid4().hex, 'bob', 'a', 0, '0'*64, 1),
                                     lambda: build_file_accept(uuid4().hex),
                                     lambda: build_file_reject(uuid4().hex)])
def test_file_operations_require_authentication(accounts, builder):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        exchange_error(transport, builder(), ErrorCode.NOT_AUTHENTICATED)
        authenticate(transport, 'alice', accounts[1]['alice'])
        assert not server.transfers()


def test_unknown_unauthorized_duplicate_and_invalid_transitions(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        charlie, _ = async_login(stack, server, accounts, 'charlie')
        missing = uuid4().hex
        bob.accept_file(missing)
        event(bob, MessageType.ERROR, missing, ErrorCode.TRANSFER_UNKNOWN)
        request = offer(alice)
        transfer_id = request.header['transfer_id']
        event(bob, MessageType.FILE_OFFER, transfer_id)
        for client in (alice, charlie):
            client.accept_file(transfer_id)
            event(client, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
            client.reject_file(transfer_id)
            event(client, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        offer(alice, transfer_id=transfer_id, filename='changed.txt')
        error = event(alice, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        assert error.header.get('ref_request_id')
        assert server.transfers()[transfer_id].offer.header['filename'] == 'hello.txt'
        bob.accept_file(transfer_id)
        event(alice, MessageType.FILE_ACCEPT, transfer_id)
        event(bob, MessageType.FILE_ACCEPT, transfer_id)
        # A second decision on a running transfer is refused locally.
        for action in (bob.accept_file, bob.reject_file):
            with pytest.raises(LanLinkError) as failure:
                action(transfer_id)
            assert failure.value.code == ErrorCode.TRANSFER_STATE
        assert server.transfers()[transfer_id].status == TransferStatus.ACCEPTED
        assert len(bob.users()) == 3


def test_reject_removes_session_but_cannot_replay_id(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = offer(alice).header['transfer_id']
        event(bob, MessageType.FILE_OFFER)
        bob.reject_file(transfer_id)
        event(alice, MessageType.FILE_REJECT)
        event(bob, MessageType.FILE_REJECT)
        assert not server.transfers()
        bob.reject_file(transfer_id)
        event(bob, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_UNKNOWN)
        offer(bob, 'alice', transfer_id)
        event(bob, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        assert alice.transfer_events.empty()


@pytest.mark.parametrize('recipient,code', [('missing', ErrorCode.RECIPIENT_OFFLINE),
                                          ('alice', ErrorCode.INVALID_FIELD)])
def test_offline_and_self_offer_are_recoverable(accounts, recipient, code):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        request = offer(alice, recipient)
        event(alice, MessageType.ERROR, request.header['transfer_id'], code)
        assert not server.transfers() and len(alice.users()) == 1


def test_concurrent_offers_and_decisions_with_chat_and_user_lists(accounts):
    with running(credentials=accounts[0], idle_timeout=10) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name)[0] for name in accounts[1]}
        names = list(clients)
        barrier = threading.Barrier(4, timeout=3)

        def send(name):
            barrier.wait()
            recipient = names[(names.index(name) + 1) % len(names)]
            requests = [offer(clients[name], recipient) for _ in range(8)]
            clients[name].send_public(f'chat:{name}')
            assert len(clients[name].users()) == 4
            return [r.header['transfer_id'] for r in requests]

        with ThreadPoolExecutor(max_workers=4) as pool:
            offered = list(pool.map(send, names))
        assert len({i for batch in offered for i in batch}) == len(server.transfers()) == 32

        def decide(name):
            incoming = [event(clients[name], MessageType.FILE_OFFER) for _ in range(8)]
            for index, message in enumerate(incoming):
                if index % 2:
                    clients[name].accept_file(message.header['transfer_id'])
                else:
                    clients[name].reject_file(message.header['transfer_id'])
            return incoming

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(decide, names))
        for name, client in clients.items():
            decisions = [client.transfer_events.get(timeout=3) for _ in range(16)]
            assert all(m.type in {MessageType.FILE_ACCEPT, MessageType.FILE_REJECT} for m in decisions)
            assert len({m.header['transfer_id'] for m in decisions}) == 16
            assert not client._pending and client._reader.is_alive()
        assert len(server.transfers()) == 16
        assert all(s.status == TransferStatus.ACCEPTED for s in server.transfers().values())


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('departing', ['alice', 'bob'])
@pytest.mark.parametrize('logout', [False, True])
def test_disconnect_and_logout_cleanup(accounts, accepted, departing, logout):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name)[0] for name in ('alice', 'bob')}
        transfer_id = offer(clients['alice']).header['transfer_id']
        event(clients['bob'], MessageType.FILE_OFFER)
        if accepted:
            clients['bob'].accept_file(transfer_id)
            for client in clients.values():
                event(client, MessageType.FILE_ACCEPT)
        if logout:
            clients[departing].logout()
        else:
            clients[departing].close()
        survivor = clients['bob' if departing == 'alice' else 'alice']
        event(survivor, MessageType.ERROR, transfer_id, ErrorCode.CONNECTION_LOST)
        wait_until(lambda: not server.transfers() and departing not in server.users())
        assert len(survivor.users()) == 1
        replacement, _ = async_login(stack, server, accounts, departing)
        replacement.accept_file(transfer_id)
        event(replacement, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_UNKNOWN)
        assert survivor.transfer_events.empty()


@pytest.mark.parametrize('accepted', [False, True])
def test_transfer_timeouts_do_not_close_healthy_connections(accounts, accepted):
    with running(credentials=accounts[0], transfer_timeout=0.3, idle_timeout=5) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = offer(alice).header['transfer_id']
        event(bob, MessageType.FILE_OFFER)
        if accepted:
            bob.accept_file(transfer_id)
            event(alice, MessageType.FILE_ACCEPT)
            event(bob, MessageType.FILE_ACCEPT)
        for client in (alice, bob):
            event(client, MessageType.ERROR, transfer_id, ErrorCode.TIMEOUT)
            assert len(client.users()) == 2 and client._reader.is_alive()
        assert not server.transfers()
        bob.accept_file(transfer_id)
        event(bob, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_UNKNOWN)


def test_transfer_deadline_preserves_partial_frame_and_does_not_extend_idle(accounts):
    with running(credentials=accounts[0], transfer_timeout=0.2, idle_timeout=0.7) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        _, bob = login(stack, server, accounts, 'bob')
        request = build_file_offer(uuid4().hex, 'bob', 'empty.txt', 0, '0'*64, 1)
        alice.send(request)
        assert bob.receive().type == MessageType.FILE_OFFER
        users = build_user_list()
        frame = encode_message(users)
        alice.connection.sendall(frame[:6])
        error = alice.receive()
        assert error.header['code'] == ErrorCode.TIMEOUT
        alice.connection.sendall(frame[6:])
        reply = alice.receive()
        assert reply.type == MessageType.USER_LIST
        assert reply.header['ref_request_id'] == users.header['request_id']
        assert bob.receive().header['code'] == ErrorCode.TIMEOUT
        # Transfer timer wakeups do not keep idle connections alive indefinitely.
        assert bob.receive() is None
        wait_until(lambda: not server.transfers())


@pytest.mark.parametrize('field,value', [('filename', '../unsafe'), ('size', -1), ('sha256', 'bad'),
                                         ('chunk_size', 0), ('sender', 'charlie')])
def test_invalid_metadata_and_spoofing_close_only_bad_peer(accounts, field, value):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        header = dict(build_file_offer(uuid4().hex, 'bob', 'a', 0, '0'*64, 1).header)
        header[field] = value
        data = json.dumps(header).encode()
        alice.connection.sendall(struct.pack('!I', len(data)) + data)
        assert alice.receive() is None
        wait_until(lambda: 'alice' not in server.users())
        assert not server.transfers() and bob.transfer_events.empty() and len(bob.users()) == 1


@pytest.mark.parametrize('kind', ['offer', 'decision'])
def test_forward_failure_cleans_session_and_notifies_survivor(accounts, monkeypatch, kind):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = uuid4().hex
        if kind == 'decision':
            offer(alice, transfer_id=transfer_id)
            event(bob, MessageType.FILE_OFFER)
        broken = 'bob' if kind == 'offer' else 'alice'
        record = server.connections()[server.users()[broken].connection_id]
        original = record.transport.send

        def fail(message):
            if message.type == (MessageType.FILE_OFFER if kind == 'offer' else MessageType.FILE_ACCEPT):
                raise ConnectionResetError('simulated recipient write failure')
            original(message)

        monkeypatch.setattr(record.transport, 'send', fail)
        if kind == 'offer':
            offer(alice, transfer_id=transfer_id)
        else:
            bob.accept_file(transfer_id)
        survivor = alice if kind == 'offer' else bob
        event(survivor, MessageType.ERROR, transfer_id, ErrorCode.CONNECTION_LOST)
        wait_until(lambda: not server.transfers() and broken not in server.users())
        assert survivor.users()[0]['username'] != broken


def test_bounded_offer_history(accounts):
    with running(credentials=accounts[0], idle_timeout=10) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        for _ in range(64):
            offer(alice)
            event(bob, MessageType.FILE_OFFER)
        request = offer(alice)
        event(alice, MessageType.ERROR, request.header['transfer_id'], ErrorCode.QUEUE_FULL)
        assert len(server.transfers()) == 64 and len(alice.users()) == 2


@pytest.mark.parametrize('builder', [lambda i: build_file_chunk(i, 0, b'x'),
                                     lambda i: build_file_end(i, 0, 0)])
def test_chunk_and_end_for_unknown_transfer_are_recoverable(accounts, builder):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        transfer_id = uuid4().hex
        alice.transport.send(builder(transfer_id))
        event(alice, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_UNKNOWN)
        assert len(alice.users()) == 1 and not server.transfers()


def test_shutdown_cleans_offered_and_accepted_sessions(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        first = offer(alice).header['transfer_id']
        event(bob, MessageType.FILE_OFFER)
        bob.accept_file(first)
        event(alice, MessageType.FILE_ACCEPT)
        event(bob, MessageType.FILE_ACCEPT)
        offer(alice)
        event(bob, MessageType.FILE_OFFER)
        records = list(server.connections().values())
        server.shutdown()
        assert not server.transfers() and not server.users() and not server.connections()
        assert all(not r.worker.is_alive() and r.socket.fileno() == -1 for r in records)
        for client in (alice, bob):
            wait_until(lambda: not client._reader.is_alive())
            assert client.transport.connection.fileno() == -1


def test_cli_offer_accept_reject_while_reader_stays_responsive(accounts, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'file with spaces.txt'
    path.write_bytes(b'metadata only')
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        bob, _ = async_login(stack, server, accounts, 'bob')
        monkeypatch.setattr('client.__main__.getpass.getpass', lambda prompt: accounts[1]['alice'])
        state = {'step': 0}

        def read_command(prompt):
            step = state['step']
            state['step'] += 1
            if step == 0:
                return f'/offer bob {path}'
            if step == 1:
                incoming = event(bob, MessageType.FILE_OFFER)
                bob.reject_file(incoming.header['transfer_id'], 'No thanks')
                incoming_id = offer(bob, 'alice').header['transfer_id']
                state['accept'] = incoming_id
                # Socket reader still dispatches chat/users while stdin waits;
                # file offers themselves go through the CLI's event queue.
                bob.send_private('alice', 'chat during file negotiation')
                wait_until(lambda: any(s.recipient_id == server.users()['alice'].connection_id
                                       for s in server.transfers().values()))
                return f'/accept {incoming_id}'
            if step == 2:
                # Bob sees the rejection echo first, then the accepted offer.
                event(bob, MessageType.FILE_REJECT)
                event(bob, MessageType.FILE_ACCEPT, state['accept'])
                state['reject'] = offer(bob, 'alice').header['transfer_id']
                wait_until(lambda: state['reject'] in server.transfers())
                return f'/reject {state["reject"]} Please ask later'
            if step == 3:
                decision = event(bob, MessageType.FILE_REJECT, state['reject'])
                assert decision.header['reason'] == 'Please ask later'
                return '/users'
            if step == 4:
                return '/logout'
            raise AssertionError('Unexpected extra input read')

        monkeypatch.setattr('client.__main__._read_command', read_command)
        assert client_main(['--port', str(server.address[1]), '--username', 'alice']) == 0
        output = capsys.readouterr().out
        assert 'Offer sent:' in output and 'File offer' in output
        assert 'chat during file negotiation' in output and 'receiving into downloads/' in output
        wait_until(lambda: not server.transfers() and 'alice' not in server.users())


def test_same_transfer_id_concurrent_offers_have_one_owner(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        charlie, _ = async_login(stack, server, accounts, 'charlie')
        transfer_id = uuid4().hex
        barrier = threading.Barrier(2, timeout=3)

        def send(client):
            barrier.wait()
            offer(client, transfer_id=transfer_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(send, (alice, charlie)))
        incoming = event(bob, MessageType.FILE_OFFER, transfer_id)
        sender = alice if incoming.header['sender'] == 'alice' else charlie
        loser = charlie if sender is alice else alice
        event(loser, MessageType.ERROR, transfer_id, ErrorCode.TRANSFER_STATE)
        assert len(server.transfers()) == 1
        bob.accept_file(transfer_id)
        event(sender, MessageType.FILE_ACCEPT, transfer_id)
        event(bob, MessageType.FILE_ACCEPT, transfer_id)
        assert loser.transfer_events.empty() and bob.transfer_events.empty()


def test_offer_waits_for_auth_ok(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        connection, bob = open_client(stack, server)
        wait_until(lambda: any(c.socket.getpeername()[1] == connection.getsockname()[1]
                               for c in server.connections().values()))
        record = next(c for c in server.connections().values()
                      if c.socket.getpeername()[1] == connection.getsockname()[1])
        entered, release, route_started, offer_sent = (threading.Event() for _ in range(4))
        original_send, original_route = record.transport.send, server._negotiate_file_transfer

        def delayed(message):
            if message.type == MessageType.AUTH_OK:
                entered.set()
                assert release.wait(timeout=3)
            if message.type == MessageType.FILE_OFFER:
                offer_sent.set()
            original_send(message)

        def route(*args):
            route_started.set()
            return original_route(*args)

        monkeypatch.setattr(record.transport, 'send', delayed)
        monkeypatch.setattr(server, '_negotiate_file_transfer', route)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(authenticate, bob, 'bob', accounts[1]['bob'])
            try:
                assert entered.wait(timeout=2)
                request = offer(alice)
                assert route_started.wait(timeout=2)
                assert not offer_sent.wait(timeout=0.05)
            finally:
                release.set()
            assert future.result(timeout=3).type == MessageType.AUTH_OK
        assert bob.receive().header['transfer_id'] == request.header['transfer_id']


def test_transfer_queue_overflow_closes_cleanly(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        # Exercise the real reader's bounded queue rather than invoking it directly.
        bob.transfer_events = queue.Queue(maxsize=1)
        offer(alice)
        wait_until(lambda: bob.transfer_events.qsize() == 1)
        offer(alice)
        wait_until(lambda: not bob._reader.is_alive())
        assert bob.transport.connection.fileno() == -1
        assert bob._error.code == ErrorCode.QUEUE_FULL
        wait_until(lambda: not server.transfers() and 'bob' not in server.users())
        assert len(alice.users()) == 1


def test_local_source_failure_and_invalid_decision_remain_recoverable(accounts, tmp_path):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        from common.errors import TransferError
        with pytest.raises(TransferError) as failure:
            alice.offer_file('bob', tmp_path / 'missing')
        assert failure.value.code == ErrorCode.FILE_NOT_FOUND
        with pytest.raises(ProtocolError):
            alice.accept_file('not-a-transfer-id')
        assert not server.transfers() and len(alice.users()) == 1


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_invalid_transfer_timeouts_rejected(timeout):
    from server.app import TCPServer
    with pytest.raises(ValueError, match='timeouts'):
        TCPServer(port=0, transfer_timeout=timeout)


def test_shutdown_waits_for_disconnect_notification_worker(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = offer(alice).header['transfer_id']
        event(bob, MessageType.FILE_OFFER)
        alice_record = server.connections()[server.users()['alice'].connection_id]
        bob_record = server.connections()[server.users()['bob'].connection_id]
        entered, release = threading.Event(), threading.Event()
        original = bob_record.transport.send

        def delayed(message):
            if message.type == MessageType.ERROR:
                entered.set()
                assert release.wait(timeout=3)
            original(message)

        monkeypatch.setattr(bob_record.transport, 'send', delayed)
        alice.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                assert entered.wait(timeout=2)
                assert alice_record.worker.is_alive()
                assert alice_record.connection_id in server.connections()
                shutdown = pool.submit(server.shutdown)
                assert server._stopping.wait(timeout=2)
                assert not shutdown.done()
            finally:
                release.set()
            shutdown.result(timeout=3)
        assert not alice_record.worker.is_alive() and not bob_record.worker.is_alive()
        assert not server.transfers() and not server.connections()


def test_decision_echo_failure_fails_accepted_session(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        transfer_id = offer(alice).header['transfer_id']
        event(bob, MessageType.FILE_OFFER)
        record = server.connections()[server.users()['bob'].connection_id]
        original = record.transport.send

        def fail_echo(message):
            if message.type == MessageType.FILE_ACCEPT:
                raise ConnectionResetError('decision echo failed')
            original(message)

        monkeypatch.setattr(record.transport, 'send', fail_echo)
        bob.accept_file(transfer_id)
        event(alice, MessageType.FILE_ACCEPT, transfer_id)
        event(alice, MessageType.ERROR, transfer_id, ErrorCode.CONNECTION_LOST)
        wait_until(lambda: not server.transfers() and 'bob' not in server.users())
        assert len(alice.users()) == 1


def test_disconnect_interrupts_local_offer_inspection(accounts, tmp_path, monkeypatch):
    from common.errors import TransferError
    from transfer.sender import inspect_file
    path = tmp_path / 'source.bin'
    path.write_bytes(b'x' * (2 * DEFAULT_LIMITS.max_chunk_bytes))
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        record = server.connections()[server.users()['alice'].connection_id]

        def inspect_with_disconnect(path, **kwargs):
            def disconnect_after_first_read(count):
                record.socket.shutdown(socket.SHUT_RDWR)
                assert alice._stopped.wait(timeout=2)
            return inspect_file(path, **kwargs, progress=disconnect_after_first_read)

        monkeypatch.setattr('client.private_chat.inspect_file', inspect_with_disconnect)
        with pytest.raises(TransferError) as failure:
            alice.offer_file('bob', path)
        assert failure.value.code == ErrorCode.CANCELLED_LOCAL
        wait_until(lambda: alice.transport.connection.fileno() == -1)
        assert not server.transfers()
