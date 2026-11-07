"""Sequential, handshake-only TCP server."""
import argparse
import socket
import sys

from common.errors import ProtocolError
from common.handshake import accept_hello
from common.protocol import Direction
from common.transport import SocketTransport


def handle_connection(connection: socket.socket):
    """Take ownership of an accepted socket and close it on every exit path."""
    with connection:
        return accept_hello(SocketTransport(connection, direction=Direction.CLIENT_TO_SERVER))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--timeout', type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.host, args.port))
            listener.listen(1)
            print(f'Listening on {listener.getsockname()}', flush=True)
            while True:
                connection, address = listener.accept()
                try:
                    with connection:
                        connection.settimeout(args.timeout)
                        hello = handle_connection(connection)
                    print(f'HELLO from {hello.header["client_name"]} at {address}', flush=True)
                except (OSError, EOFError, ProtocolError) as error:
                    print(f'Handshake failed for {address}: {error}', file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        print(f'Server error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
