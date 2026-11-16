"""Tkinter GUI client: python -m client.gui

A dark chat layout: a sidebar with the public channel and one conversation per user,
the selected conversation in the middle, and a message box at the bottom.
It reuses the CLI's building blocks (open_connection, authenticate, discover_servers,
PrivateChatClient); only the window code is new.
"""
import queue
import socket
import ssl
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from common.errors import LanLinkError
from common.handshake import initiate_hello
from common.models import Message, MessageType, TransferDirection, TransferSnapshot, TransferStatus
from common.protocol import Direction
from common.transport import SocketTransport
from client.connection import authenticate, open_connection
from client.discovery import discover_servers
from client.private_chat import PrivateChatClient

EVERYONE = 'everyone'    # conversation key of the public channel
POLL_MS = 100            # how often the window drains the event queue
USERS_REFRESH_MS = 3000  # the server never pushes user changes, so ask every 3 s
PROGRESS_HIDE_MS = 1000  # keep a finished bar on screen, so even a fast transfer is seen
# Errors a button can raise; they are shown in a dialog instead of crashing the window.
ACTION_ERRORS = (LanLinkError, OSError, ValueError, EOFError)

# Colours of the dark theme.
SIDEBAR, MAIN, FIELD, HOVER = '#2b2d31', '#313338', '#1e1f22', '#404249'
TEXT, MUTED, ACCENT, ONLINE, DANGER = '#dbdee1', '#949ba4', '#5b6cf0', '#23a55a', '#f23f43'
NAME_COLOURS = ['#f0b232', '#3ba55d', '#eb459e', '#00a8fc', '#f47b67', '#a78bfa']
FONT, BOLD, SMALL = ('Segoe UI', 11), ('Segoe UI', 11, 'bold'), ('Segoe UI', 9)


def human_size(size: float) -> str:
    """1536 -> '1.5 KB'."""
    for unit in ('B', 'KB', 'MB'):
        if size < 1024:
            return f'{size:.0f} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


def name_colour(name: str) -> str:
    """Give each username a fixed colour, so the same person always looks the same."""
    return NAME_COLOURS[sum(map(ord, name)) % len(NAME_COLOURS)]


def flat_button(parent: tk.Widget, text: str, command, colour: str = ACCENT) -> tk.Button:
    """A flat, coloured button (plain tk, because ttk buttons ignore colours on some systems)."""
    return tk.Button(parent, text=text, command=command, bg=colour, fg='white', font=BOLD,
                     activebackground=colour, activeforeground='white', relief='flat',
                     borderwidth=0, highlightthickness=0, padx=14, pady=6, cursor='hand2')


def dark_entry(parent: tk.Widget, variable: tk.StringVar, show: str = '', width: int = 20) -> tk.Entry:
    """A flat text field in the dark theme; its border turns blue while it has focus."""
    return tk.Entry(parent, textvariable=variable, show=show, width=width, bg=FIELD, fg=TEXT,
                    font=FONT, insertbackground=TEXT, relief='flat', highlightthickness=1,
                    highlightbackground=FIELD, highlightcolor=ACCENT)


