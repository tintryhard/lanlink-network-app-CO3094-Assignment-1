"""Text formatting for the CLI: user list, transfer progress bar and file event lines."""
from typing import Any

from common.errors import LanLinkError
from common.models import Message, MessageType, TransferDirection, TransferSnapshot, TransferStatus


def format_users(users: list[dict[str, Any]], me: str) -> str:
    """Render the USER_LIST payload as one line per online user."""
    lines = [f'Online users ({len(users)}):']
    for user in users:
        you = ' (you)' if user['username'] == me else ''
        lines.append(f'  {user["username"]:<20} {user["ip"]}:{user["port"]}{you}')
    return '\n'.join(lines)


def format_progress(snapshot: TransferSnapshot, width: int = 20) -> str:
    """Render a text progress bar such as `[#####...............]  25% a.txt 256/1024 B`."""
    done = int(snapshot.fraction * width)
    arrow = 'to' if snapshot.direction == TransferDirection.SEND else 'from'
    return (f'[{"#" * done}{"." * (width - done)}] {snapshot.fraction:4.0%} '
            f'{snapshot.filename} {arrow} {snapshot.peer} '
            f'{snapshot.processed_bytes}/{snapshot.total_bytes} B')


def describe_transfer_end(snapshot: TransferSnapshot) -> str:
    """One line telling how a transfer ended (saved path, verified, or the error)."""
    if snapshot.saved_path:
        result = f'completed, saved to {snapshot.saved_path}'
    elif snapshot.status == TransferStatus.COMPLETED:
        result = f'completed, {snapshot.peer} verified the file'
    else:
        result = f'{snapshot.status.value}: {snapshot.error.message if snapshot.error else ""}'
    return f'File {snapshot.transfer_id} ({snapshot.filename}) {result}'


def describe_file_event(message: Message) -> str:
    """One line for a file message from the peer: an offer, an error or a decision."""
    transfer_id = message.header['transfer_id']
    if message.type == MessageType.FILE_OFFER:
        return (f'File offer {transfer_id} from {message.header["sender"]}: '
                f'{message.header["filename"]} ({message.header["size"]} bytes). '
                f'Use /accept {transfer_id} or /reject {transfer_id}.')
    if message.type == MessageType.ERROR:
        return f'File {transfer_id}: {LanLinkError(message.header["code"])}'
    return f'File {transfer_id}: {message.type.value}'
