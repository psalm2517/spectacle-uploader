#!/usr/bin/env python3
"""Share-menu plugin for KDE Purpose: upload a file to your own server.

Purpose starts this script with --server <unix socket>. It sends "<byte count>\\n"
followed by a CBOR map ({"urls": [...], "mimeType": "..."}). We answer with JSON
lines: {"percent": n}, {"output": {"url": "..."}} or {"error": 1, "errorText": "..."}.
Purpose treats the process exiting as the end of the job.

Run by hand it is also a command line tool (see main()): `region`, `screen`,
`monitor`, `window` and `active` take a screenshot with Spectacle and upload it
straight away, `upload FILE...` uploads existing files.
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from urllib import error, parse, request

APP = "spectacle-uploader"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
CHUNK = 64 * 1024

DEFAULTS = {
    "method": "POST",
    "body": "multipart",
    "file_field": "file",
    "query": {},
    "headers": {},
    "response": {"text": True},
    "link": "{value}",
    "copy": True,
    "timeout": 300,
}


def config_path():
    override = os.environ.get("SPECTACLE_UPLOADER_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP / "config.json"


def log_path():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP / "plugin.log"


def log(message):
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


class PluginError(Exception):
    pass


def cbor_decode(data):
    value, end = _cbor_item(data, 0, 0)
    if end != len(data):
        raise ValueError("trailing bytes after CBOR value")
    return value


def _cbor_argument(data, info, pos):
    if info < 24:
        return info, pos
    width = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
    if width is None:
        raise ValueError("unsupported CBOR length encoding")
    if pos + width > len(data):
        raise ValueError("truncated CBOR")
    return int.from_bytes(data[pos : pos + width], "big"), pos + width


def _cbor_item(data, pos, depth):
    if depth > 64:
        raise ValueError("CBOR nested too deeply")
    if pos >= len(data):
        raise ValueError("truncated CBOR")
    head = data[pos]
    pos += 1
    major, info = head >> 5, head & 0x1F

    if major == 7:
        if info in (20, 21):
            return info == 21, pos
        if info in (22, 23):
            return None, pos
        floats = {25: (">e", 2), 26: (">f", 4), 27: (">d", 8)}
        if info in floats:
            fmt, width = floats[info]
            if pos + width > len(data):
                raise ValueError("truncated CBOR")
            return struct.unpack(fmt, data[pos : pos + width])[0], pos + width
        raise ValueError("unsupported CBOR simple value")

    arg, pos = _cbor_argument(data, info, pos)
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise ValueError("truncated CBOR")
        raw = data[pos : pos + arg]
        return (bytes(raw) if major == 2 else raw.decode("utf-8")), pos + arg
    if major == 6:
        return _cbor_item(data, pos, depth + 1)

    if arg > len(data) - pos:
        raise ValueError("CBOR container larger than its data")
    if major == 4:
        items = []
        for _ in range(arg):
            item, pos = _cbor_item(data, pos, depth + 1)
            items.append(item)
        return items, pos
    mapping = {}
    for _ in range(arg):
        key, pos = _cbor_item(data, pos, depth + 1)
        mapping[key], pos = _cbor_item(data, pos, depth + 1)
    return mapping, pos


def read_request(sock):
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise PluginError("Purpose closed the connection before sending a request")
        buf += chunk
    header, _, rest = buf.partition(b"\n")
    try:
        size = int(header)
    except ValueError:
        raise PluginError("malformed request header from Purpose") from None
    if not 0 < size <= MAX_REQUEST_BYTES:
        raise PluginError("request from Purpose has an unreasonable size")
    while len(rest) < size:
        chunk = sock.recv(min(CHUNK, size - len(rest)))
        if not chunk:
            raise PluginError("request from Purpose was cut short")
        rest += chunk
    try:
        payload = cbor_decode(rest[:size])
    except ValueError as exc:
        raise PluginError(f"could not read Purpose's request: {exc}") from None
    if not isinstance(payload, dict):
        raise PluginError("Purpose's request was not a map")
    return payload


class Reporter:
    def __init__(self, sock):
        self.sock = sock
        self.last_percent = -1

    def _send(self, **fields):
        try:
            self.sock.sendall(json.dumps(fields, separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass

    def percent(self, value):
        value = max(0, min(100, int(value)))
        if value != self.last_percent:
            self.last_percent = value
            self._send(percent=value)

    def output(self, url):
        self._send(output={"url": url})

    def fail(self, text):
        self._send(error=1, errorText=text)


def load_config():
    path = config_path()
    if not path.is_file():
        raise PluginError(f"no config found at {path}; copy config.example.json there and edit it")
    if path.stat().st_mode & 0o077:
        log(f"warning: {path} is readable by other users; run chmod 600 on it")
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginError(f"could not read {path}: {exc}") from None
    if not isinstance(user, dict):
        raise PluginError(f"{path} must contain a JSON object")

    cfg = {**DEFAULTS, **user}
    if not isinstance(cfg.get("url"), str) or parse.urlparse(cfg["url"]).scheme not in ("http", "https"):
        raise PluginError("config 'url' must be an http(s) URL")
    cfg["method"] = str(cfg["method"]).upper()
    if cfg["method"] not in ("POST", "PUT", "PATCH"):
        raise PluginError("config 'method' must be POST, PUT or PATCH")
    if cfg["body"] not in ("raw", "multipart"):
        raise PluginError("config 'body' must be 'raw' or 'multipart'")
    for key in ("query", "headers", "response"):
        if not isinstance(cfg[key], dict):
            raise PluginError(f"config '{key}' must be an object")
    if len([k for k in ("json_pointer", "regex", "text") if k in cfg["response"]]) != 1:
        raise PluginError("config 'response' needs exactly one of json_pointer, regex or text")
    return cfg


def expand(template, variables):
    return re.sub(r"\{(\w+)\}", lambda m: str(variables.get(m.group(1), m.group(0))), template)


def local_files(payload):
    files = []
    for raw in payload.get("urls") or []:
        parsed = parse.urlparse(str(raw))
        if parsed.scheme not in ("file", ""):
            raise PluginError(f"can only upload local files, not {parsed.scheme}:// links")
        path = Path(parse.unquote(parsed.path))
        if not path.is_file():
            raise PluginError(f"not a file: {path}")
        files.append(path)
    if not files:
        raise PluginError("nothing to upload")
    return files


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _disposition(field, name):
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
    encoded = parse.quote(name, safe="")
    return f'form-data; name="{field}"; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def _body_stream(path, prefix, suffix, on_progress):
    total = path.stat().st_size
    sent = 0
    if prefix:
        yield prefix
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            sent += len(chunk)
            on_progress(sent * 100 // max(total, 1))
            yield chunk
    if suffix:
        yield suffix


def extract_link(spec, text):
    if "json_pointer" in spec:
        try:
            value = json.loads(text)
        except ValueError:
            raise PluginError("the server's response was not JSON") from None
        for part in [p for p in spec["json_pointer"].split("/")[1:]]:
            part = part.replace("~1", "/").replace("~0", "~")
            try:
                value = value[int(part)] if isinstance(value, list) else value[part]
            except (KeyError, IndexError, ValueError, TypeError):
                value = None
                break
    elif "regex" in spec:
        match = re.search(spec["regex"], text)
        value = (match.group(1) if match and match.groups() else match.group(0)) if match else None
    else:
        value = text.strip()
    if value in (None, "") or isinstance(value, (dict, list)):
        raise PluginError("could not find the link in the server's response")
    return str(value)


def upload(cfg, path, mime, on_progress):
    variables = {"filename": path.name, "mime": mime}
    url = cfg["url"]
    if cfg["query"]:
        query = {k: expand(str(v), variables) for k, v in cfg["query"].items()}
        url += ("&" if "?" in url else "?") + parse.urlencode(query)
    headers = {k: expand(str(v), variables) for k, v in cfg["headers"].items()}
    if not any(k.lower() == "user-agent" for k in headers):
        # the default Python-urllib agent is blocked outright by many Cloudflare-fronted servers
        headers["User-Agent"] = f"{APP}/1.0"
    size = path.stat().st_size

    if cfg["body"] == "raw":
        headers.setdefault("Content-Type", mime)
        headers["Content-Length"] = str(size)
        body = _body_stream(path, b"", b"", on_progress)
    else:
        boundary = uuid.uuid4().hex
        prefix = (
            f"--{boundary}\r\nContent-Disposition: {_disposition(cfg['file_field'], path.name)}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(len(prefix) + size + len(suffix))
        body = _body_stream(path, prefix, suffix, on_progress)

    opener = request.build_opener(NoRedirect)
    req = request.Request(url, data=body, headers=headers, method=cfg["method"])
    host = parse.urlparse(cfg["url"]).netloc
    try:
        with opener.open(req, timeout=cfg["timeout"]) as response:
            text = response.read(1024 * 1024).decode("utf-8", "replace")
    except error.HTTPError as exc:
        detail = exc.read(200).decode("utf-8", "replace").strip()
        if 300 <= exc.code < 400:
            raise PluginError(f"{host} redirected the upload (HTTP {exc.code}); it probably wants a login, check your credentials") from None
        hint = " - check your credentials" if exc.code in (401, 403) else ""
        raise PluginError(f"{host} answered HTTP {exc.code}{hint}" + (f": {detail}" if detail else "")) from None
    except (error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PluginError(f"could not reach {host}: {reason}") from None

    value = extract_link(cfg["response"], text)
    return expand(cfg["link"], {**variables, "value": value})


def copy_to_clipboard(text):
    commands = (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    )
    for command in commands:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text.encode(), check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def notify(title, body):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", APP, title, body], check=False, timeout=5)


def upload_all(cfg, files, on_progress, mime=None):
    links = []
    for index, path in enumerate(files):
        kind = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        links.append(upload(cfg, path, kind, lambda p, index=index: on_progress((index * 100 + p) // len(files))))
    return links


def run(sock):
    reporter = Reporter(sock)
    try:
        payload = read_request(sock)
        cfg = load_config()
        files = local_files(payload)
        single_mime = payload.get("mimeType") if len(files) == 1 else None

        links = upload_all(cfg, files, reporter.percent, single_mime)

        joined = "\n".join(links)
        copied = cfg["copy"] and copy_to_clipboard(joined)
        if not copied:
            notify("Uploaded", joined)
        reporter.output(links[0])
        reporter.percent(100)
        log(f"uploaded {len(links)} file(s)")
    except PluginError as exc:
        log(f"error: {exc}")
        notify("Upload failed", str(exc))
        reporter.fail(str(exc))
    except Exception:
        log("unexpected error:\n" + traceback.format_exc())
        notify("Upload failed", f"unexpected error, see {log_path()}")
        reporter.fail(f"unexpected error, see {log_path()}")


CAPTURE_FLAGS = {"region": "-r", "screen": "-f", "monitor": "-m", "window": "-u", "active": "-a"}


def clipboard_image():
    """The image currently on the clipboard as PNG bytes, or None."""
    commands = (
        ["wl-paste", "--no-newline", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    )
    for command in commands:
        if shutil.which(command[0]):
            try:
                done = subprocess.run(command, capture_output=True, timeout=5, check=True)
            except (OSError, subprocess.SubprocessError):
                continue
            if done.stdout.startswith(b"\x89PNG"):
                return done.stdout
    return None


def capture(mode, directory):
    """Take a screenshot with Spectacle; returns the file, or None if it was cancelled.

    Spectacle's region overlay has its own Copy button, which puts the image on the
    clipboard and exits without writing our file. If the clipboard changed while
    Spectacle ran and now holds a new image, that is the screenshot the user took.
    """
    if not shutil.which("spectacle"):
        raise PluginError("spectacle is not installed (or not in PATH)")
    target = Path(directory) / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    before = clipboard_image()
    started = time.monotonic()
    proc = subprocess.Popen(["spectacle", "-b", "-n", CAPTURE_FLAGS[mode], "-o", str(target)])
    # On Wayland the clipboard contents die with the process that owns them, so watch
    # for the image while Spectacle is still running instead of after it exits.
    seen = None
    while proc.poll() is None:
        time.sleep(0.3)
        image = clipboard_image()
        if image and image != before:
            seen = image
    code = proc.returncode
    log(f"{mode}: spectacle exited {code} after {time.monotonic() - started:.1f}s, "
        f"file={'yes' if target.is_file() else 'no'}, clipboard image={'new' if seen else 'none'}")
    if target.is_file() and target.stat().st_size > 0:
        return target
    if code not in (0, 1):
        raise PluginError(f"spectacle failed (exit code {code})")
    if seen is None:
        image = clipboard_image()
        seen = image if image and image != before else None
    if seen:
        target.write_bytes(seen)
        return target
    return None


def run_cli(mode, paths):
    """Capture (or take the given files) and upload, copying the link. Returns an exit code."""
    scratch = None
    try:
        cfg = load_config()
        if mode == "upload":
            files = local_files({"urls": paths})
        else:
            scratch = tempfile.mkdtemp(prefix=f"{APP}-")
            shot = capture(mode, scratch)
            if shot is None:
                log(f"{mode}: cancelled, nothing captured")
                return 0
            files = [shot]
        links = upload_all(cfg, files, lambda p: None)
        joined = "\n".join(links)
        copied = cfg["copy"] and copy_to_clipboard(joined)
        print(joined)
        notify("Link copied" if copied else "Uploaded", joined)
        log(f"uploaded {len(links)} file(s)")
        return 0
    except PluginError as exc:
        log(f"error: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        notify("Upload failed", str(exc))
    except Exception:
        log("unexpected error:\n" + traceback.format_exc())
        print(f"unexpected error, see {log_path()}", file=sys.stderr)
        notify("Upload failed", f"unexpected error, see {log_path()}")
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and (argv[0] in CAPTURE_FLAGS or argv[0] == "upload"):
        if argv[0] == "upload" and len(argv) < 2:
            sys.exit("usage: spectacle-uploader upload FILE...")
        if argv[0] != "upload" and len(argv) > 1:
            sys.exit(f"usage: spectacle-uploader {argv[0]}")
        return run_cli(argv[0], argv[1:])

    parser = argparse.ArgumentParser(
        description="Upload screenshots to your own server. Started by KDE Purpose as a Share-menu plugin; "
        "or run: spectacle-uploader {region,screen,monitor,window,active} to capture and upload, "
        "spectacle-uploader upload FILE... to upload files."
    )
    parser.add_argument("--server", required=True, help="unix socket to talk to Purpose on")
    parser.add_argument("--pluginType")
    parser.add_argument("--pluginPath")
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(args.server)
        run(sock)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
