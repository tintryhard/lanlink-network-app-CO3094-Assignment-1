"""Dumb snapshot-driven file card. All slots are called on the GUI thread.

Buttons emit intent once and wait for the service's next snapshot. The card
does no disk, hash, network or background work and cannot invent completion.
"""
from typing import Mapping
import re
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QHBoxLayout, QBoxLayout, QLayout, QSizePolicy)

from common.models import TransferDirection as D, TransferSnapshot, TransferStatus as S
from client.ui.file_format import format_bytes, peer_text, status_text

TEXT = {'accept': 'Chấp nhận', 'reject': 'Từ chối', 'cancel': 'Hủy',
        'open': 'Mở thư mục', 'progress': 'Tiến độ truyền file', 'card': 'Phiên truyền file',
        'file_badge': 'FILE'}
DEFAULT_TOKENS = {'accent': '#6557d2', 'surface': '#fafafd', 'border': '#dcdce8',
                  'text': '#262538', 'muted': '#686679', 'success': '#23794e',
                  'danger': '#b43247', 'spacing': '10px', 'radius': '10px'}


def build_stylesheet(tokens: Mapping[str, str] | None = None) -> str:
    """Theme keys: accent,surface,border,text,muted,success,danger,spacing,radius.

    Colour tokens are Qt colour names/hex; spacing/radius use px or pt.
    Unknown theme keys are ignored so B can pass its complete token mapping.
    """
    t = {**DEFAULT_TOKENS, **(tokens or {})}
    for key in DEFAULT_TOKENS:
        value = t[key]
        valid = (isinstance(value, str) and
                 (bool(re.fullmatch(r'\d{1,3}(?:\.\d{1,2})?(?:px|pt)', value))
                  if key in {'spacing', 'radius'} else QColor.isValidColorName(value)))
        if not valid:
            raise ValueError(f'Invalid theme token: {key}')
    return f'''
        QWidget#fileCard {{ background: {t['surface']}; color: {t['text']};
            border: 1px solid {t['border']}; border-radius: {t['radius']}; }}
        QWidget#fileCard QLabel {{ border: none; background: transparent; color: {t['text']}; }}
        QWidget#fileCard QLabel#filePeer, QWidget#fileCard QLabel#fileSize,
        QWidget#fileCard QLabel#filePath {{ color: {t['muted']}; }}
        QWidget#fileCard QLabel#fileBadge {{ background: {t['accent']}; color: white;
            border-radius: 6px; font-weight: bold; }}
        QWidget#fileCard[transferState="failed"] QLabel#fileStatus {{ color: {t['danger']}; }}
        QWidget#fileCard[transferState="completed"] QLabel#fileStatus {{ color: {t['success']}; }}
        QWidget#fileCard QPushButton {{ border: 1px solid {t['border']}; border-radius: 5px;
            padding: 5px {t['spacing']}; background: {t['surface']}; color: {t['text']}; }}
        QWidget#fileCard QPushButton#fileAccept {{ background: {t['accent']}; color: white; }}
        QWidget#fileCard QPushButton:disabled {{ color: {t['muted']}; }}
        QWidget#fileCard QPushButton:focus {{ border: 2px solid {t['accent']}; }}
        QWidget#fileCard QProgressBar {{ border: 1px solid {t['border']}; border-radius: 4px;
            background: {t['surface']}; color: {t['text']}; text-align: center; }}
        QWidget#fileCard QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
    '''


class ElidedLabel(QLabel):
    """Middle elision keeps long untrusted names from widening the layout."""
    def __init__(self, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.full_text = ''

    def set_full_text(self, text: str) -> None:
        self.full_text = text
        self.setToolTip(text)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, max(1,self.width())))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()


