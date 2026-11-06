"""Boundary values and recoverable widget errors found during independent review."""
from dataclasses import replace

import pytest

from client.ui.file_card import FileCard, build_stylesheet
from client.ui.file_format import status_text
from common.models import TransferStatus as S
from tests.ui.test_file_card import snapshot


def test_failed_snapshot_without_error_is_displayable(qapp):
    snap = snapshot(status=S.FAILED, error=None)
    assert status_text(snap)
    card = FileCard(snap)
    assert card.snapshot == snap
    card.deleteLater()


@pytest.mark.parametrize('tokens', [{'accent': 'red; } QWidget { color: red'},
                                  {'spacing': '1px; image: url(secret)'},
                                  {'radius': None}])
def test_qss_rejects_structural_token_injection(tokens):
    with pytest.raises(ValueError):
        build_stylesheet(tokens)


def test_operation_failure_can_explicitly_release_pending_action(qapp):
    snap = snapshot()
    card = FileCard(snap)
    card.accept_button.click()
    assert not card.accept_button.isEnabled()
    # Equal queued snapshots remain idempotent; only a correlated service
    # operation failure releases pending intent for retry.
    card.set_snapshot(snap)
    assert not card.accept_button.isEnabled()
    card.operation_failed('foreign')
    assert not card.accept_button.isEnabled()
    card.operation_failed(snap.transfer_id)
    assert card.accept_button.isEnabled() and card.snapshot == snap
    card.deleteLater()


@pytest.mark.parametrize('width', [220, 280, 320, 900])
def test_narrow_card_with_scaled_font_keeps_actions_inside(qapp, width):
    from PySide6.QtGui import QFont
    card = FileCard(snapshot(filename='long' * 200 + '\u202e\U0001f600.pdf'))
    font = QFont(card.font())
    font.setPointSize(16)
    card.setFont(font)
    card.resize(width, 450)
    card.show(); qapp.processEvents()
    try:
        assert card.width() == width
        assert all(card.rect().contains(b.geometry()) for b in (card.accept_button, card.reject_button))
    finally:
        card.close(); card.deleteLater()
