"""Stage 3 credentials and sessions; password exchanges use localhost only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import secrets
import socket
import struct
import threading

import pytest

from client.connection import authenticate, request_users
from client.__main__ import main as client_main
from common.errors import ErrorCode
from common.handshake import initiate_hello
from common.models import ConnectionState, MessageType
from common.protocol import Direction, encode_message
from common.builders import (build_auth, build_chat_public, build_hello, build_logout,
                             build_user_list)
from common.transport import SocketTransport
from server.app import TCPServer
from server.auth import CredentialStore, main as credentials_main, save_user
from tests.integration.test_concurrency import connect, running, wait_until


@pytest.fixture(scope='module')
def accounts(tmp_path_factory):
    path = tmp_path_factory.mktemp('auth') / 'users.json'
    passwords = {f'user{i:02}': secrets.token_urlsafe(18) for i in range(20)}
    for name, password in passwords.items():
        save_user(path, name, password)
    return CredentialStore.load(path), passwords, path


def open_client(stack, server, *, hello=True):
    connection = connect(stack, server)
    transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
    if hello:
        initiate_hello(transport)
    return connection, transport


def exchange_error(transport, message, code):
    transport.send(message)
    reply = transport.receive()
    assert reply.type == MessageType.ERROR
    assert reply.header['ref_request_id'] == message.header['request_id']
    assert reply.header['code'] == code.value
    return reply


def test_login_list_and_session_metadata(accounts):
    store, passwords, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, unauthenticated = open_client(stack, server)
        connection, transport = open_client(stack, server)
        authenticate(transport, 'user00', passwords['user00'])
        assert request_users(transport) == [{'username': 'user00', 'online': True,
                                            'ip': connection.getsockname()[0],
                                            'port': connection.getsockname()[1]}]
        session = server.users()['user00']
        record = server.connections()[session.connection_id]
        assert session.online and record.state == ConnectionState.AUTHENTICATED
        assert record.socket.getpeername() == (session.ip, session.port)
        assert request_users(transport)[0]['username'] == 'user00'  # still active
        exchange_error(unauthenticated, build_user_list(), ErrorCode.NOT_AUTHENTICATED)


def test_invalid_and_unknown_credentials_then_valid_retry(accounts):
    store, passwords, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        exchange_error(transport, build_auth('user00', secrets.token_urlsafe()), ErrorCode.AUTH_FAILED)
        exchange_error(transport, build_auth('unknown', passwords['user00']), ErrorCode.AUTH_FAILED)
        assert not server.users()
        authenticate(transport, 'user00', passwords['user00'])
        assert list(server.users()) == ['user00']


def test_failed_attempt_limit(accounts):
    store, _, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        for _ in range(3):
            exchange_error(transport, build_auth('user00', secrets.token_urlsafe()), ErrorCode.AUTH_FAILED)
        assert transport.receive() is None
        wait_until(lambda: not server.connections())
        assert not server.users()


def test_duplicate_and_repeated_auth_keep_original_session(accounts):
    store, passwords, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, first = open_client(stack, server)
        _, second = open_client(stack, server)
        authenticate(first, 'user00', passwords['user00'])
        exchange_error(second, build_auth('user00', passwords['user00']), ErrorCode.ALREADY_LOGGED_IN)
        original = server.users()['user00']
        exchange_error(first, build_auth('user01', passwords['user01']), ErrorCode.ALREADY_LOGGED_IN)
        assert server.users()['user00'] == original
        assert request_users(first)[0]['username'] == 'user00'
        authenticate(second, 'user01', passwords['user01'])
        assert {u['username'] for u in request_users(first)} == {'user00', 'user01'}


def test_simultaneous_duplicate_claim_is_atomic(accounts):
    store, passwords, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        transports = [open_client(stack, server)[1] for _ in range(2)]
        barrier = threading.Barrier(2, timeout=3)

        def login(transport):
            barrier.wait()
            transport.send(build_auth('user00', passwords['user00']))
            return transport.receive()

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(login, transports))
        assert sum(reply.type == MessageType.AUTH_OK for reply in replies) == 1
        error = next(reply for reply in replies if reply.type == MessageType.ERROR)
        assert error.header['code'] == ErrorCode.ALREADY_LOGGED_IN.value
        assert len(server.users()) == 1


def test_twenty_simultaneous_authenticated_clients(accounts):
    store, passwords, _ = accounts
    with running(credentials=store, idle_timeout=20) as server, ExitStack() as stack:
        transports = [open_client(stack, server)[1] for _ in passwords]
        # Hashing is intentionally expensive; bound the client socket waits too.
        for transport in transports:
            transport.connection.settimeout(20)
        barrier = threading.Barrier(20, timeout=5)

        def login(item):
            transport, (name, password) = item
            barrier.wait()
            return authenticate(transport, name, password)

        with ThreadPoolExecutor(max_workers=20) as pool:
            replies = list(pool.map(login, zip(transports, passwords.items())))
        assert len(replies) == 20
        assert len(server.users()) == 20
        expected = set(passwords)
        assert {u['username'] for u in request_users(transports[0])} == expected
        assert {u['username'] for u in request_users(transports[-1])} == expected
        assert all(r.state == ConnectionState.AUTHENTICATED for r in server.connections().values())
        for transport in transports:
            transport.connection.close()
        wait_until(lambda: not server.users() and not server.connections())


@pytest.mark.parametrize('message', [build_user_list(), build_logout(), build_chat_public('test')])
def test_unauthorized_after_hello(accounts, message):
    store, _, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        exchange_error(transport, message, ErrorCode.NOT_AUTHENTICATED)
        assert not server.users()


@pytest.mark.parametrize('message', [build_auth('user00', 'not-a-real-password'), build_user_list()])
def test_authentication_requires_hello(accounts, message):
    store, _, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, transport = open_client(stack, server, hello=False)
        transport.send(message)
        reply = transport.receive()
        assert reply.type == MessageType.ERROR
        assert reply.header['code'] == ErrorCode.INVALID_FIELD.value
        assert transport.receive() is None
        assert not server.users()


@pytest.mark.parametrize('mode', ['logout', 'eof', 'reset', 'timeout', 'malformed', 'shutdown'])
def test_authenticated_cleanup_and_username_reuse(accounts, mode):
    store, passwords, _ = accounts
    with running(credentials=store, idle_timeout=0.5 if mode == 'timeout' else 45) as server, ExitStack() as stack:
        connection, transport = open_client(stack, server)
        authenticate(transport, 'user00', passwords['user00'])
        if mode == 'logout':
            transport.send(build_logout())
            assert transport.receive() is None
        elif mode == 'reset':
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            connection.close()
        elif mode == 'eof':
            connection.close()
        elif mode == 'timeout':
            assert transport.receive() is None
        elif mode == 'malformed':
            connection.sendall(b'\x00\x00\x00\x00')
            assert transport.receive() is None
        else:
            server.shutdown()
            assert transport.receive() is None
        wait_until(lambda: not server.users() and not server.connections())
        if mode != 'shutdown':
            _, replacement = open_client(stack, server)
            authenticate(replacement, 'user00', passwords['user00'])
            assert len(server.users()) == 1


def test_unsupported_authenticated_operation_is_rejected(accounts):
    store, passwords, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        _, transport = open_client(stack, server)
        authenticate(transport, 'user00', passwords['user00'])
        exchange_error(transport, build_hello(), ErrorCode.INVALID_FIELD)
        assert transport.receive() is None
        wait_until(lambda: not server.users())


def test_fragmented_and_coalesced_auth_requests(accounts):
    store, passwords, _ = accounts
    with running(credentials=store) as server, ExitStack() as stack:
        connection, transport = open_client(stack, server)
        auth = build_auth('user00', passwords['user00'])
        frame = encode_message(auth)
        connection.sendall(frame[:2])
        connection.sendall(frame[2:] + encode_message(build_user_list()))
        reply = transport.receive()
        assert reply.type == MessageType.AUTH_OK
        assert reply.header['ref_request_id'] == auth.header['request_id']
        assert json.loads(transport.receive().payload)['users'][0]['username'] == 'user00'


def test_credential_file_hashes_salts_and_updates(tmp_path):
    path = tmp_path / 'users.json'
    password = secrets.token_urlsafe()
    save_user(path, 'alice', password)
    save_user(path, 'bob', password)
    obj = json.loads(path.read_text())
    assert password not in path.read_text()
    assert obj['users']['alice']['salt'] != obj['users']['bob']['salt']
    assert obj['users']['alice']['hash'] != obj['users']['bob']['hash']
    new_password = secrets.token_urlsafe()
    save_user(path, 'alice', new_password)
    store = CredentialStore.load(path)
    assert store.verify('alice', new_password.encode())
    assert not store.verify('alice', password.encode())
    assert store.verify('bob', password.encode())
    assert not store.verify('missing', password.encode())


@pytest.mark.parametrize('data', ['{"users":{}}', '{', '{"version":1,"users":{"a":{"salt":"xx","hash":"xx"}}}'])
def test_bad_credential_files_fail_closed(tmp_path, data):
    path = tmp_path / 'users.json'
    path.write_text(data)
    with pytest.raises(ValueError, match='Invalid credential file'):
        CredentialStore.load(path)


def test_authentication_refuses_non_loopback_bind(accounts):
    with pytest.raises(ValueError, match='loopback'):
        TCPServer(host='0.0.0.0', port=0, credentials=accounts[0])


def test_cli_create_account_login_list_and_logout(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'users.json'
    password = secrets.token_urlsafe()
    monkeypatch.setattr('server.auth.getpass.getpass', lambda prompt: password)
    credentials_main(['--file', str(path), '--username', 'demo'])
    with running(credentials=CredentialStore.load(path)) as server:
        monkeypatch.setattr('client.__main__.getpass.getpass', lambda prompt: password)
        commands = iter(['', 'logout'])
        monkeypatch.setattr('client.__main__._read_command', lambda prompt: next(commands))
        assert client_main(['--port', str(server.address[1]), '--username', 'demo']) == 0
        wait_until(lambda: not server.users())
    output = capsys.readouterr()
    assert 'Authenticated as demo' in output.out
    assert 'demo' in output.out and 'Online users (1):' in output.out
    assert password not in output.out + output.err


def test_cli_failed_login_reports_error(accounts, monkeypatch, capsys):
    with running(credentials=accounts[0]) as server:
        monkeypatch.setattr('client.__main__.getpass.getpass', lambda prompt: secrets.token_urlsafe())
        assert client_main(['--port', str(server.address[1]), '--username', 'user00']) == 1
        wait_until(lambda: not server.connections())
        assert not server.users()
    assert 'Client failed' in capsys.readouterr().err
