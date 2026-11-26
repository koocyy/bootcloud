import asyncio
import base64
import hashlib
import hmac
import ipaddress
import itertools
import json
import re
import mimetypes
import os
import secrets
import time
import unicodedata
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jwcrypto import jwk, jwt
from starlette.requests import HTTPConnection


# ---------- limits: tune these to the server ----------
# A whole school can share one IP, so people are told apart by browser (the
# session cookie). The per-IP limits are only a high backstop against scripts.
ONLINE_LIMITS = {"per_browser": 12, "per_ip": 1000, "total": 2000}  # open tabs
CHAT_LIMITS = {"per_browser": 3, "per_ip": 500, "total": 1000}  # open chat windows
NEW_BROWSERS_PER_IP = (120, 600)  # at most 120 new browser IDs per IP per 10 minutes
CHAT_MESSAGES_PER_IP = (30, 10)  # at most 30 messages per IP per 10 s, all browsers together
CHAT_MIN_INTERVAL = 0.75  # seconds between messages from one browser
CHAT_HISTORY = 30  # messages kept in memory and shown to people who join later
CHAT_MAX_LENGTH = 300  # characters per message, after cleaning
CHAT_MAX_FRAME = 2000  # raw frames longer than this are ignored without parsing
CHAT_NAME_LENGTH = (2, 16)
MAX_STACKED_MARKS = 3  # combining accents allowed on one letter (stops "zalgo" text)
SEND_TIMEOUT = 5  # seconds a client gets to accept a message before it is skipped
ADMIN_SESSION = 12 * 3600  # seconds a browser stays admin after "/admin pass=..." in the chat
ADMIN_TRIES_PER_BROWSER = (5, 900)  # password attempts per browser per 15 minutes
ADMIN_TRIES_PER_IP = (20, 900)  # and per IP (a whole school) per 15 minutes

# websocket close codes the pages understand
CLOSE_BAD_NAME = 4000
CLOSE_NO_SESSION = 4001
CLOSE_BANNED = 4003
CLOSE_KICKED = 4005
CLOSE_TOO_MANY = 4029
CLOSE_POLICY = 1008


# ---------- files kept outside the repo ----------
# systemd's StateDirectory= sets STATE_DIRECTORY on the server.
# Locally it falls back to site/.state, which git ignores. This repo is public:
# never commit anything from that folder.
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", ".state").split(":")[0])
SESSION_KEY_FILE = STATE_DIR / "session.key"
CHAT_BANS_FILE = STATE_DIR / "chat-bans.txt"
# scrypt hash of the admin password, written by `python main.py set-admin-password`.
# Without it the admin panel stays off. The password itself is never stored.
ADMIN_PASSWORD_FILE = STATE_DIR / "admin-password"
BLOCKED_WORDS_FILE = STATE_DIR / "blocked-words.txt"  # edited from the admin panel


