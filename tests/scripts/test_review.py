"""Load observations must be attributed to the correct connection and sender."""
import pytest

from common.protocol import MessageType, build_chat_public, build_message
from tests.scripts.test_load_test import load


def observed_chat():
    args = load.parse_args(['--users', '3', '--messages-per-client', '0'])
    metrics = load.Measurements(['an', 'binh', 'chi'])
    message = build_chat_public('marker')
    metrics.register_chat('an', message, {'binh'}, 'baseline')
    metrics.confirm_chat(message)
    return metrics, args, message


def test_ack_on_another_connection_cannot_confirm_send():
    metrics, args, message = observed_chat()
    metrics.acknowledge(message.header['request_id'], username='chi')
    report = load.make_report(metrics, args)
    assert report['missing_acks'] == 1
    assert 'unexpected_acks' in report['verification_failures']


@pytest.mark.parametrize('recipient,sender,kind', [
    ('chi', 'an', MessageType.CHAT_PRIVATE),
    ('binh', 'chi', MessageType.CHAT_PUBLIC),
])
def test_unexpected_delivery_is_a_verification_failure(recipient, sender, kind):
    metrics, args, message = observed_chat()
    extra = {'recipient': 'binh'} if kind == MessageType.CHAT_PRIVATE else {}
    delivered = build_message(kind, message.payload, sender=sender,
                              message_id='m', timestamp_ms=1, **extra)
    metrics.deliver(recipient, delivered)
    report = load.make_report(metrics, args)
    assert report['missing_deliveries'] == 1
    assert 'unexpected_deliveries' in report['verification_failures']


def test_untracked_delivery_never_disappears_from_verification():
    metrics, args, _ = observed_chat()
    metrics.deliver('binh', build_message(MessageType.CHAT_PUBLIC, b'untracked',
                    sender='an', message_id='m', timestamp_ms=1))
    assert 'unexpected_deliveries' in load.make_report(metrics, args)['verification_failures']


def test_ctrl_c_during_thread_start_still_reports_130(tmp_path, monkeypatch):
    monkeypatch.setenv('LANLINK_LOAD_PASSWORD', 'ephemeral-test-value')
    monkeypatch.setattr(load, 'environment', lambda args: {'fixture': True})
    class Unstarted:
        ident = None
        def __init__(self, **kwargs): pass
        def start(self): raise KeyboardInterrupt
        def is_alive(self): return False
        def join(self, timeout=None): raise RuntimeError('cannot join thread before it is started')
    monkeypatch.setattr(load.threading, 'Thread', Unstarted)
    assert load.main(['--users', '1', '--duration', '.1', '--messages-per-client', '0',
                      '--report-json', str(tmp_path / 'report.json'),
                      '--report-md', str(tmp_path / 'report.md')]) == 130


def test_percentile_100_samples_uses_documented_interpolation():
    assert load.percentile(list(range(100)), 95) == pytest.approx(94.05)
