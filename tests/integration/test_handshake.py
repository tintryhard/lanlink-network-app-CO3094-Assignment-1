"""Real localhost TCP; threads only coordinate the test peers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import socket

import pytest

from client.connection import connect_hello
from common.errors import ErrorCode, ProtocolError
from common.models import MessageType
from common.protocol import Direction, encode_message
from common.builders import build_hello, build_hello_ack, build_ping
from common.transport import SocketTransport
from common.handshake import accept_hello


def handle_connection(connection: socket.socket):
    """Minimal server side: answer one HELLO, then close the socket."""
    with connection:
        return accept_hello(SocketTransport(connection, direction=Direction.CLIENT_TO_SERVER))


@contextmanager
def peer(handler):
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)

        def run():
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                return handler(connection)

        future = pool.submit(run)
        yield listener.getsockname(), future


def test_real_client_server_handshake_and_cleanup():
    accepted = []

    def server(connection):
        accepted.append(connection)
        return handle_connection(connection)

    with peer(server) as (address, future):
        reply = connect_hello(*address)
        hello = future.result(timeout=3)
        assert reply.type == MessageType.HELLO_ACK
        assert reply.header['ref_request_id'] == hello.header['request_id']
        assert accepted[0].fileno() == -1


def test_fragmented_hello_and_server_closes_after_ack():
    with peer(handle_connection) as (address, future), socket.create_connection(address, timeout=2) as connection:
        hello = build_hello()
        for byte in encode_message(hello):
            connection.sendall(bytes([byte]))
        transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
        assert transport.receive().header['ref_request_id'] == hello.header['request_id']
        assert transport.receive() is None
        future.result(timeout=3)


@pytest.mark.parametrize('mode', ['fragmented', 'wrong_id', 'wrong_type', 'eof', 'truncated'])
def test_client_reply_paths(mode):
    def server(connection):
        transport = SocketTransport(connection, direction=Direction.CLIENT_TO_SERVER)
        hello = transport.receive()
        if mode == 'eof':
            return
        if mode == 'wrong_type':
            transport.send(build_ping())
            return
        frame = encode_message(build_hello_ack('wrong' if mode == 'wrong_id' else hello.header['request_id'], 'Test'))
        if mode == 'truncated':
            connection.sendall(frame[:6])
        else:
            for byte in frame:
                connection.sendall(bytes([byte]))

    with peer(server) as (address, future):
        if mode == 'fragmented':
            assert connect_hello(*address).header['server_name'] == 'Test'
        else:
            with pytest.raises(EOFError if mode == 'eof' else ProtocolError) as error:
                connect_hello(*address)
            if mode == 'truncated':
                assert error.value.code == ErrorCode.UNEXPECTED_EOF
        future.result(timeout=3)


@pytest.mark.parametrize('data,expected', [(b'', EOFError), (b'\x00\x00', ProtocolError),
                                         (encode_message(build_ping()), ProtocolError),
                                         (b'\x00\x00\x00\x00', ProtocolError)])
def test_server_rejects_disconnect_truncation_wrong_message_and_bad_frame(data, expected):
    accepted = []

    def server(connection):
        accepted.append(connection)
        return handle_connection(connection)

    with peer(server) as (address, future), socket.create_connection(address, timeout=2) as connection:
        connection.sendall(data)
        connection.shutdown(socket.SHUT_WR)
        with pytest.raises(expected):
            future.result(timeout=3)
        assert accepted[0].fileno() == -1
        assert connection.recv(1) == b''


def test_transport_retains_coalesced_messages():
    def server(connection):
        connection.sendall(encode_message(build_ping()) + encode_message(build_ping()))

    with peer(server) as (address, future), socket.create_connection(address, timeout=2) as connection:
        transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
        assert transport.receive().type == MessageType.PING
        assert transport.receive().type == MessageType.PING
        assert transport.receive() is None
        future.result(timeout=3)


def test_timeout_closes_accepted_connection():
    accepted = []

    def server(connection):
        accepted.append(connection)
        connection.settimeout(0.05)
        return handle_connection(connection)

    with peer(server) as (address, future), socket.create_connection(address, timeout=2) as connection:
        with pytest.raises(TimeoutError):
            future.result(timeout=3)
        assert accepted[0].fileno() == -1
        assert connection.recv(1) == b''
