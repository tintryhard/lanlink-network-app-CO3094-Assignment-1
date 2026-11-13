"""Exercise independent golden frames and hostile, fragmented peer input."""
import json
import random
import struct
import unicodedata
from dataclasses import replace

import pytest

from common.errors import ErrorCode, ProtocolError
from common.models import Message, MessageType
from common.protocol import (DEFAULT_LIMITS, Direction, FrameDecoder, ProtocolLimits,
                             decode_datagram, decode_header, encode_message, max_payload_for,
                             validate_message)
from common.builders import (build_auth, build_chat_private, build_chat_public, build_discover,
                             build_hello, build_hello_ack, build_server_info)
from tests.fixtures import load_protocol_cases

CASES = load_protocol_cases()


def payload(case):
    return case['payload_utf8'].encode() if 'payload_utf8' in case else bytes.fromhex(case['payload_hex'])


def header(**extra):
    return dict(version=1, type='PING', request_id='r1', payload_length=0, **extra)


def raw(h):
    return json.dumps(h, ensure_ascii=True, separators=(',', ':')).encode()


@pytest.mark.parametrize('case', CASES['valid'], ids=lambda c: c['name'])
def test_golden(case):
    msg = Message(case['header'], payload(case))
    frame = bytes.fromhex(case['frame_hex'])
    assert encode_message(msg) == frame
    decoder = FrameDecoder(direction=Direction[case['direction']])
    assert decoder.feed(frame) == [msg]
    assert decoder.at_clean_boundary()
    decoder.feed_eof()


@pytest.mark.parametrize('case', CASES['invalid'], ids=lambda c: c['name'])
def test_invalid_golden(case):
    with pytest.raises(ProtocolError) as caught:
        if 'frame_hex' in case:
            d = FrameDecoder(direction=Direction[case['direction']])
            d.feed(bytes.fromhex(case['frame_hex']))
            d.feed_eof()
        else:
            validate_message(case['header'], bytes.fromhex(case['payload_hex']), direction=Direction[case['direction']])
    assert caught.value.code.value == case['expect_code']


def test_fixture_catalogue_covers_all_types():
    assert {c['header']['type'] for c in CASES['valid']} == {m.value for m in MessageType}


def test_all_splits_and_coalescing():
    messages = [build_hello(), build_chat_public('Xin chào 👨‍👩‍👧'), build_chat_private('an', 'hello')]
    stream = b''.join(encode_message(m) for m in messages)
    for boundary in range(len(stream) + 1):
        d = FrameDecoder()
        assert d.feed(stream[:boundary]) + d.feed(stream[boundary:]) == messages
        assert d.at_clean_boundary()
    for widths in ([1] * len(stream), [len(stream)], [random.Random(42+i).randint(1, 30) for i in range(len(stream))]):
        d, got, offset = FrameDecoder(), [], 0
        for width in widths:
            got.extend(d.feed(stream[offset:offset+width]))
            offset += width
            if offset >= len(stream):
                break
        assert got == messages
        assert d.at_clean_boundary()


@pytest.mark.parametrize('cut', [1, 3, 4, 8, -1])
def test_eof_and_poison(cut):
    frame = encode_message(build_chat_public('abc'))
    d = FrameDecoder()
    d.feed(frame[:cut])
    assert not d.at_clean_boundary()
    with pytest.raises(ProtocolError) as caught:
        d.feed_eof()
    assert caught.value.code == ErrorCode.UNEXPECTED_EOF
    with pytest.raises(ProtocolError) as again:
        d.feed(frame)
    assert again.value is caught.value


@pytest.mark.parametrize('prefix,code', [(0, ErrorCode.BAD_FRAME), (16385, ErrorCode.HEADER_TOO_LARGE)])
def test_prefix_rejected_immediately(prefix, code):
    d = FrameDecoder()
    with pytest.raises(ProtocolError) as caught:
        d.feed(struct.pack('!I', prefix))
    assert caught.value.code == code
    assert not d.at_clean_boundary()
    with pytest.raises(ProtocolError):
        d.feed(b'')


