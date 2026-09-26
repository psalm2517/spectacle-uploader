# Spectacle Uploader

**Lightshot for KDE Spectacle, on your own domain.**

Take a screenshot, click Share → *Upload to your server*, and a link like
`https://shots.yourdomain.com/AbC123.png` is on your clipboard.

It's a [KDE Purpose](https://invent.kde.org/frameworks/purpose) plugin, so it
appears in Spectacle's Export → Share menu. It is a generic HTTP client: you
say where to send the file and how to find the link in the response, so it
works with any server that accepts uploads (see the recipes below).

Single Python 3 file, standard library only. Tested on KDE Plasma 6 (Wayland).

## Install

### 1. Requirements

- KDE Plasma 6 with Spectacle 6 (it uses KDE's Purpose framework, normally installed alongside it)
- Python 3 (standard library only, nothing to `pip install`)
- Something to put the link on the clipboard. Without it the plugin shows the
  link in a notification instead:
  - Wayland: `wl-clipboard`
  - X11: `xclip` or `xsel`
- `notify-send` for notifications (usually already present)

| Distro | Command |
| --- | --- |
| Debian / Ubuntu | `sudo apt install wl-clipboard xclip libnotify-bin` |
| Fedora | `sudo dnf install wl-clipboard xclip libnotify` |
| Arch | `sudo pacman -S wl-clipboard xclip libnotify` |
| openSUSE | `sudo zypper install wl-clipboard xclip libnotify-tools` |

(You only need the one that matches your session, `wl-clipboard` on Wayland or
`xclip` on X11, but installing both is harmless.)

### 2. Install the plugin

```sh
git clone https://github.com/psalm2517/spectacle-uploader
cd spectacle-uploader
./install.sh
```

This installs to `~/.local/share/kpackage/Purpose/spectacle-uploader/` and
writes an example config to `~/.config/spectacle-uploader/config.json`. Remove
it with `./install.sh uninstall` (your config is kept).

### 3. Point it at a server

Edit `~/.config/spectacle-uploader/config.json` (see [Configure](#configure)
and [Set up a server](#set-up-a-server)), then **restart Spectacle**.

### 4. Use it

**From Spectacle:** take a screenshot, click **Export**, then **Share**, then
**Upload to your server**. The link is copied to your clipboard. Use this when
you want to annotate the screenshot first.

**With one key (the Lightshot way):** the installer also adds a
`spectacle-uploader` command that takes the screenshot and uploads it in one
step, with no Spectacle window in between:

| Command | Captures |
| --- | --- |
| `spectacle-uploader region` | a rectangle you drag out |
| `spectacle-uploader screen` | the whole desktop |
| `spectacle-uploader monitor` | the monitor the cursor is on |
| `spectacle-uploader window` | the window under the cursor |
| `spectacle-uploader active` | the active window |
| `spectacle-uploader upload FILE...` | existing files, no capture |

The link is copied to the clipboard, printed, and shown in a notification.
Cancelling a region selection does nothing. The screenshot is only kept in a
temporary folder until it is uploaded.

To bind it to a key in Plasma: System Settings → Keyboard → Shortcuts → Add New →
Command or Script, set the command to
`/home/YOU/.local/bin/spectacle-uploader region` (use the full path), then
assign the key, for example Print Screen. Menu names may differ slightly between
Plasma versions. If Print Screen is already taken by Spectacle, reassign that
one first.

## Set up a server

The plugin needs somewhere to upload to. Any HTTP endpoint that accepts a file
and tells you (or lets you work out) the resulting URL will do. Two options:

**Use a server you already have.** Look at what it expects and copy the closest
[recipe](#recipes) below. Servers that take a plain form upload or a
raw `PUT`, with a fixed token or header for auth, fit one of them.

**Run the included example server** ([`examples/server.py`](examples/server.py),
about 90 lines, standard library only). It accepts authenticated uploads and
serves the files publicly at `https://your-domain/f/<id>.<ext>`.

1. Copy `examples/server.py` to your server and pick a secret token:
   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. Run it (keep it running with systemd, Docker, or whatever you prefer):
   ```sh
   UPLOAD_TOKEN=<that token> UPLOAD_DIR=/srv/uploads ./server.py
   ```
   It listens on `127.0.0.1:8080` only.
3. Put a reverse proxy in front for HTTPS on your domain. With Caddy that is:
   ```
   files.yourdomain.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```
   Point a DNS record for `files.yourdomain.com` at the server.
4. Set the plugin config to match (this is what `config.example.json` is):
   ```json
   {
     "url": "https://files.yourdomain.com/upload",
     "method": "PUT",
     "body": "raw",
     "query": { "name": "{filename}" },
     "headers": { "Authorization": "Bearer <that token>" },
     "response": { "json_pointer": "/key" },
     "link": "https://files.yourdomain.com/f/{value}"
   }
   ```

Anyone with a link can view that file; only holders of the token can upload.
The machine must be reachable from the internet on your domain (ports 80/443
open). On a home connection that is often not possible; the Cloudflare option
below needs no open ports.
The example server has no deletion, expiry or size accounting beyond a per-file
limit (`MAX_MB`, default 50), so treat it as a starting point.

### Or use Cloudflare (Workers + R2, no server to run)

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/psalm2517/spectacle-uploader/tree/main/examples/cloudflare-worker)

[`examples/cloudflare-worker/`](examples/cloudflare-worker) is a small Worker
that stores uploads in an R2 bucket and serves them at `/f/<id>`, with the same
API as the example server above. It needs a Cloudflare account (R2 has to be
enabled on it) and no server of your own.

1. Click **Deploy to Cloudflare** above. It creates the R2 bucket and the Worker
   in your account and asks you for `UPLOAD_TOKEN`: paste a long random string
   (`openssl rand -base64 32`) and keep a copy for the plugin config.
2. Your Worker is now live at `https://shots.<your-subdomain>.workers.dev`.
   To use your own domain instead (the domain must be on your Cloudflare
   account): Workers & Pages → `shots` → Settings → Domains & Routes → Add →
   Custom domain, and enter e.g. `shots.yourdomain.com`.
3. Use the same plugin config as above, with that hostname and your token.

The button is untested by me end to end, since it needs a Cloudflare account.
If you'd rather use the command line instead:

```sh
cd examples/cloudflare-worker
npx wrangler r2 bucket create shots-files
npx wrangler deploy
npx wrangler secret put UPLOAD_TOKEN
```

To serve it on your own domain from the config file, add
`"routes": [{ "pattern": "shots.yourdomain.com", "custom_domain": true }]` to
`wrangler.jsonc` before deploying.

**Locking uploads behind Cloudflare Access instead of a shared token.** If you
put the host behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
(so a login page protects the whole site, not just uploads):

1. Create a service token (Zero Trust → Access controls → Service credentials).
2. On the Access application for your hostname, add a policy with action
   **Service Auth** that includes that token.
3. Add a second Access application for the path `f/*` on the same hostname with
   a **Bypass** policy for everyone, so that shared links open without a login.
   (The most specific path wins.)
4. Send the token from the plugin:
   ```json
   "headers": {
     "CF-Access-Client-Id": "YOUR_CLIENT_ID.access",
     "CF-Access-Client-Secret": "YOUR_CLIENT_SECRET"
   }
   ```
   Cloudflare rejects the default Python User-Agent with a 403 (error 1010);
   this plugin sends its own, so no extra setup is needed.

## Configure

Everything lives in `~/.config/spectacle-uploader/config.json`. Keep it
`chmod 600`, it may hold credentials.

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | required | `http(s)` endpoint to send the file to |
| `method` | `POST` | `POST`, `PUT` or `PATCH` |
| `body` | `multipart` | `multipart` form upload, or `raw` (file bytes as the body) |
| `file_field` | `file` | form field name for `multipart` |
| `query` | `{}` | query parameters |
| `headers` | `{}` | extra request headers, e.g. auth |
| `response` | `{"text": true}` | how to find the link: exactly one of `json_pointer` (RFC 6901), `regex` (first group, else the whole match) or `text` (the whole body) |
| `link` | `{value}` | template for the final link |
| `copy` | `true` | copy the link to the clipboard |
| `timeout` | `300` | seconds |

Values in `query` and `headers`, and `link`, can use `{filename}`,
`{mime}` and, in `link`, `{value}` (what `response` extracted).

Redirects are not followed, so a login page in front of your server shows up as
an error instead of a confusing success. Some servers and CDNs block the
default Python User-Agent; the plugin sends `spectacle-uploader/1.0` unless you
set a `user-agent` header.

## Recipes

Put one of these in `config.json`, adjusting the URL and credentials.

**Form upload, link returned as plain text** (the common "paste host" style):

```json
{ "url": "https://up.example.com/upload", "file_field": "file" }
```

**Form upload, link inside a JSON reply**, e.g. `{"data": {"url": "..."}}`:

```json
{
  "url": "https://up.example.com/upload",
  "headers": { "Authorization": "Bearer YOUR_TOKEN" },
  "response": { "json_pointer": "/data/url" }
}
```

**Raw `PUT`, server returns an id, you build the link:**

```json
{
  "url": "https://shots.example.com/api/upload",
  "method": "PUT",
  "body": "raw",
  "query": { "name": "{filename}" },
  "response": { "json_pointer": "/key" },
  "link": "https://shots.example.com/f/{value}"
}
```

## Troubleshooting

Failures show a desktop notification. Details are in
`~/.local/state/spectacle-uploader/plugin.log` (credentials are never logged).

## Tests

```sh
python3 -m unittest discover -s tests
```

## AI disclosure

This project was built with AI assistance, directed by me.

## License

Unlicense. See [LICENSE](./LICENSE).
