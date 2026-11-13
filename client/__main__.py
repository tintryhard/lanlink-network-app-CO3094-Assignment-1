"""CLI client: python -m client [--discover] [--host H --port P --username U --cafile CERT]."""
import argparse
import getpass
import math
import os
import queue
import sys
import threading

from common.errors import LanLinkError, ProtocolError
from common.handshake import initiate_hello
from common.models import Message, MessageType, TransferSnapshot
from common.protocol import Direction
from common.transport import SocketTransport
from client.connection import authenticate, connect_hello, open_connection
from client.discovery import discover_servers
from client.display import describe_file_event, describe_transfer_end, format_progress, format_users
from client.private_chat import PrivateChatClient


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
    """Interactive session: connect, log in, then run the command loop until logout or disconnect.

    Three threads cooperate. The reader thread (inside PrivateChatClient) prints
    chats and puts file events on `commands`. The input thread reads stdin and puts
    typed lines on `commands`. This main thread takes items from `commands` one at a
    time and handles them, so commands and file events never run in parallel.
    """
    with open_connection(host, port, timeout, cafile) as connection:
        transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
        initiate_hello(transport)
        print(f'Connected to {host}:{port} ({"TLS" if cafile else "plaintext, localhost only"})')
        authenticate(transport, username, getpass.getpass('Password: '))
        print(f'Authenticated as {username}')

        def display(message: Message) -> None:
            """Reader thread: print an incoming chat as [sender -> recipient] text."""
            recipient = 'all' if message.type == MessageType.CHAT_PUBLIC else username
            print(f'\n[{message.header["sender"]} -> {recipient}] '
                  f'{message.payload.decode("utf-8")}', flush=True)

        # Everything the main loop handles: typed lines (str), file events (Message or
        # final TransferSnapshot), an input error, or None when disconnected.
        commands: queue.Queue[str | Message | BaseException | None] = queue.Queue()
        disconnected = threading.Event()
        read_next = threading.Event()

        def notify_disconnect() -> None:
            """Reader thread: wake the main loop and the input thread when the connection ends."""
            disconnected.set()
            commands.put_nowait(None)
            read_next.set()
            print('\nDisconnected.', flush=True)

        def show_progress(snapshot: TransferSnapshot) -> None:
            """Engine callback: redraw the progress bar of a running transfer."""
            # \r rewrites the same line, so the bar grows in place.
            print(f'\r{format_progress(snapshot)}', end='', flush=True)

        def read_commands() -> None:
            """Input thread: read one line each time read_next is set and queue it.

            The main loop sets read_next only after it finished the previous command, so
            the '> ' prompt appears once per command. End of input (Ctrl+D) means /logout.
            """
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
                                   on_disconnect=notify_disconnect, transfer_events=commands,
                                   on_progress=show_progress)
        try:
            print(format_users(client.users(), username))
            help_text = ('Commands: /msg USER TEXT, /all TEXT, /offer USER PATH, '
                         '/accept ID, /reject ID [REASON], /users (or Enter), /logout')
            print(help_text)
            # Only this daemon reads stdin. Raw reads avoid a buffered-stdin lock
            # preventing interpreter exit while it waits for an unfinished line.
            threading.Thread(target=read_commands, name='lanlink-cli-input', daemon=True).start()
            read_next.set()  # let the input thread show the first prompt

            while not disconnected.is_set():
                command = commands.get()
                if command is None or disconnected.is_set():
                    return
                if isinstance(command, BaseException):
                    raise command
                if isinstance(command, TransferSnapshot):  # A transfer has ended.
                    print(f'\n{format_progress(command)}\n{describe_transfer_end(command)}', flush=True)
                    continue
                if isinstance(command, Message):  # A file event from the peer.
                    print(f'\n{describe_file_event(command)}', flush=True)
                    continue
                command = command.strip()
                try:
                    if command in {'logout', '/logout'}:
                        client.logout()
                        return
                    if command in {'', 'users', '/users'}:
                        print(format_users(client.users(), username))
                    elif command.startswith('/msg '):
                        # partition splits at the first space: ('bob', ' ', 'hello there').
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
    """Entry point: --discover lists servers, --username logs in, otherwise only test the handshake."""
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
