"""Local API Gateway (HTTP API v2) + Lambda emulator.

Runs the generated Lambda handler in-process behind a real HTTP server with an in-memory
store. The QA agent uses it to test generated code BEFORE anything is deployed, and the
`local` connection uses it as the deployment target for a fully offline demo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_RUNNING: dict[str, "LocalEmulator"] = {}


def _load_handler(app_dir: Path):
    """Import handler.py + operations.py from app_dir without polluting other runs."""
    for mod in ("handler", "operations"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(app_dir))
    old_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True   # keep the Lambda zip hash stable
    try:
        spec = importlib.util.spec_from_file_location(f"iat_handler_{id(app_dir)}", app_dir / "handler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_flag
        sys.path.remove(str(app_dir))
        sys.modules.pop("operations", None)


class LocalEmulator:
    def __init__(self, app_dir: Path, port: int = 0):
        os.environ["STORE"] = "memory"
        self.module = _load_handler(Path(app_dir))
        self.module._store = None
        self.requests = 0
        emulator = self

        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self):
                emulator.requests += 1
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                event = {
                    "version": "2.0",
                    "rawPath": parts.path or "/",
                    "rawQueryString": parts.query,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "requestContext": {"http": {"method": self.command, "path": parts.path}},
                    "body": body,
                    "isBase64Encoded": False,
                }
                resp = emulator.module.lambda_handler(event, None)
                payload = (resp.get("body") or "").encode()
                self.send_response(resp.get("statusCode", 200))
                for k, v in (resp.get("headers") or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _dispatch

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def start(self, key: str | None = None) -> str:
        self.thread.start()
        if key:
            stop(key)
            _RUNNING[key] = self
        return self.url

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def stop(key: str) -> bool:
    emu = _RUNNING.pop(key, None)
    if emu:
        emu.stop()
        return True
    return False


def invoke(app_dir: Path, method: str, path: str, body: dict | None = None) -> dict:
    """One-shot direct invocation (no HTTP), handy in unit tests."""
    os.environ["STORE"] = "memory"
    module = _load_handler(Path(app_dir))
    event = {"rawPath": path, "requestContext": {"http": {"method": method}},
             "body": json.dumps(body) if body is not None else ""}
    return module.lambda_handler(event, None)
