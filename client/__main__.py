"""CLI client: UDP server discovery, TCP (or TLS) handshake, login, chat and file transfer."""
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
import time
from typing import Any

from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.handshake import initiate_hello
from common.models import Message, MessageType, TransferSnapshot, TransferStatus
from common.protocol import (DEFAULT_LIMITS, Direction, build_auth, build_discover, build_logout,
                             build_user_list, decode_datagram, encode_message)
from common.tls import client_context
from common.transport import SocketTransport
from client.private_chat import PrivateChatClient


def discover_servers(port: int, wait: float = 2.0,
                     address: str = '255.255.255.255') -> list[dict[str, Any]]:
    """Broadcast a UDP DISCOVER and collect SERVER_INFO replies for `wait` seconds.

    Returns one dict per server: name, ip, port (TCP) and tls.
    """
    request = build_discover()
    servers = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.sendto(encode_message(request), (address, port))
        deadline = time.monotonic() + wait
        while (remaining := deadline - time.monotonic()) > 0:
            udp.settimeout(remaining)
            try:
                data, (ip, _) = udp.recvfrom(DEFAULT_LIMITS.max_datagram_bytes + 1)
            except socket.timeout:
                break
            try:
                reply = decode_datagram(data, direction=Direction.UDP_REPLY)
            except ProtocolError:
                continue  # not a valid SERVER_INFO: ignore
            if reply.header['ref_request_id'] == request.header['request_id']:
                servers.append({'name': reply.header['server_name'], 'ip': ip,
                                'port': reply.header['port'], 'tls': reply.header['tls']})
    return servers


def _connect(host: str, port: int, timeout: float, cafile: str | None = None) -> socket.socket:
    """Open a TCP connection; with cafile, wrap it in TLS that trusts only that certificate."""
    address = socket.gethostbyname(host)
    if cafile is None and not ipaddress.ip_address(address).is_loopback:
        raise ValueError('Plaintext is restricted to localhost; pass --cafile to use TLS')
    connection = socket.create_connection((address, port), timeout=timeout)
    if cafile is None:
        return connection
    try:
        return client_context(cafile).wrap_socket(connection, server_hostname=host)
    except BaseException:
        connection.close()
        raise


def connect_hello(host: str, port: int, *, timeout: float = 5.0, cafile: str | None = None) -> Message:
    with _connect(host, port, timeout, cafile) as connection:
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


def login_cli(host: str, port: int, username: str, timeout: float, cafile: str | None = None) -> None:
    with _connect(host, port, timeout, cafile) as connection:
        transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
        initiate_hello(transport)
        authenticate(transport, username, getpass.getpass('Password: '))
        print(f'Authenticated as {username}')
        
        def display(message: Message) -> None:
            recipient = 'all' if message.type == MessageType.CHAT_PUBLIC else username
            print(f'\n[{message.header["sender"]} -> {recipient}] '
                  f'{message.payload.decode("utf-8")}', flush=True)

        commands: queue.Queue[str | Message | BaseException | None] = queue.Queue()
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
                                   on_disconnect=notify_disconnect, transfer_events=commands)
        try:
            print(json.dumps({'users': client.users()}, ensure_ascii=False, indent=2))
            help_text = ('Commands: /msg USER TEXT, /all TEXT, /offer USER PATH, '
                         '/accept ID, /reject ID [REASON], /users (or Enter), /logout')
            print(help_text)
            # Only this daemon reads stdin. Raw reads avoid a buffered-stdin lock
            # preventing interpreter exit while it waits for an unfinished line.
            threading.Thread(target=read_commands, name='lanlink-cli-input', daemon=True).start()
            read_next.set()
            
            while not disconnected.is_set():
                command = commands.get()
                if command is None or disconnected.is_set():
                    return
                if isinstance(command, BaseException):
                    raise command
                if isinstance(command, TransferSnapshot):  # A transfer has ended.
                    if command.saved_path:
                        result = f'completed, saved to {command.saved_path}'
                    elif command.status == TransferStatus.COMPLETED:
                        result = f'completed, {command.peer} verified the file'
                    else:
                        result = f'{command.status.value}: {command.error.message if command.error else ""}'
                    print(f'\nFile {command.transfer_id} ({command.filename}) {result}', flush=True)
                    continue
                if isinstance(command, Message):
                    transfer_id = command.header['transfer_id']
                    if command.type == MessageType.FILE_OFFER:
                        print(f'\nFile offer {transfer_id} from {command.header["sender"]}: '
                              f'{command.header["filename"]} ({command.header["size"]} bytes). '
                              f'Use /accept {transfer_id} or /reject {transfer_id}.', flush=True)
                    elif command.type == MessageType.ERROR:
                        print(f'\nFile {transfer_id}: {LanLinkError(command.header["code"])}', flush=True)
                    else:
                        print(f'\nFile {transfer_id}: {command.type.value}', flush=True)
                    continue
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
                        print(f'Accepted: {ack.header["message_id"]}')
                    elif command == '/all':
                        print('Usage: /all TEXT')
                    elif command.startswith('/all '):
                        ack = client.send_public(command[5:])
                        print(f'Accepted: {ack.header["message_id"]}')
                    elif command == '/offer' or command.startswith('/offer '):
                        recipient, separator, path = command[7:].partition(' ')
                        if not recipient or not separator or not path:
                            print('Usage: /offer USER PATH')
                            continue
                        transfer_id = client.offer_file(recipient, path)
                        print(f'Offer sent: {transfer_id} (awaiting recipient decision)')
                    elif command == '/accept' or command.startswith('/accept '):
                        transfer_id = command[8:].strip()
                        if not transfer_id:
                            print('Usage: /accept ID')
                            continue
                        client.accept_file(transfer_id)
                        print(f'Accepted {transfer_id}; receiving into {client.download_dir}/')
                    elif command == '/reject' or command.startswith('/reject '):
                        transfer_id, _, reason = command[8:].partition(' ')
                        if not transfer_id:
                            print('Usage: /reject ID [REASON]')
                            continue
                        client.reject_file(transfer_id, reason or None)
                    else:
                        print(help_text)
                except LanLinkError as error:
                    if isinstance(error, ProtocolError) and connection.fileno() == -1:
                        raise
                    # A local builder/encoder rejection has not damaged the
                    # connection; let the user correct the command and retry.
                    print(f'Request failed: {error}')
                finally:
                    # Unsolicited file events must not schedule extra stdin
                    # reads while an existing prompt/command is still active.
                    read_next.set()
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
    parser.add_argument('--discover', action='store_true',
                        help='Find LanLink servers on the LAN (UDP broadcast to --port) and exit')
    parser.add_argument('--cafile', help="Server's TLS certificate (PEM); required for a non-localhost host")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be positive and finite')
    try:
        if args.discover:
            servers = discover_servers(args.port)
            for server in servers:
                print(f'{server["name"]} at {server["ip"]}:{server["port"]}'
                      f'{" (TLS)" if server["tls"] else ""}')
            if not servers:
                print('No server found')
            return 0
        if args.username:
            login_cli(args.host, args.port, args.username, args.timeout, args.cafile)
            return 0
        reply = connect_hello(args.host, args.port, timeout=args.timeout, cafile=args.cafile)
    except (OSError, EOFError, LanLinkError, ValueError) as error:
        print(f'Client failed: {error}', file=sys.stderr)
        return 1
    print(f'HELLO_ACK from {reply.header["server_name"]}; handshake complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
