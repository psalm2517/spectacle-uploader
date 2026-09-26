# spectacle-uploader

A [KDE Spectacle](https://apps.kde.org/spectacle/) share plugin that uploads
screenshots to a server you run and copies the link.

It adds **Upload to your server** to Spectacle's Export → Share menu (it is a
[KDE Purpose](https://invent.kde.org/frameworks/purpose) plugin). It is a
generic HTTP client: you say where to send the file and how to find the link
in the response. Nothing is uploaded anywhere you didn't configure.

Single Python 3 file, standard library only. Tested on KDE Plasma 6 (Wayland).

## Install

```sh
git clone https://github.com/psalm2517/spectacle-uploader
cd spectacle-uploader
./install.sh
```

This installs to `~/.local/share/kpackage/Purpose/spectacle-uploader/` and
writes an example config to `~/.config/spectacle-uploader/config.json`.
Restart Spectacle afterwards. Remove it with `./install.sh uninstall` (your
config is kept).

To copy the link to the clipboard on Wayland you need `wl-clipboard`
(`xclip` or `xsel` on X11). Without one, the link is shown as a notification.

## Configure

Edit `~/.config/spectacle-uploader/config.json` (keep it `chmod 600`, it may
hold credentials):

```json
{
  "url": "https://files.example.com/api/upload",
  "method": "PUT",
  "body": "raw",
  "query": { "name": "{filename}" },
  "headers": { "Authorization": "Bearer YOUR_TOKEN" },
  "response": { "json_pointer": "/key" },
  "link": "https://files.example.com/f/{value}"
}
```

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

## Troubleshooting

Failures show a desktop notification. Details are in
`~/.local/state/spectacle-uploader/plugin.log` (credentials are never logged).

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

[Unlicense](LICENSE): public domain, do what you like. This is a small personal
tool and is shared as is; contributions are not being sought.
