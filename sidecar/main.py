"""PanelLens sidecar entry point.

Stdout is reserved for newline-delimited JSON IPC. Diagnostics must be written
to stderr or a log file.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import Any

from PIL import Image

from bubble_filter import filter_dialogue_regions
from ocr_pipeline import recognize_korean
from translation_pipeline import (
    TranslationError,
    translation_runtime_status,
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


def clear_result_cache() -> None:
    """Clear the process-local screenshot cache for a new reading session."""
    _result_cache.clear()


def handle(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
    translation_handler: Any = translate_korean_regions,
    runtime_handler: Any = translation_runtime_status,
) -> dict[str, Any]:
    request_id = message.get("request_id")
    message_type = message.get("type")

    if message_type == "ping":
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "pong",
            "runtime": runtime_handler(),
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
                context = _translation_context(message.get("context"))
                try:
                    regions = translation_handler(regions, series, context)
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


def handle_batch(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
    translation_handler: Any = translate_korean_regions,
) -> dict[str, Any]:
    """OCR up to three ordered images and translate their regions together."""
    request_id = message.get("request_id")
    raw_images = message.get("images")
    if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= 3:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "error",
            "error": {
                "code": "invalid_batch",
                "message": "images must contain between one and three items.",
            },
        }

    started = time.perf_counter()
    series = str(message.get("series", ""))
    try:
        max_image_width = max(
            800,
            min(2400, int(message.get("max_image_width", 1600))),
        )
    except (TypeError, ValueError):
        return _batch_error(
            request_id,
            "invalid_image_size",
            "max_image_width must be an integer.",
        )
    use_result_cache = (
        ocr_handler is recognize_korean
        and bubble_handler is filter_dialogue_regions
        and translation_handler is translate_korean_regions
    )
    entries: list[dict[str, Any]] = []
    uncached_entries: list[dict[str, Any]] = []

    for raw_image in raw_images:
        if not isinstance(raw_image, dict) or not isinstance(
            raw_image.get("image_id"), str
        ):
            return _batch_error(
                request_id,
                "invalid_batch",
                "Every image requires a string image_id.",
            )
        try:
            image_bytes = base64.b64decode(
                raw_image.get("image_base64", ""),
                validate=True,
            )
        except (ValueError, TypeError) as error:
            return _batch_error(
                request_id,
                "invalid_image",
                f"image_base64 is invalid: {error}",
            )
        if not image_bytes:
            return _batch_error(
                request_id,
                "invalid_image",
                "Batched images cannot be empty.",
            )

        key = _cache_key(image_bytes, series)
        cached_result = _cached_result(key) if use_result_cache else None
        entry = {
            "image_id": raw_image["image_id"],
            "image_bytes": image_bytes,
            "cache_key": key,
            "cached": cached_result is not None,
            "ocr_processing_time_ms": 0,
            "regions": [],
            "detected_text_count": 0,
            "filtered_text_count": 0,
            "coordinate_scale": 1.0,
        }
        entries.append(entry)
        if cached_result is not None:
            entry.update(cached_result)
            continue

        ocr_started = time.perf_counter()
        try:
            processing_bytes, coordinate_scale = _resize_for_ocr(
                image_bytes,
                max_image_width,
            )
            entry["coordinate_scale"] = coordinate_scale
            regions = ocr_handler(processing_bytes)
            detected_text_count = len(regions)
            regions, filtered_text_count = bubble_handler(processing_bytes, regions)
        except Exception as error:
            logging.exception("Batched OCR failed for request %s", request_id)
            return _batch_error(request_id, "ocr_failed", str(error))
        entry.update(
            {
                "regions": regions,
                "detected_text_count": detected_text_count,
                "filtered_text_count": filtered_text_count,
                "ocr_processing_time_ms": round(
                    (time.perf_counter() - ocr_started) * 1000
                ),
            }
        )
        uncached_entries.append(entry)

    flattened_regions = [
        region
        for entry in uncached_entries
        for region in entry["regions"]
    ]
    translation_processing_time_ms = 0
    if flattened_regions:
        translation_started = time.perf_counter()
        try:
            translated = translation_handler(
                flattened_regions,
                series,
                _translation_context(message.get("context")),
            )
        except TranslationError as error:
            logging.exception("Batched translation failed for request %s", request_id)
            return _batch_error(request_id, error.code, str(error))
        translation_processing_time_ms = round(
            (time.perf_counter() - translation_started) * 1000
        )
        cursor = 0
        for entry in uncached_entries:
            region_count = len(entry["regions"])
            entry["regions"] = translated[cursor : cursor + region_count]
            cursor += region_count

    for entry in uncached_entries:
        scale = float(entry["coordinate_scale"])
        if scale == 1.0:
            continue
        for region in entry["regions"]:
            region["bbox"] = [round(float(value) * scale, 1) for value in region["bbox"]]

    response_images = []
    for entry in entries:
        if use_result_cache and not entry["cached"]:
            _store_cached_result(
                entry["cache_key"],
                entry["regions"],
                entry["detected_text_count"],
                entry["filtered_text_count"],
            )
        response_images.append(
            {
                "image_id": entry["image_id"],
                "cached": entry["cached"],
                "regions": entry["regions"],
                "ocr_processing_time_ms": entry["ocr_processing_time_ms"],
                "detected_text_count": entry["detected_text_count"],
                "filtered_text_count": entry["filtered_text_count"],
                "received_image_bytes": len(entry["image_bytes"]),
            }
        )

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "ok",
        "type": "translation_batch",
        "images": response_images,
        "translation_processing_time_ms": translation_processing_time_ms,
        "processing_time_ms": round((time.perf_counter() - started) * 1000),
    }


def _batch_error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "error",
        "error": {"code": code, "message": message},
    }


def _resize_for_ocr(image_bytes: bytes, max_width: int) -> tuple[bytes, float]:
    """Downscale unusually wide images and return the coordinate multiplier."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.width <= max_width:
            return image_bytes, 1.0
        scale = max_width / source.width
        resized = source.convert("RGB").resize(
            (max_width, max(1, round(source.height * scale))),
            Image.Resampling.LANCZOS,
        )
        output = io.BytesIO()
        resized.save(output, format="PNG", optimize=True)
        return output.getvalue(), source.width / max_width


def _translation_context(value: Any) -> list[dict[str, str]]:
    """Validate and bound optional rolling context from the native app."""
    if not isinstance(value, list):
        return []

    context: list[dict[str, str]] = []
    for item in value[-20:]:
        if not isinstance(item, dict):
            continue
        korean = str(item.get("korean", "")).strip()[:500]
        english = str(item.get("english", "")).strip()[:1000]
        if korean and english:
            context.append({"korean": korean, "english": english})
    return context


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
