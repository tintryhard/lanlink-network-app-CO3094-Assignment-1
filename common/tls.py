"""TLS contexts from the stdlib ssl module.

The server uses a self-signed certificate. Each client trusts exactly that
certificate (copied as --cafile), so only the holder of the server key passes.
"""
import ssl


def server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


def client_context(cert_file: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies the certificate
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # The certificate is pinned; a LAN server's IP changes, so skip the host name check.
    context.check_hostname = False
    context.load_verify_locations(cert_file)
    return context