class LoginWindow:
    """First window: account, server address and optional TLS; opens ChatWindow on success.

    Messages (errors, discovery results) appear in a line under the form instead of
    pop-ups. The certificate row is only shown when "Use TLS" is ticked.
    """

    def __init__(self, root: tk.Tk) -> None:
        """Build the login card and centre the window on the screen."""
        self.root = root
        root.title('LanLink')
        root.configure(bg=SIDEBAR)
        root.resizable(False, False)
        # Linux input methods (e.g. IBus) show typed text in a floating box and only
        # insert it into Tk fields later. Turning XIM off sends keys straight to the field.
        root.tk.call('tk', 'useinputmethods', '-displayof', root, False)
        card = tk.Frame(root, bg=MAIN, padx=36, pady=28)
        card.pack(padx=36, pady=36)

        logo = tk.Canvas(card, width=56, height=56, bg=MAIN, highlightthickness=0)
        logo.create_oval(2, 2, 54, 54, fill=ACCENT, outline='')
        logo.create_text(28, 28, text='L', fill='white', font=('Segoe UI', 22, 'bold'))
        logo.pack()
        tk.Label(card, text='LanLink', bg=MAIN, fg='white',
                 font=('Segoe UI', 18, 'bold')).pack(pady=(10, 18))

        self.fields = {name: tk.StringVar(value=default) for name, default in
                       [('Username', ''), ('Password', ''), ('Host', '127.0.0.1'),
                        ('Port', '5000'), ('Certificate', '')]}
        self.label(card, 'USERNAME')
        username = dark_entry(card, self.fields['Username'])
        username.pack(fill='x', ipady=7, pady=(0, 12))
        self.label(card, 'PASSWORD')
        dark_entry(card, self.fields['Password'], show='•').pack(fill='x', ipady=7, pady=(0, 12))

        server_title = tk.Frame(card, bg=MAIN)
        server_title.pack(fill='x')
        tk.Label(server_title, text='SERVER', bg=MAIN, fg=MUTED, font=SMALL).pack(side='left')
        find = tk.Label(server_title, text='Scan LAN', bg=MAIN, fg='#00a8fc', font=SMALL,
                        cursor='hand2')
        find.pack(side='right')
        find.bind('<Button-1>', lambda _event: self.discover())
        server = tk.Frame(card, bg=MAIN)
        server.pack(fill='x', pady=(2, 10))
        dark_entry(server, self.fields['Host']).pack(side='left', fill='x', expand=True, ipady=7)
        tk.Label(server, text=':', bg=MAIN, fg=MUTED, font=BOLD).pack(side='left', padx=4)
        dark_entry(server, self.fields['Port'], width=6).pack(side='left', ipady=7)

        self.use_tls = tk.BooleanVar(value=False)
        tk.Checkbutton(card, text='Use TLS', variable=self.use_tls,
                       command=self.toggle_tls, bg=MAIN, fg=TEXT, font=FONT, selectcolor=FIELD,
                       activebackground=MAIN, activeforeground='white', highlightthickness=0,
                       borderwidth=0, cursor='hand2').pack(anchor='w')
        self.cert_row = tk.Frame(card, bg=MAIN)  # packed by toggle_tls
        tk.Label(self.cert_row, text='CERTIFICATE', bg=MAIN, fg=MUTED, font=SMALL).pack(anchor='w', pady=(0, 2))
        cert = tk.Frame(self.cert_row, bg=MAIN)
        cert.pack(fill='x')
        dark_entry(cert, self.fields['Certificate']).pack(side='left', fill='x', expand=True, ipady=7)
        flat_button(cert, 'Browse', self.choose_cert, HOVER).pack(side='left', padx=(6, 0))

        self.message = tk.Label(card, bg=MAIN, fg=DANGER, font=SMALL, wraplength=300, justify='left')
        self.message.pack(fill='x', pady=(8, 0))
        self.login_button = flat_button(card, 'Log in', self.connect)
        self.login_button.pack(fill='x', pady=(8, 0), ipady=3)
        root.bind('<Return>', lambda _event: self.connect())
        username.focus()
        # Centre the window on the screen.
        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
        y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
        root.geometry(f'+{x}+{y}')

    @staticmethod
    def label(parent: tk.Widget, text: str) -> None:
        """A small grey caption above a field."""
        tk.Label(parent, text=text, bg=MAIN, fg=MUTED, font=SMALL).pack(anchor='w', pady=(0, 2))

    def say(self, text: str, error: bool = True) -> None:
        """Show a line under the form: red for errors, green for good news."""
        self.message.configure(text=text, fg=DANGER if error else ONLINE)

    @staticmethod
    def explain(error: BaseException) -> str:
        """A short message for a login error instead of the raw exception text."""
        if isinstance(error, LanLinkError):
            return str(error)  # e.g. wrong password, user already online
        if isinstance(error, ConnectionRefusedError):
            return 'Connection refused.'
        if isinstance(error, TimeoutError):  # socket.timeout is TimeoutError on 3.10+
            return 'Connection timed out.'
        if isinstance(error, socket.gaierror):
            return 'Unknown host.'
        if isinstance(error, FileNotFoundError):
            return 'Certificate not found.'
        if isinstance(error, ssl.SSLError):
            return 'TLS handshake failed.'
        if isinstance(error, ValueError):
            return 'Plaintext only works on localhost. Use TLS.' if 'Plaintext' in str(error) else 'Invalid port.'
        return 'Connection failed.'

    def toggle_tls(self) -> None:
        """Show the certificate row only while TLS is ticked."""
        if self.use_tls.get():
            self.cert_row.pack(fill='x', pady=(6, 0), before=self.message)
        else:
            self.cert_row.pack_forget()

    def choose_cert(self) -> None:
        """Pick the server's certificate file (server.crt)."""
        path = filedialog.askopenfilename(title='Select server certificate')
        if path:
            self.fields['Certificate'].set(path)

    def discover(self) -> None:
        """UDP broadcast on the port typed above; fill the server fields with the first reply."""
        self.say('Scanning...', error=False)
        self.root.update_idletasks()  # draw the text before the 2 s search blocks the window
        try:
            servers = discover_servers(int(self.fields['Port'].get()))
        except ACTION_ERRORS as error:
            self.say('Scan failed.')
            return
        if not servers:
            self.say('No server found.')
            return
        server = servers[0]
        self.fields['Host'].set(server['ip'])
        self.fields['Port'].set(str(server['port']))
        if server['tls'] != self.use_tls.get():
            self.use_tls.set(server['tls'])
            self.toggle_tls()
        self.say(f'Found {server["name"]} at {server["ip"]}:{server["port"]}', error=False)

    def connect(self) -> None:
        """Same steps as the CLI's login_cli: connect, HELLO, AUTH, then start the session."""
        host = self.fields['Host'].get().strip()
        username = self.fields['Username'].get().strip()
        cafile = (self.fields['Certificate'].get().strip() or None) if self.use_tls.get() else None
        if not username:
            self.say('Username is required.')
            return
        self.say('')
        self.login_button.configure(text='Connecting...', state='disabled')
        self.root.update_idletasks()
        connection = None
        try:
            port = int(self.fields['Port'].get())
            connection = open_connection(host, port, 5.0, cafile)
            transport = SocketTransport(connection, direction=Direction.SERVER_TO_CLIENT)
            initiate_hello(transport)
            authenticate(transport, username, self.fields['Password'].get())
        except ACTION_ERRORS as error:
            if connection is not None:
                connection.close()
            self.login_button.configure(text='Log in', state='normal')
            self.say(self.explain(error))
            return
        status = f'{host}:{port}'
        for child in self.root.winfo_children():
            child.destroy()
        self.root.unbind('<Return>')
        self.root.resizable(True, True)
        ChatWindow(self.root, transport, username, status)


