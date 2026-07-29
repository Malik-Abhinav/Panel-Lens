"""PanelLens sidecar entry point.

Stdout is reserved for newline-delimited JSON IPC. Diagnostics must be written
to stderr or a log file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import base64
from typing import Any


PROTOCOL_VERSION = 1
logging.basicConfig(
    filename=os.environ.get("PANELLENS_SIDECAR_LOG", "/tmp/panellens-sidecar.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> dict[str, Any]:
    request_id = message.get("request_id")
    message_type = message.get("type")

    if message_type == "ping":
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "pong",
        }

    if message_type == "translate":
        encoded_image = message.get("image_base64", "")
        try:
            image_bytes = base64.b64decode(encoded_image, validate=True)
        except (ValueError, TypeError) as error:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "status": "error",
                "error": {
                    "code": "invalid_image",
                    "message": f"image_base64 is invalid: {error}",
                },
            }

        logging.info(
            "Received translation request %s with %d image bytes",
            request_id,
            len(image_bytes),
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "translation",
            "regions": [
                {
                    "bbox": [120, 160, 240, 100],
                    "original": "테스트 대사",
                    "translation": "Test dialogue",
                    "language": "ko",
                    "confidence": 0.99,
                }
            ],
            "processing_time_ms": 1,
            "received_image_bytes": len(image_bytes),
        }

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "error",
        "error": {
            "code": "unsupported_message",
            "message": f"Unsupported message type: {message_type!r}",
        },
    }


def main() -> None:
    logging.info("PanelLens sidecar started with protocol version %s", PROTOCOL_VERSION)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("The message must be a JSON object")
            respond(handle(message))
        except (json.JSONDecodeError, ValueError) as error:
            logging.warning("Invalid IPC message: %s", error)
            respond(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": None,
                    "status": "error",
                    "error": {
                        "code": "invalid_message",
                        "message": str(error),
                    },
                }
            )


if __name__ == "__main__":
    main()
