"""Stable keyed rows, active ordering, counts and signal forwarding."""
from dataclasses import replace
from PySide6.QtCore import QCoreApplication,QEvent
from common.models import TransferDirection as D,TransferStatus as S,TransferSnapshot
from client.ui.file_card import FileCard
from client.ui.transfers_panel import TransfersPanel


def snap(number,status=S.OFFERED,processed=0):
    return TransferSnapshot(f'{number:032x}',D.RECEIVE,'An','x.pdf',100,processed,status,
                            saved_path='d/x.pdf' if status==S.COMPLETED else None)


def ordered(panel):
    return [panel.rows.itemAt(i).widget().transfer_id() for i in range(panel.count())]


def test_upsert_order_counts_remove_empty(qapp):
    panel=TransfersPanel();panel.show();qapp.processEvents()
    assert panel.empty_label.isVisible() and panel.count()==0
    first=snap(1);second=snap(2);finished=snap(3,S.COMPLETED,100)
    for s in (first,second,finished):panel.upsert_snapshot(s)
    assert ordered(panel)==[second.transfer_id,first.transfer_id,finished.transfer_id]
    original=panel._cards[first.transfer_id]
    panel.upsert_snapshot(replace(first,status=S.TRANSFERRING,processed_bytes=50))
    assert panel._cards[first.transfer_id] is original
    assert panel.count_label.text()=='2 đang chạy · 1 đã xong'
    panel.upsert_snapshot(replace(second,status=S.FAILED))
    assert ordered(panel)==[first.transfer_id,finished.transfer_id,second.transfer_id]
    panel.clear_finished();assert panel.count()==1
    panel.remove_transfer(first.transfer_id);panel.remove_transfer('missing')
    assert panel.count()==0 and panel.empty_label.isVisible()
    panel.close();panel.deleteLater()


def test_reemits_all_row_signals(qapp):
    panel=TransfersPanel();events=[]
    for name in ('accept','reject','cancel','open_location'):
        getattr(panel,name+'_requested').connect(lambda value,n=name:events.append((n,value)))
    for number,action in enumerate(('accept','reject','cancel','open_location'),1):
        s=snap(number,S.COMPLETED if action=='open_location' else S.TRANSFERRING if action=='cancel' else S.OFFERED,
               100 if action=='open_location' else 0)
        panel.upsert_snapshot(s);card=panel._cards[s.transfer_id]
        getattr(card,'open_button' if action=='open_location' else action+'_button').click()
        assert events[-1]==(action,'d/x.pdf' if action=='open_location' else s.transfer_id)
    assert len(events)==4
    panel.deleteLater()


def test_200_rows_reuse_widgets(qapp):
    panel=TransfersPanel()
    for number in range(220):panel.upsert_snapshot(snap(number,S.TRANSFERRING))
    identities={key:id(card) for key,card in panel._cards.items()}
    for progress in range(1,11):
        for number in range(220):panel.upsert_snapshot(snap(number,S.TRANSFERRING,progress))
    assert identities=={key:id(card) for key,card in panel._cards.items()}
    assert len(panel.findChildren(FileCard))==220
    panel.clear_finished();assert panel.count()==220
    panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
