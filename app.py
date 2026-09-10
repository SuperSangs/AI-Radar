from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from radar.collector import refresh_all, _env_int
from radar.daily_summary import DailySummaryError, get_or_create_daily_summary
from radar.store import Store
from radar.translator import TranslationError, translate_to_chinese


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ROOT / ".env.local")
DB_PATH = Path(os.environ.get("AI_RADAR_DB", ROOT / "data" / "ai-radar.db"))
REFRESH_MINUTES = max(5, int(os.environ.get("AI_RADAR_REFRESH_MINUTES", "30")))
STORE = Store(DB_PATH)
refresh_lock = threading.Lock()
state = {"refreshing": False, "last_result": None}


def run_refresh() -> None:
    if not refresh_lock.acquire(blocking=False):
        return
    state["refreshing"] = True
    try:
        state["last_result"] = refresh_all(STORE)
    finally:
        state["refreshing"] = False
        refresh_lock.release()


def scheduler() -> None:
    while True:
        updated_at = STORE.summary().get("updated_at")
        if updated_at:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if not updated.tzinfo:
                updated = updated.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - updated).total_seconds()
            remaining = REFRESH_MINUTES * 60 - age
            if remaining > 0:
                time.sleep(remaining)
        run_refresh()
        time.sleep(REFRESH_MINUTES * 60)


class Handler(BaseHTTPRequestHandler):
    server_version = "AIRadar/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/items":
            self.api_items(parse_qs(parsed.query))
        elif parsed.path == "/api/sources":
            self.send_json({"sources": STORE.list_sources()})
        elif parsed.path == "/api/summary":
            self.send_json({**STORE.summary(), **state})
        elif parsed.path == "/api/daily-summary":
            self.api_daily_summary()
        elif parsed.path.startswith("/api/"):
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        else:
            self.serve_static(parsed.path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/translate":
            self.api_translate()
            return
        if path == "/api/daily-summary":
            self.api_daily_summary(force=True)
            return
        if path != "/api/refresh":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if state["refreshing"]:
            self.send_json({"refreshing": True, "message": "刷新正在进行"}, HTTPStatus.ACCEPTED)
            return
        threading.Thread(target=run_refresh, daemon=True).start()
        self.send_json({"refreshing": True, "message": "已开始刷新"}, HTTPStatus.ACCEPTED)

    def api_translate(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 24_000:
            self.send_json({"error": "Invalid request body"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return
        text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else ""
        if not text:
            self.send_json({"error": "Translation text is required"}, HTTPStatus.BAD_REQUEST)
            return
        print(f"[translate] received {len(text)} characters", flush=True)
        try:
            translation, model = translate_to_chinese(text)
        except TranslationError as exc:
            print(f"[translate] failed: {exc}", flush=True)
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        print(f"[translate] completed with {model}", flush=True)
        self.send_json({"translation": translation, "model": model})

    def api_daily_summary(self, *, force: bool = False) -> None:
        print(f"[daily-summary] requested force={force}", flush=True)
        try:
            payload = get_or_create_daily_summary(STORE, force=force)
        except DailySummaryError as exc:
            print(f"[daily-summary] failed: {exc}", flush=True)
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        print(
            f"[daily-summary] ready model={payload['model']} cached={payload['cached']} "
            f"items={payload['item_count']}",
            flush=True,
        )
        self.send_json(payload)

    def api_items(self, params: dict[str, list[str]]) -> None:
        range_value = params.get("range", ["24h"])[0]
        hours = {"24h": 24, "3d": 72, "7d": 168}.get(range_value, 24)
        region = params.get("region", ["all"])[0]
        content_type = params.get("type", ["all"])[0]
        query = params.get("q", [""])[0].strip()[:80]
        try:
            limit = int(params.get("limit", ["100"])[0])
        except ValueError:
            limit = 100
        allowed_regions = {"all", "china", "global"}
        allowed_types = {"all", "project", "model", "paper", "news", "discussion"}
        if region not in allowed_regions or content_type not in allowed_types:
            self.send_json({"error": "Invalid filter"}, HTTPStatus.BAD_REQUEST)
            return
        items = STORE.query_items(hours=hours, region=region, content_type=content_type, query=query, limit=limit)
        x_limit = _env_int('X_MAX_RESULTS', 70, 10, 70)
        # X ranks before other sources and is capped at 70; the unfiltered
        # default response already contains the full X selection.
        x_count = (sum(item['source_key'] == 'x-ai' for item in items)
                   if hours == 24 and region == 'all' and content_type == 'all' and not query and limit >= 70
                   else STORE.visible_x_count())
        self.send_json({"items": items, "count": len(items), "range": range_value,
                        "x_collection": {"visible_24h": x_count,
                                         "target": _env_int('X_VISIBLE_TARGET', 30, 30, 70),
                                         "daily_read_limit": x_limit,
                                         "daily_read_remaining": STORE.x_resources_remaining(x_limit)}})

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        path = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in path.parents and path != WEB_ROOT:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            path = WEB_ROOT / "index.html"
        mime, _ = mimetypes.guess_type(path.name)
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        compress = False
        for encoding in self.headers.get('Accept-Encoding', '').split(','):
            parts = [part.strip().lower() for part in encoding.split(';')]
            if parts[0] == 'gzip':
                try:
                    quality = next((float(part[2:]) for part in parts[1:] if part.startswith('q=')), 1.0)
                except ValueError:
                    quality = 0
                compress = quality > 0 and len(body) >= 1024
        if compress:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header('Vary', 'Accept-Encoding')
        if compress:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Radar")
    parser.add_argument("command", nargs="?", choices=("serve", "refresh"), default="serve")
    parser.add_argument("--host", default=os.environ.get("AI_RADAR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_RADAR_PORT", "8765")))
    args = parser.parse_args()
    if args.command == "refresh":
        print(json.dumps(refresh_all(STORE), ensure_ascii=False, indent=2))
        return

    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AI Radar running at http://{args.host}:{args.port}")
    print(f"Refreshing {len(STORE.list_sources())} sources every {REFRESH_MINUTES} minutes")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
