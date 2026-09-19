"""Authenticated loopback adapter for browser-native PanelLens acquisition."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from main import (
    _warm_translation_model,
    cancel_request,
    clear_all_caches,
    clear_result_cache,
    handle,
    handle_batch,
    reprioritize_request,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 24 * 1024 * 1024
SESSION_TOKEN = os.environ.get("PANELLENS_HTTP_TOKEN") or secrets.token_urlsafe(32)
ALLOWED_ORIGINS = {
    value.strip()
    for value in os.environ.get("PANELLENS_HTTP_ALLOWED_ORIGINS", "").split(",")
    if value.strip()
}
_cache_clear_lock = threading.Lock()


def origin_allowed(origin: str) -> bool:
    return bool(re.fullmatch(r"chrome-extension://[a-p]{32}", origin)) or origin in ALLOWED_ORIGINS


def request_authorized(origin: str, supplied_token: str) -> bool:
    return origin_allowed(origin) and hmac.compare_digest(supplied_token, SESSION_TOKEN)


def route_request(
    method: str,
    path: str,
    payload: Any = None,
    pipeline_handler: Callable[[dict[str, Any]], dict[str, Any]] = handle,
    batch_handler: Callable[[dict[str, Any]], dict[str, Any]] = handle_batch,
    cancel_handler: Callable[[str], bool] = cancel_request,
    reprioritize_handler: Callable[[str, Any], bool] = reprioritize_request,
) -> tuple[int, dict[str, Any]]:
    """Route one decoded HTTP request without coupling tests to a socket."""
    if path == "/v1/translation/settings" and method in {"GET", "POST"}:
        if method == "POST" and not isinstance(payload, dict):
            return _error(HTTPStatus.BAD_REQUEST, "invalid_request", "Expected a JSON object.")
        result = pipeline_handler({**(payload or {}), "type": "configure_translation" if method == "POST" else "translation_settings"})
        return (HTTPStatus.OK if result.get("status") == "ok" else HTTPStatus.CONFLICT), result

    if method == "GET" and path == "/v1/health":
        result = pipeline_handler({"type": "ping", "request_id": "http-health"})
        return HTTPStatus.OK, result

    if method == "POST" and path == "/v1/sessions/clear":
        with _cache_clear_lock:
            clear_result_cache()
        return HTTPStatus.OK, {"status": "ok", "type": "session_cleared"}

    if method == "POST" and path == "/v1/caches/clear":
        with _cache_clear_lock:
            clear_all_caches()
        return HTTPStatus.OK, {"status": "ok", "type": "persistent_cache_cleared"}

    if method == "POST" and path == "/v1/images/translate":
        if not isinstance(payload, dict):
            return _error(HTTPStatus.BAD_REQUEST, "invalid_request", "Expected a JSON object.")

        required = ("request_id", "session_id", "image_id", "image_base64")
        missing = [key for key in required if not isinstance(payload.get(key), str)]
        if missing:
            return _error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                f"Missing string field(s): {', '.join(missing)}",
            )
        if payload.get("priority", "visible") not in {"visible", "prefetch", "P0", "P1", "P2", "P3"}:
            return _error(HTTPStatus.BAD_REQUEST, "invalid_priority", "priority must be visible, prefetch, or P0-P3.")

        message = {
            "type": "translate",
            "request_id": payload["request_id"],
            "image_id": payload["image_id"],
            "priority": payload.get("priority", "visible"),
            "image_base64": payload["image_base64"],
            "series": str(payload.get("series", "")),
            "context": payload.get("context", []),
        }
        result = pipeline_handler(message)
        result["image_id"] = payload["image_id"]
        result["cached"] = bool(result.get("cache_hit", False))
        status = HTTPStatus.OK if result.get("status") == "ok" else HTTPStatus.UNPROCESSABLE_ENTITY
        return status, result

    if method == "POST" and path.startswith("/v1/requests/"):
        parts = path.split("/")
        if len(parts) != 5 or parts[4] not in {"cancel", "reprioritize"}:
            return _error(HTTPStatus.NOT_FOUND, "not_found", "Unknown request control endpoint.")
        request_id, action = parts[3], parts[4]
        try:
            changed = cancel_handler(request_id) if action == "cancel" else reprioritize_handler(
                request_id, payload.get("priority") if isinstance(payload, dict) else None
            )
        except ValueError as error:
            return _error(HTTPStatus.BAD_REQUEST, "invalid_priority", str(error))
        return HTTPStatus.OK, {"status": "ok", "request_id": request_id, "changed": changed}

    if method == "POST" and path == "/v1/images/translate-batch":
        if not isinstance(payload, dict):
            return _error(HTTPStatus.BAD_REQUEST, "invalid_request", "Expected a JSON object.")
        required = ("request_id", "session_id")
        missing = [key for key in required if not isinstance(payload.get(key), str)]
        images = payload.get("images")
        if missing or not isinstance(images, list):
            return _error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Batch requires request_id, session_id, and an images array.",
            )
        if not 1 <= len(images) <= 3:
            return _error(
                HTTPStatus.BAD_REQUEST,
                "invalid_batch",
                "A batch must contain between one and three images.",
            )
        result = batch_handler(
            {
                "request_id": payload["request_id"],
                "priority": payload.get("priority", "visible"),
                "images": images,
                "series": str(payload.get("series", "")),
                "context": payload.get("context", []),
                "max_image_width": payload.get("max_image_width", 1600),
            }
        )
        status = HTTPStatus.OK if result.get("status") == "ok" else HTTPStatus.UNPROCESSABLE_ENTITY
        return status, result

    return _error(HTTPStatus.NOT_FOUND, "not_found", "Unknown PanelLens endpoint.")


def _error(status: HTTPStatus, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"status": "error", "error": {"code": code, "message": message}}


class PanelLensRequestHandler(BaseHTTPRequestHandler):
    server_version = "PanelLensBrowser/1"

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self._write_json(*_error(HTTPStatus.FORBIDDEN, "origin_rejected", "Caller origin is not allowed."))
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            self._write_json(*_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Invalid local session token."))
            return
        self._dispatch(None)

    def do_POST(self) -> None:
        if not self._authorized():
            self._write_json(*_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Invalid local session token."))
            return
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length or "0")
        except ValueError:
            self._write_json(*_error(HTTPStatus.BAD_REQUEST, "invalid_length", "Invalid Content-Length."))
            return
        if length < 0:
            self._write_json(*_error(HTTPStatus.BAD_REQUEST, "invalid_length", "Invalid Content-Length."))
            return
        if length > MAX_REQUEST_BYTES:
            self._write_json(*_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Image request is too large."))
            return
        try:
            payload = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_json(*_error(HTTPStatus.BAD_REQUEST, "invalid_json", "Request body is not valid JSON."))
            return
        self._dispatch(payload)

    def _dispatch(self, payload: Any) -> None:
        status, response = route_request(self.command, self.path, payload)
        self._write_json(status, response)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PanelLens-Token, X-PanelLens-Page-Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        return origin_allowed(origin)

    def _authorized(self) -> bool:
        return request_authorized(
            self.headers.get("Origin", ""),
            self.headers.get("X-PanelLens-Token", ""),
        )

    def log_message(self, format: str, *args: object) -> None:
        logging.info("HTTP %s", format % args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    sys.stderr.write(f"PanelLens local session token: {SESSION_TOKEN}\n")
    sys.stderr.flush()
    threading.Thread(
        target=_warm_translation_model,
        name="translation-model-warmup",
        daemon=True,
    ).start()
    server = ThreadingHTTPServer((HOST, args.port), PanelLensRequestHandler)
    logging.info("PanelLens browser service listening on http://%s:%d", HOST, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
