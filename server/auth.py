"""Local salted password hashes and an interactive credential-file command."""
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile

from common.errors import ProtocolError
from common.protocol import build_auth

ITERATIONS = 600_000


class CredentialStore:
    def __init__(self, users):
        # Copy the records so verification can run concurrently without mutation.
        self._users = dict(users)
        self._dummy = (secrets.token_bytes(16), secrets.token_bytes(32))

    @classmethod
    def load(cls, path):
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
        salt, expected = self._users.get(username, self._dummy)
        actual = hashlib.pbkdf2_hmac('sha256', password, salt, ITERATIONS)
        matches = hmac.compare_digest(actual, expected)
        return matches and username in self._users


def save_user(path, username: str, password: str):
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
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
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
