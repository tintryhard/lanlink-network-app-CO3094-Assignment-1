"""Pure Vietnamese transfer text; formatting never reads files or changes state."""
from common.errors import user_message
from common.models import TransferDirection as D, TransferSnapshot, TransferStatus as S

TEXT = {
    'incoming': '{peer} muốn gửi file cho bạn', 'waiting': 'Đang chờ {peer} xác nhận…',
    'accepted': 'Đã chấp nhận, đang bắt đầu…', 'verifying': 'Đang xác minh toàn vẹn…',
    'completed': 'Đã hoàn tất', 'rejected_send': 'Đã bị từ chối',
    'rejected_receive': 'Bạn đã từ chối', 'cancelled': 'Đã hủy',
    'failed': 'Truyền file thất bại', 'from': 'Từ {peer}', 'to': 'Gửi cho {peer}',
}


def format_bytes(value: int) -> str:
    """Binary units, one Vietnamese decimal digit after KiB, and no division by zero."""
    amount = float(max(0, value))
    units = ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB')
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            number = str(int(amount)) if unit == 'B' else f'{amount:.1f}'.replace('.', ',')
            return f'{number} {unit}'
        amount /= 1024
    raise AssertionError('Unreachable unit')


def status_text(snapshot: TransferSnapshot) -> str:
    """Text derives only from the received snapshot, never from a button click."""
    status = snapshot.status
    if status == S.OFFERED:
        return TEXT['incoming' if snapshot.direction == D.RECEIVE else 'waiting'].format(peer=snapshot.peer)
    if status == S.TRANSFERRING:
        return f'{format_bytes(snapshot.processed_bytes)} / {format_bytes(snapshot.total_bytes)} · {snapshot.fraction:.0%}'
    if status == S.REJECTED:
        return TEXT['rejected_receive' if snapshot.direction == D.RECEIVE else 'rejected_send']
    if status == S.FAILED and snapshot.error:
        translated = user_message(snapshot.error.code)
        detail = snapshot.error.message.strip()
        return translated + (f'\n{detail}' if detail and detail != translated else '')
    return TEXT[status.value]


def peer_text(snapshot: TransferSnapshot) -> str:
    return TEXT['from' if snapshot.direction == D.RECEIVE else 'to'].format(peer=snapshot.peer)
