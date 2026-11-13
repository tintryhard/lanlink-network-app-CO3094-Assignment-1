"""Password store (salted PBKDF2 hashes in a JSON file) and the account-creation command."""
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Mapping

from common.errors import ProtocolError
from common.builders import build_auth

# PBKDF2-HMAC-SHA256 rounds: slow on purpose, so guessing passwords from a stolen file is costly.
ITERATIONS = 600_000


class CredentialStore:
    """Accounts loaded from the JSON user file: username -> (salt, PBKDF2 hash)."""
    def __init__(self, users: Mapping[str, tuple[bytes, bytes]]) -> None:
        """Keep a copy of the records plus a random dummy record (see verify)."""
        # Copy the records so verification can run concurrently without mutation.
        self._users = dict(users)
        self._dummy = (secrets.token_bytes(16), secrets.token_bytes(32))

    @classmethod
    def load(cls, path: str | Path) -> 'CredentialStore':
        """Read and check the user file: {"version": 1, "users": {name: {salt, hash}}}."""
        try:
            obj = json.loads(Path(path).read_text(encoding='utf-8'))
            if (not isinstance(obj, dict) or set(obj) != {'version', 'users'} or
                    type(obj['version']) is not int or obj['version'] != 1 or
                    not isinstance(obj['users'], dict)):
                raise ValueError
            users = {}
            for username, record in obj['users'].items():
                # Reuse the protocol's username validation; never retain this payload.
                build_auth(username, 'validation')
                if not isinstance(record, dict) or set(record) != {'salt', 'hash'}:
                    raise ValueError
                salt, digest = bytes.fromhex(record['salt']), bytes.fromhex(record['hash'])
                if len(salt) != 16 or len(digest) != 32:
                    raise ValueError
                users[username] = (salt, digest)
            return cls(users)
        except (ValueError, TypeError, KeyError, ProtocolError):
            raise ValueError('Invalid credential file; expected version 1 salted PBKDF2 records') from None

    def verify(self, username: str, password: bytes) -> bool:
        """True if `password` matches the stored hash of `username`.

        An unknown user is checked against the dummy record, so a wrong name and a
        wrong password take the same time and an attacker cannot tell them apart.
        """
        salt, expected = self._users.get(username, self._dummy)
        actual = hashlib.pbkdf2_hmac('sha256', password, salt, ITERATIONS)
        matches = hmac.compare_digest(actual, expected)  # constant-time comparison
        return matches and username in self._users


def save_user(path: str | Path, username: str, password: str) -> None:
    """Create/update a local account using an atomic file replacement."""
    build_auth(username, password)
    path = Path(path)
    store = CredentialStore.load(path) if path.exists() else CredentialStore({})
    salt = secrets.token_bytes(16)
    store._users[username] = (salt, hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS))
    obj = {'version': 1, 'users': {name: {'salt': s.hex(), 'hash': digest.hex()}
                                 for name, (s, digest) in store._users.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp uses owner-only permissions on POSIX; never write plaintext.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.users-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(obj, output, indent=2, sort_keys=True)
            output.write('\n')
        os.replace(temporary, path)  # atomic rename: readers see the old file or the new one, never half
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> None:
    """CLI: ask for a password twice and save the account (python -m server.auth)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', default='data/users.json')
    parser.add_argument('--username', required=True)
    args = parser.parse_args(argv)
    try:
        password = getpass.getpass('New password: ')
        if password != getpass.getpass('Confirm password: '):
            parser.error('Passwords do not match')
        save_user(args.file, args.username, password)
    except (OSError, ValueError, ProtocolError, EOFError) as error:
        parser.error(str(error))
    print(f'Account saved for {args.username}; restart the server to load changes.')


if __name__ == '__main__':
    main()
