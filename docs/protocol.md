# LanLink Protocol v1 (draft — contract not yet approved)

Owner: C. Reviewers: A, B.

## Frame proposal
4-byte big-endian JSON-header length + UTF-8 JSON header + payload_length bytes.

## Message types (proposed)
HELLO, HELLO_ACK, AUTH, AUTH_OK, USER_LIST, LOGOUT, ERROR,
CHAT_PUBLIC, CHAT_PRIVATE, CHAT_ACK,
FILE_OFFER, FILE_ACCEPT, FILE_REJECT, FILE_CHUNK, FILE_END, FILE_RESULT, FILE_CANCEL,
PING, PONG.

UDP discovery: DISCOVER, SERVER_INFO.

## Required contract review
- Message schema and error codes
- Frame bounds and payload validation
- File transfer and connection state diagrams
- Event model and TransferSnapshot
- Test fixtures (valid/invalid frames)

> This file is a draft; A, B and C must approve version 1 before depending on it.
