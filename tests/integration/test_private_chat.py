"""Stage 4A: real localhost private routing and asynchronous client reception."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import queue
import secrets
import socket
import struct
import threading
import time

import pytest

from client.__main__ import authenticate, main as client_main, request_users
from client.private_chat import PrivateChatClient
from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.models import Message, MessageType
from common.protocol import (build_chat_ack, build_chat_private, build_chat_public,
                             build_error, build_user_list, encode_message)
from server.app import TCPServer
from server.auth import CredentialStore, save_user
from tests.integration.test_auth import open_client, exchange_error
from tests.integration.test_concurrency import running, wait_until


@pytest.fixture(scope='module')
def accounts(tmp_path_factory):
    path = tmp_path_factory.mktemp('private-chat') / 'users.json'
    passwords = {name: secrets.token_urlsafe(18) for name in ('alice', 'bob', 'charlie', 'dave')}
    for username, password in passwords.items():
        save_user(path, username, password)
    return CredentialStore.load(path), passwords


def login(stack, server, accounts, name):
    connection, transport = open_client(stack, server)
    authenticate(transport, name, accounts[1][name])
    return connection, transport


def async_login(stack, server, accounts, name):
    connection, transport = login(stack, server, accounts, name)
    inbox = queue.Queue()
    client = PrivateChatClient(transport, timeout=3, on_message=inbox.put_nowait)
    stack.callback(client.close)
    return client, inbox


def test_alice_bob_metadata_ack_and_charlie_isolation(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        _, bob = login(stack, server, accounts, 'bob')
        charlie_socket, charlie = login(stack, server, accounts, 'charlie')
        text = 'Xin chào Bob 👋'
        request = build_chat_private('bob', text)
        before = int(time.time() * 1000)
        alice.send(request)
        message = bob.receive()
        ack = alice.receive()
        after = int(time.time() * 1000)
        assert message.type == MessageType.CHAT_PRIVATE
        assert message.header['sender'] == 'alice'
        assert message.header['recipient'] == 'bob'
        assert message.payload.decode('utf-8') == text
        assert len(message.header['message_id']) == 32
        assert before <= message.header['timestamp_ms'] <= after
        assert ack.type == MessageType.CHAT_ACK
        assert ack.header['status'] == 'accepted'
        assert ack.header['ref_request_id'] == request.header['request_id']
        assert ack.header['message_id'] == message.header['message_id']
        assert ack.header['timestamp_ms'] == message.header['timestamp_ms']
        charlie_socket.settimeout(0.1)
        with pytest.raises(socket.timeout):
            charlie.receive()
        charlie_socket.settimeout(2)
        assert len(request_users(charlie)) == 3
        # Routing retains exactly the recipient's registered transport.
        record = server.connections()[server.users()['bob'].connection_id]
        assert record.transport.connection is record.socket


@pytest.mark.parametrize('recipient', ['bob', 'unknown'])
def test_offline_returns_error_and_preserves_sender(accounts, recipient):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        exchange_error(alice, build_chat_private(recipient, 'hello'), ErrorCode.RECIPIENT_OFFLINE)
        assert request_users(alice)[0]['username'] == 'alice'


def test_unauthenticated_private_chat_rejected(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        exchange_error(transport, build_chat_private('bob', 'hello'), ErrorCode.NOT_AUTHENTICATED)
        assert not server.users()


@pytest.mark.parametrize('field,value', [('sender', 'charlie'), ('message_id', 'forged'), ('timestamp_ms', 1)])
def test_client_cannot_forge_server_identity(accounts, field, value):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        bob_socket, bob = login(stack, server, accounts, 'bob')
        message = build_chat_private('bob', 'forged')
        header = dict(message.header)
        # encode_message defaults to direction ANY; use a full server-shaped
        # frame to exercise server ingress direction validation over real TCP.
        header.update(sender='charlie', message_id='forged', timestamp_ms=1)
        header[field] = value
        alice.connection.sendall(encode_message(Message(header, message.payload)))
        assert alice.receive() is None
        wait_until(lambda: 'alice' not in server.users())
        bob_socket.settimeout(0.1)
        with pytest.raises(socket.timeout):
            bob.receive()


def test_public_chat_remains_unsupported(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, alice = login(stack, server, accounts, 'alice')
        exchange_error(alice, build_chat_public('not supported'), ErrorCode.INVALID_FIELD)
        assert alice.receive() is None


def test_self_message_and_user_list_reader_dispatch(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, inbox = async_login(stack, server, accounts, 'alice')
        ack = alice.send_private('alice', 'self')
        message = inbox.get(timeout=2)
        assert ack.header['message_id'] == message.header['message_id']
        assert message.header['sender'] == 'alice'
        assert alice.users()[0]['username'] == 'alice'


@pytest.mark.parametrize('public_api', [False, True], ids=['correlation', 'users-api'])
def test_concurrent_user_lists_with_reversed_responses(accounts, monkeypatch, public_api):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        record = server.connections()[server.users()['alice'].connection_id]
        original_send = record.transport.send
        responses = []
        pending_refs = set()
        count = 8

        def reverse_batch(message):
            if message.type != MessageType.USER_LIST:
                return original_send(message)
            responses.append(message)
            if len(responses) == count:
                # No response is delivered until all eight requests are outstanding.
                with alice._lock:
                    pending_refs.update(alice._pending)
                for response in reversed(responses):
                    original_send(response)

        monkeypatch.setattr(record.transport, 'send', reverse_batch)
        requests = [build_user_list() for _ in range(count)]
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(alice.users) if public_api else
                       pool.submit(alice._request, request, MessageType.USER_LIST)
                       for request in requests]
            results = [future.result(timeout=4) for future in futures]
        assert len(pending_refs) == count
        assert {m.header['ref_request_id'] for m in responses} == pending_refs
        if public_api:
            assert all([u['username'] for u in users] == ['alice'] for users in results)
        else:
            assert [m.header['ref_request_id'] for m in results] == [
                r.header['request_id'] for r in requests]
            assert all(json.loads(m.payload)['users'][0]['username'] == 'alice' for m in results)
        with alice._lock:
            assert not alice._pending
        assert alice._reader.is_alive()


def test_user_list_timeout_wakes_concurrent_waiters_and_cleans_up(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        alice.timeout = 2
        record = server.connections()[server.users()['alice'].connection_id]
        received = []
        both_received = threading.Event()

        def withhold_response(message):
            received.append(message)
            if len(received) == 2:
                both_received.set()

        monkeypatch.setattr(record.transport, 'send', withhold_response)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(alice.users) for _ in range(2)]
            assert both_received.wait(timeout=1)
            with alice._lock:
                assert len(alice._pending) == 2
            errors = []
            for future in futures:
                with pytest.raises((TimeoutError, EOFError)) as caught:
                    future.result(timeout=3)
                errors.append(caught.value)
        assert any(isinstance(error, TimeoutError) for error in errors)
        wait_until(lambda: not alice._reader.is_alive())
        assert not alice._pending
        wait_until(lambda: 'alice' not in server.users())
        with pytest.raises(EOFError):
            alice.users()  # Timed-out connections cannot be reused for a later request.


@pytest.mark.parametrize('response', ['unknown-ref', 'missing-ref', 'wrong-type', 'error'])
def test_user_list_unexpected_and_error_responses(accounts, monkeypatch, response):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        record = server.connections()[server.users()['alice'].connection_id]
        original_send = record.transport.send

        def send_response(message):
            ref = message.header['ref_request_id']
            if response == 'missing-ref':
                header = dict(message.header)
                del header['ref_request_id']
                # Bypass the encoder only to inject a deliberately invalid peer frame.
                data = json.dumps(header, separators=(',', ':')).encode()
                record.socket.sendall(struct.pack('!I', len(data)) + data + message.payload)
            elif response == 'unknown-ref':
                original_send(build_user_list([], ref_request_id='unknown'))
            elif response == 'wrong-type':
                original_send(build_chat_ack(ref, 'message', 1))
            else:
                original_send(build_error(ErrorCode.NOT_AUTHENTICATED, 'Rejected', ref_request_id=ref))

        monkeypatch.setattr(record.transport, 'send', send_response)
        if response == 'error':
            with pytest.raises(LanLinkError) as caught:
                alice.users()
            assert caught.value.code == ErrorCode.NOT_AUTHENTICATED
            assert alice._reader.is_alive()
        else:
            with pytest.raises(ProtocolError) as caught:
                alice.users()
            expected = ErrorCode.MISSING_FIELD if response == 'missing-ref' else ErrorCode.INVALID_FIELD
            assert caught.value.code == expected
            wait_until(lambda: not alice._reader.is_alive())
        assert not alice._pending


def test_duplicate_user_list_response_is_rejected(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        request = build_user_list()
        reply = alice._request(request, MessageType.USER_LIST)
        record = server.connections()[server.users()['alice'].connection_id]
        record.transport.send(reply)
        wait_until(lambda: not alice._reader.is_alive())
        with pytest.raises(ProtocolError, match='Unexpected server response'):
            alice.users()
        assert not alice._pending


def test_synchronous_user_list_helper_rejects_mismatched_reference(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        _, transport = login(stack, server, accounts, 'alice')
        record = server.connections()[server.users()['alice'].connection_id]
        original_send = record.transport.send
        monkeypatch.setattr(record.transport, 'send', lambda message:
                            original_send(build_user_list([], ref_request_id='unknown')))
        with pytest.raises(ProtocolError, match='does not match request'):
            request_users(transport)


def test_many_concurrent_routes_and_reply_writes_share_transport(accounts):
    with running(credentials=accounts[0], idle_timeout=15) as server, ExitStack() as stack:
        clients = {name: async_login(stack, server, accounts, name) for name in accounts[1]}
        alice, alice_inbox = clients['alice']
        barrier = threading.Barrier(4, timeout=3)

        def exchange(name):
            client, _ = clients[name]
            barrier.wait()
            replies = []
            for i in range(20):
                target = 'bob' if name == 'alice' else 'alice'
                replies.append(client.send_private(target, f'{name}:{i}:' + 'x' * 4000))
                if name == 'alice':
                    assert len(client.users()) == 4
            return replies

        with ThreadPoolExecutor(max_workers=4) as pool:
            acks = list(pool.map(exchange, clients))
        received = [alice_inbox.get(timeout=2) for _ in range(60)]
        assert len({m.header['message_id'] for m in received}) == 60
        assert {m.header['sender'] for m in received} == {'bob', 'charlie', 'dave'}
        expected = {ack.header['message_id'] for batch in acks[1:] for ack in batch}
        assert {m.header['message_id'] for m in received} == expected
        bob_messages = [clients['bob'][1].get(timeout=2) for _ in range(20)]
        assert {m.header['message_id'] for m in bob_messages} == {a.header['message_id'] for a in acks[0]}
        assert clients['charlie'][1].empty() and clients['dave'][1].empty()


def test_recipient_disconnect_does_not_break_sender(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        connection, _ = login(stack, server, accounts, 'bob')
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        connection.close()
        wait_until(lambda: 'bob' not in server.users())
        with pytest.raises(LanLinkError) as error:
            alice.send_private('bob', 'after reset')
        assert error.value.code == ErrorCode.RECIPIENT_OFFLINE
        assert alice.users()[0]['username'] == 'alice'


def test_forward_write_failure_returns_connection_lost(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        login(stack, server, accounts, 'bob')
        record = server.connections()[server.users()['bob'].connection_id]

        def fail(message):
            raise ConnectionResetError('injected write failure on real peer transport')

        monkeypatch.setattr(record.transport, 'send', fail)
        with pytest.raises(LanLinkError) as error:
            alice.send_private('bob', 'failure')
        assert error.value.code == ErrorCode.CONNECTION_LOST
        wait_until(lambda: 'bob' not in server.users())
        assert alice.users()[0]['username'] == 'alice'


def test_auth_ok_precedes_unsolicited_chat(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        _, bob = open_client(stack, server)
        entered = threading.Event()
        release = threading.Event()
        record = next(r for r in server.connections().values()
                      if r.socket.getpeername() == bob.connection.getsockname())
        send = record.transport.send

        def delayed(message):
            if message.type == MessageType.AUTH_OK:
                entered.set()
                assert release.wait(3)
            return send(message)

        monkeypatch.setattr(record.transport, 'send', delayed)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                auth = pool.submit(authenticate, bob, 'bob', accounts[1]['bob'])
                assert entered.wait(2)
                ack = pool.submit(alice.send_private, 'bob', 'queued until auth reply')
                release.set()
                assert auth.result(timeout=3).type == MessageType.AUTH_OK
                assert bob.receive().header['sender'] == 'alice'
                assert ack.result(timeout=3).type == MessageType.CHAT_ACK
        finally:
            release.set()


def test_shutdown_terminates_client_readers_and_cleans_sessions(accounts):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, inbox = async_login(stack, server, accounts, 'bob')
        alice.send_private('bob', 'before shutdown')
        assert inbox.get(timeout=2).payload == b'before shutdown'
        server.shutdown()
        for client in (alice, bob):
            wait_until(lambda: not client._reader.is_alive())
        assert not server.users() and not server.connections()
        with pytest.raises(EOFError):
            alice.users()


def test_cli_receives_while_input_is_waiting(accounts, monkeypatch, capsys):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        bob, inbox = async_login(stack, server, accounts, 'bob')
        monkeypatch.setattr('client.__main__.getpass.getpass', lambda prompt: accounts[1]['alice'])
        commands = iter(['/msg bob from CLI', '/users', '/logout'])
        first = True

        def input_waiting(prompt):
            nonlocal first
            if first:
                first = False
                bob.send_private('alice', 'received while input waits')
                wait_until(lambda: 'received while input waits' in capsys.readouterr().out)
            return next(commands)

        monkeypatch.setattr('builtins.input', input_waiting)
        assert client_main(['--port', str(server.address[1]), '--username', 'alice']) == 0
        assert inbox.get(timeout=2).payload == b'from CLI'
        wait_until(lambda: 'alice' not in server.users())
        assert 'Accepted:' in capsys.readouterr().out


def test_plaintext_server_never_binds_non_loopback():
    with pytest.raises(ValueError, match='loopback'):
        TCPServer(host='0.0.0.0', port=0)


def test_disconnect_wakes_pending_ack_waiter(accounts, monkeypatch):
    with running(credentials=accounts[0]) as server, ExitStack() as stack:
        alice, _ = async_login(stack, server, accounts, 'alice')
        bob, inbox = async_login(stack, server, accounts, 'bob')
        record = server.connections()[server.users()['alice'].connection_id]
        original_send = record.transport.send

        def disconnect_before_ack(message):
            if message.type == MessageType.CHAT_ACK:
                record.socket.shutdown(socket.SHUT_RDWR)
                return
            return original_send(message)

        monkeypatch.setattr(record.transport, 'send', disconnect_before_ack)
        with pytest.raises(EOFError):
            alice.send_private('bob', 'accepted write but ACK connection lost')
        assert inbox.get(timeout=2).header['sender'] == 'alice'
        wait_until(lambda: not alice._reader.is_alive())
        assert not alice._pending
        wait_until(lambda: 'alice' not in server.users())
        assert bob.users()[0]['username'] == 'bob'


@pytest.mark.parametrize('username', [None, 'alice'])
def test_cli_rejects_remote_address_before_connect(username, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Non-localhost address must be rejected before connecting')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    arguments = ['--host', '192.0.2.1']
    if username:
        arguments.extend(['--username', username])
    assert client_main(arguments) == 1
    assert 'localhost' in capsys.readouterr().err
