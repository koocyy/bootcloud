# bootcloud

Source for **https://bootcloud.eu**, a FastAPI app served by uvicorn: a game
browser with self-hosted browser games, a live "online" counter and a chat.

## Layout

```
site/                          the app
  main.py                      FastAPI app (uvicorn runs main:app)
  index.html                   page served at /
  chat.html                    chat window served at /chat (opens as a popup)
  admin.html                   admin panel served at /admin (opens from the chat)
  assets/online.js             "N online" counter in the tab title, on every page
  assets/chat-popup.js         opens links marked data-chat-popup in the chat popup
  games/                       self-hosted games, served at /games/
  temp.html                    style reference: every HTML element in the design (not served)
deploy/                        how the server runs it
  bootcloud.service            systemd unit: the app, HTTPS on port 443
  bootcloud-redirect.service   systemd unit: port 80 -> https redirect
  http_redirect.py             the tiny redirect app
requirements.txt               Python packages the server runs
```

## Run it locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
cd site
../.venv/bin/uvicorn main:app --reload        # http://127.0.0.1:8000
```

Run uvicorn from inside `site/`: `main.py` opens `index.html` by relative path.
Locally the session key and other state go in `site/.state/`, which git ignores.
To try the admin panel locally, run `python main.py set-admin-password` in `site/`.

## Deploying a change

On the server: `git pull`, then restart the `bootcloud` service if Python changed.

- Pages and files (`index.html`, `chat.html`, `admin.html`, `assets/`, `games/`)
  are read from disk on every request, so they are live right after `git pull`.
- Changes to `main.py` (or any Python) need the service restarted.
- Changes to `deploy/bootcloud.service` need the unit copied into systemd and
  `systemctl daemon-reload` before the restart. The files in `deploy/` are
  reference copies of the live units.
- New Python package: add it to `requirements.txt` and install it into the
  server's virtualenv before restarting.

## Browser sessions, chat and limits

**Every browser gets an ID.** The first page load sets a `session` cookie: an
encrypted token (JWT as JWE) holding a random browser ID. It lasts a year and is
renewed on each visit, so a browser keeps its ID; the online count, the chat and
all limits work per browser, because many students share one school IP. A browser
profile is not a PC: another browser or a private window counts as someone new.

**Server state** lives in the app's state directory (systemd's `StateDirectory=`,
passed to the app as `$STATE_DIRECTORY`), never in the repo:

| File | What |
|---|---|
| `session.key` | Key for the session cookies, made on first start. Keep it: deleting it gives every browser a new ID. |
| `admin-password` | scrypt hash of the admin password. Delete it to turn the admin panel off. |
| `chat-bans.txt` | Banned browser tags, one per line; a `#` starts a note. Edits apply without a restart. |
| `blocked-words.txt` | Words the chat refuses, one per line. Edited from the admin panel. |

**Chat history** is the last 30 messages, kept in memory only: a restart clears
it. People who join see those 30, and the chat window never shows more.

**Admin panel:** type `/admin pass=<password>` in the chat. The command is never
posted; with the right password that browser is admin for 12 hours (or until it
logs out or the server restarts) and the admin panel opens in a popup (red, so
it can't be mistaken for the blue chat). Wrong guesses are limited to 5 per
browser and 20 per IP every 15 minutes. What it can do:

- **See:** people online and in the chat (now and peak), messages sent, uptime,
  who is admin; search the kept messages.
- **Messages:** delete one, clear all, send an announcement, pin a message to the
  top of everyone's chat.
- **People:** mute (5 min to 24 h: can read, can't write), kick (can come back
  by reloading), ban and unban, all by browser tag.
- **Whole chat:** pause it, slow mode (one message per 5–60 s per browser), and
  blocked words (whole words, any case). Admins are exempt from all three, and
  their messages carry an ADMIN badge that a name can't fake.
- **Admins:** log out, or log out every admin at once.

Bans and blocked words survive restarts; mutes, pause, slow mode and the pin are
in memory and reset on restart. Messages starting with `/` or containing `pass=`
are never posted, so a mistyped command can't leak the password.

The password is not in this repo (it is public). Set it with
`python main.py set-admin-password`, run from `site/` as the service's user with
`STATE_DIRECTORY` pointing at the state directory; only the hash is stored.

**Limits** are at the top of `main.py` (tabs and chat windows per browser,
per-IP backstops, message rate, history length). Two more are uvicorn flags and
one is systemd, in `deploy/bootcloud.service`: `--ws-max-size` (largest websocket
message), `--limit-concurrency` (open connections) and `MemoryMax` (the app is
restarted instead of starving the server).

## Things to know

- **Only routes defined in `main.py` exist.** Any other path is a 404 unless it
  falls under the `/assets` or `/games` folders, which are served as files.
- **API docs are off.** `/docs`, `/redoc` and `/openapi.json` are disabled in
  `main.py` so they aren't public. Remove the three arguments to `FastAPI()`
  when you want them locally.
- **This repo is public.** Never commit passwords, keys, `.env` files or server
  notes. Files named `*.local.md` are git-ignored for notes that stay private.
