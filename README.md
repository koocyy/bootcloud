# bootcloud

Source for **https://bootcloud.eu**, a FastAPI app served by uvicorn on our
server (`jarvis`). There is no nginx or other web server in front of it.

## Layout

```
site/                          the app
  main.py                      FastAPI app (uvicorn runs main:app)
  index.html                   page served at /
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

## How it runs on the server

| What | Where |
|---|---|
| This repo | `/srv/bootcloud` (shortcut: `~/bootcloud`) |
| Python virtualenv | `/srv/bootcloud-venv` |
| App service | `bootcloud.service`, uvicorn on port 443 with the TLS certificate |
| Redirect service | `bootcloud-redirect.service`, port 80, sends everything to `https://bootcloud.eu` |
| Certificate | Let's Encrypt, in `/etc/letsencrypt/live/bootcloud.eu/` |

Both services run as the unprivileged `www-data` user, start on boot and
restart themselves if they crash.

## Deploying a change

```bash
cd ~/bootcloud
git pull
sudo systemctl restart bootcloud     # only needed when Python code changed
```

- `index.html` is read from disk on every request, so it is live right after `git pull`.
- Changes to `main.py` (or any Python) need the restart.
- New Python package: add it to `requirements.txt`, then on the server run
  `/srv/bootcloud-venv/bin/pip install -r requirements.txt` and restart.

Useful commands:

```bash
systemctl status bootcloud            # is it running?
sudo journalctl -u bootcloud -f       # live log, including Python errors
sudo systemctl restart bootcloud
```

## Things to know

- **Only routes defined in `main.py` exist.** Any other path is a 404, including
  CSS/JS/image files, until a route or a `StaticFiles` mount is added for them.
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
