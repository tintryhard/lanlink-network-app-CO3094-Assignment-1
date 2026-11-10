"""Concurrent TCP server with local authentication."""
import argparse
import math
import socket
import sys

from server.app import TCPServer
from server.auth import CredentialStore
from common.handshake import accept_hello
from common.models import Message
from common.protocol import Direction
from common.transport import SocketTransport


def handle_connection(connection: socket.socket) -> Message:
    """Take ownership of an accepted socket and close it on every exit path."""
    with connection:
        return accept_hello(SocketTransport(connection, direction=Direction.CLIENT_TO_SERVER))


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--max-clients', type=int, default=64)
    parser.add_argument('--idle-timeout', type=float, default=120.0)
    parser.add_argument('--transfer-timeout', type=float, default=60.0,
                        help='Seconds allowed for each offered/accepted negotiation phase')
    parser.add_argument('--users', help='Path to hashed credential JSON (requires loopback bind)')
    args = parser.parse_args(argv)
    if (not math.isfinite(args.timeout) or args.timeout <= 0 or
            not math.isfinite(args.idle_timeout) or args.idle_timeout <= 0 or
            not math.isfinite(args.transfer_timeout) or args.transfer_timeout <= 0 or args.max_clients < 1):
        parser.error("timeouts must be positive and finite; max-clients must be positive")
    server = None
    try:
        credentials = CredentialStore.load(args.users) if args.users else None
        server = TCPServer(args.host, args.port, max_clients=args.max_clients,
                           handshake_timeout=args.timeout, idle_timeout=args.idle_timeout, credentials=credentials,
                           transfer_timeout=args.transfer_timeout)
        print(f'Listening on {server.address}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f'Server error: {error}', file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
