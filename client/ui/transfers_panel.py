"""Scrollable keyed file cards; ordinary progress updates reuse the existing row."""
from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QScrollArea
from common.models import TransferSnapshot
from client.ui.file_card import FileCard

TEXT={'empty':'Chưa có phiên truyền file nào','counts':'{active} đang chạy · {finished} đã xong',
      'title':'Truyền file'}


class TransfersPanel(QWidget):
    accept_requested=Signal(str)
    reject_requested=Signal(str)
    cancel_requested=Signal(str)
    open_location_requested=Signal(str)

    def __init__(self,parent=None,*,tokens=None):
        super().__init__(parent)
        self.setObjectName('transfersPanel'); self.setAccessibleName(TEXT['title'])
        self._cards={}; self._serial={}; self._next_serial=0
        self._active=0; self._finished=0; self._tokens=tokens
        layout=QVBoxLayout(self)
        self.count_label=QLabel(self); self.count_label.setObjectName('transferCounts')
        layout.addWidget(self.count_label)
        self.empty_label=QLabel(TEXT['empty'],self); self.empty_label.setObjectName('transfersEmpty')
        self.empty_label.setWordWrap(True); layout.addWidget(self.empty_label)
        self.scroll=QScrollArea(self); self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName('transfersScroll')
        self.container=QWidget(); self.rows=QVBoxLayout(self.container)
        self.rows.setContentsMargins(0,0,0,0); self.rows.addStretch(1)
        self.scroll.setWidget(self.container); layout.addWidget(self.scroll)
        self._refresh_counts()

    def count(self) -> int:
        return len(self._cards)

    def _refresh_counts(self) -> None:
        self.count_label.setText(TEXT['counts'].format(active=self._active,finished=self._finished))
        self.empty_label.setVisible(not self._cards)
        self.scroll.setVisible(bool(self._cards))

    def _reorder(self) -> None:
        # Reorder only on insertion/terminal change, never for a chunk update.
        ordered=sorted(self._cards,key=lambda key:(self._cards[key].snapshot.is_terminal,-self._serial[key]))
        for index,key in enumerate(ordered):
            self.rows.insertWidget(index,self._cards[key])

    @Slot(object)
    def upsert_snapshot(self,snapshot:TransferSnapshot) -> None:
        key=snapshot.transfer_id
        card=self._cards.get(key)
        if card is None:
            card=FileCard(snapshot,self.container,tokens=self._tokens)
            self._cards[key]=card; self._serial[key]=self._next_serial; self._next_serial+=1
            card.accept_requested.connect(self.accept_requested.emit)
            card.reject_requested.connect(self.reject_requested.emit)
            card.cancel_requested.connect(self.cancel_requested.emit)
            card.open_location_requested.connect(self.open_location_requested.emit)
            if snapshot.is_terminal:self._finished+=1
            else:self._active+=1
            self._reorder(); self._refresh_counts()
        else:
            was_terminal=card.snapshot.is_terminal
            card.set_snapshot(snapshot)
            if card.snapshot.is_terminal!=was_terminal:
                self._active-=1;self._finished+=1
                self._reorder();self._refresh_counts()

    @Slot(str)
    def operation_failed(self, transfer_id: str) -> None:
        """Release only the row whose service operation failed."""
        card = self._cards.get(transfer_id)
        if card is not None:
            card.operation_failed(transfer_id)

    @Slot(str)
    def remove_transfer(self,transfer_id:str) -> None:
        card=self._cards.pop(transfer_id,None)
        if card is None:return
        self._serial.pop(transfer_id)
        if card.snapshot.is_terminal:self._finished-=1
        else:self._active-=1
        self.rows.removeWidget(card);card.hide();card.setParent(None);card.deleteLater()
        self._refresh_counts()

    @Slot()
    def clear_finished(self) -> None:
        for key,card in list(self._cards.items()):
            if card.snapshot.is_terminal:self.remove_transfer(key)
