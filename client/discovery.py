"""UDP discovery: find LanLink servers on the LAN by broadcast."""
import socket
import time
from typing import Any

from common.builders import build_discover
from common.errors import ProtocolError
from common.protocol import DEFAULT_LIMITS, Direction, decode_datagram, encode_message


def discover_servers(port: int, wait: float = 2.0,
                     address: str = '255.255.255.255') -> list[dict[str, Any]]:
    """Broadcast a UDP DISCOVER and collect SERVER_INFO replies for `wait` seconds.

    Returns one dict per server: name, ip, port (TCP) and tls.
    """
    request = build_discover()
    servers = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.sendto(encode_message(request), (address, port))
        deadline = time.monotonic() + wait
        # := (walrus) assigns `remaining` and tests it in one step; one deadline for all replies.
        while (remaining := deadline - time.monotonic()) > 0:
            udp.settimeout(remaining)
            try:
                data, (ip, _) = udp.recvfrom(DEFAULT_LIMITS.max_datagram_bytes + 1)
            except socket.timeout:
                break
            try:
                reply = decode_datagram(data, direction=Direction.UDP_REPLY)
            except ProtocolError:
                continue  # not a valid SERVER_INFO: ignore
            if reply.header['ref_request_id'] == request.header['request_id']:
                servers.append({'name': reply.header['server_name'], 'ip': ip,
                                'port': reply.header['port'], 'tls': reply.header['tls']})
    return servers
