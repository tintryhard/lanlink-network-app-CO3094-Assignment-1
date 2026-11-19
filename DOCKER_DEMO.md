# LanLink live demo with Docker: step by step

One computer. The server and every client run in their own container on a private Docker network. Each container has its own IP address.

Each step has two versions:
- **Windows:** Windows 11 with Docker Desktop. Run the commands in **Git Bash**.
- **Linux:** Docker Engine with the Compose plugin. Run the commands in a terminal **in the desktop session**, not over SSH.

Where both versions are identical, the command is shown once. Run every command from the repository root.

---

## Before the demo (once)

### 1. Start Docker

**Windows:** open **Docker Desktop** and wait for "Engine running". Then check:

```bash
docker info
```

**Linux:**

```bash
sudo systemctl start docker && docker info
```

The output must include `Server Version: ...`. If `docker` only works with `sudo`, add yourself to the `docker` group with `sudo usermod -aG docker $USER`, then log out and back in.

### 2. Allow GUI windows from containers

**Windows:** WSLg is built into Windows 11, so nothing needs installing. Update it once, in PowerShell as Administrator:

```powershell
wsl --update
```

**Linux:** run this once per login session:

```bash
xhost +local:docker
```

### 3. Create the TLS certificate

This writes `certs/server.crt` and `certs/server.key`. Never share `server.key`.

**Windows:**

```bash
MSYS_NO_PATHCONV=1 sh scripts/make_dev_cert.sh
```

**Linux:**

```bash
sh scripts/make_dev_cert.sh
```

### 4. Prepare the demo files

Windows and Linux:

```bash
mkdir -p demo downloads && ls demo
```

Copy `photo.jpg` and a large `video.mp4` (under 100 MiB) into `demo/`.

### 5. Build the images

Windows and Linux:

```bash
docker compose build server
```

```bash
docker compose --profile gui build gui
```

### 6. Create the three demo users

Each command asks for the password twice; the input is hidden. Run the command once for each of `alice`, `bob` and `charlie`.

**Windows:**

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm -v ./data:/app/data client python -m server.auth --file data/users.json --username alice
```

**Linux:**

```bash
docker compose run --rm -v ./data:/app/data client python -m server.auth --file data/users.json --username alice
```

Then check the result. The output must be `['alice', 'bob', 'charlie']`:

```bash
python3 -c "import json; print(sorted(json.load(open('data/users.json'))['users']))"
```

On Windows, if `python3` is not found, use `python`.

> On Windows, `MSYS_NO_PATHCONV=1` is required. Without it, Git Bash rewrites `/app/data`, and the accounts are saved inside the throwaway container instead of in `data/`.

---

## Start (about 1 minute before presenting)

### Terminal 1: server

Windows and Linux:

```bash
docker compose up -d server
```

```bash
docker compose logs --tail 3 server
```

The last line must be `Listening on ('0.0.0.0', 5000) (TLS)`.

### Terminal 2: alice's window

**Windows:**

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm --name alice-gui -e DISPLAY=:0 -v /run/desktop/mnt/host/wslg/.X11-unix:/tmp/.X11-unix gui
```

**Linux:**

```bash
docker compose run --rm --name alice-gui -e DISPLAY=$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix gui
```

### Terminal 3: bob's window