def load_session_secret() -> bytes:
    """The key survives restarts, so browsers keep their ID instead of getting a new one."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        secret = SESSION_KEY_FILE.read_bytes()
    except FileNotFoundError:
        secret = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(SESSION_KEY_FILE, flags, 0o600), "wb") as key_file:
            key_file.write(secret)
    if len(secret) != 32:
        raise RuntimeError(f"{SESSION_KEY_FILE} is not a 32-byte key; delete it to make a new one")
    return secret


session_secret = load_session_secret()
session_key = jwk.JWK(kty="oct", k=base64.urlsafe_b64encode(session_secret).rstrip(b"=").decode())

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# browsers only compile WebAssembly served with this type; some systems don't know it
mimetypes.add_type("application/wasm", ".wasm")

app.mount("/assets", StaticFiles(directory="assets"), name="assets")
# self-hosted games; html=True serves games/<name>/index.html for /games/<name>/
app.mount("/games", StaticFiles(directory="games", html=True), name="games")


def client_ip(connection: HTTPConnection) -> str:
    """The key per-IP limits use. uvicorn faces the internet directly (no proxy), so
    the address is real. IPv6 is grouped by /64, because one machine or network
    usually holds a whole /64 and could otherwise rotate through endless addresses."""
    host = connection.client.host if connection.client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6 and address.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(getattr(address, "ipv4_mapped", None) or address)


class RateWindow:
    """Allows at most `limit` hits per key within any `window` seconds.
    Tracks at most `max_keys` keys; past that it forgets the least recently seen one,
    so every call stays O(1) however many addresses show up."""

    def __init__(self, limit: int, window: float, max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        recent = self.hits.get(key)
        if recent is None:
            recent = self.hits[key] = deque()
            if len(self.hits) > self.max_keys:
                self.hits.popitem(last=False)
        else:
            self.hits.move_to_end(key)
        while recent and recent[0] <= now - self.window:
            recent.popleft()
        if len(recent) >= self.limit:
            return False
        recent.append(now)
        return True


# ---------- browser sessions ----------
# Every browser gets a random ID in an encrypted, signed token (a JWT encrypted
# as JWE, so the browser can neither read nor forge it). The ID stays the same
# across visits; each page load renews the cookie for another year.
SESSION_COOKIE = "session"
SESSION_LIFETIME = 365 * 24 * 3600
SESSION_ALGS = ["dir", "A256GCM"]

new_browsers = RateWindow(*NEW_BROWSERS_PER_IP)


def make_session_token(browser_id: str) -> str:
    now = int(time.time())
    token = jwt.JWT(
        header={"alg": "dir", "enc": "A256GCM"},
        claims={"bid": browser_id, "iat": now, "exp": now + SESSION_LIFETIME},
    )
    token.make_encrypted_token(session_key)
    return token.serialize()


def read_session(token: str | None) -> str | None:
    """Returns the browser ID from a valid, unexpired token, otherwise None."""
    if not token or len(token) > 1000:
        return None
    try:
        claims = json.loads(jwt.JWT(jwt=token, key=session_key, expected_type="JWE", algs=SESSION_ALGS).claims)
    except Exception:
        return None
    # jwcrypto does not check "exp" on encrypted tokens, so check it here
    expires, browser_id = claims.get("exp"), claims.get("bid")
    if not isinstance(expires, int) or expires < time.time():
        return None
    if not isinstance(browser_id, str) or not 16 <= len(browser_id) <= 64:
        return None
    return browser_id


def public_tag(browser_id: str) -> str:
    """Short public label for a browser: shown next to chat names and used for bans.
    Derived with the secret, so it can't be turned back into the browser ID."""
    return hmac.new(session_secret, b"tag:" + browser_id.encode(), hashlib.sha256).hexdigest()[:6]


@app.middleware("http")
async def browser_session(request: Request, call_next):
    response = await call_next(request)
    if request.method != "GET" or not response.headers.get("content-type", "").startswith("text/html"):
        return response
    browser_id = read_session(request.cookies.get(SESSION_COOKIE))
    if browser_id is None:
        if not new_browsers.allow(client_ip(request)):
            return response  # page still loads; live features stay off for this browser
        browser_id = secrets.token_urlsafe(16)
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(browser_id),
        max_age=SESSION_LIFETIME,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


# ---------- live channels ----------
# State lives in memory, which works because uvicorn runs this app as a single
# process. A restart clears the chat history.


@dataclass
class Hub:
    """One live channel. Counts people by browser and enforces connection caps."""

    per_browser: int
    per_ip: int
    total: int
    clients: dict[WebSocket, tuple[str, str]] = field(default_factory=dict)
    browsers: Counter = field(default_factory=Counter)
    ips: Counter = field(default_factory=Counter)
    stalled: set[WebSocket] = field(default_factory=set)
    peak: int = 0  # most people at once since the server started (admin stats)

    @property
    def people(self) -> int:
        return len(self.browsers)

    def is_full_for(self, browser_id: str, ip: str) -> bool:
        return (
            len(self.clients) >= self.total
            or self.browsers[browser_id] >= self.per_browser
            or self.ips[ip] >= self.per_ip
        )

    def add(self, websocket: WebSocket, browser_id: str, ip: str) -> bool:
        """Returns True when this is the browser's first connection (the count changed)."""
        self.clients[websocket] = (browser_id, ip)
        self.browsers[browser_id] += 1
        self.ips[ip] += 1
        self.peak = max(self.peak, self.people)
        return self.browsers[browser_id] == 1

    def remove(self, websocket: WebSocket) -> bool:
        """Returns True when the browser's last connection left (the count changed)."""
        self.stalled.discard(websocket)
        browser_id, ip = self.clients.pop(websocket)
        for counter, key in ((self.browsers, browser_id), (self.ips, ip)):
            counter[key] -= 1
            if counter[key] <= 0:
                del counter[key]
        return browser_id not in self.browsers


