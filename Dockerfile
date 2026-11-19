# LanLink: server and CLI client (Python standard library only, no pip install needed)
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY common ./common
COPY server ./server
COPY client ./client
COPY transfer ./transfer
# certs/ and data/ are mounted at run time, never baked into the image