class ChatWindow:
    """Logged-in window: sidebar of conversations, message history, message box, transfers.

    Threads: PrivateChatClient's reader thread must not touch Tk widgets, so every
    callback only puts the item on self.events. The Tk thread drains that queue every
    POLL_MS (see poll) and updates the widgets.

    Conversations: EVERYONE for public chat, otherwise the other user's name. Each one
    keeps its own history, so switching in the sidebar redraws the message area.
    """

    def __init__(self, root: tk.Tk, transport: SocketTransport, username: str, status: str) -> None:
        """Build the widgets and start the session."""
        self.root = root
        self.username = username
        self.connected = True
        # Everything from the network: chat Messages, file event Messages,
        # TransferSnapshots (progress or final), or None when disconnected.
        self.events: queue.Queue[Message | TransferSnapshot | None] = queue.Queue()
        # history[conversation] = list of (sender or None for a notice, text, 'HH:MM').
        self.history: dict[str, list[tuple[str | None, str, str]]] = {EVERYONE: []}
        self.unread: dict[str, int] = {}
        self.online: list[str] = []
        self.current = EVERYONE
        self.sidebar_keys: list[str] = []  # conversation shown at each sidebar row
        self.transfer_peer: dict[str, str] = {}  # transfer_id -> conversation it belongs to
        self.my_offers: set[str] = set()  # transfer_ids we offered (we are the sender)
        self.hide_job: str | None = None  # pending after() call that hides the transfer bar
        root.title(f'LanLink - {username}')
        root.configure(bg=MAIN)
        root.geometry('900x560')
        root.minsize(640, 400)

        # Sidebar: channel and direct messages, then the user panel at the bottom.
        side = tk.Frame(root, bg=SIDEBAR, width=230)
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        tk.Label(side, text='LanLink', bg=SIDEBAR, fg='white', font=('Segoe UI', 14, 'bold'),
                 anchor='w', padx=14, pady=12).pack(fill='x')
        tk.Label(side, text='CHATS', bg=SIDEBAR, fg=MUTED, font=SMALL,
                 anchor='w', padx=14).pack(fill='x')
        self.sidebar = tk.Listbox(side, bg=SIDEBAR, fg=TEXT, font=FONT, relief='flat',
                                  highlightthickness=0, borderwidth=0, activestyle='none',
                                  selectbackground=HOVER, selectforeground='white',
                                  exportselection=False)
        self.sidebar.pack(fill='both', expand=True, padx=8, pady=4)
        self.sidebar.bind('<<ListboxSelect>>', lambda _event: self.open_selected())
        me = tk.Frame(side, bg=FIELD, padx=10, pady=8)
        me.pack(fill='x', side='bottom')
        self.me_dot = tk.Label(me, text='●', bg=FIELD, fg=ONLINE, font=FONT)
        self.me_dot.pack(side='left')
        names = tk.Frame(me, bg=FIELD)
        names.pack(side='left', padx=6)
        tk.Label(names, text=username, bg=FIELD, fg='white', font=BOLD, anchor='w').pack(fill='x')
        self.status = tk.Label(names, text=status, bg=FIELD, fg=MUTED, font=SMALL, anchor='w')
        self.status.pack(fill='x')

        # Main area: header, messages, transfer bar, message box.
        main = tk.Frame(root, bg=MAIN)
        main.pack(side='left', fill='both', expand=True)
        self.header = tk.Label(main, bg=MAIN, fg='white', font=('Segoe UI', 12, 'bold'),
                               anchor='w', padx=16, pady=12)
        self.header.pack(fill='x')
        tk.Frame(main, bg=FIELD, height=1).pack(fill='x')
        self.log = tk.Text(main, height=10, bg=MAIN, fg=TEXT, font=FONT, relief='flat', wrap='word',
                           padx=16, pady=10, highlightthickness=0, state='disabled', cursor='arrow')
        self.log.tag_configure('time', foreground=MUTED, font=SMALL)
        self.log.tag_configure('notice', foreground=MUTED, font=('Segoe UI', 10, 'italic'))
        for colour in NAME_COLOURS:
            self.log.tag_configure(colour, foreground=colour, font=BOLD)

        self.transfer_box = tk.Frame(main, bg=SIDEBAR, padx=12, pady=6)  # shown during a transfer
        self.progress_text = tk.Label(self.transfer_box, bg=SIDEBAR, fg=TEXT, font=SMALL, anchor='w')
        self.progress_text.pack(fill='x')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('Lan.Horizontal.TProgressbar', background=ACCENT, troughcolor=FIELD,
                        bordercolor=SIDEBAR, lightcolor=ACCENT, darkcolor=ACCENT)
        self.progress = ttk.Progressbar(self.transfer_box, maximum=1.0,
                                        style='Lan.Horizontal.TProgressbar')
        self.progress.pack(fill='x', pady=(4, 0))

        bar = self.bar = tk.Frame(main, bg=HOVER, padx=6, pady=6)
        bar.pack(fill='x', padx=16, pady=(0, 16), side='bottom')
        attach = tk.Button(bar, text='+', command=self.send_file, bg=HOVER, fg=TEXT,
                           font=('Segoe UI', 14, 'bold'), relief='flat', borderwidth=0,
                           activebackground=HOVER, activeforeground='white', cursor='hand2',
                           highlightthickness=0)
        attach.pack(side='left', padx=(4, 8))
        self.text = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self.text, bg=HOVER, fg=TEXT, font=FONT, relief='flat',
                         insertbackground=TEXT, highlightthickness=0)
        entry.pack(side='left', fill='x', expand=True, ipady=6)
        entry.bind('<Return>', lambda _event: self.send_chat())
        entry.focus()
        # Grey hint inside the empty message box; hidden as soon as something is typed.
        self.hint = tk.Label(bar, bg=HOVER, fg=MUTED, font=FONT)
        self.text.trace_add('write', lambda *_: self.place_hint())
        self.hint.bind('<Button-1>', lambda _event: entry.focus())
        # Packed last so it takes the space left between the header and the message box.
        self.log.pack(fill='both', expand=True)

        self.client = PrivateChatClient(transport, on_message=self.events.put,
                                        on_disconnect=lambda: self.events.put(None),
                                        transfer_events=self.events, on_progress=self.events.put)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        self.refresh_users()
        self.show(EVERYONE)
        root.after(POLL_MS, self.poll)

    # --- conversations -------------------------------------------------------

    def add(self, conversation: str, sender: str | None, text: str, stamp: str | None = None) -> None:
        """Store a line in a conversation; draw it if it is open, else count it as unread."""
        line = (sender, text, stamp or time.strftime('%H:%M'))
        self.history.setdefault(conversation, []).append(line)
        if conversation == self.current:
            previous = self.history[conversation][-2][0] if len(self.history[conversation]) > 1 else None
            self.draw(line, previous)
        else:
            self.unread[conversation] = self.unread.get(conversation, 0) + 1
            self.draw_sidebar()

    def draw(self, line: tuple[str | None, str, str], previous_sender: str | None) -> None:
        """Append one line to the message area; repeated senders skip the name row."""
        sender, text, stamp = line
        self.log.configure(state='normal')
        if sender is None:
            self.log.insert('end', f'{text}\n', 'notice')
        else:
            if sender != previous_sender:
                self.log.insert('end', '\n' + sender, name_colour(sender))
                self.log.insert('end', f'  {stamp}\n', 'time')
            self.log.insert('end', f'{text}\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def show(self, conversation: str) -> None:
        """Open a conversation: redraw its history and clear its unread count."""
        self.current = conversation
        self.unread.pop(conversation, None)
        self.header.configure(text='# everyone' if conversation == EVERYONE else f'@ {conversation}')
        self.place_hint()
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        previous = None
        for line in self.history.get(conversation, []):
            self.draw(line, previous)
            previous = line[0]
        self.draw_sidebar()

    def place_hint(self) -> None:
        """Show 'Message #everyone' (or '@user') in the message box while it is empty."""
        if self.text.get():
            self.hint.place_forget()
        else:
            target = '#everyone' if self.current == EVERYONE else f'@{self.current}'
            self.hint.configure(text=f'Message {target}')
            self.hint.place(x=46, rely=0.5, anchor='w')

    def draw_sidebar(self) -> None:
        """Redraw the conversation list: the channel, then every known user."""
        known = [name for name in self.history if name != EVERYONE]
        users = sorted(set(self.online) | set(known))
        self.sidebar_keys = [EVERYONE] + users
        self.sidebar.delete(0, 'end')
        for index, key in enumerate(self.sidebar_keys):
            label = '#  everyone' if key == EVERYONE else f'{"●" if key in self.online else "○"}  {key}'
            if self.unread.get(key):
                label += f'   ({self.unread[key]})'
            self.sidebar.insert('end', label)
            if key != EVERYONE and key not in self.online:
                self.sidebar.itemconfig(index, fg=MUTED)  # offline
            elif key != EVERYONE:
                self.sidebar.itemconfig(index, fg=ONLINE if not self.unread.get(key) else 'white')
        self.sidebar.selection_set(self.sidebar_keys.index(self.current))

    def open_selected(self) -> None:
        """Sidebar click: open the conversation under the selection."""
        picked = self.sidebar.curselection()
        if picked:
            self.show(self.sidebar_keys[picked[0]])

    # --- actions -------------------------------------------------------------

    def refresh_users(self) -> None:
        """Ask the server for USER_LIST and redraw the sidebar."""
        if not self.connected:
            return
        try:
            users = self.client.users()
        except ACTION_ERRORS:
            return  # A lost connection is reported by poll() through the None event.
        self.online = [user['username'] for user in users if user['username'] != self.username]
        self.draw_sidebar()
        self.root.after(USERS_REFRESH_MS, self.refresh_users)

    def send_chat(self) -> None:
        """Send the typed text to the open conversation: public or private."""
        text = self.text.get().strip()
        if not text or not self.connected:
            return
        try:
            if self.current == EVERYONE:
                self.client.send_public(text)
            else:
                self.client.send_private(self.current, text)
        except ACTION_ERRORS as error:
            messagebox.showerror('Message not sent', str(error))
            return
        self.text.set('')
        self.add(self.current, self.username, text)

    def send_file(self) -> None:
        """The + button: offer a file to the user of the open conversation."""
        recipient = self.current
        if recipient == EVERYONE or not self.connected:
            messagebox.showinfo('Send file', 'Select a user first.')
            return
        path = filedialog.askopenfilename(title=f'Send file to {recipient}')
        if not path:
            return
        try:
            transfer_id = self.client.offer_file(recipient, path)
        except ACTION_ERRORS as error:
            messagebox.showerror('File not sent', str(error))
            return
        self.transfer_peer[transfer_id] = recipient
        self.my_offers.add(transfer_id)
        self.add(recipient, None, f'You sent {Path(path).name}. Waiting for {recipient}...')

    def ask_offer(self, offer: Message) -> None:
        """Ask the user to accept or reject an incoming FILE_OFFER."""
        transfer_id, sender = offer.header['transfer_id'], offer.header['sender']
        name = offer.header['filename']
        try:
            if messagebox.askyesno('Incoming file', f'{sender} sent {name} ({human_size(offer.header["size"])}).\n'
                                                    'Accept?'):
                self.client.accept_file(transfer_id)
                self.add(sender, None, f'Receiving {name}...')
            else:
                self.client.reject_file(transfer_id)
                self.add(sender, None, f'You declined {name}.')
        except ACTION_ERRORS as error:
            messagebox.showerror('Incoming file', str(error))

    # --- network events ------------------------------------------------------

    def poll(self) -> None:
        """Tk thread: handle every queued network event, then check again in POLL_MS."""
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.connected = False
                self.me_dot.configure(fg=DANGER)
                self.status.configure(text='Disconnected', fg=DANGER)
                self.add(self.current, None, 'Disconnected.')
            elif isinstance(item, TransferSnapshot):
                self.handle_progress(item)
            elif item.type in {MessageType.CHAT_PRIVATE, MessageType.CHAT_PUBLIC}:
                sender = item.header['sender']
                conversation = EVERYONE if item.type == MessageType.CHAT_PUBLIC else sender
                stamp = time.strftime('%H:%M', time.localtime(item.header['timestamp_ms'] / 1000))
                self.add(conversation, sender, item.payload.decode('utf-8'), stamp)
            elif item.type == MessageType.FILE_OFFER:
                sender = item.header['sender']
                self.transfer_peer[item.header['transfer_id']] = sender
                self.add(sender, None, f'{sender} sent {item.header["filename"]} '
                                       f'({human_size(item.header["size"])})')
                # after_idle: the dialog runs outside this loop, so a dialog that
                # stays open does not stop other events from being handled.
                self.root.after_idle(self.ask_offer, item)
            else:  # FILE_ACCEPT, FILE_REJECT or a transfer ERROR
                transfer_id = item.header['transfer_id']
                peer = self.transfer_peer.get(transfer_id, self.current)
                if item.type == MessageType.FILE_ACCEPT:
                    if transfer_id in self.my_offers:  # else: the echo of our own accept
                        self.add(peer, None, f'{peer} accepted.')
                elif item.type == MessageType.FILE_REJECT:
                    self.add(peer, None, f'{peer} declined.')
                else:  # the server reports a failed transfer
                    # No final snapshot follows a REJECT or ERROR, so hide the bar here.
                    self.hide_progress()
                    self.add(peer, None, f'Transfer failed: {LanLinkError(item.header["code"])}')
        self.root.after(POLL_MS, self.poll)

    def handle_progress(self, snapshot: TransferSnapshot) -> None:
        """Show the transfer bar while a transfer runs; note the result when it ends."""
        if snapshot.status == TransferStatus.OFFERED:
            return  # nothing to show until the peer accepts
        sending = snapshot.direction == TransferDirection.SEND
        self.progress['value'] = snapshot.fraction
        self.progress_text.configure(
            text=f'{"Sending" if sending else "Receiving"} {snapshot.filename} · '
                 f'{human_size(snapshot.processed_bytes)} / {human_size(snapshot.total_bytes)}')
        if self.hide_job is not None:  # a new update cancels the pending hide
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        self.transfer_box.pack(fill='x', padx=16, pady=(0, 8), side='bottom', after=self.bar)
        if not snapshot.is_terminal:
            return
        self.hide_job = self.root.after(PROGRESS_HIDE_MS, self.hide_progress)
        if snapshot.status != TransferStatus.COMPLETED:
            reason = snapshot.error.message if snapshot.error else snapshot.status.value.lower()
            self.add(snapshot.peer, None, f'Transfer failed: {reason}')
        elif sending:
            self.add(snapshot.peer, None, f'{snapshot.filename} delivered.')
        else:
            self.add(snapshot.peer, None, f'Saved {snapshot.filename} to {self.client.download_dir}')

    def hide_progress(self) -> None:
        """Remove the transfer bar."""
        self.hide_job = None
        self.transfer_box.pack_forget()

    def quit(self) -> None:
        """Window closed: log out (or just close if already disconnected), then exit."""
        try:
            if self.connected:
                self.client.logout()
            else:
                self.client.close()
        except ACTION_ERRORS:
            pass
        self.root.destroy()


def main() -> None:
    """Open the login window."""
    root = tk.Tk()
    LoginWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
