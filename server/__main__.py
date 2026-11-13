"""Run the LanLink server: python -m server --host HOST --port PORT [--users FILE] [--cert C --key K]."""
import argparse
import math
import sys

from server.app import TCPServer
from server.auth import CredentialStore
from common.tls import server_context


def main(argv: list[str] | None = None) -> int | None:
    """CLI entry point: parse options, build TCPServer (with TLS if --cert/--key) and run it until Ctrl+C."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--max-clients', type=int, default=64)
    parser.add_argument('--idle-timeout', type=float, default=120.0)
    parser.add_argument('--transfer-timeout', type=float, default=60.0,
                        help='Seconds allowed for each offered/accepted negotiation phase')
    parser.add_argument('--users', help='Path to hashed credential JSON')
    parser.add_argument('--cert', help='TLS certificate (PEM); required to bind a non-loopback host')
    parser.add_argument('--key', help='TLS private key (PEM) for --cert')
    args = parser.parse_args(argv)
    if (args.cert is None) != (args.key is None):
        parser.error('--cert and --key must be given together')
    if (not math.isfinite(args.timeout) or args.timeout <= 0 or
            not math.isfinite(args.idle_timeout) or args.idle_timeout <= 0 or
            not math.isfinite(args.transfer_timeout) or args.transfer_timeout <= 0 or args.max_clients < 1):
        parser.error("timeouts must be positive and finite; max-clients must be positive")
    server = None
    try:
        credentials = CredentialStore.load(args.users) if args.users else None
        tls = server_context(args.cert, args.key) if args.cert else None
        server = TCPServer(args.host, args.port, max_clients=args.max_clients,
                           handshake_timeout=args.timeout, idle_timeout=args.idle_timeout, credentials=credentials,
                           transfer_timeout=args.transfer_timeout, tls=tls)
        print(f'Listening on {server.address}{" (TLS)" if tls else ""}', flush=True)
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
