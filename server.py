#!/usr/bin/env python3
"""Minimal local HTTP server for apt-commute-map (Python 3.9 stdlib only)."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import posixpath
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Type


DEFAULT_DESTINATION = {
    "name": "테헤란로 501",
    "address": "서울 강남구 테헤란로 501",
    "lat": 37.5082,
    "lng": 127.0604,
    "verified": False,
}


def load_dotenv(env_path: Path) -> Dict[str, str]:
    """Parse simple KEY=VALUE lines from .env."""
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        values[key] = val
    return values


def read_runtime_config(project_root: Path) -> Dict[str, Any]:
    """Build safe frontend config without exposing KAKAO_REST_API_KEY."""
    env_file = load_dotenv(project_root / ".env")
    kakao_js_key = (
        os.environ.get("VITE_KAKAO_JS_KEY")
        or os.environ.get("KAKAO_JS_KEY")
        or env_file.get("VITE_KAKAO_JS_KEY")
        or env_file.get("KAKAO_JS_KEY")
        or ""
    ).strip()
    rest_key = (os.environ.get("KAKAO_REST_API_KEY") or env_file.get("KAKAO_REST_API_KEY") or "").strip()

    destination = dict(DEFAULT_DESTINATION)
    app_json = project_root / "docs" / "data" / "apartments.json"
    if app_json.exists():
        try:
            data = json.loads(app_json.read_text(encoding="utf-8"))
            meta_dest = (data.get("meta") or {}).get("destination")
            if isinstance(meta_dest, dict) and meta_dest.get("lat") is not None:
                destination = meta_dest
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "kakaoJsKey": kakao_js_key,
        "hasKakaoJsKey": bool(kakao_js_key),
        "hasRestKey": bool(rest_key),
        "destination": destination,
        "envPath": str(project_root / ".env"),
    }


def has_hidden_or_traversal_segment(raw_path: str) -> bool:
    """Return True if any URL path segment starts with '.' or attempts traversal."""
    decoded = urllib.parse.unquote(raw_path.split("?", 1)[0].split("#", 1)[0])
    normalized_slashes = decoded.replace("\\", "/")
    for segment in normalized_slashes.split("/"):
        if not segment:
            continue
        if segment.startswith(".") or segment in (".", ".."):
            return True
    return False


def make_handler(project_root: Path) -> Type[http.server.SimpleHTTPRequestHandler]:
    """Create a request handler bound to project_root/docs as static root."""
    docs_dir = (project_root / "docs").resolve()

    class AppRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(docs_dir), **kwargs)

        def end_headers(self) -> None:
            req_path = self.path.split("?", 1)[0]
            if req_path.startswith("/api/") or req_path.startswith("/data/"):
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            super().end_headers()

        def translate_path(self, path: str) -> str:
            # Ensure translated path cannot escape docs_dir.
            clean_path = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
            norm_path = posixpath.normpath(clean_path)
            words = [w for w in norm_path.split("/") if w and not w.startswith(".") and w != ".."]
            target = docs_dir
            for word in words:
                target = target / word
            return str(target)

        def do_GET(self) -> None:
            req_path = self.path.split("?", 1)[0]
            if has_hidden_or_traversal_segment(self.path):
                self.send_error(404, "Not Found")
                return

            if req_path == "/api/config":
                payload = json.dumps(read_runtime_config(project_root), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            super().do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            # Keep default stdout logging concise.
            super().log_message(format, *args)

    return AppRequestHandler


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> int:
    project_root = Path(__file__).resolve().parent
    default_port = int(os.environ.get("PORT", "5173"))

    parser = argparse.ArgumentParser(description="Run local web server for apt-commute-map.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=default_port, help=f"Bind port (default: {default_port})")
    args = parser.parse_args()

    handler_cls = make_handler(project_root)
    server = ReusableThreadingHTTPServer((args.host, args.port), handler_cls)
    cfg = read_runtime_config(project_root)
    print(f"apt-commute-map 실행 중: http://{args.host}:{args.port}")
    print(f"  - 정적 루트: {project_root / 'docs'}")
    print(f"  - 카카오 JS 키: {'설정됨' if cfg['hasKakaoJsKey'] else '미설정 (.env 확인)'}")
    print(f"  - 카카오 REST 키: {'설정됨' if cfg['hasRestKey'] else '미설정 (.env 확인)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
