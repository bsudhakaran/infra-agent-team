"""Lambda entry point (API Gateway HTTP API, payload v2.0).

This file is part of the vetted blueprint and is NOT rewritten by the LLM; only
operations.py and app_meta.json are generated per request. Routes:

  GET  /                 -> the HTML UI
  GET  /health           -> {"status": "ok"}
  GET  /api/operations   -> available operations
  POST /api/submit       -> {"operation": str, "input": str} -> runs it and stores the result
  GET  /api/report       -> aggregated report of submissions
"""

import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path

from operations import OPERATIONS

HERE = Path(__file__).resolve().parent
META = json.loads((HERE / "app_meta.json").read_text()) if (HERE / "app_meta.json").exists() else {}
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "5000"))
REPORT_WINDOW = 200


class MemoryStore:
    """Used by the local emulator and unit tests."""

    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)

    def recent(self, limit):
        return list(reversed(self.items))[:limit]


class DynamoStore:
    def __init__(self, table_name):
        import boto3

        self.table = boto3.resource("dynamodb").Table(table_name)

    def put(self, item):
        self.table.put_item(Item=item)

    def recent(self, limit):
        from boto3.dynamodb.conditions import Key

        resp = self.table.query(
            KeyConditionExpression=Key("pk").eq("SUBMISSION"), ScanIndexForward=False, Limit=limit
        )
        return resp.get("Items", [])


_store = None


def get_store():
    global _store
    if _store is None:
        _store = MemoryStore() if os.environ.get("STORE") == "memory" else DynamoStore(os.environ["TABLE_NAME"])
    return _store


def _resp(status, body, content_type="application/json"):
    payload = body if isinstance(body, str) else json.dumps(body, default=str)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
        "body": payload,
    }


def _ui():
    html = (HERE / "ui.html").read_text()
    title = META.get("title", "Input Operations")
    desc = META.get("description", "")
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return html.replace("__APP_TITLE__", esc(title)).replace("__APP_DESCRIPTION__", esc(desc))


def _submit(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "body must be JSON"})
    if not isinstance(data, dict):
        return _resp(400, {"error": "body must be a JSON object"})
    op, text = data.get("operation"), data.get("input")
    if op not in OPERATIONS:
        return _resp(400, {"error": f"unknown operation '{op}'", "available": sorted(OPERATIONS)})
    if not isinstance(text, str):
        return _resp(400, {"error": "input must be a string"})
    if len(text) > MAX_INPUT_CHARS:
        return _resp(413, {"error": f"input exceeds {MAX_INPUT_CHARS} characters"})

    started = time.time()
    item = {
        "pk": "SUBMISSION",
        "sk": f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())}#{uuid.uuid4().hex[:8]}",
        "operation": op,
        "input_preview": text[:60],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        result = OPERATIONS[op]["fn"](text)
        json.dumps(result)  # contract: must be serializable
        item.update(status="ok", result_json=json.dumps(result))
        status, body = 200, {"operation": op, "result": result}
    except ValueError as e:
        item.update(status="error", result_json=json.dumps(str(e)))
        status, body = 400, {"operation": op, "error": str(e)}
    item["duration_ms"] = round((time.time() - started) * 1000, 3)
    get_store().put({k: str(v) for k, v in item.items()})
    return _resp(status, body)


def _report():
    items = get_store().recent(REPORT_WINDOW)
    by_op = Counter(i["operation"] for i in items)
    errors = sum(1 for i in items if i.get("status") == "error")
    recent = [
        {
            "operation": i["operation"],
            "input": i.get("input_preview", ""),
            "result": json.loads(i.get("result_json", "null")),
            "status": i.get("status"),
            "created_at": i.get("created_at"),
        }
        for i in items[:10]
    ]
    return _resp(200, {
        "total": len(items),
        "errors": errors,
        "by_operation": dict(by_op),
        "recent": recent,
        "window": REPORT_WINDOW,
    })


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = (event.get("rawPath") or "/").rstrip("/") or "/"
    try:
        if method == "GET" and path == "/":
            return _resp(200, _ui(), "text/html; charset=utf-8")
        if method == "GET" and path == "/health":
            return _resp(200, {"status": "ok", "app": META.get("app_name", "app")})
        if method == "GET" and path == "/api/operations":
            return _resp(200, {"operations": [
                {"name": k, "label": v.get("label", k), "description": v.get("description", "")}
                for k, v in OPERATIONS.items()
            ]})
        if method == "POST" and path == "/api/submit":
            return _submit(event)
        if method == "GET" and path == "/api/report":
            return _report()
        return _resp(404, {"error": "not found"})
    except Exception as e:  # never leak stack traces to clients
        print(f"ERROR {type(e).__name__}: {e}")
        return _resp(500, {"error": "internal error"})
