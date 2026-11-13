"""Open a TCP (or TLS) connection to the server, then HELLO, AUTH and USER_LIST requests."""
import ipaddress
import json
import socket
from typing import Any

from common.builders import build_auth, build_user_list
from common.errors import ErrorCode, LanLinkError, ProtocolError
from common.handshake import initiate_hello
from common.models import Message, MessageType
from common.protocol import Direction
from common.tls import client_context
from common.transport import SocketTransport


def open_connection(host: str, port: int, timeout: float, cafile: str | None = None) -> socket.socket:
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
    """Connect, exchange HELLO / HELLO_ACK and close. Returns the HELLO_ACK."""
    with open_connection(host, port, timeout, cafile) as connection:
        return initiate_hello(SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT))


def authenticate(transport: SocketTransport, username: str, password: str) -> Message:
    """Send AUTH and check the AUTH_OK reply; a wrong password raises LanLinkError."""
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
    """Send USER_LIST and return the users from the reply (used before the reader thread exists)."""
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
