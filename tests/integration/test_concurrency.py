"""Bounded real-localhost tests for Stage 2 (no application features)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import socket
import struct
import threading
import time

import pytest

from common.handshake import initiate_hello
from common.models import ConnectionState
from common.protocol import Direction, build_ping
from common.transport import SocketTransport
from server.app import TCPServer


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('Condition did not become true within timeout')
        time.sleep(0.01)


@contextmanager
def running(**kwargs):
    kwargs.setdefault('port', 0)
    server = TCPServer(**kwargs)
    errors = []

    def serve():
        try:
            server.serve_forever()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not errors
        assert not server.connections()


def connect(stack, server):
    return stack.enter_context(socket.create_connection(server.address, timeout=2))


def handshake(connection):
    return initiate_hello(SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT))


def test_twenty_simultaneously_connected_clients():
    with running() as server, ExitStack() as stack:
        clients = [connect(stack, server) for _ in range(20)]
        wait_until(lambda: len(server.connections()) == 20)
        barrier = threading.Barrier(20, timeout=3)

        def start(connection):
            barrier.wait()
            return handshake(connection)

        with ThreadPoolExecutor(max_workers=20) as pool:
            replies = list(pool.map(start, clients))
        assert len(replies) == 20
        wait_until(lambda: all(r.state == ConnectionState.HELLO_ACCEPTED
                               for r in server.connections().values()))
        records = server.connections()
        assert len(records) == 20
        assert len({r.worker.ident for r in records.values()}) == 20
        assert all(r.worker.is_alive() for r in records.values())
        for client in clients:
            client.close()
        wait_until(lambda: not server.connections())


def test_slow_client_isolated_and_absolute_deadline():
    with running(handshake_timeout=0.3) as server, ExitStack() as stack:
        slow = connect(stack, server)
        slow.sendall(b'\x00')
        fast = connect(stack, server)
        assert handshake(fast).header['server_name'] == 'LanLink'
        # Keep supplying bytes before a per-recv timeout could expire.
        deadline = time.monotonic() + 0.7
        while time.monotonic() < deadline:
            try:
                slow.sendall(b'\x00')
            except OSError:
                break
            time.sleep(0.05)
        wait_until(lambda: len(server.connections()) == 1)
        assert next(iter(server.connections().values())).state == ConnectionState.HELLO_ACCEPTED


@pytest.mark.parametrize('bad', [b'', b'\x00\x00', b'\x00\x00\x00\x00'])
def test_bad_peer_does_not_affect_other_connections(bad):
    with running() as server, ExitStack() as stack:
        healthy = connect(stack, server)
        handshake(healthy)
        broken = connect(stack, server)
        broken.sendall(bad)
        broken.shutdown(socket.SHUT_WR)
        assert broken.recv(1) == b''
        wait_until(lambda: len(server.connections()) == 1)
        assert handshake(connect(stack, server))


def test_reset_after_handshake_and_cleanup():
    with running() as server, ExitStack() as stack:
        client = connect(stack, server)
        handshake(client)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        client.close()
        wait_until(lambda: not server.connections())
        assert handshake(connect(stack, server))


def test_capacity_limit_and_idle_cleanup():
    with running(max_clients=1, idle_timeout=0.2) as server, ExitStack() as stack:
        first = connect(stack, server)
        handshake(first)
        extra = connect(stack, server)
        try:
            assert extra.recv(1) == b''
        except ConnectionResetError:
            pass
        assert len(server.connections()) <= 1
        wait_until(lambda: not server.connections())
        assert handshake(connect(stack, server))


def test_shutdown_interrupts_workers_and_allows_same_port_restart():
    with running() as server, ExitStack() as stack:
        active = connect(stack, server)
        handshake(active)
        stalled = connect(stack, server)
        stalled.sendall(b'\x00')
        wait_until(lambda: len(server.connections()) == 2)
        records = list(server.connections().values())
        address = server.address
        server.shutdown()
        assert all(not record.worker.is_alive() and record.socket.fileno() == -1
                   for record in records)
        assert not server.connections()
        for client in (active, stalled):
            assert client.recv(1) == b''
        with running(host=address[0], port=address[1]) as replacement:
            assert handshake(connect(stack, replacement))


def test_concurrent_writes_preserve_frames():
    # Independent localhost listener exercises the shared transport's write lock.
    with socket.socket() as listener, ExitStack() as stack:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        client = stack.enter_context(socket.create_connection(listener.getsockname(), timeout=3))
        accepted, _ = listener.accept()
        stack.enter_context(accepted)
        accepted.settimeout(3)
        accepted.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        sender = SocketTransport(accepted, direction=Direction.CLIENT_TO_SERVER)
        receiver = SocketTransport(client, direction=Direction.SERVER_TO_CLIENT)
        messages = [build_ping() for _ in range(400)]
        barrier = threading.Barrier(4, timeout=3)

        def send_batch(index):
            barrier.wait()
            for message in messages[index::4]:
                sender.send(message)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(send_batch, index) for index in range(4)]
            received = [receiver.receive() for _ in messages]
            for future in futures:
                future.result(timeout=3)
        assert {m.header['request_id'] for m in received} == {m.header['request_id'] for m in messages}
