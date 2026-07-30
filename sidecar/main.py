"""PanelLens sidecar entry point.

Stdout is reserved for newline-delimited JSON IPC. Diagnostics must be written
to stderr or a log file.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import Any

from bubble_filter import filter_dialogue_regions
from ocr_pipeline import recognize_korean
from translation_pipeline import (
    TranslationError,
    translate_korean_regions,
    warm_translation_model,
)

PROTOCOL_VERSION = 1
RESULT_CACHE_SIZE = max(
    1, int(os.environ.get("PANELLENS_RESULT_CACHE_SIZE", "8"))
)
_result_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
logging.basicConfig(
    filename=os.environ.get("PANELLENS_SIDECAR_LOG", "/tmp/panellens-sidecar.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cache_key(image_bytes: bytes, series: str) -> str:
    digest = hashlib.sha256()
    digest.update(image_bytes)
    digest.update(b"\0")
    digest.update(series.strip().encode("utf-8"))
    return digest.hexdigest()


def _cached_result(key: str) -> dict[str, Any] | None:
    result = _result_cache.get(key)
    if result is None:
        return None

    _result_cache.move_to_end(key)
    return copy.deepcopy(result)


def _store_cached_result(
    key: str,
    regions: list[dict[str, Any]],
    detected_text_count: int,
    filtered_text_count: int,
) -> None:
    _result_cache[key] = {
        "regions": copy.deepcopy(regions),
        "detected_text_count": detected_text_count,
        "filtered_text_count": filtered_text_count,
    }
    _result_cache.move_to_end(key)
    while len(_result_cache) > RESULT_CACHE_SIZE:
        _result_cache.popitem(last=False)


def handle(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
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
        ocr_processing_time_ms = 0
        translation_processing_time_ms = 0
        cache_hit = False
        detected_text_count = 0
        filtered_text_count = 0
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
            detected_text_count = len(regions)
        else:
            series = str(message.get("series", ""))
            use_result_cache = (
                ocr_handler is recognize_korean
                and bubble_handler is filter_dialogue_regions
                and translation_handler is translate_korean_regions
            )
            key = _cache_key(image_bytes, series)
            cached_result = _cached_result(key) if use_result_cache else None
            cache_hit = cached_result is not None

            if cached_result is not None:
                regions = cached_result["regions"]
                detected_text_count = cached_result["detected_text_count"]
                filtered_text_count = cached_result["filtered_text_count"]
            else:
                ocr_started = time.perf_counter()
                try:
                    regions = ocr_handler(image_bytes)
                except Exception as error:
                    logging.exception(
                        "Korean OCR failed for request %s", request_id
                    )
                    return {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "status": "error",
                        "error": {
                            "code": "ocr_failed",
                            "message": str(error),
                        },
                    }
                detected_text_count = len(regions)
                regions, filtered_text_count = bubble_handler(
                    image_bytes,
                    regions,
                )
                ocr_processing_time_ms = round(
                    (time.perf_counter() - ocr_started) * 1000
                )

                translation_started = time.perf_counter()
                try:
                    regions = translation_handler(regions, series)
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
                translation_processing_time_ms = round(
                    (time.perf_counter() - translation_started) * 1000
                )

                if use_result_cache:
                    _store_cached_result(
                        key,
                        regions,
                        detected_text_count,
                        filtered_text_count,
                    )

        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "translation",
            "regions": regions,
            "processing_time_ms": round(
                (time.perf_counter() - started) * 1000
            ),
            "ocr_processing_time_ms": ocr_processing_time_ms,
            "translation_processing_time_ms": translation_processing_time_ms,
            "cache_hit": cache_hit,
            "detected_text_count": detected_text_count,
            "filtered_text_count": filtered_text_count,
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
    threading.Thread(
        target=_warm_translation_model,
        name="translation-model-warmup",
        daemon=True,
    ).start()
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


def _warm_translation_model() -> None:
    started = time.perf_counter()
    if warm_translation_model():
        logging.info(
            "Translation model warmed in %.1f seconds",
            time.perf_counter() - started,
        )
    else:
        logging.info("Translation model warm-up skipped because Ollama is unavailable")


if __name__ == "__main__":
    main()
