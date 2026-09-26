import email.parser
import http.server
import importlib.util
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "contents" / "code" / "main.py"

_spec = importlib.util.spec_from_file_location("plugin_main", MAIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


def cbor_encode(value):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * width):
                return bytes([major << 5 | info]) + n.to_bytes(width, "big")
        raise ValueError("too large")

    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor_encode(k) + cbor_encode(v) for k, v in value.items())
    raise TypeError(type(value))


class Recorder:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers
        self.requests = []
        self.url = ""
        self.server = None


def start_server(status=200, body=b"", headers=None):
    rec = Recorder(status, body, headers or {})

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("content-length", 0))
            data = self.rfile.read(length)
            rec.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": data,
                }
            )
            self.send_response(rec.status)
            for key, value in rec.headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(rec.body)))
            self.end_headers()
            self.wfile.write(rec.body)

        do_PUT = do_POST = do_PATCH = _handle

        def log_message(self, *args):
            pass

    rec.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=rec.server.serve_forever, daemon=True).start()
    rec.url = f"http://127.0.0.1:{rec.server.server_address[1]}"
    return rec


class PluginCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="su-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.dir),
            "XDG_STATE_HOME": str(self.dir / "state"),
            "SPECTACLE_UPLOADER_CONFIG": str(self.dir / "config.json"),
            "STUB_OUT": str(self.dir / "stub-out"),
        }
        self.stub("notify-send", "true\n")

    def stub(self, name, script):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)

    def write_config(self, cfg, mode=0o600):
        path = self.dir / "config.json"
        path.write_text(json.dumps(cfg))
        path.chmod(mode)

    def server(self, **kwargs):
        rec = start_server(**kwargs)
        self.addCleanup(rec.server.shutdown)
        return rec

    def make_file(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def file_url(self, path):
        return "file://" + quote(str(path))

    def log_text(self):
        path = self.dir / "state" / "spectacle-uploader" / "plugin.log"
        return path.read_text() if path.exists() else ""

    def run_plugin(self, payload, pieces=1):
        self.n = getattr(self, "n", 0) + 1
        sock_path = str(self.dir / f"p{self.n}.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock_path)
        listener.listen(1)
        listener.settimeout(15)
        self.addCleanup(listener.close)
        proc = subprocess.Popen(
            [str(MAIN), "--server", sock_path, "--pluginType", "Export", "--pluginPath", str(MAIN)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        conn, _ = listener.accept()
        conn.settimeout(30)
        data = cbor_encode(payload)
        wire = str(len(data)).encode() + b"\n" + data
        step = max(1, len(wire) // pieces)
        for start in range(0, len(wire), step):
            conn.sendall(wire[start : start + step])
            if pieces > 1:
                time.sleep(0.02)
        received = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            received += chunk
        proc.wait(timeout=30)
        messages = [json.loads(line) for line in received.splitlines() if line]
        merged = {}
        for message in messages:
            merged.update(message)
        self.last_stderr = proc.stderr.read().decode()
        return merged, messages, proc

    def upload_payload(self, *paths, mime="image/png"):
        return {"urls": [self.file_url(p) for p in paths], "mimeType": mime}


class UploadTests(PluginCase):
    def test_raw_put_uploads_the_file_and_builds_the_link(self):
        srv = self.server(body=b'{"key":"AbC12345.png"}')
        self.write_config(
            {
                "url": srv.url + "/api/upload",
                "method": "PUT",
                "body": "raw",
                "query": {"name": "{filename}"},
                "headers": {"X-Token": "secret-value"},
                "response": {"json_pointer": "/key"},
                "link": "https://files.example/f/{value}",
            }
        )
        data = b"\x89PNG" + os.urandom(3000)
        shot = self.make_file("my shot.png", data)
        result, messages, proc = self.run_plugin(self.upload_payload(shot))
        self.assertEqual(result["output"], {"url": "https://files.example/f/AbC12345.png"})
        self.assertEqual(result["percent"], 100)
        self.assertNotIn("error", result)
        self.assertEqual(proc.returncode, 0)
        req = srv.requests[0]
        self.assertEqual(req["method"], "PUT")
        self.assertEqual(req["path"], "/api/upload?name=my+shot.png")
        self.assertEqual(req["headers"]["x-token"], "secret-value")
        self.assertEqual(req["headers"]["content-type"], "image/png")
        self.assertEqual(req["headers"]["content-length"], str(len(data)))
        self.assertEqual(req["body"], data)

    def test_multipart_post_sends_a_proper_form_field(self):
        srv = self.server(body=b'{"data":{"url":"https://files.example/x.png"}}')
        self.write_config(
            {"url": srv.url + "/upload", "file_field": "upload", "response": {"json_pointer": "/data/url"}}
        )
        data = os.urandom(5000)
        shot = self.make_file("café \"1\".png", data)
        result, _, _ = self.run_plugin(self.upload_payload(shot))
        self.assertEqual(result["output"], {"url": "https://files.example/x.png"})
        req = srv.requests[0]
        self.assertEqual(req["method"], "POST")
        ctype = req["headers"]["content-type"]
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertEqual(req["headers"]["content-length"], str(len(req["body"])))
        parsed = email.parser.BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req["body"])
        part = parsed.get_payload()[0]
        self.assertEqual(part.get_param("name", header="content-disposition"), "upload")
        self.assertIn(b"filename=\"caf_ _1_.png\"", req["body"])
        self.assertIn(b"filename*=UTF-8''caf%C3%A9%20%221%22.png", req["body"])
        self.assertEqual(part.get_content_type(), "image/png")
        self.assertEqual(part.get_payload(decode=True), data)

    def test_link_extraction_variants(self):
        cases = [
            ({"regex": r"id=(\w+)"}, b"saved id=Zz9 ok", "https://h/Zz9"),
            ({"regex": r"https://\S+"}, b"here: https://h/whole ok", "https://h/https://h/whole"),
            ({"text": True}, b"  plain-value\n", "https://h/plain-value"),
            ({"json_pointer": "/files/0/name"}, b'{"files":[{"name":"n.png"}]}', "https://h/n.png"),
            ({"json_pointer": "/a~1b"}, b'{"a/b":"esc"}', "https://h/esc"),
        ]
        for spec, body, expected in cases:
            with self.subTest(spec=spec):
                srv = self.server(body=body)
                self.write_config({"url": srv.url, "response": spec, "link": "https://h/{value}"})
                shot = self.make_file("a.png", b"x")
                result, _, _ = self.run_plugin(self.upload_payload(shot))
                self.assertEqual(result.get("output"), {"url": expected}, result)

    def test_multiple_files_return_the_first_link_and_copy_them_all(self):
        srv = self.server(body=b"link")
        self.write_config({"url": srv.url, "link": "https://h/{filename}"})
        self.stub("wl-copy", 'cat > "$STUB_OUT"\n')
        a, b = self.make_file("a.png", b"aaa"), self.make_file("b.txt", b"bbb")
        result, _, _ = self.run_plugin(self.upload_payload(a, b))
        self.assertEqual(result["output"], {"url": "https://h/a.png"})
        self.assertEqual((self.dir / "stub-out").read_text(), "https://h/a.png\nhttps://h/b.txt")
        types = [r["headers"]["content-type"] for r in srv.requests]
        self.assertTrue(types[0].startswith("multipart/") and types[1].startswith("multipart/"))
        self.assertIn(b"Content-Type: text/plain", srv.requests[1]["body"])

    def test_notification_only_when_nothing_can_copy_the_link(self):
        srv = self.server(body=b"https://h/x")
        self.write_config({"url": srv.url})
        self.stub("notify-send", 'echo "$@" > "$STUB_OUT"\n')
        shot = self.make_file("a.png", b"x")
        self.run_plugin(self.upload_payload(shot))
        self.assertIn("https://h/x", (self.dir / "stub-out").read_text())

        self.stub("wl-copy", 'cat > "$STUB_OUT.clip"\n')
        (self.dir / "stub-out").unlink()
        self.run_plugin(self.upload_payload(shot))
        self.assertFalse((self.dir / "stub-out").exists(), "no notification when the copy worked")
        self.assertEqual((self.dir / "stub-out.clip").read_text(), "https://h/x")

    def test_copy_can_be_switched_off(self):
        srv = self.server(body=b"https://h/x")
        self.write_config({"url": srv.url, "copy": False})
        self.stub("wl-copy", 'cat > "$STUB_OUT"\n')
        self.stub("notify-send", "true\n")
        self.run_plugin(self.upload_payload(self.make_file("a.png", b"x")))
        self.assertFalse((self.dir / "stub-out").exists())

    def test_progress_ends_at_100_and_never_goes_backwards(self):
        srv = self.server(body=b"ok")
        self.write_config({"url": srv.url, "body": "raw", "method": "PUT"})
        shot = self.make_file("big.bin", os.urandom(300 * 1024))
        _, messages, _ = self.run_plugin(self.upload_payload(shot, mime="application/octet-stream"))
        percents = [m["percent"] for m in messages if "percent" in m]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(percents[-1], 100)

    def test_request_split_across_many_socket_writes(self):
        srv = self.server(body=b"ok")
        self.write_config({"url": srv.url})
        result, _, _ = self.run_plugin(self.upload_payload(self.make_file("a.png", b"x")), pieces=9)
        self.assertEqual(result["output"], {"url": "ok"})


class FailureTests(PluginCase):
    def failure(self, payload=None, **kwargs):
        result, _, proc = self.run_plugin(payload, **kwargs)
        self.assertEqual(result.get("error"), 1, result)
        self.assertNotIn("output", result)
        self.assertEqual(proc.returncode, 0)
        return result["errorText"]

    def test_redirects_are_reported_not_followed(self):
        srv = self.server(status=302, headers={"location": "https://login.example/"})
        self.write_config({"url": srv.url})
        text = self.failure(self.upload_payload(self.make_file("a.png", b"x")))
        self.assertIn("redirected", text)
        self.assertIn("credentials", text)
        self.assertEqual(len(srv.requests), 1)

    def test_http_errors_say_what_happened(self):
        for status, expect in ((401, "check your credentials"), (403, "check your credentials"), (413, "too big"), (500, "HTTP 500")):
            with self.subTest(status=status):
                srv = self.server(status=status, body=b"too big for us" if status == 413 else b"")
                self.write_config({"url": srv.url})
                text = self.failure(self.upload_payload(self.make_file("a.png", b"x")))
                self.assertIn(f"HTTP {status}", text)
                self.assertIn(expect, text)

    def test_failure_raises_a_desktop_notification(self):
        srv = self.server(status=500)
        self.write_config({"url": srv.url})
        self.stub("notify-send", 'echo "$@" > "$STUB_OUT"\n')
        self.failure(self.upload_payload(self.make_file("a.png", b"x")))
        self.assertIn("Upload failed", (self.dir / "stub-out").read_text())

    def test_unreachable_server(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.write_config({"url": f"http://127.0.0.1:{port}/up"})
        self.assertIn("could not reach", self.failure(self.upload_payload(self.make_file("a.png", b"x"))))

    def test_link_missing_from_the_response(self):
        for spec, body in (({"json_pointer": "/nope"}, b'{"a":1}'), ({"regex": "id=(\\d+)"}, b"nothing"), ({"json_pointer": "/a"}, b"<html>"), ({"text": True}, b"   ")):
            with self.subTest(spec=spec):
                srv = self.server(body=body)
                self.write_config({"url": srv.url, "response": spec})
                text = self.failure(self.upload_payload(self.make_file("a.png", b"x")))
                self.assertTrue("link" in text or "JSON" in text, text)

    def test_config_problems(self):
        shot = self.make_file("a.png", b"x")
        (self.dir / "config.json").unlink(missing_ok=True)
        self.assertIn("no config found", self.failure(self.upload_payload(shot)))
        (self.dir / "config.json").write_text("{not json")
        self.assertIn("could not read", self.failure(self.upload_payload(shot)))
        bad = [
            ({"url": "ftp://x"}, "http(s) URL"),
            ({"url": "http://x", "method": "GET"}, "POST, PUT or PATCH"),
            ({"url": "http://x", "body": "json"}, "'raw' or 'multipart'"),
            ({"url": "http://x", "headers": []}, "'headers' must be an object"),
            ({"url": "http://x", "response": {"regex": "a", "text": True}}, "exactly one"),
        ]
        for cfg, expect in bad:
            with self.subTest(cfg=cfg):
                self.write_config(cfg)
                self.assertIn(expect, self.failure(self.upload_payload(shot)))

    def test_bad_inputs(self):
        srv = self.server(body=b"ok")
        self.write_config({"url": srv.url})
        self.assertIn("nothing to upload", self.failure({"urls": [], "mimeType": "image/png"}))
        self.assertIn("nothing to upload", self.failure({"mimeType": "image/png"}))
        self.assertIn("not a file", self.failure({"urls": ["file:///definitely/not/here.png"]}))
        self.assertIn("only upload local files", self.failure({"urls": ["https://example.com/a.png"]}))
        self.assertEqual(srv.requests, [])

    def test_secrets_never_reach_the_log_or_stderr(self):
        srv = self.server(status=500)
        self.write_config({"url": srv.url, "headers": {"X-Secret": "hunter2-token"}})
        self.failure(self.upload_payload(self.make_file("a.png", b"x")))
        self.assertNotIn("hunter2-token", self.log_text())
        self.assertNotIn("hunter2-token", self.last_stderr)

    def test_lax_config_permissions_warn_but_still_work(self):
        srv = self.server(body=b"ok")
        self.write_config({"url": srv.url}, mode=0o644)
        result, _, _ = self.run_plugin(self.upload_payload(self.make_file("a.png", b"x")))
        self.assertEqual(result["output"], {"url": "ok"})
        self.assertIn("chmod 600", self.log_text())

    def test_garbage_from_purpose_is_an_error_not_a_crash(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock_path = str(self.dir / "g.sock")
        listener.bind(sock_path)
        listener.listen(1)
        self.addCleanup(listener.close)
        for wire in (b"abc\nxyz", b"5\n\xff\xff\xff\xff\xff", b"99999999999\n", b"3\n\x01\x02\x03"):
            with self.subTest(wire=wire):
                proc = subprocess.Popen([str(MAIN), "--server", sock_path], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                conn, _ = listener.accept()
                conn.sendall(wire)
                conn.shutdown(socket.SHUT_WR)
                received = b""
                while chunk := conn.recv(4096):
                    received += chunk
                proc.wait(timeout=20)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(json.loads(received.splitlines()[-1])["error"], 1)
                self.assertNotIn("Traceback", proc.stderr.read().decode())


class CborTests(unittest.TestCase):
    def test_rfc8949_appendix_a_vectors(self):
        vectors = {
            "00": 0, "17": 23, "1818": 24, "1864": 100, "1903e8": 1000, "1a000f4240": 1000000,
            "1b000000e8d4a51000": 1000000000000, "20": -1, "29": -10, "3863": -100, "3903e7": -1000,
            "60": "", "6161": "a", "6449455446": "IETF", "62c3bc": "ü", "80": [], "83010203": [1, 2, 3],
            "a0": {}, "a26161016162820203": {"a": 1, "b": [2, 3]}, "f4": False, "f5": True, "f6": None,
            "fb3ff8000000000000": 1.5, "f93e00": 1.5, "fa47c35000": 100000.0, "4401020304": b"\x01\x02\x03\x04",
            "c11a514b67b0": 1363896240, "8301820203820405": [1, [2, 3], [4, 5]],
        }
        for hex_bytes, expected in vectors.items():
            with self.subTest(hex=hex_bytes):
                self.assertEqual(plugin.cbor_decode(bytes.fromhex(hex_bytes)), expected)

    def test_round_trip_of_the_kind_of_request_purpose_sends(self):
        payload = {"urls": ["file:///tmp/a%20b.png", "file:///tmp/é.png"], "mimeType": "image/png", "n": -70000, "flags": [True, False, None], "nested": {"k": {"z": [1.25]}}}
        self.assertEqual(plugin.cbor_decode(cbor_encode(payload)), payload)

    def test_malformed_input_raises_value_error(self):
        bad = {
            "truncated array": "8201",
            "truncated string": "6461",
            "trailing bytes": "0102",
            "indefinite array": "9fff",
            "huge container": "9b00ffffffffffffff",
            "huge string": "5b00ffffffffffffff",
            "invalid utf-8": "62fffe",
            "empty": "",
            "unsupported simple": "f0",
            "deeply nested": "81" * 80 + "00",
        }
        for name, hex_bytes in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    plugin.cbor_decode(bytes.fromhex(hex_bytes))


class PackagingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="su-inst-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        self.plugin_dir = self.home / ".local/share/kpackage/Purpose/spectacle-uploader"
        self.config = self.home / ".config/spectacle-uploader/config.json"

    def install(self, *args):
        return subprocess.run([str(ROOT / "install.sh"), *args], env=self.env, capture_output=True, text=True)

    def test_install_matches_what_purpose_expects(self):
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        main = self.plugin_dir / "contents/code/main.py"
        self.assertTrue((self.plugin_dir / "metadata.json").is_file())
        self.assertEqual(main.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)

    def test_installed_plugin_runs_as_a_program(self):
        self.install()
        done = subprocess.run([str(self.plugin_dir / "contents/code/main.py"), "--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--server", done.stdout)

    def test_reinstall_keeps_an_edited_config_and_uninstall_keeps_it_too(self):
        self.install()
        self.config.write_text('{"url": "http://mine"}')
        self.install()
        self.assertEqual(self.config.read_text(), '{"url": "http://mine"}')
        done = self.install("uninstall")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(self.plugin_dir.exists())
        self.assertTrue(self.config.exists())

    def test_unknown_command_is_refused(self):
        self.assertEqual(self.install("frobnicate").returncode, 2)

    def test_metadata_has_what_purpose_needs(self):
        meta = json.loads((ROOT / "metadata.json").read_text())
        self.assertEqual(meta["KPlugin"]["Id"], "spectacle-uploader")
        self.assertIn("Export", meta["X-Purpose-PluginTypes"])
        self.assertTrue(meta["X-Purpose-ActionDisplay"])

    def test_example_config_is_valid_for_the_plugin(self):
        example = json.loads((ROOT / "config.example.json").read_text())
        cfg = {**plugin.DEFAULTS, **example}
        self.assertIn(cfg["method"], ("POST", "PUT", "PATCH"))
        self.assertEqual(len([k for k in ("json_pointer", "regex", "text") if k in cfg["response"]]), 1)
        self.assertIn("example.com", cfg["url"])


if __name__ == "__main__":
    unittest.main()
