"""Stage 9: UDP discovery. A client finds the server with DISCOVER / SERVER_INFO."""
import socket

from client.discovery import discover_servers
from client.__main__ import main as client_main
from common.protocol import encode_message
from common.builders import build_server_info
from tests.integration.test_concurrency import running


def test_client_discovers_server_tcp_port(capsys):
    with running() as server:
        port = server.address[1]
        # Sent directly to loopback; on a LAN the default broadcast address is used.
        assert discover_servers(port, 1, address='127.0.0.1') == [
            {'name': 'LanLink', 'ip': '127.0.0.1', 'port': port, 'tls': False}]


def test_server_ignores_junk_and_keeps_answering():
    with running() as server:
        port = server.address[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(b'not a frame', ('127.0.0.1', port))
            # A SERVER_INFO is a reply, never a valid request.
            udp.sendto(encode_message(build_server_info('0' * 32, 'fake', 1, tls=False)),
                       ('127.0.0.1', port))
        assert len(discover_servers(port, 1, address='127.0.0.1')) == 1


def test_no_server_returns_empty_list(capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as unused:
        unused.bind(('127.0.0.1', 0))  # a port with no server behind it
        port = unused.getsockname()[1]
        assert discover_servers(port, 0.3, address='127.0.0.1') == []
    assert client_main(['--discover', '--port', str(port)]) == 0
    assert 'No server found' in capsys.readouterr().out
