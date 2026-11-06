"""Import the CLI by path and drive real codec messages through fake connections."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import sys
import threading
import pytest

from common.errors import ErrorCode,TransferError
from common.models import Message,TransferStatus
from common.protocol import (Direction,FrameDecoder,MessageType,build_hello_ack,build_auth_ok,
    build_chat_ack,build_message,build_ping,build_error,encode_message,build_chat_public)

SPEC=importlib.util.spec_from_file_location('lanlink_load_test',Path(__file__).resolve().parents[2]/'scripts/load_test.py')
load=importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name]=load
SPEC.loader.exec_module(load)


class Clock:
    def __init__(self):self.now=0.0
    def __call__(self):return self.now
    def advance(self,delta=0.05):self.now+=delta


class FakeConnection:
    """FIXTURE — NOT A SERVER: scripted replies exercise only LoadClient logic."""
    def __init__(self,clock,*,auth_error=False,ack=True,close_after_login=False):
        self.clock=clock;self.decoder=FrameDecoder(direction=Direction.CLIENT_TO_SERVER)
        self.pending=bytearray();self.sent=[];self.closed=False
        self.auth_error=auth_error;self.ack=ack;self.close_after_login=close_after_login
        self.logged_in=False
    def settimeout(self,value):self.timeout=value
    def queue(self,msg):self.pending.extend(encode_message(msg))
    def sendall(self,data):
        for msg in self.decoder.feed(data):
            self.sent.append(msg.type)
            if msg.type==MessageType.HELLO:self.queue(build_hello_ack(msg.header['request_id'],'fixture'))
            elif msg.type==MessageType.AUTH:
                if self.auth_error:self.queue(build_error(ErrorCode.AUTH_FAILED,'Authentication failed'))
                else:
                    self.queue(build_auth_ok(msg.header['request_id'],msg.header['username']))
                    self.queue(build_ping());self.logged_in=True
            elif msg.type in {MessageType.CHAT_PUBLIC,MessageType.CHAT_PRIVATE} and self.ack:
                self.queue(build_chat_ack(msg.header['request_id'],'m'+str(len(self.sent)),1))
    def recv(self,maximum):
        self.clock.advance()
        if self.pending:
            # Fragment both prefix and JSON, never rely on one recv per message.
            got=bytes(self.pending[:37]);del self.pending[:37];return got
        if self.close_after_login and self.logged_in:return b''
        raise socket.timeout()
    def close(self):self.closed=True


def setup_client(tmp_path,*,connection_options=None,argv=None):
    args=load.parse_args(['--users','1','--duration','2','--messages-per-client','2',*(argv or [])])
    credential=load.Credential('an','secret-in-test-only')
    clock=Clock();metrics=load.Measurements(['an'],clock)
    coordinator=load.Coordinator([credential],args,clock)
    coordinator.download_root=tmp_path/'downloads'
    connection=FakeConnection(clock,**(connection_options or {}))
    def connect(stage):stage('tcp_connected');stage('tls_established');return connection
    client=load.LoadClient(credential,args,metrics,coordinator,connect=connect,clock=clock)
    return client,connection,metrics,args


@pytest.mark.parametrize('values,p,expected',[([],50,None),([1],95,1),([0,10],50,5),
    ([0,10],95,9.5),([1,2,3,4],99,3.97),([4,1,3,2],0,1),([4,1,3,2],100,4)])
def test_percentiles(values,p,expected):
    assert load.percentile(values,p)==pytest.approx(expected) if expected is not None else load.percentile(values,p) is None


def test_empty_latency_is_not_success_sample():
    stats=load.latency_summary([])
    assert stats['samples']==0 and stats['p95_ms'] is None
    assert load.latency_summary([.1,.2])['p50_ms']==pytest.approx(150)
    with pytest.raises(ValueError):load.percentile([1],101)


@pytest.mark.parametrize('argv',[['--users','0'],['--port','0'],['--duration','0'],['--duration','nan'],
    ['--duration','inf'],['--private-ratio','1.1'],['--slow-readers','20'],['--file-mib','101'],
    ['--users','1','--file-mib','1'],['--messages-per-client','-1'],['--join-mode','bad']])
def test_cli_invalid(argv):
    with pytest.raises(SystemExit):load.parse_args(argv)


def test_cli_defaults():
    args=load.parse_args([])
    assert (args.users,args.duration,args.messages_per_client,args.private_ratio)==(20,300,10,.3)
    assert args.server_hostname==args.host
    assert not hasattr(args,'insecure')


def test_credentials_env_and_file(tmp_path,monkeypatch):
    args=load.parse_args(['--users','2','--password-env','LANLINK_TEST_SECRET'])
    monkeypatch.setenv('LANLINK_TEST_SECRET','DO_NOT_EXPOSE')
    credentials=load.load_credentials(args)
    assert [c.username for c in credentials]==['loaduser01','loaduser02']
    assert 'DO_NOT_EXPOSE' not in repr(credentials)
    path=tmp_path/'users.json';path.write_text(json.dumps({'users':[{'username':'an','password':'x'},{'username':'binh','password':'y'}]}))
    args.users_file=path
    assert load.load_credentials(args)[1].username=='binh'
    path.write_text('[{"username":"an","password":"x"},{"username":"an","password":"y"}]')
    with pytest.raises(ValueError):load.load_credentials(args)
    args.users_file=None;monkeypatch.delenv('LANLINK_TEST_SECRET')
    with pytest.raises(ValueError) as caught:load.load_credentials(args)
    assert 'DO_NOT_EXPOSE' not in str(caught.value)


def test_client_loop_with_fragmented_fake_connection(tmp_path):
    client,connection,metrics,args=setup_client(tmp_path)
    client.run()
    report=load.make_report(metrics,args)
    assert report['exit_code']==0 and report['messages_sent']==report['chat_acks']==2
    assert report['stages']['tls_established']==report['stages']['authenticated']==1
    assert MessageType.PONG in connection.sent and MessageType.LOGOUT in connection.sent
    assert connection.closed and not metrics.clients['an']['crashed']
    assert report['ack_latency']['samples']==2


@pytest.mark.parametrize('options,expected',[({'auth_error':True},'AUTH_FAILED'),({'close_after_login':True},'CONNECTION_LOST')])
def test_client_failure_codes(tmp_path,options,expected):
    client,connection,metrics,args=setup_client(tmp_path,connection_options=options)
    client.run();report=load.make_report(metrics,args)
    assert report['errors'][expected]==1 and report['exit_code']==1
    assert connection.closed


def test_missing_ack_is_not_fabricated(tmp_path):
    client,_,metrics,args=setup_client(tmp_path,connection_options={'ack':False})
    client.run();report=load.make_report(metrics,args)
    assert report['messages_sent']==2 and report['chat_acks']==0
    assert report['missing_acks']==2 and report['ack_latency']['p50_ms'] is None
    assert report['exit_code']==1


def test_deliveries_duplicates_missing_and_errors(tmp_path):
    args=load.parse_args(['--users','2','--messages-per-client','1'])
    clock=Clock();m=load.Measurements(['an','binh'],clock)
    msg=build_chat_public('correlation-marker')
    m.register_chat('an',msg,{'binh'},'baseline');m.confirm_chat(msg)
    clock.advance(.25);m.acknowledge(msg.header['request_id'], 'an')
    report=load.make_report(m,args)
    assert report['missing_deliveries']==1
    delivery=build_message(MessageType.CHAT_PUBLIC,msg.payload,sender='an',message_id='m1',timestamp_ms=1)
    m.deliver('binh',delivery);m.deliver('binh',delivery)
    m.error('an',TransferError(ErrorCode.TIMEOUT));m.error('binh',RuntimeError('private details'),crashed=True)
    report=load.make_report(m,args)
    assert report['missing_deliveries']==0 and report['duplicate_deliveries']==1
    assert report['errors']=={'TIMEOUT':1,'RuntimeError':1}
    assert 'private details' not in json.dumps(report)
    assert report['delivery_latency']['p50_ms']==250


def test_report_rendering_and_files(tmp_path):
    _,_,metrics,args=setup_client(tmp_path)
    report=load.make_report(metrics,args)
    load.write_reports(report,tmp_path/'r.json',tmp_path/'r.md')
    assert json.loads((tmp_path/'r.json').read_text())['messages_sent']==0
    text=(tmp_path/'r.md').read_text()
    assert '| Metric | Observation |' in text and 'NOT OBSERVED' in text
    assert 'secret-in-test-only' not in text


def test_tls_factory_keeps_verification(monkeypatch):
    args=load.parse_args(['--cafile','ca.pem','--server-hostname','lanlink.example'])
    class Raw:
        def close(self):self.closed=True
    class Context:
        def wrap_socket(self,raw,*,server_hostname):
            assert server_hostname=='lanlink.example';return raw
    context=Context();raw=Raw();stages=[]
    def default(*,cafile):assert cafile=='ca.pem';return context
    monkeypatch.setattr(load.ssl,'create_default_context',default)
    monkeypatch.setattr(load.socket,'create_connection',lambda address,timeout:raw)
    assert load.create_tls_connection(args,stages.append) is raw
    assert context.check_hostname is True and context.verify_mode==ssl.CERT_REQUIRED
    assert stages==['tcp_connected','tls_established']


def test_main_no_connection_reports_failure(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('LANLINK_LOAD_PASSWORD','test-only')
    def disconnected(stage):raise ConnectionRefusedError('fixture')
    result=load.main(['--users','1','--duration','.01','--messages-per-client','0',
        '--report-json',str(tmp_path/'r.json'),'--report-md',str(tmp_path/'r.md')],connect=disconnected)
    assert result==1
    assert 'No verified TLS connection' in capsys.readouterr().err
    report=json.loads((tmp_path/'r.json').read_text())
    assert report['errors']=={'ConnectionRefusedError':1}
    assert report['stages'].get('authenticated',0)==0


def test_file_scenario_pumps_real_engines(tmp_path):
    args=load.parse_args(['--users','2','--duration','10','--file-mib','1','--messages-per-client','2'])
    credentials=[load.Credential('an','x'),load.Credential('binh','y')]
    clock=Clock();metrics=load.Measurements(['an','binh'],clock);coord=load.Coordinator(credentials,args,clock)
    coord.file_source=tmp_path/'source';coord.download_root=tmp_path/'d'
    load.generate_file(coord.file_source,1048576)
    sender=load.LoadClient(credentials[0],args,metrics,coord,clock=clock)
    receiver=load.LoadClient(credentials[1],args,metrics,coord,clock=clock)
    replies=[]
    receiver.send=lambda msg:replies.append(msg)
    def deliver(msg):
        if msg.type==MessageType.FILE_OFFER:
            msg=Message({**msg.header,'sender':'an'},msg.payload)
        # Queue replies to avoid reentering an executing outgoing generator.
        frame=encode_message(msg)
        receiver.dispatch(FrameDecoder(direction=Direction.SERVER_TO_CLIENT).feed(frame)[0])
    sender.send=deliver
    for _ in range(100):
        sender.pump_file(4)
        while replies:sender.dispatch(replies.pop(0))
        clock.advance(.01)
        if sender.sender and sender.sender.snapshot.is_terminal:break
    assert sender.sender.snapshot.status==receiver.receiver.snapshot.status==TransferStatus.COMPLETED
    report=load.make_report(metrics,args)
    assert report['file']['verified'] is True
    assert 'insufficient_chat_phase_samples' in report['verification_failures']
    assert not list(tmp_path.rglob('*.part'))


def test_slow_reader_probe_never_assumes_close(tmp_path):
    client,connection,metrics,args=setup_client(tmp_path)
    # A scripted monotonic recv clock drives the probe; no real socket is used.
    client.connection=connection
    client.slow_reader(client.clock())
    assert metrics.clients['an']['slow_probe']=='no_close_observed'
    connection.logged_in=True;connection.close_after_login=True
    client.slow_reader(client.clock())
    assert metrics.clients['an']['slow_probe']=='EOF' and client.closed_by_server


def test_tls_handshake_failure_closes_raw_socket(monkeypatch):
    args=load.parse_args([])
    class Raw:
        closed=False
        def close(self):self.closed=True
    class Context:
        def wrap_socket(self,*a,**k):raise ssl.SSLCertVerificationError('fixture certificate')
    raw=Raw()
    monkeypatch.setattr(load.ssl,'create_default_context',lambda **kw:Context())
    monkeypatch.setattr(load.socket,'create_connection',lambda *a,**k:raw)
    stages=[]
    with pytest.raises(ssl.SSLCertVerificationError):load.create_tls_connection(args,stages.append)
    assert raw.closed and stages==['tcp_connected']


def test_unexpected_client_exception_is_counted_as_crash(tmp_path):
    client,connection,metrics,args=setup_client(tmp_path)
    def crash(stage):raise RuntimeError('details must not enter report')
    client.connect=crash;client.run()
    report=load.make_report(metrics,args)
    assert report['clients']['an']['crashed'] and report['exit_code']==1
    assert report['errors']=={'RuntimeError':1}
    assert 'details must not enter report' not in json.dumps(report)
    assert client.coordinator.ready.is_set()


def test_main_ctrl_c_stops_and_joins(tmp_path,monkeypatch):
    monkeypatch.setenv('LANLINK_LOAD_PASSWORD','test-only')
    # On Windows subprocess uses threading.Thread for pipe readers too. Keep
    # that unrelated environment lookup outside this worker-join simulation.
    monkeypatch.setattr(load,'environment',lambda args:{'fixture':True})
    class InterruptingThread:
        def __init__(self,**kwargs):self.first=True
        def start(self):pass
        def is_alive(self):return True
        def join(self,timeout=None):
            if self.first:
                self.first=False
                raise KeyboardInterrupt
    monkeypatch.setattr(load.threading,'Thread',InterruptingThread)
    result=load.main(['--users','1','--duration','.01','--messages-per-client','0',
        '--report-json',str(tmp_path/'r.json'),'--report-md',str(tmp_path/'r.md')],connect=lambda s:None)
    assert result==130
    assert 'interrupted' in json.loads((tmp_path/'r.json').read_text())['verification_failures']


def test_slow_reader_queue_full_and_reading_metrics(tmp_path):
    client,connection,metrics,args=setup_client(tmp_path)
    args.users=2;args.slow_readers=1
    client.coordinator.slow={'an'};client.connection=connection
    connection.queue(build_error(ErrorCode.QUEUE_FULL,'Queue full'))
    client.slow_reader(client.clock())
    assert client.closed_by_server and metrics.clients['an']['slow_probe']=='ERROR_QUEUE_FULL'
    assert not metrics.clients['an']['errors']


def test_csv_credentials(tmp_path):
    path=tmp_path/'users.csv';path.write_text('username,password\nan,fixture\n',encoding='utf-8')
    args=load.parse_args(['--users','1','--users-file',str(path)])
    assert load.load_credentials(args)[0].username=='an'
