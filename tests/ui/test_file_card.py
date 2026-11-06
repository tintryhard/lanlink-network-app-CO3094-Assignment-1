"""State/action/geometry checks; no GUI testing dependency besides PySide6."""
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt,qInstallMessageHandler
from PySide6.QtTest import QTest
from common.errors import ErrorCode,user_message
from common.models import ErrorInfo,TransferDirection as D,TransferStatus as S,TransferSnapshot
from client.ui.file_card import FileCard,build_stylesheet
from client.ui.file_format import format_bytes,status_text


def snapshot(direction=D.RECEIVE,status=S.OFFERED,**extra):
    fields=dict(transfer_id='a'*32,direction=direction,peer='Minh',filename='Báo cáo.pdf',
                total_bytes=100,processed_bytes=100 if status==S.COMPLETED else 0,status=status,
                saved_path='Downloads/Báo cáo.pdf' if direction==D.RECEIVE and status==S.COMPLETED else None,
                error=ErrorInfo(ErrorCode.DISK_FULL,'detail') if status==S.FAILED else None)
    return TransferSnapshot(**{**fields,**extra})


@pytest.mark.parametrize('direction',list(D))
@pytest.mark.parametrize('status',list(S))
def test_every_state(qapp,direction,status):
    s=snapshot(direction,status)
    card=FileCard(s);card.resize(280,card.sizeHint().height());card.show();qapp.processEvents()
    assert card.status_label.text()==status_text(s)
    expected=[]
    if status==S.OFFERED and direction==D.RECEIVE:expected=[card.accept_button,card.reject_button]
    elif status in {S.OFFERED,S.ACCEPTED,S.TRANSFERRING}:expected=[card.cancel_button]
    elif status==S.COMPLETED and direction==D.RECEIVE:expected=[card.open_button]
    assert [b for b in card._buttons if b.isVisible()]==expected
    if status==S.VERIFYING:
        assert card.progress_bar.maximum()==0 and not card.cancel_button.isEnabled()
    else:assert card.progress_bar.maximum()==1000
    assert card.property('transferState')==status.value
    for button in expected:
        assert button.isEnabled() and button.accessibleName()
        assert card.rect().contains(button.geometry())
    card.close();card.deleteLater()


@pytest.mark.parametrize('action',['accept','reject','cancel'])
def test_intent_emitted_once_without_fake_state(qapp,action):
    s=snapshot(D.SEND if action=='cancel' else D.RECEIVE)
    card=FileCard(s);seen=[]
    getattr(card,action+'_requested').connect(seen.append)
    button=getattr(card,action+'_button')
    original=card.status_label.text()
    button.click();button.click()
    assert seen==[s.transfer_id]
    assert all(not b.isEnabled() for b in card._buttons)
    assert card.status_label.text()==original
    card.set_snapshot(s)
    assert not button.isEnabled()
    card.set_snapshot(replace(s,status=S.ACCEPTED))
    assert card.cancel_button.isEnabled()
    card.deleteLater()


def test_terminal_stickiness_foreign_ids_and_monotonicity(qapp):
    original=snapshot(status=S.TRANSFERRING,processed_bytes=50)
    card=FileCard(original)
    for s in [replace(original,transfer_id='b'*32),replace(original,processed_bytes=1),replace(original,status=S.OFFERED)]:
        card.set_snapshot(s);assert card.snapshot==original
    done=replace(original,status=S.COMPLETED,processed_bytes=100,saved_path='d/file')
    card.set_snapshot(done)
    for status in S:
        card.set_snapshot(replace(done,status=status))
        assert card.snapshot.status==S.COMPLETED
    paths=[];card.open_location_requested.connect(paths.append);card.open_button.click()
    assert paths==['d/file']
    card.deleteLater()


@pytest.mark.parametrize('width',[280,900])
def test_zero_long_text_and_button_geometry(qapp,width):
    s=snapshot(filename='Báo cáo rất dài '*100+'.pdf',total_bytes=0)
    card=FileCard(s);card.resize(width,260);card.show();qapp.processEvents()
    assert card.filename_label.toolTip()==s.filename
    for b in [card.accept_button,card.reject_button]:
        assert card.rect().contains(b.geometry())
    assert card.progress_bar.value()==0
    card.set_snapshot(replace(s,status=S.COMPLETED,saved_path='long/path/'*100))
    assert card.progress_bar.value()==1000
    assert card.rect().contains(card.open_button.geometry())
    card.close();card.deleteLater()
    failed=FileCard(snapshot(status=S.FAILED,error=ErrorInfo(ErrorCode.INTERNAL,('long details\n'*500))))
    failed.resize(width,failed.sizeHint().height());failed.show();qapp.processEvents()
    assert len(failed.status_label.text())<=240
    assert 'long details' in failed.status_label.toolTip()
    failed.close();failed.deleteLater()


def test_keyboard_and_plain_text(qapp):
    card=FileCard(snapshot(peer='<b>Minh</b>'))
    card.show();seen=[];card.accept_requested.connect(seen.append)
    card.accept_button.setFocus();QTest.keyClick(card.accept_button,Qt.Key.Key_Space)
    assert seen==['a'*32]
    assert card.status_label.textFormat()==Qt.TextFormat.PlainText
    card.close();card.deleteLater()


def test_qss_default_and_custom(qapp):
    warnings=[]
    previous=qInstallMessageHandler(lambda mode,context,text:warnings.append(text))
    try:
        for tokens in (None,{'accent':'#2244ff','spacing':'8px','radius':'4px'}):
            sheet=build_stylesheet(tokens)
            card=FileCard(snapshot(),tokens=tokens);card.show();qapp.processEvents()
            assert sheet and not any('stylesheet' in text.lower() for text in warnings)
            card.close();card.deleteLater()
    finally:qInstallMessageHandler(previous)


@pytest.mark.parametrize('value,expected',[(0,'0 B'),(1023,'1023 B'),(1024,'1,0 KiB'),
    (1048576,'1,0 MiB'),(100*1048576,'100,0 MiB')])
def test_format_bytes(value,expected):
    assert format_bytes(value)==expected
