"""Independent adversarial regressions from the C review."""
import json
import random
import struct

import pytest

from common.errors import ProtocolError
from common.protocol import (Direction, FrameDecoder, decode_datagram, decode_header,
                             validate_message)
from tests.fixtures import load_protocol_cases


def test_deep_in_memory_header_is_a_protocol_error():
    header = {}
    for _ in range(1500):
        header = {'nested': header}
    with pytest.raises(ProtocolError):
        validate_message(header, b'')


def test_golden_bytes_are_independently_reproducible():
    # This oracle deliberately uses only struct/json, never encode_message.
    for case in load_protocol_cases()['valid']:
        header = json.dumps(case['header'], ensure_ascii=False, sort_keys=True,
                            separators=(',', ':')).encode('utf-8')
        payload = (case['payload_utf8'].encode('utf-8') if 'payload_utf8' in case
                   else bytes.fromhex(case['payload_hex']))
        assert (struct.pack('!I', len(header)) + header + payload).hex() == case['frame_hex']


def test_seeded_hostile_bytes_only_raise_protocol_errors():
    rng = random.Random(3094)
    corpus = [b'[]', b'null', b'"x"', b'\xef\xbb\xbf{}', b'\xc0\xaf',
              b'[' * 2000 + b'0' + b']' * 2000, b'{"n":' + b'9' * 10000 + b'}',
              b'{"x":"\\ud800"}', b'{"x":1e9999}']
    corpus += [rng.randbytes(rng.randrange(0, 512)) for _ in range(1000)]
    for data in corpus:
        for operation in (decode_header, decode_datagram, FrameDecoder().feed):
            try:
                operation(data)
            except ProtocolError:
                pass


def test_all_catalogue_directions_are_explicit():
    for case in load_protocol_cases()['valid']:
        expected = Direction[case['direction']]
        payload = (case['payload_utf8'].encode() if 'payload_utf8' in case
                   else bytes.fromhex(case['payload_hex']))
        kind = case['header']['type']
        if kind in {'HELLO', 'AUTH', 'HELLO_ACK', 'AUTH_OK', 'ERROR', 'CHAT_ACK',
                    'USER_LIST', 'CHAT_PUBLIC', 'CHAT_PRIVATE', 'FILE_OFFER', 'DISCOVER', 'SERVER_INFO'}:
            permitted = {expected, Direction.ANY}
        else:
            permitted = {Direction.CLIENT_TO_SERVER, Direction.SERVER_TO_CLIENT, Direction.ANY}
        for direction in Direction:
            try:
                validate_message(case['header'], payload, direction=direction)
            except ProtocolError:
                assert direction not in permitted, (case['name'], direction)
            else:
                assert direction in permitted, (case['name'], direction)


def test_seeded_header_mutations_only_raise_protocol_errors():
    rng = random.Random(3094)
    values = [None, True, False, -1, 0, 1.0, 1e3, 2**63 - 1, 2**63,
              [], {}, '\ud800', '\u0085', '', 'x' * 512]
    for case in load_protocol_cases()['valid']:
        for _ in range(25):
            header = dict(case['header'])
            field = rng.choice(list(header))
            header[field] = rng.choice(values)
            try:
                validate_message(header, b'')
            except ProtocolError:
                pass


def test_huge_input_prefix_and_empty_feeds_stay_bounded():
    decoder = FrameDecoder()
    assert decoder.feed(b'') == [] and decoder.at_clean_boundary()
    with pytest.raises(ProtocolError):
        decoder.feed(b'\xff\xff\xff\xff' + b'9' * 10_000_000)
    assert not decoder._buffer
    with pytest.raises(ProtocolError):
        decoder.feed(b'')


def test_recursive_immutability_and_header_control_policy():
    from common.builders import build_hello_ack, build_chat_public
    message = build_hello_ack('r', 'name')
    with pytest.raises(TypeError): message.header['limits']['max_chunk_bytes'] = 1
    assert build_chat_public('new\nline\u0085').payload.decode() == 'new\nline\u0085'
    with pytest.raises(ProtocolError):
        validate_message(dict(version=1, type='HELLO', request_id='r', payload_length=0,
                              client_name='control\u0085', client_version='1'), b'')