**Windows:**

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm --name bob-gui -e DISPLAY=:0 -v /run/desktop/mnt/host/wslg/.X11-unix:/tmp/.X11-unix gui
```

**Linux:**

```bash
docker compose run --rm --name bob-gui -e DISPLAY=$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix gui
```

### Terminal 4: charlie (CLI)

Windows and Linux. Enter charlie's password when asked:

```bash
docker compose run --rm --name charlie client python -m client --host 172.30.0.10 --port 5000 --username charlie --cafile server.crt
```

### Terminal 5: spare

Windows and Linux. Type this now, but **do not press Enter** until demo step 10:

```bash
docker kill bob-gui
```

---

## Demo steps

The demo steps are the same on Windows and Linux.

**1. Show the network.** In Terminal 1 (the server terminal):

```bash
docker network inspect lanlink_lan --format "{{range .Containers}}{{.Name}}  {{.IPv4Address}}{{println}}{{end}}"
```

> "Every client is a separate host with its own IP on one subnet."

**2. Log in as alice.** In alice's window:
1. Click **Scan LAN**. It shows `Found LanLink at 172.30.0.10:5000`.
2. In **Certificate**, type `/app/server.crt`.
3. Enter `alice` and the password, then click **Log in**.

> "UDP broadcast finds the server, then TLS → HELLO → AUTH."

**3. Log in as bob, with a wrong password first.** In bob's window, do the same as step 2, but enter a wrong password once, then the correct one.

> "Passwords are checked against salted PBKDF2 hashes. The third failure closes the connection."

**4. List the online users.** In charlie's terminal:

```
/users
```

> "Three users, three different IPs."

**5. Private message.** In alice's window, open **bob** and send `Hi Bob`.

> "The server stamps sender, ID and time. The ACK means accepted by the server, not read."

**6. Public message.** In charlie's terminal:

```
/all Hello everyone
```

**7. Message an offline user.** In charlie's terminal:

```
/msg dave hi
```

> "The sender gets RECIPIENT_OFFLINE instead of an ACK."

**8. Accepted file transfer.**
1. In alice's window, open **charlie**, click **+** and choose `/app/demo/photo.jpg`.
2. In charlie's terminal:

```
/accept <ID>
```

> "The recipient approves first. The file is sent in 64 KiB chunks and checked with SHA-256 before it is saved."

The received file appears in `downloads/` in the project.

**9. Rejected file transfer.** In charlie's terminal:

```
/offer bob demo/photo.jpg
```

In bob's window, click **Reject**.

**10. Disconnect during a transfer.**
1. In alice's window, open **bob** and send `/app/demo/video.mp4`.
2. Bob clicks **Accept**.
3. **Immediately** press Enter in the spare terminal (`docker kill bob-gui`).

> "Alice is told the transfer failed. The partial file is never completed."

**11. The others keep working.** In charlie's terminal:

```
/msg alice still here
```

The message appears in alice's window.

> "One client failing does not affect the others."

---

## After the demo

Windows and Linux:

```bash
docker compose --profile gui --profile client down
```

```bash
docker rm -f alice-gui bob-gui charlie
```

The second command can print `No such container`. That is harmless.

**Windows:** quit Docker Desktop from the system tray, then free its memory (PowerShell):

```powershell
wsl --shutdown
```

**Linux:** revoke the display access:

```bash
xhost -local:docker
```

---

## If something goes wrong

| Problem | Windows | Linux |
|---|---|---|
| Cannot connect to the Docker daemon (`dockerDesktopLinuxEngine` / `docker.sock`) | Start Docker Desktop and wait for "Engine running" | `sudo systemctl start docker`; add yourself to the `docker` group |
| Server log: `No such file or directory: 'data/users.json'` | Redo step 6 **with** `MSYS_NO_PATHCONV=1`, check with `ls data`, then `docker compose restart server` | Redo step 6, check with `ls data`, then `docker compose restart server` |
| `mkdir C:\Program Files\Git\run: Access is denied` | Add the `MSYS_NO_PATHCONV=1` prefix | — |
| GUI: `couldn't connect to display` | Use the Windows command with `DISPLAY=:0` and the WSLg volume; run `wsl --update` and restart Docker Desktop | Run `xhost +local:docker` in the desktop session; `echo $DISPLAY` must print `:0` or similar |
| `container name ... is already in use` | `docker rm -f <name>`, then run again | Same |
| `Plaintext is restricted to localhost` | The CLI is missing `--cafile server.crt`, or **Use TLS** is not ticked in the GUI | Same |
| Scan LAN finds nothing | Type `172.30.0.10` as the host, port `5000`, and tick **Use TLS** | Same |
