# bootcloud

Source for **https://bootcloud.eu**, a FastAPI app served by uvicorn on our
server (`jarvis`). There is no nginx or other web server in front of it.

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
Locally the session key is created in `site/.state/`, which git ignores.

## How it runs on the server

| What | Where |
|---|---|
| This repo | `/srv/bootcloud` (shortcut: `~/bootcloud`) |
| Python virtualenv | `/srv/bootcloud-venv` |
| App service | `bootcloud.service`, uvicorn on port 443 with the TLS certificate |
| Redirect service | `bootcloud-redirect.service`, port 80, sends everything to `https://bootcloud.eu` |
| Certificate | Let's Encrypt, in `/etc/letsencrypt/live/bootcloud.eu/` |
| Session key, admin password hash, bans, blocked words | `/var/lib/bootcloud/` (created by systemd, owned by `www-data`) |

Both services run as the unprivileged `www-data` user, start on boot and
restart themselves if they crash.

## Deploying a change

```bash
cd ~/bootcloud
git pull
sudo systemctl restart bootcloud     # only needed when Python code changed
```

- Pages and files (`index.html`, `chat.html`, `assets/`, `games/`) are read from
  disk on every request, so they are live right after `git pull`.
- Changes to `main.py` (or any Python) need the restart.
- Changes to `deploy/bootcloud.service` need it copied into place first:
  `sudo cp deploy/bootcloud.service /etc/systemd/system/ && sudo systemctl daemon-reload`,
  then the restart.
- New Python package: add it to `requirements.txt`, then on the server run
  `/srv/bootcloud-venv/bin/pip install -r requirements.txt` and restart.

Useful commands:

```bash
systemctl status bootcloud            # is it running?
sudo journalctl -u bootcloud -f       # live log, including Python errors
sudo systemctl restart bootcloud
```

## Browser sessions, chat and limits

**Every browser gets an ID.** The first page load sets a `session` cookie: an
encrypted token (JWT as JWE) holding a random browser ID. It lasts a year and is
renewed on each visit, so a browser keeps its ID; the online count, the chat and
all limits work per browser, because many students share one school IP. A browser
profile is not a PC: another browser or a private window counts as someone new.

**The key** is `/var/lib/bootcloud/session.key`, made on first start. Keep it:
deleting it gives every browser a new ID. Never copy it into the repo.

**Chat history** is the last 30 messages, kept in memory only: a restart clears
it. People who join see those 30, and the chat window never shows more.

**Banning someone from the chat:** every chat name shows a tag like `#a1b2c3`.
Put it on its own line in `/var/lib/bootcloud/chat-bans.txt` (a `#` after it
starts a comment). It applies at once, no restart needed:

```bash
echo 'a1b2c3  # spam, 2026-11-26' | sudo -u www-data tee -a /var/lib/bootcloud/chat-bans.txt
```

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

Bans and blocked words are files in `/var/lib/bootcloud/` (`chat-bans.txt`,
`blocked-words.txt`) and survive restarts; mutes, pause, slow mode and the pin
are in memory and reset on restart.

The password is not in this repo (it is public). Set it on the server, from
`site/`; only a scrypt hash is stored, in `/var/lib/bootcloud/admin-password`:

```bash
cd /srv/bootcloud/site
sudo -u www-data env STATE_DIRECTORY=/var/lib/bootcloud \
    /srv/bootcloud-venv/bin/python main.py set-admin-password
```

Run it again to change the password; delete that file to turn the panel off.

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
- **The files in `deploy/` are reference copies.** The live units are in
  `/etc/systemd/system/`. After changing one, an admin copies it there and runs
  `sudo systemctl daemon-reload && sudo systemctl restart bootcloud`.
- **Certificate renewal is automatic** (`certbot.timer`). To renew, certbot
  briefly stops the redirect service to use port 80 itself, then restarts the
  app so it loads the new certificate. Port 80 must stay forwarded on the router.
- **This repo is public.** Never commit passwords, keys or `.env` files.

### Pushing from the server

The server's copy pulls from GitHub without a login, because the repo is public.
To **push** from the server, each person needs their own GitHub credentials there,
e.g. an SSH key added to their GitHub account, then:

```bash
git remote set-url origin git@github.com:koocyy/bootcloud.git
```
