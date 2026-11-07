"""Stage 1 application handshake, independent of socket operations."""
from common.errors import ErrorCode, ProtocolError
from common.models import Message, MessageType
from common.protocol import build_hello, build_hello_ack


def accept_hello(transport, server_name: str = 'LanLink') -> Message:
    hello = transport.receive()
    if hello is None:
        raise EOFError('Peer disconnected before HELLO')
    if hello.type != MessageType.HELLO:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'Expected HELLO')
    reply = build_hello_ack(hello.header['request_id'], server_name)
    transport.send(reply)
    return hello


def initiate_hello(transport, client_name: str = 'LanLink') -> Message:
    hello = build_hello(client_name)
    transport.send(hello)
    reply = transport.receive()
    if reply is None:
        raise EOFError('Peer disconnected before HELLO_ACK')
    if reply.type != MessageType.HELLO_ACK:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'Expected HELLO_ACK')
    if reply.header['ref_request_id'] != hello.header['request_id']:
        raise ProtocolError(ErrorCode.INVALID_FIELD, 'HELLO_ACK does not match HELLO')
    return reply
