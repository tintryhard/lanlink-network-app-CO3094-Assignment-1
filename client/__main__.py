"""TCP handshake, localhost login and private chat demonstration."""
import argparse
import getpass
import ipaddress
import json
import math
import os
import queue
import socket
import sys
import threading
from typing import Any

from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.handshake import initiate_hello
from common.models import Message, MessageType
from common.protocol import Direction, build_auth, build_logout, build_user_list
from common.transport import SocketTransport
from client.private_chat import PrivateChatClient


def _connect_local(host: str, port: int, timeout: float) -> socket.socket:
    address = socket.gethostbyname(host)
    if not ipaddress.ip_address(address).is_loopback:
        raise ValueError('TCP client is restricted to localhost until TLS is implemented')
    return socket.create_connection((address, port), timeout=timeout)


def connect_hello(host: str, port: int, *, timeout: float = 5.0) -> Message:
    with _connect_local(host, port, timeout) as connection:
        if not ipaddress.ip_address(connection.getpeername()[0]).is_loopback:
            raise ValueError('TCP client is restricted to localhost until TLS is implemented')
        return initiate_hello(SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT))


def authenticate(transport: SocketTransport, username: str, password: str) -> Message:
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


def request_users(transport: SocketTransport) -> list[dict[str, Any]]:
    request = build_user_list()
    transport.send(request)
    reply = transport.receive()
    if reply is None:
        raise EOFError('Server disconnected before USER_LIST')
    if reply.header.get('ref_request_id') != request.header['request_id']:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'USER_LIST response does not match request')
    if reply.type == MessageType.ERROR:
        raise LanLinkError(reply.header['code'])
    if reply.type != MessageType.USER_LIST:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'Expected USER_LIST')
    return json.loads(reply.payload)['users']


def _read_command(prompt: str) -> str:
    """Read a line without holding stdin's buffered lock during interpreter exit."""
    print(prompt, end='', flush=True)
    data = bytearray()
    while True:
        byte = os.read(sys.stdin.fileno(), 1)
        if not byte:
            if not data:
                raise EOFError
            break
        if byte == b'\n':
            break
        data.extend(byte)
    return data.decode(sys.stdin.encoding or 'utf-8', errors=sys.stdin.errors or 'strict').rstrip('\r')


def login_cli(host: str, port: int, username: str, timeout: float) -> None:
    with _connect_local(host, port, timeout) as connection:
        if not ipaddress.ip_address(connection.getpeername()[0]).is_loopback:
            raise ValueError('Password login is restricted to localhost until TLS is implemented')
        transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
        initiate_hello(transport)
        authenticate(transport, username, getpass.getpass('Password: '))
        print(f'Authenticated as {username}')
        def display(message: Message) -> None:
            print(f'\n[{message.header["sender"]} -> {username}] '
                  f'{message.payload.decode("utf-8")}', flush=True)

        commands: queue.Queue[str | BaseException | None] = queue.Queue()
        disconnected = threading.Event()
        read_next = threading.Event()

        def notify_disconnect() -> None:
            disconnected.set()
            commands.put_nowait(None)
            read_next.set()
            print('\nDisconnected.', flush=True)

        def read_commands() -> None:
            while True:
                read_next.wait()
                read_next.clear()
                if disconnected.is_set():
                    return
                try:
                    command = _read_command('> ')
                except EOFError:
                    command = '/logout'
                except BaseException as error:
                    commands.put_nowait(error)
                    return
                if disconnected.is_set():
                    return
                commands.put_nowait(command)

        client = PrivateChatClient(transport, timeout=timeout, on_message=display,
                                   on_disconnect=notify_disconnect)
        try:
            print(json.dumps({'users': client.users()}, ensure_ascii=False, indent=2))
            print('Commands: /msg USER TEXT, /users (or Enter), /logout')
            # Only this daemon reads stdin. Raw reads avoid a buffered-stdin lock
            # preventing interpreter exit while it waits for an unfinished line.
            threading.Thread(target=read_commands, name='lanlink-cli-input', daemon=True).start()
            while not disconnected.is_set():
                read_next.set()
                command = commands.get()
                if command is None or disconnected.is_set():
                    return
                if isinstance(command, BaseException):
                    raise command
                command = command.strip()
                try:
                    if command in {'logout', '/logout'}:
                        client.logout()
                        return
                    if command in {'', 'users', '/users'}:
                        print(json.dumps({'users': client.users()}, ensure_ascii=False, indent=2))
                    elif command.startswith('/msg '):
                        recipient, separator, text = command[5:].partition(' ')
                        if not separator or not text:
                            print('Usage: /msg USER TEXT')
                            continue
                        ack = client.send_private(recipient, text)
                        print(f'Accepted: {ack.header["message_id"]} (server acceptance; not a read receipt)')
                    else:
                        print('Commands: /msg USER TEXT, /users, /logout')
                except LanLinkError as error:
                    if isinstance(error, ProtocolError) and connection.fileno() == -1:
                        raise
                    # A local builder/encoder rejection has not damaged the
                    # connection; let the user correct the command and retry.
                    print(f'Request failed: {error}')
        except (OSError, EOFError):
            # Network loss can also wake an in-flight request rather than the
            # command queue. Do not silently classify stdin errors as disconnects.
            if connection.fileno() != -1:
                raise
            return
        finally:
            client.close()


def main(argv: list[str] | None = None) -> int:
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
