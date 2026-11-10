# bootcloud

Source for **https://bootcloud.eu**, served by nginx from our server.

## Layout

```
site/                  the website; nginx serves this folder as-is
  index.html
nginx/
  bootcloud.conf       copy of the live nginx site config (reference)
```

## Where it lives on the server

The repo is cloned to **`/srv/bootcloud`** on the server, and every team member
has a shortcut to it at **`~/bootcloud`**.

nginx serves `/srv/bootcloud/site` directly, so a change there is live on the
next page load. No build step and no restart needed for HTML/CSS/JS/images.

## Workflow

On the server:

```bash
cd ~/bootcloud
git pull                  # get the latest from GitHub
# edit files in site/ - changes are live immediately
git add -A && git commit -m "describe the change"
git push                  # needs your own GitHub login on the server (see below)
```

Or edit on your own computer, push to GitHub, then on the server run
`cd ~/bootcloud && git pull` to put it live.

### Pushing from the server

The server's copy pulls from GitHub without a login, because the repo is public.
To **push** from the server, each person needs their own GitHub credentials there,
e.g. an SSH key added to their GitHub account, then:

```bash
git remote set-url origin git@github.com:koocyy/bootcloud.git
```

## nginx config

`nginx/bootcloud.conf` mirrors `/etc/nginx/sites-available/bootcloud`.
The live file is the one nginx uses; if you change one, update the other.

```bash
sudo nginx -t && sudo systemctl reload nginx   # check, then apply
```

HTTPS certificates come from Let's Encrypt via certbot and renew automatically
(`certbot.timer`). The `# managed by Certbot` lines are written by certbot.
Don't remove them.
