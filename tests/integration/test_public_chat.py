"""Stage 4B: localhost broadcasts using existing authenticated connections."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import socket
import threading
import time

import pytest

from client.__main__ import authenticate, main as client_main
from common.errors import ErrorCode, ProtocolError
from common.models import Message, MessageType
from common.protocol import DEFAULT_LIMITS, build_chat_public, encode_message
from tests.integration.test_auth import exchange_error, open_client
from tests.integration.test_concurrency import running, wait_until
from tests.integration.test_private_chat import accounts, async_login, login


@pytest.mark.parametrize('text', ['Xin chào 🌍\nDòng hai',
                                 '🙂' * (DEFAULT_LIMITS.max_chat_bytes // 4)])
def test_public_metadata_integrity_and_authenticated_audience(accounts, text):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name) for name in accounts[1]}
        unauthenticated_socket, unauthenticated = open_client(stack, server)
        alice, alice_inbox = clients['alice']
        request = build_chat_public(text)
        before = int(time.time() * 1000)
        ack = alice._request(request, MessageType.CHAT_ACK)
        after = int(time.time() * 1000)
        assert ack.header['ref_request_id'] == request.header['request_id']
        assert ack.header['status'] == 'accepted'
        for name, (_, inbox) in clients.items():
            if name == 'alice':
                continue
            message = inbox.get(timeout=2)
            assert message.type == MessageType.CHAT_PUBLIC
            assert message.payload == text.encode('utf-8')
            assert message.header['sender'] == 'alice'
            assert 'recipient' not in message.header
            assert message.header['message_id'] == ack.header['message_id']
            assert len(message.header['message_id']) == 32
            assert message.header['timestamp_ms'] == ack.header['timestamp_ms']
            assert before <= message.header['timestamp_ms'] <= after
            assert inbox.empty()
            record = server.connections()[server.users()[name].connection_id]
            assert record.transport.connection is record.socket
        assert alice_inbox.empty()  # No public sender echo.
        unauthenticated_socket.settimeout(0.05)
        with pytest.raises(socket.timeout):
            unauthenticated.receive()
        assert len(alice.users()) == 4


def test_public_requires_authentication_and_allows_login_after_error(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        exchange_error(transport, build_chat_public('not authenticated'), ErrorCode.NOT_AUTHENTICATED)
        authenticate(transport, 'alice', accounts[1]['alice'])
        request = build_chat_public('now authenticated')
        transport.send(request)
        assert transport.receive().header['ref_request_id'] == request.header['request_id']


def test_concurrent_public_private_and_user_list_writes_remain_intact(accounts):
    with running(credentials=accounts[0], idle_timeout=15) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name) for name in accounts[1]}
        names = list(clients)
        barrier = threading.Barrier(len(names), timeout=3)

        def exchange(name):
            client, _ = clients[name]
            target = names[(names.index(name) + 1) % len(names)]
            barrier.wait()
            sent = []
            for index in range(10):
                text = f'{name}:{index}:🙂:' + 'x' * 4000
                sent.append((client.send_public(text), MessageType.CHAT_PUBLIC, text, None))
                if index % 5 == 0:
                    private = f'private:{name}:{index}'
                    sent.append((client.send_private(target, private), MessageType.CHAT_PRIVATE,
                                 private, target))
                    assert len(client.users()) == len(names)
            return name, sent

        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            futures = [pool.submit(exchange, name) for name in names]
            batches = dict(future.result(timeout=8) for future in futures)
        ids = [ack.header['message_id'] for sent in batches.values() for ack, _, _, _ in sent]
        assert len(ids) == len(set(ids)) == 48
        for name, (client, inbox) in clients.items():
            expected = {ack.header['message_id']: (sender, kind, text, ack.header['timestamp_ms'])
                        for sender, sent in batches.items() for ack, kind, text, target in sent
                        if (kind == MessageType.CHAT_PUBLIC and sender != name) or target == name}
            received = [inbox.get(timeout=2) for _ in expected]
            assert len(received) == 32
            assert {message.header['message_id'] for message in received} == set(expected)
            for message in received:
                sender, kind, text, timestamp = expected[message.header['message_id']]
                assert message.type == kind and message.header['sender'] == sender
                assert message.payload == text.encode('utf-8')
                assert message.header['timestamp_ms'] == timestamp
            assert inbox.empty() and client._reader.is_alive()


def test_offline_users_are_skipped_without_history_or_sender_echo(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, alice_inbox = async_login(stack, server, accounts, 'alice')
        bob, _ = async_login(stack, server, accounts, 'bob')
        charlie, charlie_inbox = async_login(stack, server, accounts, 'charlie')
        bob.logout()
        wait_until(lambda: 'bob' not in server.users())
        ack = alice.send_public('while Bob is offline')
        assert charlie_inbox.get(timeout=2).header['message_id'] == ack.header['message_id']
        bob, bob_inbox = async_login(stack, server, accounts, 'bob')
        ack = alice.send_public('after Bob returns')
        assert bob_inbox.get(timeout=2).payload == b'after Bob returns'
        assert charlie_inbox.get(timeout=2).header['message_id'] == ack.header['message_id']
        assert bob_inbox.empty() and alice_inbox.empty()
        assert len(charlie.users()) == 3  # Dave has credentials but never logged in.


@pytest.mark.parametrize('failed', [('bob',), ('bob', 'charlie')], ids=['partial', 'all'])
def test_public_ack_accepts_best_effort_with_recipient_write_failures(accounts, monkeypatch, failed):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name)
                   for name in ('alice', 'bob', 'charlie')}
        attempts = []

        def fail(message):
            attempts.append(message)
            raise BrokenPipeError('injected recipient write failure on a real connection')

        for name in failed:
            record = server.connections()[server.users()[name].connection_id]
            monkeypatch.setattr(record.transport, 'send', fail)
        alice, _ = clients['alice']
        ack = alice.send_public('best effort')
        assert ack.type == MessageType.CHAT_ACK and ack.header['status'] == 'accepted'
        assert len(attempts) == len(failed)
        assert {m.header['message_id'] for m in attempts} == {ack.header['message_id']}
        assert {m.header['timestamp_ms'] for m in attempts} == {ack.header['timestamp_ms']}
        for name in failed:
            wait_until(lambda: name not in server.users())
            wait_until(lambda: not clients[name][0]._reader.is_alive())
            assert clients[name][0].transport.connection.fileno() == -1
        if 'charlie' not in failed:
            assert clients['charlie'][1].get(timeout=2).header['message_id'] == ack.header['message_id']
            clients['charlie'][0].send_private('alice', 'still usable')
            assert clients['alice'][1].get(timeout=2).payload == b'still usable'
        assert alice._reader.is_alive() and alice.users()


def test_public_snapshot_does_not_target_replacement_session(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        _, bob_inbox = async_login(stack, server, accounts, 'bob')
        charlie, _ = async_login(stack, server, accounts, 'charlie')
        record = server.connections()[server.users()['bob'].connection_id]
        original_send = record.transport.send
        entered, release = threading.Event(), threading.Event()

        def delayed(message):
            entered.set()
            assert release.wait(timeout=3)
            return original_send(message)

        monkeypatch.setattr(record.transport, 'send', delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(alice.send_public, 'old snapshot')
            try:
                assert entered.wait(timeout=2)
                charlie.logout()
                wait_until(lambda: 'charlie' not in server.users())
                _, replacement_inbox = async_login(stack, server, accounts, 'charlie')
            finally:
                release.set()
            ack = future.result(timeout=3)
        assert bob_inbox.get(timeout=2).header['message_id'] == ack.header['message_id']
        assert replacement_inbox.empty()
        ack = alice.send_public('new snapshot')
        assert replacement_inbox.get(timeout=2).header['message_id'] == ack.header['message_id']


def test_public_waits_for_auth_ok(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        _, bob = open_client(stack, server)
        record = next(r for r in server.connections().values()
                      if r.socket.getpeername() == bob.connection.getsockname())
        original_send = record.transport.send
        entered, release, auth_sent = threading.Event(), threading.Event(), threading.Event()
        routing_started, forward_attempted = threading.Event(), threading.Event()
        original_route = server._route_public

        def route(username, message, transport):
            routing_started.set()
            return original_route(username, message, transport)

        def delayed(message):
            if message.type == MessageType.AUTH_OK:
                entered.set()
                assert release.wait(timeout=3)
                original_send(message)
                auth_sent.set()
            else:
                forward_attempted.set()
                assert auth_sent.is_set(), 'Unsolicited public chat preceded AUTH_OK'
                original_send(message)

        monkeypatch.setattr(record.transport, 'send', delayed)
        monkeypatch.setattr(server, '_route_public', route)
        with ThreadPoolExecutor(max_workers=2) as pool:
            auth = pool.submit(authenticate, bob, 'bob', accounts[1]['bob'])
            try:
                assert entered.wait(timeout=2)
                broadcast = pool.submit(alice.send_public, 'after auth')
                assert routing_started.wait(timeout=2)
                assert not forward_attempted.wait(timeout=0.05)
            finally:
                release.set()
            assert auth.result(timeout=3).type == MessageType.AUTH_OK
            ack = broadcast.result(timeout=3)
        assert bob.receive().header['message_id'] == ack.header['message_id']


def test_shutdown_interrupts_in_flight_public_broadcast(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name)
                   for name in ('alice', 'bob', 'charlie')}
        record = server.connections()[server.users()['bob'].connection_id]
        original_send = record.transport.send
        entered, release = threading.Event(), threading.Event()

        def delayed(message):
            entered.set()
            assert release.wait(timeout=3)
            return original_send(message)

        monkeypatch.setattr(record.transport, 'send', delayed)
        with ThreadPoolExecutor(max_workers=2) as pool:
            broadcast = pool.submit(clients['alice'][0].send_public, 'during shutdown')
            try:
                assert entered.wait(timeout=2)
                shutdown = pool.submit(server.shutdown)
                assert server._stopping.wait(timeout=2)
                assert clients['alice'][0]._stopped.wait(timeout=2)
            finally:
                release.set()
            shutdown.result(timeout=3)
            with pytest.raises((OSError, EOFError)):
                broadcast.result(timeout=3)
        for client, _ in clients.values():
            wait_until(lambda: not client._reader.is_alive())
            assert client.transport.connection.fileno() == -1
        assert not server.users() and not server.connections()


@pytest.mark.parametrize('text', ['', 'x' * (DEFAULT_LIMITS.max_chat_bytes + 1)])
def test_invalid_public_payload_is_recoverable_locally(accounts, text):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        _, bob_inbox = async_login(stack, server, accounts, 'bob')
        with pytest.raises(ProtocolError):
            alice.send_public(text)
        assert len(alice.users()) == 2
        assert not alice._pending and bob_inbox.empty()


def test_public_sender_cannot_spoof_metadata(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        bob, inbox = async_login(stack, server, accounts, 'bob')
        message = build_chat_public('forged')
        header = dict(message.header, sender='bob', message_id='forged', timestamp_ms=1)
        alice.connection.sendall(encode_message(Message(header, message.payload)))
        assert alice.receive() is None
        wait_until(lambda: 'alice' not in server.users())
        assert inbox.empty() and bob.users()[0]['username'] == 'bob'


def test_cli_sends_and_receives_public_while_preserving_private_chat(accounts, monkeypatch, capsys):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        bob, inbox = async_login(stack, server, accounts, 'bob')
        monkeypatch.setattr('client.__main__.getpass.getpass', lambda prompt: accounts[1]['alice'])
        commands = iter(['/all', '/all hello everyone', '/msg bob still private', '/users', '/logout'])
        first = True

        def read_command(prompt):
            nonlocal first
            if first:
                first = False
                bob.send_public('incoming public during prompt')
                wait_until(lambda: '[bob -> all] incoming public during prompt' in capsys.readouterr().out)
            return next(commands)

        monkeypatch.setattr('client.__main__._read_command', read_command)
        assert client_main(['--port', str(server.address[1]), '--username', 'alice']) == 0
        public, private = inbox.get(timeout=2), inbox.get(timeout=2)
        assert public.type == MessageType.CHAT_PUBLIC and public.payload == b'hello everyone'
        assert private.type == MessageType.CHAT_PRIVATE and private.payload == b'still private'
        output = capsys.readouterr().out
        assert 'Usage: /all TEXT' in output and 'best-effort broadcast' in output
        wait_until(lambda: 'alice' not in server.users())