@pytest.mark.parametrize('data', [b'{', b'[]', b'null', b'"x"', b'\xff',
    b'{"version":1,"version":1}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":-Infinity}', b'{"x":"\\ud800"}', b'{"x":{"y":"\\udfff"}}'])
def test_strict_json(data):
    with pytest.raises(ProtocolError) as caught:
        decode_header(data)
    assert caught.value.code == ErrorCode.BAD_JSON


@pytest.mark.parametrize('value', [True, False, 1.0, -1, 2**63, None, '1'])
@pytest.mark.parametrize('field', ['version', 'payload_length'])
def test_integers_are_strict(value, field):
    h = header(); h[field] = value
    with pytest.raises(ProtocolError) as caught:
        decode_header(raw(h))
    assert caught.value.code == ErrorCode.INVALID_FIELD


@pytest.mark.parametrize('field', ['sender', 'message_id', 'timestamp_ms'])
def test_spoofed_server_fields(field):
    h = header(); h[field] = 'forged'
    with pytest.raises(ProtocolError) as caught:
        validate_message(h, b'', direction=Direction.CLIENT_TO_SERVER)
    assert caught.value.code == ErrorCode.SPOOFED_FIELD


@pytest.mark.parametrize('name', ['../x', '..\\x', 'a/b', 'a\\b', '..', '.', 'a\0b', 'a\nb', 'é'*128, ''])
def test_unsafe_filenames(name):
    h = dict(next(c['header'] for c in CASES['valid'] if c['header']['type'] == 'FILE_OFFER'))
    h['filename'] = name
    with pytest.raises(ProtocolError) as caught:
        validate_message(h, b'')
    assert caught.value.code == ErrorCode.UNSAFE_FILENAME


@pytest.mark.parametrize('field,value', [('transfer_id', 'A'*32), ('sha256', 'A'*64),
    ('chunk_size', 0), ('size', True), ('chunk_size', 65537), ('recipient', 'a b')])
def test_bad_offer_fields(field, value):
    h = dict(next(c['header'] for c in CASES['valid'] if c['header']['type'] == 'FILE_OFFER'))
    h[field] = value
    with pytest.raises(ProtocolError):
        validate_message(h, b'')


@pytest.mark.parametrize('kind,maximum', [('CHAT_PUBLIC',8192), ('CHAT_PRIVATE',8192), ('FILE_CHUNK',65536), ('AUTH',256)])
def test_payload_boundaries(kind, maximum):
    c = next(c for c in CASES['valid'] if c['header']['type'] == kind)
    h = dict(c['header']); h['payload_length'] = maximum
    validate_message(h, b'x'*maximum)
    h['payload_length'] += 1
    with pytest.raises(ProtocolError) as caught:
        decode_header(raw(h))
    assert caught.value.code == ErrorCode.PAYLOAD_TOO_LARGE
    h['payload_length'] = 0
    with pytest.raises(ProtocolError):
        validate_message(h, b'')


@pytest.mark.parametrize('text', ['Xin chào, đây là tiếng Việt có dấu', '👨‍👩‍👧', unicodedata.normalize('NFD', 'Báo cáo')])
def test_unicode_byte_lengths(text):
    msg = build_chat_public(text)
    assert msg.header['payload_length'] == len(text.encode())
    assert FrameDecoder().feed(encode_message(msg))[0].payload.decode() == text


def test_unicode_rejected_in_payload_and_builders():
    for build, args in [(build_chat_public, ('\ud800',)), (build_auth, ('an', '\ud800'))]:
        with pytest.raises(ProtocolError):
            build(*args)
    with pytest.raises(ProtocolError):
        validate_message(dict(version=1,type='CHAT_PUBLIC',request_id='r',payload_length=1), b'\xff')


