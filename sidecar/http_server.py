"""Loopback-only HTTP adapter for the browser-extension feasibility spike."""

from __future__ import annotations

import argparse
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from main import _warm_translation_model, clear_result_cache, handle, handle_batch

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 60 * 1024 * 1024
_translation_lock = threading.Lock()


def route_request(
    method: str,
    path: str,
    payload: Any = None,
    pipeline_handler: Callable[[dict[str, Any]], dict[str, Any]] = handle,
    batch_handler: Callable[[dict[str, Any]], dict[str, Any]] = handle_batch,
) -> tuple[int, dict[str, Any]]:
    """Route one decoded HTTP request without coupling tests to a socket."""
    if method == "GET" and path == "/v1/health":
        result = pipeline_handler({"type": "ping", "request_id": "http-health"})
        return HTTPStatus.OK, result

    if method == "POST" and path == "/v1/sessions/clear":
        with _translation_lock:
            clear_result_cache()
        return HTTPStatus.OK, {"status": "ok", "type": "session_cleared"}

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
        if payload.get("priority", "visible") not in {"visible", "prefetch"}:
            return _error(HTTPStatus.BAD_REQUEST, "invalid_priority", "priority must be visible or prefetch.")

        message = {
            "type": "translate",
            "request_id": payload["request_id"],
            "image_base64": payload["image_base64"],
            "series": str(payload.get("series", "")),
            "context": payload.get("context", []),
        }
        with _translation_lock:
            result = pipeline_handler(message)
        result["image_id"] = payload["image_id"]
        result["cached"] = bool(result.get("cache_hit", False))
        status = HTTPStatus.OK if result.get("status") == "ok" else HTTPStatus.UNPROCESSABLE_ENTITY
        return status, result

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
        with _translation_lock:
            result = batch_handler(
                {
                    "request_id": payload["request_id"],
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
    server_version = "PanelLensSpike/1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch(None)

    def do_POST(self) -> None:
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length or "0")
        except ValueError:
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, format: str, *args: object) -> None:
        logging.info("HTTP %s", format % args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    threading.Thread(
        target=_warm_translation_model,
        name="translation-model-warmup",
        daemon=True,
    ).start()
    server = ThreadingHTTPServer((HOST, args.port), PanelLensRequestHandler)
    logging.info("PanelLens spike server listening on http://%s:%d", HOST, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