async def send_safely(hub: Hub, websocket: WebSocket, text: str) -> None:
    try:
        await asyncio.wait_for(websocket.send_text(text), timeout=SEND_TIMEOUT)
    except Exception:
        # skip it from now on; uvicorn's ping closes it for good within ~40 s
        hub.stalled.add(websocket)


async def broadcast(hub: Hub, text: str) -> None:
    targets = [ws for ws in hub.clients if ws not in hub.stalled]
    await asyncio.gather(*(send_safely(hub, ws, text) for ws in targets))


def same_origin(websocket: WebSocket) -> bool:
    """Only our own pages may connect: blocks other sites from using visitors' browsers."""
    origin, host = websocket.headers.get("origin"), websocket.headers.get("host")
    return bool(origin and host) and urlsplit(origin).netloc.lower() == host.lower()


async def admit(websocket: WebSocket, hub: Hub) -> tuple[str, str, bool] | None:
    """Accepts the connection and counts it in the hub if the caps allow it.
    Returns (browser ID, IP, is the browser's first connection) or None.
    The caller must hub.remove() or leave() the connection once it is admitted."""
    if not same_origin(websocket):
        await websocket.close(code=CLOSE_POLICY)  # before accept: the handshake is refused
        return None
    await websocket.accept()
    browser_id = read_session(websocket.cookies.get(SESSION_COOKIE))
    if browser_id is None:
        await websocket.close(code=CLOSE_NO_SESSION)
        return None
    ip = client_ip(websocket)
    if hub.is_full_for(browser_id, ip):
        await websocket.close(code=CLOSE_TOO_MANY)
        return None
    # count it in the same step as the check, with no await in between, so many
    # connections opened at once can't all pass the check before any is counted
    first = hub.add(websocket, browser_id, ip)
    return browser_id, ip, first


async def announce(hub: Hub, websocket: WebSocket, first: bool, presence: Callable[[int], str]) -> None:
    if first:
        await broadcast(hub, presence(hub.people))
    else:
        # another tab of the same browser: the count is unchanged, just tell this one
        await send_safely(hub, websocket, presence(hub.people))


async def leave(hub: Hub, websocket: WebSocket, presence: Callable[[int], str]) -> None:
    if hub.remove(websocket):
        await broadcast(hub, presence(hub.people))


