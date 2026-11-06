"""Portable names and exclusive destination reservations; never clobber a peer file."""
import errno
import os
from pathlib import Path
import re
import unicodedata

from common.errors import ErrorCode, TransferError


def io_error(error: OSError, *, missing: ErrorCode = ErrorCode.FILE_NOT_FOUND) -> TransferError:
    """Translate OS errors without exposing platform-specific exception strings."""
    if error.errno == errno.ENOSPC:
        code = ErrorCode.DISK_FULL
    elif error.errno in {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTDIR}:
        code = ErrorCode.PERMISSION_DENIED
    elif error.errno == errno.ENOENT:
        code = missing
    else:
        code = ErrorCode.INTERNAL
    return TransferError(code)


def _utf8_prefix(text: str, budget: int) -> str:
    return text.encode('utf-8')[:max(0, budget)].decode('utf-8', errors='ignore')


def fit_filename(name: str, budget: int = 255, suffix: str = '') -> str:
    """Keep an extension and collision/part suffix inside the UTF-8 byte budget."""
    extension = Path(name).suffix
    stem = name[:-len(extension)] if extension else name
    available = budget - len(suffix.encode('utf-8'))
    extension = _utf8_prefix(extension, max(0, available - 4))
    stem = _utf8_prefix(stem, available - len(extension.encode('utf-8')))
    return (stem or '_') + suffix + extension


def sanitize_filename(name: str) -> str:
    """Reject paths, preserve Vietnamese, and neutralise Windows special names."""
    if not isinstance(name, str):
        raise TransferError(ErrorCode.UNSAFE_FILENAME)
    try:
        name.encode('utf-8', errors='strict')
    except UnicodeError:
        raise TransferError(ErrorCode.UNSAFE_FILENAME) from None
    name = unicodedata.normalize('NFC', name)
    if (not name or name in {'.', '..'} or any(c in name for c in '/\\')
            or re.match(r'^[A-Za-z]:', name)
            or any(unicodedata.category(c) == 'Cc' for c in name)):
        raise TransferError(ErrorCode.UNSAFE_FILENAME)
    # Format controls can disguise an executable extension or create invisible
    # duplicates. Replace them as well as Windows-forbidden punctuation.
    name = ''.join('_' if c in '<>:"|?*' or unicodedata.category(c) == 'Cf'
                   else c for c in name).rstrip(' .')
    if not name or name in {'.', '..'}:
        raise TransferError(ErrorCode.UNSAFE_FILENAME)
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])', name.split('.')[0].rstrip(' '), re.I):
        name = '_' + name
    return fit_filename(name)


def reserve_destination(download_dir: Path | str, name: str) -> tuple[Path, int]:
    """Atomically reserve a unique bare name using O_EXCL, including symlinks."""
    name = sanitize_filename(name)
    try:
        directory = Path(download_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for number in range(100000):
            candidate = directory / fit_filename(name, suffix=f' ({number})' if number else '')
            if candidate.parent.resolve() != directory:
                raise TransferError(ErrorCode.UNSAFE_FILENAME)
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                return candidate, fd
            except FileExistsError:
                continue
        raise TransferError(ErrorCode.INTERNAL)
    except OSError as error:
        raise io_error(error, missing=ErrorCode.PERMISSION_DENIED) from None
