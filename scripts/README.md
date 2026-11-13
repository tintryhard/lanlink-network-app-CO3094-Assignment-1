# scripts/

| File | What it does |
|---|---|
| `make_dev_cert.sh` | Creates a self-signed TLS certificate `certs/server.crt` and its key `certs/server.key` (RSA 2048, valid 365 days) with `openssl` |

```bash
sh scripts/make_dev_cert.sh
```

- The server uses both files: `--cert certs/server.crt --key certs/server.key`.
- Clients get **only** `server.crt`, passed as `--cafile`. Never share `server.key`: anyone holding it can impersonate the server.
- `certs/` is ignored by git.