@pytest.mark.parametrize('obj', [[], {'users':{}}, {'users':[{}]}, {'users':[{'username':'an','online':1,'ip':None,'port':None}]},
    {'users':[{'username':'an','online':True,'ip':'bad','port':1}]},
    {'users':[{'username':'an','online':True,'ip':None,'port':True}]}, {'users':[], 'extra':1}])
def test_user_list_shape(obj):
    p = json.dumps(obj).encode()
    h = dict(version=1,type='USER_LIST',request_id='r',ref_request_id='request',payload_length=len(p))
    with pytest.raises(ProtocolError) as caught:
        validate_message(h,p,direction=Direction.SERVER_TO_CLIENT)
    assert caught.value.code == ErrorCode.BAD_PAYLOAD


def test_file_result_error_condition():
    h = dict(version=1,type='FILE_RESULT',request_id='r',payload_length=0,
             transfer_id='a'*32,ok=False,received_bytes=0)
    with pytest.raises(ProtocolError):
        validate_message(h,b'')
    h['error_code']='HASH_MISMATCH'
    validate_message(h,b'')
    h['ok']=True
    with pytest.raises(ProtocolError):
        validate_message(h,b'')


def test_udp_exact_frame_and_direction():
    discover = encode_message(build_discover())
    assert decode_datagram(discover).type == MessageType.DISCOVER
    reply = build_server_info('r', 'LanLink', 9000)
    assert decode_datagram(encode_message(reply),direction=Direction.UDP_REPLY) == reply
    for data,d in [(discover+b'x', Direction.UDP_REQUEST), (discover*2, Direction.UDP_REQUEST),
                   (discover, Direction.UDP_REPLY), (encode_message(build_auth('an','test')),Direction.UDP_REQUEST),
                   (b'x'*1025, Direction.UDP_REQUEST), (discover[:-1],Direction.UDP_REQUEST)]:
        with pytest.raises(ProtocolError):
            decode_datagram(data,direction=d)
    assert b'password' not in discover


@pytest.mark.parametrize('field', list(DEFAULT_LIMITS.to_dict()))
@pytest.mark.parametrize('value', [0,-1,True,1.5,2**63])
def test_limit_range(field,value):
    values = DEFAULT_LIMITS.to_dict(); values[field]=value
    with pytest.raises(ProtocolError):
        ProtocolLimits.from_dict(values)


def test_limits_and_hello_ack():
    assert ProtocolLimits.from_dict(DEFAULT_LIMITS.to_dict()) == DEFAULT_LIMITS
    assert build_hello_ack('r','server').header['limits'] == DEFAULT_LIMITS.to_dict()
    for obj in ({}, {**DEFAULT_LIMITS.to_dict(),'x':1}, {'max_chunk_bytes':1}):
        with pytest.raises(ProtocolError):
            ProtocolLimits.from_dict(obj)
    with pytest.raises(ProtocolError):
        replace(DEFAULT_LIMITS, idle_timeout_s=15)
    assert max_payload_for(MessageType.PING) == 0


def test_message_immutable_and_no_password_repr():
    msg = build_auth('an', 'DO_NOT_EXPOSE')
    assert 'DO_NOT_EXPOSE' not in repr(msg)
    with pytest.raises(TypeError):
        msg.header['username']='other'


@pytest.mark.parametrize('kind,field', [('FILE_CHUNK','seq'),('FILE_END','total_chunks'),
    ('FILE_END','total_bytes'),('FILE_RESULT','received_bytes'),('CHAT_ACK','timestamp_ms'),
    ('SERVER_INFO','port'),('FILE_OFFER','size'),('FILE_OFFER','chunk_size')])
@pytest.mark.parametrize('value',[True,1.0,-1,2**63])
def test_each_numeric_header_field(kind,field,value):
    h=dict(next(c['header'] for c in CASES['valid'] if c['header']['type']==kind))
    h[field]=value
    with pytest.raises(ProtocolError) as caught:
        decode_header(raw(h))
    assert caught.value.code==ErrorCode.INVALID_FIELD