async def receive_text(websocket: WebSocket) -> str | None:
    """Next text frame; None when the client has gone. Binary frames come back as ""."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    return message.get("text") or ""


# ---------- online counter ----------
# every open tab of a page that loads assets/online.js holds one websocket here
online_hub = Hub(**ONLINE_LIMITS)


def online_presence(people: int) -> str:
    return str(people)


@app.websocket("/ws/online")
async def online_counter(websocket: WebSocket):
    admitted = await admit(websocket, online_hub)
    if admitted is None:
        return
    try:
        await announce(online_hub, websocket, admitted[2], online_presence)
        # the page never sends anything, so any message means a misbehaving client
        if await receive_text(websocket) is not None:
            await websocket.close(code=CLOSE_POLICY)
    finally:
        await leave(online_hub, websocket, online_presence)


# ---------- chat ----------
# No accounts: people pick a name, and the tag derived from their browser ID
# (shown as #a1b2c3) tells apart two people using the same name.
chat_hub = Hub(**CHAT_LIMITS)
chat_history: deque[dict] = deque(maxlen=CHAT_HISTORY)
chat_message_ids = itertools.count(1)  # lets the admin panel delete one message
chat_names: dict[WebSocket, str] = {}  # name each chat window joined with
chat_last_message: dict[str, float] = {}  # browser ID -> when it last sent
chat_ip_messages = RateWindow(*CHAT_MESSAGES_PER_IP)
TAG_PATTERN = re.compile(r"[0-9a-f]{6}")
_bans_cache: tuple[float | None, dict[str, str]] = (None, {})


def read_bans() -> dict[str, str]:
    """chat-bans.txt as {tag: note}: one tag per line, anything after # is the note.
    Re-read when the file changes, so edits by hand apply without a restart."""
    global _bans_cache
    try:
        modified = CHAT_BANS_FILE.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _bans_cache[0] != modified:
        bans = {}
        for line in CHAT_BANS_FILE.read_text(encoding="utf-8").splitlines():
            tag, _, note = line.partition("#")
            if tag.strip():
                bans[tag.strip().lower()] = note.strip()
        _bans_cache = (modified, bans)
    return _bans_cache[1]


def banned_tags() -> frozenset[str]:
    return frozenset(read_bans())


def write_bans(bans: dict[str, str]) -> None:
    global _bans_cache
    lines = [f"{tag}  # {note}" if note else tag for tag, note in bans.items()]
    CHAT_BANS_FILE.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    _bans_cache = (None, {})  # file times can be coarse: force a re-read


# moderation set from the admin panel; only the blocked words survive a restart
SLOW_MODE_CHOICES = (0, 5, 10, 30, 60)  # seconds between messages per browser; 0 = normal
MUTE_CHOICES = (5, 15, 60, 1440)  # minutes
chat_settings = {"paused": False, "slow": 0, "pinned": ""}
chat_mutes: dict[str, float] = {}  # tag -> when the mute ends (time.time())
chat_stats = {"started": time.time(), "messages": 0}
_words_cache: tuple[float | None, list[str], re.Pattern | None] = (None, [], None)


def blocked_words() -> tuple[list[str], re.Pattern | None]:
    """blocked-words.txt: one word or phrase per line. Matched as whole words,
    ignoring case, so blocking "ass" doesn't also block "class"."""
    global _words_cache
    try:
        modified = BLOCKED_WORDS_FILE.stat().st_mtime
    except FileNotFoundError:
        return [], None
    if _words_cache[0] != modified:
        words = [w.strip().casefold() for w in BLOCKED_WORDS_FILE.read_text(encoding="utf-8").splitlines() if w.strip()]
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)") if words else None
        _words_cache = (modified, words, pattern)
    return _words_cache[1], _words_cache[2]


def write_blocked_words(words: list[str]) -> None:
    global _words_cache
    BLOCKED_WORDS_FILE.write_text("".join(w + "\n" for w in words), encoding="utf-8")
    _words_cache = (None, [], None)


def mute_left(tag: str) -> int:
    """Minutes of mute left for a browser tag, 0 when not muted."""
    until = chat_mutes.get(tag)
    if until is None:
        return 0
    if until <= time.time():
        del chat_mutes[tag]
        return 0
    return max(1, round((until - time.time()) / 60))


def public_settings() -> dict:
    return {"type": "settings", **chat_settings}


def chat_refusal(browser_id: str, tag: str, ip: str, text: str, admin: bool) -> str | None:
    """Why this message can't be posted right now, or None to post it. Admins skip
    pause, slow mode and the word filter, but not the server-wide IP limit."""
    if chat_settings["paused"] and not admin:
        return "The chat is paused by an admin. That message wasn't sent."
    if minutes := mute_left(tag):
        return f"You're muted for {minutes} more minute{'s' if minutes != 1 else ''}. That message wasn't sent."
    _, pattern = blocked_words()
    if pattern and not admin and pattern.search(text.casefold()):
        return "That message has a word that isn't allowed here. It wasn't sent."
    interval = CHAT_MIN_INTERVAL if admin else max(CHAT_MIN_INTERVAL, chat_settings["slow"])
    if time.monotonic() - chat_last_message.get(browser_id, 0) < interval:
        if interval > CHAT_MIN_INTERVAL:
            return f"Slow mode is on: one message every {interval:g} seconds. That message wasn't sent."
        return "Slow down a little. That message wasn't sent."
    if not chat_ip_messages.allow(ip):
        return "Slow down a little. That message wasn't sent."
    return None


