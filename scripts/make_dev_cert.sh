#!/bin/sh
# Create a self-signed TLS certificate for the LanLink server (valid 365 days).
# Server: --cert certs/server.crt --key certs/server.key
# Client: copy only certs/server.crt and pass it as --cafile (never share server.key).
set -e
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=lanlink-dev" \
    -keyout certs/server.key -out certs/server.crt
chmod 600 certs/server.key
echo "Created certs/server.crt and certs/server.key"
