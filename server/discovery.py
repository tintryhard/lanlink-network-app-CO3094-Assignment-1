"""UDP discovery responder: answers DISCOVER with SERVER_INFO."""
import socket
import threading

from common.builders import build_server_info
from common.errors import ProtocolError
from common.protocol import DEFAULT_LIMITS, decode_datagram, encode_message


def answer_discovery(udp: socket.socket, stopping: threading.Event, tcp_port: int, tls: bool) -> None:
    """UDP thread: answer each DISCOVER with SERVER_INFO (name, TCP port, TLS on/off).

    Runs until `stopping` is set or the socket is closed. The client takes the
    server's IP from the datagram's source address, so the reply has no host field.
    """
    while not stopping.is_set():
        try:
            # One byte over the limit, so decode_datagram can reject oversize data.
            data, sender = udp.recvfrom(DEFAULT_LIMITS.max_datagram_bytes + 1)
        except socket.timeout:
            continue
        except OSError:
            return  # closed by TCPServer.shutdown()
        try:
            request = decode_datagram(data)  # only a valid DISCOVER passes
        except ProtocolError:
            continue  # UDP has no session to report errors on: ignore junk
        reply = build_server_info(request.header['request_id'], 'LanLink', tcp_port, tls=tls)
        try:
            udp.sendto(encode_message(reply), sender)
        except OSError:
            pass