def clean_text(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    # whitespace (including newlines) becomes single spaces, control chars go,
    # and stacks of combining accents are cut down so text can't spill over others
    single_line = " ".join(unicodedata.normalize("NFC", value).split())
    kept, stacked = [], 0
    for ch in single_line:
        if unicodedata.category(ch).startswith("M"):
            stacked += 1
            if stacked > MAX_STACKED_MARKS:
                continue
        else:
            stacked = 0
        if ch.isprintable():
            kept.append(ch)
    return "".join(kept)[:max_length].strip()


def chat_presence(people: int) -> str:
    return json.dumps({"type": "presence", "count": people})


def chat_notice(text: str, refused: bool = False) -> str:
    # refused: the notice answers a message that wasn't posted, so the page puts it back in the box
    return json.dumps({"type": "notice", "text": text, "refused": refused})


@app.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    admitted = await admit(websocket, chat_hub)
    if admitted is None:
        return
    browser_id, ip, first = admitted
    tag = public_tag(browser_id)
    name = clean_text(websocket.query_params.get("name"), CHAT_NAME_LENGTH[1])
    refusal = CLOSE_BANNED if tag in banned_tags() else CLOSE_BAD_NAME if len(name) < CHAT_NAME_LENGTH[0] else None
    if refusal is not None:
        chat_hub.remove(websocket)  # never announced, so no presence update either
        await websocket.close(code=refusal)
        return

    try:
        # same timeout as every other send: a client that doesn't read can't hang here
        try:
            await asyncio.wait_for(websocket.send_text(json.dumps({
                "type": "history",
                "you": tag,
                "limit": CHAT_HISTORY,  # the page shows at most this many, also for live messages
                "messages": list(chat_history),
                "settings": public_settings(),  # paused, slow mode, pinned message
            })), timeout=SEND_TIMEOUT)
        except Exception:
            return  # gone or not reading; the finally below frees its slot
        chat_names[websocket] = name
        await announce(chat_hub, websocket, first, chat_presence)
        while (raw := await receive_text(websocket)) is not None:
            if len(raw) > CHAT_MAX_FRAME:
                continue
            try:
                text = clean_text(json.loads(raw).get("text"), CHAT_MAX_LENGTH)
            except (ValueError, AttributeError):
                continue
            if not text:
                continue
            if tag in banned_tags():
                await websocket.close(code=CLOSE_BANNED)
                break
            if ADMIN_COMMAND.match(text):
                # a command, never a chat message: the password must not reach anyone
                await send_safely(chat_hub, websocket, json.dumps(await admin_login(browser_id, ip, text)))
                continue
            if text.startswith("/"):
                # a mistyped command (like "/admn pass=...") must not get posted either
                await send_safely(chat_hub, websocket, chat_notice(
                    "Unknown command. Messages starting with / aren't posted.", refused=True))
                continue
            if PASSWORD_LIKE.search(text):
                await send_safely(chat_hub, websocket, chat_notice(
                    "Messages with pass= aren't posted, so a password can't leak by accident.", refused=True))
                continue
            admin = is_admin(browser_id)
            if refusal := chat_refusal(browser_id, tag, ip, text, admin):
                await send_safely(chat_hub, websocket, chat_notice(refusal, refused=True))
                continue
            chat_last_message[browser_id] = time.monotonic()
            message = {
                "type": "message",
                "id": next(chat_message_ids),
                "name": name,
                "tag": tag,
                "admin": admin,  # set by the server, so a name can't fake the badge
                "text": text,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            chat_history.append(message)
            chat_stats["messages"] += 1
            await broadcast(chat_hub, json.dumps(message))
    finally:
        chat_names.pop(websocket, None)
        await leave(chat_hub, websocket, chat_presence)
        if browser_id not in chat_hub.browsers:
            chat_last_message.pop(browser_id, None)


# ---------- admin ----------
# Typing "/admin pass=..." in the chat makes that browser admin for ADMIN_SESSION
# seconds; the chat page then opens /admin, which talks to /ws/admin.
# Anything starting with "/admin" is the command. No word-boundary rule: \b means
# different things in Python and in the page's JavaScript (é is a letter to one and
# not the other), and the two must agree, or a command could get posted as a message.
ADMIN_COMMAND = re.compile(r"/admin", re.IGNORECASE)
ADMIN_PASSWORD_ARG = re.compile(r"pass(?:word)?=(\S+)", re.IGNORECASE)
# last guard against a mistyped command: never post anything that looks like it has a password
PASSWORD_LIKE = re.compile(r"pass(?:word)?\s*=", re.IGNORECASE)
SCRYPT = {"n": 2**14, "r": 8, "p": 1}

admin_sessions: dict[str, float] = {}  # browser ID -> when its admin rights run out
admin_tries_browser = RateWindow(*ADMIN_TRIES_PER_BROWSER)
admin_tries_ip = RateWindow(*ADMIN_TRIES_PER_IP)


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_admin_password(password: str) -> bool | None:
    """True or False, or None when no admin password is set on this server."""
    try:
        _, salt, expected = ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").strip().split("$")
    except (FileNotFoundError, ValueError):
        return None
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT)
    return hmac.compare_digest(digest.hex(), expected)


def is_admin(browser_id: str) -> bool:
    expires = admin_sessions.get(browser_id)
    if expires is not None and expires > time.monotonic():
        return True
    admin_sessions.pop(browser_id, None)
    return False


async def admin_login(browser_id: str, ip: str, command: str) -> dict:
    """Handles "/admin pass=..." from the chat. Returns the reply for that window only."""
    if not (admin_tries_browser.allow(browser_id) and admin_tries_ip.allow(ip)):
        return {"type": "admin", "ok": False, "text": "Too many tries. Wait 15 minutes."}
    password = ADMIN_PASSWORD_ARG.search(command)
    # scrypt is slow on purpose (~50 ms); run it off the event loop so chat doesn't stall
    result = await asyncio.to_thread(check_admin_password, password.group(1)) if password else False
    if result is None:
        return {"type": "admin", "ok": False, "text": "The admin panel isn't set up on this server."}
    if not result:
        return {"type": "admin", "ok": False, "text": "Wrong admin password."}
    admin_sessions[browser_id] = time.monotonic() + ADMIN_SESSION
    return {"type": "admin", "ok": True}


ADMIN_MAX_FRAME = 4000  # admin messages can carry the blocked-words list
BLOCKED_WORDS_LIMIT = (100, 30)  # at most 100 words, each up to 30 characters
admin_sockets: set[WebSocket] = set()  # open admin panels, for "log out all admins"


def admin_state() -> dict:
    chatters: dict[str, dict] = {}
    for websocket, (browser_id, _) in chat_hub.clients.items():
        person = chatters.setdefault(public_tag(browser_id), {"names": set(), "windows": 0, "bid": browser_id})
        person["windows"] += 1
        if websocket in chat_names:
            person["names"].add(chat_names[websocket])
    words, _ = blocked_words()
    return {
        "type": "state",
        "online": {"people": online_hub.people, "tabs": len(online_hub.clients), "peak": online_hub.peak},
        "chat": {"people": chat_hub.people, "windows": len(chat_hub.clients), "limit": CHAT_HISTORY, "peak": chat_hub.peak},
        "stats": {
            "uptime": int(time.time() - chat_stats["started"]),
            "messages": chat_stats["messages"],
            "admins": sum(1 for bid in list(admin_sessions) if is_admin(bid)),
        },
        "settings": dict(chat_settings),
        "slow_choices": SLOW_MODE_CHOICES,
        "mute_choices": MUTE_CHOICES,
        "chatters": [
            {
                "tag": tag,
                "names": sorted(p["names"]),
                "windows": p["windows"],
                "muted": mute_left(tag),
                "admin": is_admin(p["bid"]),
            }
            for tag, p in sorted(chatters.items())
        ],
        "messages": list(chat_history),
        "mutes": [{"tag": tag, "minutes": m} for tag in list(chat_mutes) if (m := mute_left(tag))],
        "bans": [{"tag": tag, "note": note} for tag, note in read_bans().items()],
        "words": words,
    }


async def close_quietly(websocket: WebSocket, code: int) -> None:
    try:
        await asyncio.wait_for(websocket.close(code=code), timeout=SEND_TIMEOUT)
    except Exception:
        pass


def chat_windows_of(tag: str) -> list[WebSocket]:
    return [ws for ws, (bid, _) in chat_hub.clients.items() if public_tag(bid) == tag]


async def tell_everyone_settings(notice: str | None = None) -> None:
    await broadcast(chat_hub, json.dumps(public_settings()))
    if notice:
        await broadcast(chat_hub, chat_notice(notice))


async def admin_action(data: dict) -> str | None:
    """Runs one action from the admin panel. Returns an error to show, or None."""
    action = data.get("action")
    tag = str(data.get("tag", "")).strip().lower().lstrip("#")
    if action in {"ban", "mute", "unmute", "kick"} and not TAG_PATTERN.fullmatch(tag):
        return "A tag is 6 characters, like a1b2c3."

    if action == "delete":
        before = len(chat_history)
        kept = [m for m in chat_history if m.get("id") != data.get("id")]
        if len(kept) == before:
            return "That message is already gone."
        chat_history.clear()
        chat_history.extend(kept)
        await broadcast(chat_hub, json.dumps({"type": "delete", "id": data.get("id")}))
    elif action == "clear":
        chat_history.clear()
        await broadcast(chat_hub, json.dumps({"type": "clear"}))
    elif action == "announce":
        text = clean_text(data.get("text"), CHAT_MAX_LENGTH)
        if not text:
            return "Write something to announce."
        await broadcast(chat_hub, chat_notice(f"Admin: {text}"))
    elif action == "ban":
        bans = dict(read_bans())
        # "#" starts the note in chat-bans.txt, so it can't appear inside one
        bans[tag] = clean_text(str(data.get("note") or "").replace("#", " "), 100)
        write_bans(bans)
        # close that browser's chat windows now; banned browsers can't rejoin
        await asyncio.gather(*(close_quietly(ws, CLOSE_BANNED) for ws in chat_windows_of(tag)))
    elif action == "unban":
        bans = dict(read_bans())
        if bans.pop(tag, None) is None:
            return "That tag isn't banned."
        write_bans(bans)
    elif action == "kick":
        windows = chat_windows_of(tag)
        if not windows:
            return "That browser isn't in the chat."
        # unlike a ban, they can come back by reloading the chat window
        await asyncio.gather(*(close_quietly(ws, CLOSE_KICKED) for ws in windows))
    elif action == "mute":
        minutes = data.get("minutes")
        if minutes not in MUTE_CHOICES:
            return "Pick how long to mute for."
        chat_mutes[tag] = time.time() + minutes * 60
        length = f"{minutes // 60} hour{'s' if minutes >= 120 else ''}" if minutes >= 60 else f"{minutes} minutes"
        for ws in chat_windows_of(tag):
            await send_safely(chat_hub, ws, chat_notice(f"An admin muted you for {length}. You can still read the chat."))
    elif action == "unmute":
        if chat_mutes.pop(tag, None) is None:
            return "That browser isn't muted."
        for ws in chat_windows_of(tag):
            await send_safely(chat_hub, ws, chat_notice("You can write in the chat again."))
    elif action == "pause":
        paused = bool(data.get("paused"))
        if paused != chat_settings["paused"]:
            chat_settings["paused"] = paused
            await tell_everyone_settings("An admin paused the chat." if paused else "The chat is open again.")
    elif action == "slow":
        seconds = data.get("seconds")
        if seconds not in SLOW_MODE_CHOICES:
            return "Pick one of the slow mode options."
        if seconds != chat_settings["slow"]:
            chat_settings["slow"] = seconds
            await tell_everyone_settings(
                f"Slow mode is on: one message every {seconds} seconds." if seconds else "Slow mode is off.")
    elif action == "pin":
        chat_settings["pinned"] = clean_text(data.get("text"), 200)
        await tell_everyone_settings()
    elif action == "words":
        raw_words = data.get("words")
        if not isinstance(raw_words, list):
            return "Send the words as a list."
        cleaned = (clean_text(x, BLOCKED_WORDS_LIMIT[1]).casefold() for x in raw_words)
        words = list(dict.fromkeys(w for w in cleaned if w))  # drop repeats, keep order
        if len(words) > BLOCKED_WORDS_LIMIT[0]:
            return f"At most {BLOCKED_WORDS_LIMIT[0]} blocked words."
        write_blocked_words(words)
    elif action == "logout_all":
        admin_sessions.clear()
        await asyncio.gather(*(close_quietly(ws, CLOSE_BANNED) for ws in list(admin_sockets)))
    elif action != "state":
        return "Unknown action."
    return None


@app.websocket("/ws/admin")
async def admin(websocket: WebSocket):
    if not same_origin(websocket):
        await websocket.close(code=CLOSE_POLICY)
        return
    await websocket.accept()
    browser_id = read_session(websocket.cookies.get(SESSION_COOKIE))
    if browser_id is None or not is_admin(browser_id):
        await websocket.close(code=CLOSE_BANNED)  # 4003: not allowed
        return
    admin_sockets.add(websocket)
    try:
        while (raw := await receive_text(websocket)) is not None:
            # re-checked on every action: logging out or running out of time applies at once
            if not is_admin(browser_id):
                await close_quietly(websocket, CLOSE_BANNED)
                return
            try:
                data = json.loads(raw) if len(raw) <= ADMIN_MAX_FRAME else {}
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("action") == "logout":
                admin_sessions.pop(browser_id, None)
                await close_quietly(websocket, CLOSE_BANNED)
                return
            error = await admin_action(data)
            if not is_admin(browser_id):  # "log out all admins" includes this one
                return
            reply = admin_state()
            if error:
                reply["error"] = error
            try:
                await asyncio.wait_for(websocket.send_text(json.dumps(reply)), timeout=SEND_TIMEOUT)
            except Exception:
                return
    finally:
        admin_sockets.discard(websocket)


# ---------- pages ----------
# the browser_session middleware above sets the session cookie on every page


@app.get("/chat")
async def serve_chat():
    return FileResponse("chat.html")


@app.get("/admin")
async def serve_admin():
    # the page holds no data; everything comes over /ws/admin, which checks admin rights
    return FileResponse("admin.html")


@app.get("/")
async def serve_main():
    return FileResponse("index.html")


if __name__ == "__main__":
    # Sets the admin password: python main.py set-admin-password
    # Run it from site/ as the user the service runs as, with STATE_DIRECTORY set
    # to the service's state directory, so the app can read the file it writes.
    import getpass
    import sys

    if sys.argv[1:] != ["set-admin-password"]:
        sys.exit("usage: python main.py set-admin-password")
    new_password = unicodedata.normalize("NFC", getpass.getpass("New admin password: "))
    if not new_password or any(ch.isspace() for ch in new_password):
        sys.exit("Use a password without spaces. Nothing changed.")
    if new_password != unicodedata.normalize("NFC", getpass.getpass("Repeat it: ")):
        sys.exit("The two didn't match. Nothing changed.")
    if len(new_password) < 12:
        print("Note: short passwords are easy to guess. 12+ characters is safer.")
    ADMIN_PASSWORD_FILE.write_text(hash_admin_password(new_password) + "\n", encoding="utf-8")
    os.chmod(ADMIN_PASSWORD_FILE, 0o600)
    print(f"Saved to {ADMIN_PASSWORD_FILE}. In the chat, type: /admin pass=<your password>")
