"""TCP handshake and localhost login demonstration."""
import argparse
import getpass
import ipaddress
import json
import math
import socket
import sys

from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.handshake import initiate_hello
from common.models import MessageType
from common.protocol import Direction, build_auth, build_logout, build_user_list
from common.transport import SocketTransport


def connect_hello(host: str, port: int, *, timeout: float = 5.0):
    with socket.create_connection((host, port), timeout=timeout) as connection:
        return initiate_hello(SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT))


def authenticate(transport, username: str, password: str):
    request = build_auth(username, password)
    transport.send(request)
    reply = transport.receive()
    if reply is None:
        raise EOFError('Server disconnected before AUTH response')
    if reply.header.get('ref_request_id') != request.header['request_id']:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'Authentication response does not match request')
    if reply.type == MessageType.ERROR:
        raise LanLinkError(reply.header['code'])
    if reply.type != MessageType.AUTH_OK or reply.header['username'] != username:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'Expected AUTH_OK for requested username')
    return reply


def request_users(transport):
    transport.send(build_user_list())
    reply = transport.receive()
    if reply is None:
        raise EOFError('Server disconnected before USER_LIST')
    if reply.type == MessageType.ERROR:
        raise LanLinkError(reply.header['code'])
    if reply.type != MessageType.USER_LIST:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'Expected USER_LIST')
    return json.loads(reply.payload)['users']


def login_cli(host, port, username, timeout):
    with socket.create_connection((host, port), timeout=timeout) as connection:
        if not ipaddress.ip_address(connection.getpeername()[0]).is_loopback:
            raise ValueError('Password login is restricted to localhost until TLS is implemented')
        transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
        initiate_hello(transport)
        authenticate(transport, username, getpass.getpass('Password: '))
        print(f'Authenticated as {username}')
        while True:
            print(json.dumps({'users': request_users(transport)}, ensure_ascii=False, indent=2))
            try:
                command = input('Enter to refresh users; logout to exit: ').strip().lower()
            except EOFError:
                command = 'logout'
            if command == 'logout':
                transport.send(build_logout())
                if transport.receive() is not None:
                    raise ProtocolError(ErrorCode.INVALID_FIELD, 'Expected close after LOGOUT')
                return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--username', help='Prompt for password, show users and remain logged in')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be positive and finite')
    try:
        if args.username:
            login_cli(args.host, args.port, args.username, args.timeout)
            return 0
        reply = connect_hello(args.host, args.port, timeout=args.timeout)
    except (OSError, EOFError, LanLinkError, ValueError) as error:
        print(f'Client failed: {error}', file=sys.stderr)
        return 1
    print(f'HELLO_ACK from {reply.header["server_name"]}; handshake complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
