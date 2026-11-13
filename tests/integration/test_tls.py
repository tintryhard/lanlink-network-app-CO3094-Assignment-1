"""Stage 7: TLS through the stdlib ssl module; plaintext stays loopback-only."""
from contextlib import ExitStack
import queue
import shutil
import ssl
import subprocess

import pytest

from client.connection import open_connection, authenticate, connect_hello
from client.discovery import discover_servers
from client.private_chat import PrivateChatClient
from common.handshake import initiate_hello
from common.models import MessageType, TransferStatus
from common.protocol import Direction
from common.tls import server_context
from common.transport import SocketTransport
from server.app import TCPServer
from tests.integration.test_concurrency import running
from tests.integration.test_file_negotiation import event
from tests.integration.test_private_chat import accounts

pytestmark = pytest.mark.skipif(shutil.which('openssl') is None, reason='openssl not installed')


def make_cert(directory):
    cert, key = directory / 'server.crt', directory / 'server.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=lanlink-test', '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True)
    return str(cert), str(key)


@pytest.fixture(scope='module')
def cert(tmp_path_factory):
    return make_cert(tmp_path_factory.mktemp('tls'))


def tls_client(stack, server, accounts, name, cafile):
    connection = stack.enter_context(open_connection('127.0.0.1', server.address[1], 3, cafile))
    transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
    initiate_hello(transport)
    authenticate(transport, name, accounts[1][name])
    client = PrivateChatClient(transport, timeout=3, on_message=queue.Queue().put_nowait)
    stack.callback(client.close)
    return client, connection


def test_chat_and_file_transfer_over_tls(accounts, cert, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'secret.txt'
    source.write_bytes(b'top secret')
    with running(credentials=accounts[0], tls=server_context(*cert)) as server, ExitStack() as stack:
        alice, connection = tls_client(stack, server, accounts, 'alice', cert[0])
        bob, _ = tls_client(stack, server, accounts, 'bob', cert[0])
        assert isinstance(connection, ssl.SSLSocket) and connection.version() in {'TLSv1.2', 'TLSv1.3'}
        assert alice.send_private('bob', 'hi').type == MessageType.CHAT_ACK
        transfer_id = alice.offer_file('bob', source)
        event(bob, MessageType.FILE_OFFER, transfer_id)
        bob.accept_file(transfer_id)
        event(bob, MessageType.FILE_ACCEPT)
        assert bob.transfer_events.get(timeout=5).status == TransferStatus.COMPLETED
        assert (tmp_path / 'downloads' / 'secret.txt').read_bytes() == b'top secret'


def test_client_rejects_an_untrusted_certificate(accounts, cert, tmp_path):
    other_cert, _ = make_cert(tmp_path)
    with running(credentials=accounts[0], tls=server_context(*cert)) as server:
        with pytest.raises(ssl.SSLCertVerificationError):
            connect_hello('127.0.0.1', server.address[1], timeout=3, cafile=other_cert)


def test_plaintext_client_cannot_talk_to_tls_server(accounts, cert):
    with running(credentials=accounts[0], tls=server_context(*cert)) as server:
        with pytest.raises((OSError, EOFError)):
            connect_hello('127.0.0.1', server.address[1], timeout=3)


def test_lan_bind_and_connect_require_tls(cert):
    TCPServer(host='0.0.0.0', port=0, tls=server_context(*cert)).shutdown()  # allowed with TLS
    with pytest.raises(ValueError, match='loopback'):
        TCPServer(host='0.0.0.0', port=0)
    with pytest.raises(ValueError, match='--cafile'):
        open_connection('192.0.2.1', 5000, 1)  # refused before any packet is sent


def test_discovery_reports_tls(cert):
    with running(tls=server_context(*cert)) as server:
        servers = discover_servers(server.address[1], 1, address='127.0.0.1')
        assert [s['tls'] for s in servers] == [True]