class FileCard(QWidget):
    accept_requested = Signal(str)
    reject_requested = Signal(str)
    cancel_requested = Signal(str)
    open_location_requested = Signal(str)

    def __init__(self, snapshot: TransferSnapshot | None = None, parent=None,
                 *, tokens: Mapping[str,str] | None = None):
        super().__init__(parent)
        self.setObjectName('fileCard')
        self.setAccessibleName(TEXT['card'])
        self.setMinimumWidth(0)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(build_stylesheet(tokens))
        self._snapshot = None
        self._pending = False
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(12,12,12,12)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.badge = QLabel(self); self.badge.setObjectName('fileBadge')
        self.badge.setTextFormat(Qt.TextFormat.PlainText)
        self.badge.setFixedSize(46,40); self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.badge)
        titles = QVBoxLayout()
        self.filename_label = ElidedLabel('fileName',self)
        self.size_label = QLabel(self); self.size_label.setObjectName('fileSize')
        titles.addWidget(self.filename_label); titles.addWidget(self.size_label)
        top.addLayout(titles,1); layout.addLayout(top)
        self.peer_label = ElidedLabel('filePeer',self); layout.addWidget(self.peer_label)
        self.status_label = QLabel(self); self.status_label.setObjectName('fileStatus')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar(self); self.progress_bar.setObjectName('fileProgress')
        self.progress_bar.setAccessibleName(TEXT['progress'])
        self.progress_bar.setRange(0,1000); self.progress_bar.setFixedHeight(18)
        layout.addWidget(self.progress_bar)
        self.path_label = ElidedLabel('filePath',self); layout.addWidget(self.path_label)
        actions = QHBoxLayout(); actions.addStretch(1)
        self._actions = actions
        self.accept_button = self._button(TEXT['accept'],'fileAccept',actions)
        self.reject_button = self._button(TEXT['reject'],'fileReject',actions)
        self.cancel_button = self._button(TEXT['cancel'],'fileCancel',actions)
        self.open_button = self._button(TEXT['open'],'fileOpenLocation',actions)
        layout.addLayout(actions)
        self.accept_button.clicked.connect(lambda: self._request(self.accept_requested))
        self.reject_button.clicked.connect(lambda: self._request(self.reject_requested))
        self.cancel_button.clicked.connect(lambda: self._request(self.cancel_requested))
        self.open_button.clicked.connect(self._open_location)
        QWidget.setTabOrder(self.accept_button,self.reject_button)
        QWidget.setTabOrder(self.reject_button,self.cancel_button)
        QWidget.setTabOrder(self.cancel_button,self.open_button)
        for button in self._buttons:
            button.hide()
        if snapshot is not None:
            self.set_snapshot(snapshot)

    @property
    def _buttons(self):
        return (self.accept_button,self.reject_button,self.cancel_button,self.open_button)

    @property
    def snapshot(self) -> TransferSnapshot | None:
        return self._snapshot

    def _button(self,text,name,layout):
        button=QPushButton(text,self)
        button.setObjectName(name); button.setAccessibleName(text)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(button)
        return button

    def transfer_id(self) -> str:
        return self._snapshot.transfer_id if self._snapshot else ''

    def _adapt_actions(self) -> None:
        visible = [b for b in self._buttons if not b.isHidden()]
        needed = sum(b.sizeHint().width() for b in visible) + max(0, len(visible) - 1) * 6
        direction = (QBoxLayout.Direction.TopToBottom if needed > self.width() - 24
                     else QBoxLayout.Direction.LeftToRight)
        self._actions.setDirection(direction)

    def resizeEvent(self, event) -> None:
        self._adapt_actions()
        super().resizeEvent(event)

    def _request(self,signal) -> None:
        if self._pending or self._snapshot is None:
            return
        self._pending=True
        for button in self._buttons:
            button.setEnabled(False)
        signal.emit(self.transfer_id())

    @Slot()
    def _open_location(self) -> None:
        if self._snapshot and self._snapshot.saved_path:
            self.open_location_requested.emit(self._snapshot.saved_path)

    @Slot(str)
    def operation_failed(self, transfer_id: str) -> None:
        """B calls on a correlated operation failure to permit intent retry."""
        if self._snapshot and transfer_id == self.transfer_id() and self._pending:
            self._pending = False
            for button in self._buttons:
                button.setEnabled(True)
            self.cancel_button.setEnabled(self._snapshot.status != S.VERIFYING)

    @Slot(object)
    def set_snapshot(self,snapshot:TransferSnapshot) -> None:
        previous=self._snapshot
        if previous is not None:
            if snapshot.transfer_id != previous.transfer_id or snapshot.processed_bytes < previous.processed_bytes:
                return
            if previous.is_terminal and snapshot.status != previous.status:
                return
            order={S.OFFERED:0,S.ACCEPTED:1,S.TRANSFERRING:2,S.VERIFYING:3}
            if not previous.is_terminal and not snapshot.is_terminal and order[snapshot.status]<order[previous.status]:
                return
            if previous == snapshot:
                return
        self._snapshot=snapshot; self._pending=False
        self.filename_label.set_full_text(snapshot.filename)
        extension=snapshot.filename.rsplit('.',1)[-1][:6] if '.' in snapshot.filename else TEXT['file_badge']
        self.badge.setText(extension.upper())
        self.size_label.setText(format_bytes(snapshot.total_bytes))
        self.peer_label.set_full_text(peer_text(snapshot))
        text=status_text(snapshot)
        self.status_label.setToolTip(text)
        # A compact preview preserves button geometry; tooltip retains full details.
        lines=text.splitlines()
        preview='\n'.join(lines[:3])
        if len(preview)>240 or len(lines)>3:
            preview=preview[:237]+'…'
        self.status_label.setText(preview)
        self.progress_bar.setRange(0,0 if snapshot.status==S.VERIFYING else 1000)
        if snapshot.status!=S.VERIFYING:
            self.progress_bar.setValue(round(snapshot.fraction*1000))
            self.progress_bar.setFormat(f'{snapshot.fraction:.0%}')
        self.path_label.set_full_text(snapshot.saved_path or '')
        self.path_label.setVisible(snapshot.direction==D.RECEIVE and snapshot.status==S.COMPLETED and bool(snapshot.saved_path))
        for button in self._buttons:
            button.hide(); button.setEnabled(True)
        if snapshot.status==S.OFFERED and snapshot.direction==D.RECEIVE:
            self.accept_button.show(); self.reject_button.show()
        elif snapshot.status in {S.OFFERED,S.ACCEPTED,S.TRANSFERRING}:
            self.cancel_button.show()
        elif snapshot.status==S.COMPLETED and snapshot.direction==D.RECEIVE and snapshot.saved_path:
            self.open_button.show()
        self.cancel_button.setEnabled(snapshot.status!=S.VERIFYING)
        self._adapt_actions()
        self.setProperty('transferState',snapshot.status.value)
        self.style().unpolish(self); self.style().polish(self)
        self.updateGeometry()
