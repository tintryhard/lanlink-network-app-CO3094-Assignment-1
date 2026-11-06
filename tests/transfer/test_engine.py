"""Disk, cancellation and corruption behaviour, end-to-end through the codec fake."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import hashlib
import os
from pathlib import Path
import random
import threading
import tracemalloc

import pytest

from common.errors import ErrorCode as E, TransferError
from common.models import Message, TransferStatus as S
from common.protocol import DEFAULT_LIMITS, build_file_accept, build_file_result, MessageType
from tests.fakes.fake_transport import FakeRelay
from transfer.paths import reserve_destination, sanitize_filename
from transfer.progress import ProgressThrottle
from transfer.receiver import IncomingOffer, Receiver
from transfer.sender import SendSession, inspect_file, iter_chunks


def source(tmp_path, size=20000, name='source.bin'):
    path=tmp_path/name
    rng=random.Random(7)
    with path.open('wb') as out:
        remaining=size
        while remaining:
            data=rng.randbytes(min(65536,remaining))
            out.write(data)
            remaining-=len(data)
    return path


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(65536),b''):
            h.update(data)
    return h.hexdigest()


def session(tmp_path,size=20000,**kwargs):
    return SendSession(inspect_file(source(tmp_path,size)), 'receiver', chunk_size=kwargs.pop('chunk_size',4096),**kwargs)


@pytest.mark.parametrize('size',[0,1,4095,4096,4097,1048576,50*1048576,100*1048576])
def test_sizes_streaming_and_memory(tmp_path,size):
    tracemalloc.start()
    sender=session(tmp_path,size,chunk_size=65536 if size>=50*1048576 else 4096)
    relay=FakeRelay(sender,tmp_path/'downloads')
    receiver=relay.run()
    _,peak=tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert sender.snapshot.status==receiver.snapshot.status==S.COMPLETED
    assert digest(receiver.snapshot.saved_path)==sender.info.sha256
    assert Path(receiver.snapshot.saved_path).stat().st_size==size
    assert receiver._fd is None and sender._chunks is None
    if size>=50*1048576:
        print(f'{size // 1048576} MiB transfer tracemalloc peak: {peak} bytes')
        assert peak < 8*1048576


def test_end_is_not_sender_success(tmp_path):
    sender=session(tmp_path,0)
    assert list(sender.outgoing())==[]
    sender.handle_message(build_file_accept(sender.transfer_id))
    messages=list(sender.outgoing())
    assert [m.type for m in messages]==[MessageType.FILE_END]
    assert sender.snapshot.status==S.VERIFYING
    sender.handle_message(build_file_result(sender.transfer_id,True,0,sender.info.sha256))
    assert sender.snapshot.status==S.COMPLETED
    assert sender.cancel() is None


@pytest.mark.parametrize('fault,code', [({'flip_chunk':1},E.HASH_MISMATCH),({'duplicate_chunk':0},E.SEQ_MISMATCH),
    ({'skip_chunk':0},E.SEQ_MISMATCH),({'reorder':True},E.SEQ_MISMATCH),({'drop_after':2},E.CONNECTION_LOST)])
def test_wire_faults(tmp_path,fault,code):
    updates=[]
    sender=session(tmp_path,on_update=updates.append)
    relay=FakeRelay(sender,tmp_path/'downloads',**fault)
    receiver=relay.run()
    assert sender.snapshot.status==receiver.snapshot.status==S.FAILED
    assert sender.snapshot.error.code==receiver.snapshot.error.code==code
    assert not list((tmp_path/'downloads').iterdir())
    assert all(s.status!=S.COMPLETED for s in updates)


@pytest.mark.parametrize('side',['sender','receiver'])
def test_cancel_both_directions(tmp_path,side):
    updates=[]
    sender=session(tmp_path,on_update=updates.append)
    relay=FakeRelay(sender,tmp_path/'downloads',cancel_at=2,cancel_side=side)
    receiver=relay.run()
    assert sender.snapshot.status==receiver.snapshot.status==S.CANCELLED
    assert sender.cancel() is None and receiver.cancel() is None
    assert sum(s.is_terminal for s in updates)==1
    assert not list((tmp_path/'downloads').iterdir())


def test_rejected_before_disk(tmp_path):
    sender=session(tmp_path)
    receiver=FakeRelay(sender,tmp_path/'never_created').run(accept=False)
    assert sender.snapshot.status==receiver.snapshot.status==S.REJECTED
    assert not receiver.download_dir.exists()


def test_oversize_refused_before_hash(tmp_path,monkeypatch):
    path=source(tmp_path,10)
    limits=replace(DEFAULT_LIMITS,max_file_bytes=5)
    monkeypatch.setattr(Path,'open',lambda *a,**k:pytest.fail('must not open oversized source'))
    with pytest.raises(TransferError) as caught:
        inspect_file(path,limits=limits)
    assert caught.value.code==E.FILE_TOO_LARGE
    receiver=Receiver(IncomingOffer('a'*32,'an','x',10,'0'*64,1),tmp_path/'d',limits=limits)
    with pytest.raises(TransferError) as caught:
        receiver.start()
    assert caught.value.code==E.FILE_TOO_LARGE
    assert not receiver.download_dir.exists()


def test_extra_bytes_and_short_end(tmp_path):
    for index,extra in enumerate([True,False]):
        sender=session(tmp_path,1)
        relay=FakeRelay(sender,tmp_path/f'd{index}')
        receiver=relay.receiver
        receiver.accept()
        if extra:
            with pytest.raises(TransferError) as caught:
                receiver.write(0,b'ab')
            assert caught.value.code==E.SIZE_MISMATCH
        else:
            result=receiver.finish(0,0)
            assert result.header['error_code']==E.SIZE_MISMATCH
        assert receiver.snapshot.status==S.FAILED


def test_collision_and_concurrent_reservations(tmp_path):
    d=tmp_path/'d';d.mkdir()
    (d/'a.txt').write_bytes(b'existing')
    def reserve(_):
        p,fd=reserve_destination(d,'a.txt');os.close(fd);return p.name
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(reserve,range(2)))=={'a (1).txt','a (2).txt'}
    assert (d/'a.txt').read_bytes()==b'existing'


def test_two_receivers_same_name(tmp_path):
    senders=[SendSession(inspect_file(source(tmp_path,1,name=f'{i}.bin')),'peer') for i in range(2)]
    relays=[FakeRelay(s,tmp_path/'d') for s in senders]
    for r in relays:
        r.receiver.offer=replace(r.receiver.offer,filename='a.txt')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda r:r.run(),relays))
    assert {Path(r.snapshot.saved_path).name for r in results}=={'a.txt','a (1).txt'}


@pytest.mark.parametrize('bad',['../x','..\\x','/etc/passwd','C:\\x','a/b','a\0b','..','.', '','C:x','\ud800'])
def test_reject_paths(bad):
    with pytest.raises(TransferError) as caught:
        sanitize_filename(bad)
    assert caught.value.code==E.UNSAFE_FILENAME


@pytest.mark.parametrize('name,expected',[('x. ','x'),('CON.txt','_CON.txt'),('aux','_aux'),
    ('COM9.pdf','_COM9.pdf'),('a<>|?*.txt','a_____.txt'),('Báo cáo tuần 3.pdf','Báo cáo tuần 3.pdf')])
def test_sanitised_names(name,expected):
    assert sanitize_filename(name)==expected


def test_long_name_caps_and_preserves_extension(tmp_path):
    name=sanitize_filename('á'*300+'.pdf')
    assert len(name.encode())<=255 and name.endswith('.pdf')
    path,fd=reserve_destination(tmp_path,name);os.close(fd)
    assert path.parent==tmp_path.resolve()


@pytest.mark.parametrize('failure,code',[(errno.ENOSPC,E.DISK_FULL),(errno.EACCES,E.PERMISSION_DENIED)])
@pytest.mark.parametrize('operation',['open','write','fsync','replace'])
def test_io_failure_cleanup(tmp_path,monkeypatch,failure,code,operation):
    sender=session(tmp_path,1)
    receiver=FakeRelay(sender,tmp_path/'d').receiver
    def fail(*args,**kwargs):
        raise OSError(failure,'injected')
    if operation=='open':
        monkeypatch.setattr(os,'open',fail)
        with pytest.raises(TransferError) as caught:
            receiver.start()
        assert caught.value.code==code
    else:
        receiver.accept()
        data=next(iter_chunks(sender.info.path,chunk_size=4096)).data
        if operation=='write':
            monkeypatch.setattr(os,'write',fail)
            with pytest.raises(TransferError):
                receiver.write(0,data)
        else:
            receiver.write(0,data)
            monkeypatch.setattr(os,operation,fail)
            result=receiver.finish(1,1)
            assert not result.header['ok']
    assert receiver.snapshot.error.code==code
    assert receiver._fd is None
    assert not list((tmp_path/'d').iterdir())


def test_part_creation_failure_cleans_reservation(tmp_path,monkeypatch):
    sender=session(tmp_path)
    r=FakeRelay(sender,tmp_path/'d').receiver
    original=os.open
    def fail_part(path,*args,**kwargs):
        if str(path).endswith('.part'):
            raise OSError(errno.EACCES,'injected')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(os,'open',fail_part)
    with pytest.raises(TransferError):r.start()
    assert not list((tmp_path/'d').iterdir())


def test_directory_failures(tmp_path):
    sender=session(tmp_path)
    blocked=tmp_path/'not_directory';blocked.write_bytes(b'x')
    r=FakeRelay(sender,blocked/'child').receiver
    with pytest.raises(TransferError) as caught:r.start()
    assert caught.value.code==E.PERMISSION_DENIED


@pytest.mark.parametrize('action',['modify','truncate','delete'])
def test_source_changes(tmp_path,action):
    sender=session(tmp_path)
    def mutate(msg):
        if msg.type==MessageType.FILE_CHUNK and msg.header['seq']==0:
            if action=='delete':
                # Windows denies deleting a file open for reading; truncate/modify
                # exercise live edits there; deletion before send is tested below.
                return
            with sender.info.path.open('r+b') as stream:
                if action=='truncate':stream.truncate(1)
                else:stream.seek(10000);stream.write(b'changed')
    if action=='delete':sender.info.path.unlink()
    receiver=FakeRelay(sender,tmp_path/'d',delay=mutate).run()
    assert sender.snapshot.status==S.FAILED
    assert sender.snapshot.error.code in {E.SOURCE_CHANGED,E.FILE_NOT_FOUND}
    assert receiver.snapshot.status==S.CANCELLED
    assert not list((tmp_path/'d').iterdir())


def test_timeouts(tmp_path):
    now=[0.0];clock=lambda:now[0]
    sender=session(tmp_path,0,clock=clock)
    now[0]=60;sender.check_timeout()
    assert sender.snapshot.error.code==E.TIMEOUT
    sender=session(tmp_path,0,clock=clock)
    relay=FakeRelay(sender,tmp_path/'d');r=relay.receiver
    sender.handle_message(r.accept())
    now[0]+=30;r.check_timeout()
    assert r.snapshot.error.code==E.TIMEOUT
    list(sender.outgoing())
    now[0]+=30;sender.check_timeout()
    assert sender.snapshot.status==S.FAILED and sender.snapshot.error.code==E.TIMEOUT


def test_progress_throttle_monotonic_and_first_last(tmp_path):
    now=[0.0];updates=[]
    sender=session(tmp_path,1048576,clock=lambda:now[0],on_update=updates.append)
    def tick(msg):now[0]+=0.001
    FakeRelay(sender,tmp_path/'d',delay=tick).run()
    data_updates=[s for s in updates if s.status==S.TRANSFERRING]
    assert len(data_updates)<=5
    assert updates[0].status==S.OFFERED and updates[-1].status==S.COMPLETED
    assert sorted(s.processed_bytes for s in updates)==[s.processed_bytes for s in updates]
    assert any(s.processed_bytes==1048576 for s in data_updates)


def test_cross_thread_cancel_while_flowing(tmp_path):
    updates=[]
    sender=session(tmp_path,1048576,on_update=updates.append)
    relay=FakeRelay(sender,tmp_path/'d')
    ready=threading.Event();cancelled=threading.Event()
    def cancel():
        ready.wait(5)
        msg=sender.cancel()
        if msg:relay.receiver.handle_message(msg)
        cancelled.set()
    thread=threading.Thread(target=cancel);thread.start()
    def delay(msg):
        if msg.type==MessageType.FILE_CHUNK:
            ready.set();assert cancelled.wait(5)
    relay.delay=delay
    relay.run();thread.join(5)
    assert not thread.is_alive()
    assert sender.snapshot.status==relay.receiver.snapshot.status==S.CANCELLED
    assert sum(s.is_terminal for s in updates)==1
    assert sender._chunks is None and relay.receiver._fd is None


def test_unrelated_transfer_and_bad_result(tmp_path):
    sender=session(tmp_path,0)
    with pytest.raises(TransferError) as caught:sender.handle_message(build_file_accept('f'*32))
    assert caught.value.code==E.TRANSFER_UNKNOWN
    sender.handle_message(build_file_accept(sender.transfer_id));list(sender.outgoing())
    sender.handle_message(build_file_result(sender.transfer_id,True,0,'0'*64))
    assert sender.snapshot.error.code==E.HASH_MISMATCH


def test_partial_os_writes(tmp_path,monkeypatch):
    sender=session(tmp_path,100)
    original=os.write
    monkeypatch.setattr(os,'write',lambda fd,data:original(fd,data[:7]))
    r=FakeRelay(sender,tmp_path/'d').run()
    assert r.snapshot.status==S.COMPLETED
    assert digest(r.snapshot.saved_path)==sender.info.sha256


def test_symlink_final_component_is_never_followed(tmp_path):
    target=tmp_path/'existing';target.write_bytes(b'keep')
    link=tmp_path/'a.txt'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('Creating symlinks requires privileges on this Windows host')
    path,fd=reserve_destination(tmp_path,'a.txt');os.close(fd)
    assert path.name=='a (1).txt' and target.read_bytes()==b'keep'


def test_low_free_space_and_chunk_shape(tmp_path,monkeypatch):
    sender=session(tmp_path)
    r=FakeRelay(sender,tmp_path/'d').receiver
    import transfer.receiver as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda p:type('Usage',(),{'free':0})())
    with pytest.raises(TransferError) as caught:r.start()
    assert caught.value.code==E.DISK_FULL
    assert not list((tmp_path/'d').iterdir())
    monkeypatch.undo()
    r=FakeRelay(sender,tmp_path/'d').receiver;r.start()
    with pytest.raises(TransferError) as caught:r.write(0,b'short')
    assert caught.value.code==E.SIZE_MISMATCH


def test_iter_chunks_laziness_and_inspection_cancel(tmp_path):
    path=source(tmp_path,8193)
    info=inspect_file(path)
    iterator=iter_chunks(path,chunk_size=4096,expected=info)
    assert [len(c.data) for c in iterator]==[4096,4096,1]
    with pytest.raises(TransferError) as caught:inspect_file(path,cancel=lambda:True)
    assert caught.value.code==E.CANCELLED_LOCAL
    with pytest.raises(TransferError):inspect_file(tmp_path)
    with pytest.raises(TransferError):inspect_file(tmp_path/'missing')


def test_receiver_cancel_from_another_thread(tmp_path):
    sender=session(tmp_path)
    r=FakeRelay(sender,tmp_path/'d').receiver;r.start()
    r.write(0,next(iter_chunks(sender.info.path,chunk_size=4096)).data)
    thread=threading.Thread(target=r.abort);thread.start();thread.join(5)
    r.connection_lost();r.abort()
    assert not thread.is_alive() and r.snapshot.status==S.CANCELLED
    assert r._fd is None and not list((tmp_path/'d').iterdir())


def test_long_received_name_and_nfc(tmp_path):
    sender=session(tmp_path,0)
    r=FakeRelay(sender,tmp_path/'d').receiver
    r.offer=replace(r.offer,filename='á'*300+'.pdf')
    r.start();result=r.finish(0,0)
    assert result.header['ok']
    assert len(Path(r.snapshot.saved_path).name.encode())<=255


def test_wrong_result_byte_count(tmp_path):
    sender=session(tmp_path,1)
    sender.handle_message(build_file_accept(sender.transfer_id));list(sender.outgoing())
    sender.handle_message(build_file_result(sender.transfer_id,True,0))
    assert sender.snapshot.error.code==E.SIZE_MISMATCH


@pytest.mark.skipif(os.name=='nt',reason='Windows disallows unlinking an open source; deletion before send is covered separately')
def test_source_deleted_after_first_chunk(tmp_path):
    sender=session(tmp_path)
    def remove_source(msg):
        if msg.type==MessageType.FILE_CHUNK and msg.header['seq']==0:
            sender.info.path.unlink()
    receiver=FakeRelay(sender,tmp_path/'d',delay=remove_source).run()
    assert sender.snapshot.status==S.FAILED and sender.snapshot.error.code==E.SOURCE_CHANGED
    assert receiver.snapshot.status==S.CANCELLED
    assert not list((tmp_path/'d').iterdir())
