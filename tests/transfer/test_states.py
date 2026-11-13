"""Exhaustive state graph and the exact shared snapshot contract."""
from dataclasses import fields, FrozenInstanceError
import pytest
import common.models as models
from common.errors import ErrorCode, TransferError
from common.models import ALLOWED_TRANSITIONS, TransferStatus as S, check_transition

EXPECTED = {
    S.OFFERED: {S.ACCEPTED, S.REJECTED, S.CANCELLED, S.FAILED},
    S.ACCEPTED: {S.TRANSFERRING, S.CANCELLED, S.FAILED},
    S.TRANSFERRING: {S.VERIFYING, S.CANCELLED, S.FAILED},
    S.VERIFYING: {S.COMPLETED, S.CANCELLED, S.FAILED},
    S.COMPLETED: set(), S.REJECTED: set(), S.FAILED: set(), S.CANCELLED: set(),
}


@pytest.mark.parametrize('old',list(S))
@pytest.mark.parametrize('new',list(S))
def test_all_transitions(old,new):
    assert ALLOWED_TRANSITIONS[old] == EXPECTED[old]
    if new in EXPECTED[old]:
        check_transition(old,new)
    else:
        with pytest.raises(TransferError) as caught:
            check_transition(old,new)
        assert caught.value.code == ErrorCode.TRANSFER_STATE
    if models.is_terminal(old):
        assert not ALLOWED_TRANSITIONS[old]


def test_exact_contract():
    assert [f.name for f in fields(models.TransferSnapshot)] == [
        'transfer_id','direction','peer','filename','total_bytes','processed_bytes','status','error','saved_path']
    snap=models.TransferSnapshot('a'*32,models.TransferDirection.SEND,'an','x',0,0,S.OFFERED)
    assert snap.fraction==0
    with pytest.raises(FrozenInstanceError):
        snap.status=S.COMPLETED
    assert models.TransferSnapshot('a'*32,models.TransferDirection.RECEIVE,'an','x',0,0,S.COMPLETED).fraction==1
