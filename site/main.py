import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import time
import unicodedata
from collections import Counter, deque
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

# websocket close codes the pages understand
CLOSE_BAD_NAME = 4000
CLOSE_NO_SESSION = 4001
CLOSE_BANNED = 4003
CLOSE_TOO_MANY = 4029
CLOSE_POLICY = 1008


# ---------- files kept outside the repo ----------
# systemd's StateDirectory= sets STATE_DIRECTORY (/var/lib/bootcloud on the server).
# Locally it falls back to site/.state, which git ignores. This repo is public:
# never commit anything from that folder.
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", ".state").split(":")[0])
SESSION_KEY_FILE = STATE_DIR / "session.key"
CHAT_BANS_FILE = STATE_DIR / "chat-bans.txt"


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
    # uvicorn faces the internet directly (no proxy), so this is the real address
    return connection.client.host if connection.client else "unknown"


class RateWindow:
    """Allows at most `limit` hits per key within any `window` seconds."""

    def __init__(self, limit: int, window: float, max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.hits) > self.max_keys:
            self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - self.window}
        recent = self.hits.setdefault(key, deque())
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


async def admit(websocket: WebSocket, hub: Hub) -> tuple[str, str] | None:
    """Accepts the connection if it may join the hub. Returns (browser ID, IP) or None."""
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
    return browser_id, ip


async def join(hub: Hub, websocket: WebSocket, browser_id: str, ip: str, presence: Callable[[int], str]) -> None:
    if hub.add(websocket, browser_id, ip):
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
    await join(online_hub, websocket, *admitted, online_presence)
    try:
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
chat_last_message: dict[str, float] = {}  # browser ID -> when it last sent
chat_ip_messages = RateWindow(*CHAT_MESSAGES_PER_IP)
_bans_cache: tuple[float | None, frozenset[str]] = (None, frozenset())


def banned_tags() -> frozenset[str]:
    """Tags listed in chat-bans.txt, one per line (# starts a comment). Re-read when the file changes."""
    global _bans_cache
    try:
        modified = CHAT_BANS_FILE.stat().st_mtime
    except FileNotFoundError:
        return frozenset()
    if _bans_cache[0] != modified:
        lines = CHAT_BANS_FILE.read_text(encoding="utf-8").splitlines()
        tags = (line.split("#")[0].strip().lower() for line in lines)
        _bans_cache = (modified, frozenset(tag for tag in tags if tag))
    return _bans_cache[1]


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


def chat_notice(text: str) -> str:
    return json.dumps({"type": "notice", "text": text})


@app.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    admitted = await admit(websocket, chat_hub)
    if admitted is None:
        return
    browser_id, ip = admitted
    tag = public_tag(browser_id)
    if tag in banned_tags():
        await websocket.close(code=CLOSE_BANNED)
        return
    name = clean_text(websocket.query_params.get("name"), CHAT_NAME_LENGTH[1])
    if len(name) < CHAT_NAME_LENGTH[0]:
        await websocket.close(code=CLOSE_BAD_NAME)
        return

    await websocket.send_text(json.dumps({
        "type": "history",
        "you": tag,
        "limit": CHAT_HISTORY,  # the page shows at most this many, also for live messages
        "messages": list(chat_history),
    }))
    await join(chat_hub, websocket, browser_id, ip, chat_presence)
    try:
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
            now = time.monotonic()
            if now - chat_last_message.get(browser_id, 0) < CHAT_MIN_INTERVAL or not chat_ip_messages.allow(ip):
                await send_safely(chat_hub, websocket, chat_notice("Slow down a little. That message wasn't sent."))
                continue
            chat_last_message[browser_id] = now
            message = {
                "type": "message",
                "name": name,
                "tag": tag,
                "text": text,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            chat_history.append(message)
            await broadcast(chat_hub, json.dumps(message))
    finally:
        await leave(chat_hub, websocket, chat_presence)
        if browser_id not in chat_hub.browsers:
            chat_last_message.pop(browser_id, None)


# ---------- pages ----------
# the browser_session middleware above sets the session cookie on every page


@app.get("/chat")
async def serve_chat():
    return FileResponse("chat.html")


@app.get("/")
async def serve_main():
    return FileResponse("index.html")