def test_negotiated_payload_bounds_before_buffering():
    limits=replace(DEFAULT_LIMITS,max_chat_bytes=10,max_chunk_bytes=20,max_user_list_bytes=30)
    for kind,length in [('CHAT_PUBLIC',11),('FILE_CHUNK',21),('USER_LIST',31)]:
        h=dict(next(c['header'] for c in CASES['valid'] if c['header']['type']==kind))
        h['payload_length']=length
        if kind == 'USER_LIST':
            h['ref_request_id'] = 'request'
        data=raw(h)
        d=FrameDecoder(limits)
        with pytest.raises(ProtocolError) as caught:
            d.feed(struct.pack('!I',len(data))+data)
        assert caught.value.code==ErrorCode.PAYLOAD_TOO_LARGE
        assert not d._buffer


def test_user_list_limits_and_values():
    from common.builders import build_user_list
    users=[{'username':'an','online':True,'ip':'127.0.0.1','port':9000},
           {'username':'binh','online':False,'ip':'::1','port':None}]
    msg=build_user_list(users,ref_request_id='request')
    limit=len(msg.payload)
    exact=replace(DEFAULT_LIMITS,max_user_list_bytes=limit)
    assert FrameDecoder(exact,Direction.SERVER_TO_CLIENT).feed(encode_message(msg,exact))==[msg]
    with pytest.raises(ProtocolError):
        encode_message(msg,replace(exact,max_user_list_bytes=limit-1))
    with pytest.raises(ProtocolError):build_user_list([users[0],users[0]],ref_request_id='request')
    with pytest.raises(ProtocolError):
        validate_message(msg.header,msg.payload,direction=Direction.CLIENT_TO_SERVER)


def test_user_list_directional_correlation_schema():
    from common.builders import build_user_list
    request = build_user_list()
    assert not request.payload and 'ref_request_id' not in request.header
    validate_message(request.header, request.payload, direction=Direction.CLIENT_TO_SERVER)
    response = build_user_list([], ref_request_id=request.header['request_id'])
    assert response.header['ref_request_id'] == request.header['request_id']
    validate_message(response.header, response.payload, direction=Direction.SERVER_TO_CLIENT)
    for direction in (Direction.ANY, Direction.SERVER_TO_CLIENT):
        header = dict(response.header)
        del header['ref_request_id']
        with pytest.raises(ProtocolError) as caught:
            validate_message(header, response.payload, direction=direction)
        assert caught.value.code == ErrorCode.MISSING_FIELD
    with pytest.raises(ProtocolError) as caught:
        build_user_list([])
    assert caught.value.code == ErrorCode.MISSING_FIELD
    with pytest.raises(ProtocolError):
        build_user_list(ref_request_id='not-allowed-on-request')


@pytest.mark.parametrize('ref', ['', None, 42, 'invalid ref'])
def test_user_list_invalid_response_reference(ref):
    from common.builders import build_user_list
    with pytest.raises(ProtocolError):
        build_user_list([], ref_request_id=ref)


def test_builder_defaults_and_length_mismatch():
    msg=Message({'type':'PING','request_id':'r'})
    assert FrameDecoder().feed(encode_message(msg))[0].header['version']==1
    h=dict(version=1,type='CHAT_PUBLIC',request_id='r',payload_length=2)
    with pytest.raises(ProtocolError) as caught:encode_message(Message(h,b'x'))
    assert caught.value.code==ErrorCode.PAYLOAD_LENGTH_MISMATCH


def test_udp_encoding_honours_negotiated_datagram_limit():
    message=build_discover()
    exact=replace(DEFAULT_LIMITS,max_datagram_bytes=len(encode_message(message)))
    assert decode_datagram(encode_message(message,exact),exact)==message
    with pytest.raises(ProtocolError) as caught:
        encode_message(message,replace(exact,max_datagram_bytes=exact.max_datagram_bytes-1))
    assert caught.value.code==ErrorCode.PAYLOAD_TOO_LARGE
