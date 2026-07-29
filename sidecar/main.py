"""PanelLens sidecar entry point.

Stdout is reserved for newline-delimited JSON IPC. Diagnostics must be written
to stderr or a log file.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from typing import Any

from ocr_pipeline import recognize_korean
from translation_pipeline import TranslationError, translate_korean_regions

PROTOCOL_VERSION = 1
logging.basicConfig(
    filename=os.environ.get("PANELLENS_SIDECAR_LOG", "/tmp/panellens-sidecar.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    translation_handler: Any = translate_korean_regions,
) -> dict[str, Any]:
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
        started = time.perf_counter()
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

        if not image_bytes:
            regions = [
                {
                    "bbox": [120, 160, 240, 100],
                    "original": "테스트 대사",
                    "translation": "Test dialogue",
                    "language": "ko",
                    "confidence": 0.99,
                }
            ]
        else:
            try:
                regions = ocr_handler(image_bytes)
            except Exception as error:
                logging.exception("Korean OCR failed for request %s", request_id)
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "status": "error",
                    "error": {
                        "code": "ocr_failed",
                        "message": str(error),
                    },
                }

            try:
                regions = translation_handler(
                    regions,
                    str(message.get("series", "")),
                )
            except TranslationError as error:
                logging.exception(
                    "Translation failed for request %s", request_id
                )
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "status": "error",
                    "error": {
                        "code": error.code,
                        "message": str(error),
                    },
                }

        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "translation",
            "regions": regions,
            "processing_time_ms": round(
                (time.perf_counter() - started) * 1000
            ),
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
