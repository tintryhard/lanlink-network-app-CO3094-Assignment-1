"""Handshake-only TCP client."""
import argparse
import socket
import sys

from common.errors import ProtocolError
from common.handshake import initiate_hello
from common.protocol import Direction
from common.transport import SocketTransport


def connect_hello(host: str, port: int, *, timeout: float = 5.0):
    with socket.create_connection((host, port), timeout=timeout) as connection:
        return initiate_hello(SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--timeout', type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        reply = connect_hello(args.host, args.port, timeout=args.timeout)
    except (OSError, EOFError, ProtocolError) as error:
        print(f'Handshake failed: {error}', file=sys.stderr)
        return 1
    print(f'HELLO_ACK from {reply.header["server_name"]}; handshake complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
