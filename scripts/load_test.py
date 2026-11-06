"""Measured TCP/TLS load driver, independent of A's transport and server code.

One worker owns each verified TLS connection and its file engines. Shared
monotonic timestamps correlate chat payload markers within this process only.
Reports describe observations; an absent ACK, hash or sample is never success.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import csv
import json
import math
import os
from pathlib import Path
import platform
import random
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.errors import ErrorCode, LanLinkError, ProtocolError, TransferError
from common.models import Message, TransferStatus
from common.protocol import (
    DEFAULT_LIMITS, Direction, FrameDecoder, MessageType, ProtocolLimits,
    build_auth, build_chat_private, build_chat_public, build_file_cancel,
    build_file_reject, build_file_result, build_hello, build_logout,
    build_ping, build_pong, encode_message,
)
from transfer.receiver import IncomingOffer, Receiver
from transfer.sender import SendSession, inspect_file


@dataclass(frozen=True)
class Credential:
    username: str
    password: str = field(repr=False)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--cafile', help='Trusted CA PEM; default uses the system trust store')
    parser.add_argument('--server-hostname', help='Certificate DNS name; default is --host')
    parser.add_argument('--users', type=int, default=20)
    parser.add_argument('--user-prefix', default='loaduser')
    parser.add_argument('--password-env', default='LANLINK_LOAD_PASSWORD')
    parser.add_argument('--users-file', type=Path, help='JSON list / {users:[...]} or CSV username,password')
    parser.add_argument('--duration', type=float, default=300)
    parser.add_argument('--messages-per-client', type=int, default=10)
    parser.add_argument('--join-mode', choices=['simultaneous', 'staggered'], default='simultaneous')
    parser.add_argument('--private-ratio', type=float, default=0.3)
    parser.add_argument('--file-mib', type=int, default=0)
    parser.add_argument('--slow-readers', type=int, default=0)
    parser.add_argument('--report-json', type=Path, default=Path('load-report.json'))
    parser.add_argument('--report-md', type=Path, default=Path('load-report.md'))
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.users < 1 or not math.isfinite(args.duration) or not args.duration > 0:
        parser.error('port must be 1..65535, users >= 1 and duration > 0')
    if not 0 <= args.private_ratio <= 1 or args.messages_per_client < 0:
        parser.error('private ratio must be 0..1 and messages-per-client >= 0')
    if not 0 <= args.slow_readers < args.users:
        parser.error('slow-readers must be 0..users-1')
    if args.file_mib < 0 or args.file_mib * 1048576 > DEFAULT_LIMITS.max_file_bytes:
        parser.error('file-mib must fit the default application file limit')
    if args.file_mib and (args.users - args.slow_readers < 2 or args.messages_per_client < 2):
        parser.error('file scenario needs two reading clients and at least two messages per client')
    args.server_hostname = args.server_hostname or args.host
    return args


def load_credentials(args: argparse.Namespace) -> list[Credential]:
    """No passwords in argv, reports or exceptions; file shape is explicit for A."""
    try:
        if args.users_file:
            with args.users_file.open(encoding='utf-8-sig', newline='') as source:
                if args.users_file.suffix.lower() == '.csv':
                    rows = list(csv.DictReader(source))
                else:
                    rows = json.load(source)
                    if isinstance(rows, dict):
                        rows = rows['users']
            if not isinstance(rows, list) or len(rows) < args.users:
                raise ValueError
            credentials = []
            for row in rows[:args.users]:
                if not isinstance(row, dict) or set(row) != {'username', 'password'}:
                    raise ValueError
                credentials.append(Credential(row['username'], row['password']))
        else:
            password = os.environ.get(args.password_env)
            if not password:
                raise ValueError
            credentials = [Credential(f'{args.user_prefix}{index+1:02d}', password) for index in range(args.users)]
        for credential in credentials:
            build_auth(credential.username, credential.password)
        if len({c.username for c in credentials}) != len(credentials):
            raise ValueError
        return credentials
    except (OSError, KeyError, ValueError, TypeError, LanLinkError):
        raise ValueError('Invalid credentials: provide unique username/password rows in --users-file or set --password-env') from None


def percentile(values: list[float], percent: float) -> float | None:
    """Linear interpolation of sorted samples at (n-1)*percent/100."""
    if not 0 <= percent <= 100:
        raise ValueError('Percentile outside 0..100')
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered)-1) * percent / 100
    low = int(position)
    high = min(low+1, len(ordered)-1)
    return ordered[low] + (ordered[high]-ordered[low]) * (position-low)


def latency_summary(samples: list[float]) -> dict:
    """Input seconds, output milliseconds; None means no measurement."""
    values = [sample * 1000 for sample in samples]
    return {'samples': len(values), 'min_ms': min(values) if values else None,
            'p50_ms': percentile(values, 50), 'p95_ms': percentile(values, 95),
            'p99_ms': percentile(values, 99), 'max_ms': max(values) if values else None}


def error_key(error: Exception) -> str:
    return error.code.value if isinstance(error, LanLinkError) else type(error).__name__


@dataclass
class SentChat:
    username: str
    request_id: str
    marker: bytes
    expected: set[str]
    sent_at: float
    phase: str
    message_type: MessageType = MessageType.CHAT_PUBLIC
    recipient: str | None = None
    confirmed: bool = False
    ack_at: float | None = None
    ack_count: int = 0
    deliveries: Counter = field(default_factory=Counter)


class Measurements:
    """Protect shared observations; never retain AUTH messages or credentials."""
    def __init__(self, usernames: list[str], clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.clients = {name: {'stages': [], 'errors': Counter(), 'crashed': False,
                               'slow_probe': 'not_run'} for name in usernames}
        self.chats: dict[str, SentChat] = {}
        self.markers: dict[bytes, str] = {}
        self.delivery_latencies: list[float] = []
        self.delivery_count = 0
        self.untracked_deliveries = 0
        self.unexpected_deliveries = 0
        self.unexpected_acks = 0
        self.slow_readers: set[str] = set()
        self.file_states: dict[str, dict] = {}
        self.file_hash = None
        self.received_hash = None
        self.file_attempted = False
        self.file_active = False
        self.file_started_at = None
        self.file_finished_at = None
        self.interrupted = False

    def stage(self, username: str, stage: str) -> None:
        with self.lock:
            self.clients[username]['stages'].append(stage)

    def error(self, username: str, error: Exception, *, crashed: bool = False) -> None:
        with self.lock:
            client = self.clients[username]
            client['errors'][error_key(error)] += 1
            client['crashed'] |= crashed

    def register_chat(self, username: str, message: Message, expected: set[str], phase: str) -> None:
        with self.lock:
            key = message.header['request_id']
            self.chats[key] = SentChat(username, key, message.payload, expected, self.clock(), phase,
                                       message.type, message.header.get('recipient'))
            self.markers[message.payload] = key

    def confirm_chat(self, message: Message) -> None:
        with self.lock:
            self.chats[message.header['request_id']].confirmed = True

    def acknowledge(self, ref_request_id: str, username: str) -> None:
        with self.lock:
            chat = self.chats.get(ref_request_id)
            if chat is not None and chat.username == username:
                chat.ack_count += 1
                if chat.ack_at is None:
                    chat.ack_at = self.clock()
            else:
                self.unexpected_acks += 1

    def deliver(self, username: str, message: Message) -> None:
        with self.lock:
            self.delivery_count += 1
            key = self.markers.get(message.payload)
            if key is None:
                self.untracked_deliveries += 1
                self.unexpected_deliveries += 1
                return
            chat = self.chats[key]
            allowed = chat.expected | ({chat.username} | self.slow_readers
                                      if chat.message_type == MessageType.CHAT_PUBLIC else set())
            if (username not in allowed or message.type != chat.message_type
                    or message.header.get('sender') != chat.username
                    or message.header.get('recipient') != chat.recipient):
                self.unexpected_deliveries += 1
                return
            chat.deliveries[username] += 1
            if chat.deliveries[username] == 1:
                self.delivery_latencies.append(self.clock() - chat.sent_at)

    def file_update(self, username: str, snapshot) -> None:
        with self.lock:
            previous = self.file_states.get(username)
            self.file_states[username] = {'status': snapshot.status.value,
                'processed_bytes': snapshot.processed_bytes, 'total_bytes': snapshot.total_bytes,
                'error': snapshot.error.code.value if snapshot.error else None}
            if snapshot.error and snapshot.is_terminal and (not previous or previous['status'] != snapshot.status.value):
                self.clients[username]['errors'][snapshot.error.code.value] += 1
            if snapshot.direction.value == 'send':
                self.file_active = snapshot.status in {TransferStatus.ACCEPTED, TransferStatus.TRANSFERRING, TransferStatus.VERIFYING}
                if self.file_active and self.file_started_at is None:
                    self.file_started_at = self.clock()
                if snapshot.is_terminal:
                    self.file_finished_at = self.clock()

    def pending_for(self, username: str) -> bool:
        with self.lock:
            return any(chat.confirmed and ((chat.username == username and chat.ack_at is None)
                       or (username in chat.expected and not chat.deliveries[username])) for chat in self.chats.values())


class Coordinator:
    """Join barrier includes failed attempts, so one failed login cannot hang peers."""
    def __init__(self, credentials: list[Credential], args, clock=time.monotonic):
        self.args, self.clock = args, clock
        self.usernames = [c.username for c in credentials]
        self.slow = set(self.usernames[-args.slow_readers:]) if args.slow_readers else set()
        self.readers = [name for name in self.usernames if name not in self.slow]
        self.lock = threading.Lock()
        self.joined = set()
        self.online = set()
        self.ready = threading.Event()
        self.started_at = None
        self.file_source: Path | None = None
        self.download_root: Path | None = None

    def joined_client(self, name: str, online: bool) -> None:
        with self.lock:
            self.joined.add(name)
            if online:
                self.online.add(name)
            if len(self.joined) == len(self.usernames) and not self.ready.is_set():
                self.started_at = self.clock()
                self.ready.set()

    def peers(self, username: str) -> set[str]:
        with self.lock:
            return self.online - self.slow - {username}

    def leave(self, username: str) -> None:
        with self.lock:
            self.online.discard(username)


def create_tls_connection(args, stage: Callable[[str], None]):
    """Default factory. Server certificate and hostname verification always stay on."""
    context = ssl.create_default_context(cafile=args.cafile)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    raw = socket.create_connection((args.host, args.port), timeout=5)
    stage('tcp_connected')
    try:
        connection = context.wrap_socket(raw, server_hostname=args.server_hostname)
        stage('tls_established')
        return connection
    except BaseException:
        raw.close()
        raise


class LoadClient:
    """Worker loop takes a connect(stage_callback) factory and monotonic clock."""
    def __init__(self, credential: Credential, args, measurements: Measurements, coordinator: Coordinator,
                 *, connect=None, clock=time.monotonic, stop: threading.Event | None = None):
        self.credential, self.args = credential, args
        self.metrics, self.coordinator = measurements, coordinator
        self.clock, self.stop = clock, stop if stop is not None else threading.Event()
        self.connect = connect or (lambda stage: create_tls_connection(args, stage))
        self.connection = None
        self.limits = DEFAULT_LIMITS
        self.decoder = FrameDecoder(direction=Direction.SERVER_TO_CLIENT)
        self.authenticated = False
        self.sender = None
        self.receiver = None
        self.outgoing = None
        self.sent_count = 0
        self.last_read = clock()
        self.last_ping = clock()
        self.file_offered = False
        self.closed_by_server = False
        self.run_id = uuid4().hex
        self.rng = random.Random(self.run_id)

    @property
    def username(self) -> str:
        return self.credential.username

    def stage(self, value: str) -> None:
        self.metrics.stage(self.username, value)

    def send(self, message: Message) -> None:
        self.connection.sendall(encode_message(message, self.limits))

    def receive(self, *, allow_close: bool = False) -> list[Message] | None:
        try:
            data = self.connection.recv(65536)
        except (socket.timeout, TimeoutError):
            return []
        if not data:
            self.decoder.feed_eof()
            if allow_close:
                return None
            raise TransferError(ErrorCode.CONNECTION_LOST)
        self.last_read = self.clock()
        messages = self.decoder.feed(data)
        for message in messages:
            self.dispatch(message)
        return messages

    def wait_reply(self, kind: MessageType, request_id: str) -> Message:
        deadline = self.clock() + 10
        while not self.stop.is_set() and self.clock() < deadline:
            for message in self.receive() or []:
                if message.type == kind and message.header.get('ref_request_id') == request_id:
                    return message
        raise TransferError(ErrorCode.TIMEOUT)

    def dispatch(self, message: Message) -> None:
        h, kind = message.header, message.type
        if kind == MessageType.PING:
            self.send(build_pong(h['request_id']))
        elif kind == MessageType.CHAT_ACK:
            self.metrics.acknowledge(h['ref_request_id'], self.username)
        elif kind in {MessageType.CHAT_PUBLIC, MessageType.CHAT_PRIVATE}:
            self.metrics.deliver(self.username, message)
        elif kind == MessageType.ERROR:
            if self.username in self.coordinator.slow and h['code'] == ErrorCode.QUEUE_FULL:
                self.metrics.clients[self.username]['slow_probe'] = 'ERROR_QUEUE_FULL'
                return
            if self.sender and h.get('transfer_id') == self.sender.transfer_id:
                self.sender.handle_message(message)
            if self.receiver and h.get('transfer_id') == self.receiver.offer.transfer_id:
                self.receiver.handle_message(message)
            raise TransferError(ErrorCode(h['code']))
        elif kind == MessageType.FILE_OFFER:
            if (not self.args.file_mib or self.username != self.coordinator.readers[1]
                    or h['sender'] != self.coordinator.readers[0] or self.receiver is not None):
                self.send(build_file_reject(h['transfer_id'], 'Unexpected load-test offer'))
                return
            offer = IncomingOffer.from_message(message, limits=self.limits)
            self.receiver = Receiver(offer, self.coordinator.download_root / self.username,
                limits=self.limits, clock=self.clock,
                on_update=lambda snap:self.metrics.file_update(self.username, snap))
            try:
                self.send(self.receiver.accept())
            except TransferError as error:
                self.send(build_file_result(offer.transfer_id, False, 0, error_code=error.code, limits=self.limits))
                raise
        elif kind in {MessageType.FILE_ACCEPT, MessageType.FILE_REJECT, MessageType.FILE_RESULT}:
            if self.sender is None:
                raise TransferError(ErrorCode.TRANSFER_UNKNOWN)
            self.sender.handle_message(message)
            if kind == MessageType.FILE_RESULT:
                with self.metrics.lock:
                    self.metrics.received_hash = h.get('sha256')
            if kind == MessageType.FILE_ACCEPT:
                self.outgoing = self.sender.outgoing()
        elif kind in {MessageType.FILE_CHUNK, MessageType.FILE_END, MessageType.FILE_CANCEL}:
            if self.receiver and h['transfer_id'] == self.receiver.offer.transfer_id:
                for reply in self.receiver.handle_message(message):
                    self.send(reply)
            elif self.sender:
                self.sender.handle_message(message)
            else:
                raise TransferError(ErrorCode.TRANSFER_UNKNOWN)

    def login(self) -> None:
        self.stage('connect_attempted')
        self.connection = self.connect(self.stage)
        self.connection.settimeout(0.05)
        hello = build_hello('LanLink load test', '1.0')
        self.send(hello)
        reply = self.wait_reply(MessageType.HELLO_ACK, hello.header['request_id'])
        self.limits = ProtocolLimits.from_dict(reply.header['limits'])
        self.decoder.limits = self.limits
        self.stage('hello_accepted')
        self.stage('auth_attempted')
        auth = build_auth(self.username, self.credential.password)
        self.send(auth)
        self.wait_reply(MessageType.AUTH_OK, auth.header['request_id'])
        self.authenticated = True
        self.stage('authenticated')

    def phase(self) -> str:
        with self.metrics.lock:
            if self.metrics.file_active:
                return 'during_file'
            return 'post_file' if self.metrics.file_attempted else 'baseline'

    def send_chat(self) -> None:
        peers = self.coordinator.peers(self.username)
        marker = f'lanlink-load:{self.run_id}:{self.sent_count}'
        if peers and self.rng.random() < self.args.private_ratio:
            recipient = self.rng.choice(sorted(peers))
            message = build_chat_private(recipient, marker, limits=self.limits)
            expected = {recipient}
        else:
            message = build_chat_public(marker, limits=self.limits)
            expected = peers
        self.metrics.register_chat(self.username, message, expected, self.phase())
        self.send(message)
        self.metrics.confirm_chat(message)
        self.sent_count += 1

    def pump_file(self, elapsed: float) -> None:
        if (self.args.file_mib and self.username == self.coordinator.readers[0]
                and not self.file_offered and elapsed >= self.args.duration / 3):
            self.file_offered = True
            with self.metrics.lock:
                self.metrics.file_attempted = True
            info = inspect_file(self.coordinator.file_source, limits=self.limits, cancel=self.stop)
            self.sender = SendSession(info, self.coordinator.readers[1], limits=self.limits, clock=self.clock,
                on_update=lambda snap:self.metrics.file_update(self.username, snap))
            with self.metrics.lock:
                self.metrics.file_hash = info.sha256
            self.send(self.sender.offer_message())
        if self.outgoing is not None:
            try:
                self.send(next(self.outgoing))
            except StopIteration:
                self.outgoing = None
        for engine in (self.sender, self.receiver):
            if engine is not None:
                was_terminal = engine.snapshot.is_terminal
                engine.check_timeout()
                if not was_terminal and engine.snapshot.is_terminal and engine.snapshot.status != TransferStatus.COMPLETED:
                    self.send(build_file_cancel(engine.snapshot.transfer_id, engine.snapshot.error.code))

    def slow_reader(self, deadline: float) -> None:
        # No recv occurs in this interval. Probe afterwards; do not infer closure
        # from silence, nor assume ten small messages will saturate a queue.
        while not self.stop.is_set() and self.clock() < deadline:
            self.stop.wait(min(0.05, max(0, deadline-self.clock())))
        probe_deadline = self.clock() + 2
        self.metrics.clients[self.username]['slow_probe'] = 'no_close_observed'
        while not self.stop.is_set() and self.clock() < probe_deadline:
            messages = self.receive(allow_close=True)
            if messages is None:
                self.metrics.clients[self.username]['slow_probe'] = 'EOF'
                self.closed_by_server = True
                return
            if self.metrics.clients[self.username]['slow_probe'] == 'ERROR_QUEUE_FULL':
                self.closed_by_server = True
                return

    def chat_loop(self) -> None:
        started = self.coordinator.started_at
        deadline = started + self.args.duration
        if self.username in self.coordinator.slow:
            self.slow_reader(deadline)
            return
        next_chat = self.clock()
        baseline_count = max(1, self.args.messages_per_client // 2)
        while not self.stop.is_set() and self.clock() < deadline:
            now = self.clock()
            self.pump_file(now-started)
            permitted = (not self.args.file_mib or self.sent_count < baseline_count
                         or self.phase() == 'during_file' or self.metrics.file_finished_at is not None)
            if permitted and self.sent_count < self.args.messages_per_client and now >= next_chat:
                self.send_chat()
                next_chat = now + self.rng.uniform(0.05, 0.15)
            if now-self.last_ping >= self.limits.heartbeat_interval_s:
                self.send(build_ping()); self.last_ping = now
            if now-self.last_read >= self.limits.idle_timeout_s:
                raise TransferError(ErrorCode.TIMEOUT)
            self.receive()
        drain_until = self.clock() + 2
        while not self.stop.is_set() and self.clock() < drain_until and self.metrics.pending_for(self.username):
            self.receive()

    def run(self) -> None:
        joined = False
        try:
            self.login()
            self.coordinator.joined_client(self.username, True); joined = True
            while not self.stop.is_set() and not self.coordinator.ready.wait(0.05):
                self.receive()
            if not self.stop.is_set():
                self.chat_loop()
            if not self.stop.is_set() and not self.closed_by_server:
                self.send(build_logout('Load test finished'))
        except (LanLinkError, OSError) as error:
            self.metrics.error(self.username, error)
        except Exception as error:
            self.metrics.error(self.username, error, crashed=True)
        finally:
            if not joined:
                self.coordinator.joined_client(self.username, False)
            for engine in (self.sender, self.receiver):
                if engine is not None:
                    engine.connection_lost()
            if self.outgoing is not None:
                self.outgoing.close()
            if self.connection is not None:
                try:
                    self.connection.close()
                except OSError as error:
                    self.metrics.error(self.username, error)
            self.coordinator.leave(self.username)


def environment(args) -> dict:
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=3, check=False)
        commit = result.stdout.strip() if result.returncode == 0 else 'unknown'
    except (OSError, subprocess.SubprocessError):
        commit = 'unknown'
    return {'python': platform.python_version(), 'os': platform.platform(),
            'cpu_count': os.cpu_count(), 'server_host': args.host, 'server_port': args.port, 'commit': commit}


def make_report(metrics: Measurements, args) -> dict:
    """Only confirmed sends count; missing observations remain explicit failures."""
    with metrics.lock:
        chats = [chat for chat in metrics.chats.values() if chat.confirmed]
        errors = Counter()
        for client in metrics.clients.values():
            errors.update(client['errors'])
        stages = Counter(stage for client in metrics.clients.values() for stage in set(client['stages']))
        missing_deliveries = sum(len(chat.expected - chat.deliveries.keys()) for chat in chats)
        missing_acks = sum(chat.ack_at is None for chat in chats)
        duplicate_deliveries = sum(sum(max(0,count-1) for count in chat.deliveries.values()) for chat in chats)
        ack_latencies = [chat.ack_at-chat.sent_at for chat in chats if chat.ack_at is not None]
        phase_latency = {phase: latency_summary([chat.ack_at-chat.sent_at for chat in chats
                         if chat.ack_at is not None and chat.phase == phase]) for phase in ('baseline','during_file','post_file')}
        failures = []
        if errors: failures.append('client_errors')
        if missing_acks: failures.append('missing_acks')
        if missing_deliveries: failures.append('missing_deliveries')
        if duplicate_deliveries: failures.append('duplicate_deliveries')
        if metrics.unexpected_acks: failures.append('unexpected_acks')
        if metrics.unexpected_deliveries: failures.append('unexpected_deliveries')
        if any(chat.ack_count > 1 for chat in chats): failures.append('duplicate_acks')
        if metrics.interrupted: failures.append('interrupted')
        if stages['authenticated'] != args.users: failures.append('not_all_clients_authenticated')
        if len(chats) != (args.users - args.slow_readers) * args.messages_per_client:
            failures.append('unsent_planned_messages')
        failure_stages = {'connect': Counter(), 'tls': Counter(), 'hello': Counter(), 'auth': Counter(), 'active': Counter()}
        for client in metrics.clients.values():
            stage = ('active' if 'authenticated' in client['stages'] else 'auth' if 'auth_attempted' in client['stages']
                     else 'hello' if 'tls_established' in client['stages'] else 'tls' if 'tcp_connected' in client['stages'] else 'connect')
            failure_stages[stage].update(client['errors'])
        file_verified = None
        if args.file_mib:
            file_verified = (len(metrics.file_states) == 2
                and all(state['status']=='completed' for state in metrics.file_states.values())
                and metrics.file_hash is not None and metrics.file_hash == metrics.received_hash)
            if not file_verified: failures.append('file_not_verified')
            if not phase_latency['baseline']['samples'] or not phase_latency['during_file']['samples']:
                failures.append('insufficient_chat_phase_samples')
        slow_names = set(list(metrics.clients)[-args.slow_readers:]) if args.slow_readers else set()
        reading_names = set(metrics.clients) - slow_names
        reading_errors = {name:dict(metrics.clients[name]['errors']) for name in sorted(reading_names)
                          if metrics.clients[name]['errors']}
        reading_missing_acks = sum(chat.username in reading_names and chat.ack_at is None for chat in chats)
        reading_missing_deliveries = sum(len((chat.expected & reading_names) - chat.deliveries.keys()) for chat in chats)
        observed_stages = {stage:stages[stage] for stage in (
            'connect_attempted','tcp_connected','tls_established','hello_accepted','auth_attempted','authenticated')}
        return {
            'environment': environment(args), 'configuration': {'users':args.users,'duration_s':args.duration,
                'messages_per_client':args.messages_per_client,'join_mode':args.join_mode,
                'private_ratio':args.private_ratio,'file_mib':args.file_mib,'slow_readers':args.slow_readers},
            'stages':observed_stages, 'errors':dict(errors), 'clients':metrics.clients,
            'failures_by_stage':{key:dict(value) for key,value in failure_stages.items()},
            'messages_sent':len(chats), 'chat_acks':sum(c.ack_count for c in chats),
            'deliveries_received':metrics.delivery_count, 'untracked_deliveries':metrics.untracked_deliveries,
            'unexpected_acks':metrics.unexpected_acks, 'unexpected_deliveries':metrics.unexpected_deliveries,
            'missing_acks':missing_acks, 'missing_deliveries':missing_deliveries,
            'duplicate_acks':sum(max(0,c.ack_count-1) for c in chats),'duplicate_deliveries':duplicate_deliveries,
            'ack_latency':latency_summary(ack_latencies), 'delivery_latency':latency_summary(metrics.delivery_latencies),
            'ack_latency_by_phase':phase_latency,
            'file':{'enabled':bool(args.file_mib),'attempted':metrics.file_attempted,'verified':file_verified,
                    'expected_sha256':metrics.file_hash,'received_sha256':metrics.received_hash,
                    'started_at_monotonic':metrics.file_started_at,'finished_at_monotonic':metrics.file_finished_at,
                    'states':dict(metrics.file_states)},
            'delivery_policy':'Expected recipients are reading peers authenticated at send time; public sender echoes are optional and observed.',
            'slow_reader_note':'Probed after not reading; no_close_observed means saturation/controlled close was not established.',
            'reading_clients':{'count':len(reading_names),'errors':reading_errors,
                'authenticated':sum('authenticated' in metrics.clients[name]['stages'] for name in reading_names),
                'missing_acks':reading_missing_acks,'missing_deliveries':reading_missing_deliveries},
            'verification_failures':failures, 'exit_code':1 if failures else 0,
        }


def render_markdown(report: dict) -> str:
    def shown(value):
        return 'NOT OBSERVED' if value is None else str(value).replace('|','\\|').replace('\n',' ')
    rows = [('Messages sent',report['messages_sent']),('CHAT_ACK received',report['chat_acks']),
            ('Deliveries received',report['deliveries_received']),('Missing ACKs',report['missing_acks']),
            ('Missing deliveries',report['missing_deliveries']),('Duplicate deliveries',report['duplicate_deliveries']),
            ('File verified',report['file']['verified']),('Exit code',report['exit_code'])]
    text = '# LanLink measured load report\n\n| Metric | Observation |\n|---|---|\n'
    text += '\n'.join(f'| {key} | {shown(value)} |' for key,value in rows)
    text += '\n\n| Latency | Samples | min ms | p50 ms | p95 ms | p99 ms | max ms |\n|---|---:|---:|---:|---:|---:|---:|\n'
    summaries = {'ACK overall':report['ack_latency'],'Delivery overall':report['delivery_latency'],
                 **report['ack_latency_by_phase']}
    for phase,stats in summaries.items():
        cells = [shown(stats[key]) for key in ('samples','min_ms','p50_ms','p95_ms','p99_ms','max_ms')]
        text += '| ' + phase + ' | ' + ' | '.join(cells) + ' |\n'
    text += '\nEnvironment: ' + json.dumps(report['environment'],ensure_ascii=False) + '\n\n'
    text += 'Stages: ' + json.dumps(report['stages']) + '\n\nErrors: ' + json.dumps(report['errors']) + '\n\n'
    text += report['delivery_policy'] + '\n\n' + report['slow_reader_note'] + '\n\n'
    probes = {name:data['slow_probe'] for name,data in report['clients'].items() if data['slow_probe']!='not_run'}
    text += 'Slow reader probes: ' + json.dumps(probes) + '\n\n'
    text += 'Reading client observations: ' + json.dumps(report['reading_clients']) + '\n\n'
    text += 'Verification failures: ' + shown(report['verification_failures']) + '\n'
    return text


def write_reports(report: dict, json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True,exist_ok=True);md_path.parent.mkdir(parents=True,exist_ok=True)
    json_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    md_path.write_text(render_markdown(report),encoding='utf-8')


def generate_file(path: Path, size: int) -> None:
    """Generate random test bytes on disk in bounded chunks, not in memory."""
    with path.open('wb') as output:
        remaining = size
        while remaining:
            data = os.urandom(min(65536, remaining))
            output.write(data); remaining -= len(data)


def main(argv=None, *, connect=None, clock=time.monotonic) -> int:
    args = parse_args(argv)
    try:
        credentials = load_credentials(args)
    except ValueError as error:
        print(str(error),file=sys.stderr)
        return 2
    metrics = Measurements([c.username for c in credentials],clock)
    coordinator = Coordinator(credentials,args,clock)
    metrics.slow_readers = coordinator.slow
    stop = threading.Event()
    try:
        with tempfile.TemporaryDirectory(prefix='lanlink-load-') as directory:
            coordinator.download_root = Path(directory)/'downloads'
            coordinator.file_source = Path(directory)/'load-source.bin'
            if args.file_mib:generate_file(coordinator.file_source,args.file_mib*1048576)
            threads = []
            try:
                for credential in credentials:
                    client = LoadClient(credential,args,metrics,coordinator,connect=connect,clock=clock,stop=stop)
                    thread = threading.Thread(target=client.run,name=f'load-{credential.username}')
                    threads.append(thread);thread.start()
                    if args.join_mode=='staggered':stop.wait(0.1)
                while any(t.is_alive() for t in threads):
                    for thread in threads:thread.join(0.05)
            except KeyboardInterrupt:
                metrics.interrupted = True;stop.set()
            finally:
                stop.set()
                for thread in threads:
                    # KeyboardInterrupt can interrupt Thread.start before it
                    # creates a native thread. Such a Thread cannot be joined.
                    if getattr(thread, 'ident', None) is not None or thread.is_alive():
                        thread.join()
    except (OSError, KeyboardInterrupt) as error:
        metrics.error(credentials[0].username,error)
        metrics.interrupted |= isinstance(error,KeyboardInterrupt)
    report = make_report(metrics,args)
    try:
        write_reports(report,args.report_json,args.report_md)
    except OSError as error:
        print(f'Cannot write load report ({type(error).__name__}).',file=sys.stderr)
        return 2
    if not report['stages'].get('tls_established'):
        print('No verified TLS connection reached the server. Check host, port and CA/hostname. Errors: '
              + json.dumps(report['errors']),file=sys.stderr)
    print(f"Observed {report['messages_sent']} sends, {report['chat_acks']} ACKs; reports: {args.report_json}, {args.report_md}")
    return 130 if metrics.interrupted else report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
