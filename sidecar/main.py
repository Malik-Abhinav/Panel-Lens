"""PanelLens sidecar entry point.

Stdout is reserved for newline-delimited JSON IPC. Diagnostics must be written
to stderr or a log file.
"""

from __future__ import annotations

import atexit
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
from performance_instrumentation import bind_request, current_context, emit
from pipeline_scheduler import JobPriority, JobState, PipelineJob, PipelineScheduler
from persistent_cache import default_cache, ocr_cache_key, translation_cache_key
from translation_settings import pipeline_operation, configure, describe
from translation_pipeline import (
    OLLAMA_MODEL,
    TRANSLATION_ADAPTER,
    TRANSLATION_PROMPT_VERSION,
    TranslationError,
    _active_adapter,
    translation_runtime_status,
    translation_runtime_identity,
    translation_runtime_capabilities,
    translate_korean_regions,
    warm_translation_model,
    translation_model_state,
)

PROTOCOL_VERSION = 1
RESULT_CACHE_SIZE = max(
    1, int(os.environ.get("PANELLENS_RESULT_CACHE_SIZE", "8"))
)
_result_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_result_cache_lock = threading.RLock()
_scheduler_lock = threading.Lock()
_pipeline_scheduler: PipelineScheduler | None = None
_request_jobs: dict[str, PipelineJob] = {}
_request_jobs_lock = threading.RLock()
_persistent_cache = None
_persistent_cache_lock = threading.Lock()

OCR_CONFIG = {
    "model": "PP-OCRv5_mobile_det+korean_PP-OCRv5_mobile_rec",
    "model_version": "PP-OCRv5",
    "preprocessing_version": "rgb-natural-v1",
    "resizing_policy": "single-image-natural-size",
    "filtering_version": "dialogue-filter-v1",
    "configuration": {"language": "korean", "minimum_confidence": 0.4},
}


def persistent_cache():
    global _persistent_cache
    with _persistent_cache_lock:
        if _persistent_cache is None:
            _persistent_cache = default_cache()
        return _persistent_cache


def set_persistent_cache(cache) -> None:
    """Replace the cache instance for tests or an explicitly configured service."""
    global _persistent_cache
    with _persistent_cache_lock:
        _persistent_cache = cache
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
    digest.update(b"\0")
    digest.update(
        json.dumps(
            translation_runtime_identity(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _cached_result(key: str) -> dict[str, Any] | None:
    with _result_cache_lock:
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
    with _result_cache_lock:
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
    with _result_cache_lock:
        _result_cache.clear()


def clear_all_caches() -> None:
    clear_result_cache()
    persistent_cache().clear(persistent=True)


def _translation_cache_config(series: str, context: list[dict[str, str]]) -> dict[str, Any]:
    identity = translation_runtime_identity()
    return {
        "source_language": "ko",
        "target_language": "en",
        "series_identity": series.strip(),
        "glossary": [],
        "context_policy": "rolling-pairs-v1" if context else "context-free-v1",
        "rolling_context": context,
        "model": identity["model"],
        "model_version": identity["model_version"],
        "artifact_sha256": identity["artifact_sha256"],
        "model_repository_sha": identity["model_repository_sha"],
        "runtime": identity["runtime"],
        "prompt_version": identity["prompt_version"],
        "adapter_config_version": f"{TRANSLATION_ADAPTER}:{_active_adapter()}",
    }


def _ocr_with_split_cache(image_bytes, ocr_handler, bubble_handler, sources):
    key = ocr_cache_key(image_bytes, OCR_CONFIG)
    emit("cache_lookup", cache_level="ocr")
    cached, source = persistent_cache().get("ocr", key)
    sources["ocr"] = source
    emit("cache_hit" if cached else "cache_miss", cache_level=source)
    if cached is not None:
        return cached
    result = _scheduled_ocr(image_bytes, ocr_handler, bubble_handler)
    persistent_cache().put("ocr", key, result)
    emit("cache_store", cache_level="disk_ocr")
    return result


def _translation_with_split_cache(ocr, series, context, translation_handler, sources):
    config = _translation_cache_config(series, context)
    key = translation_cache_key(ocr["regions"], config)
    emit("cache_lookup", cache_level="translation")
    cached, source = persistent_cache().get("translation", key)
    sources["translation"] = source
    emit(
        "cache_hit" if cached else "cache_miss",
        cache_level=source,
        cache_status="hit" if cached else "miss",
        **translation_runtime_identity(),
    )
    if cached is not None:
        return {**ocr, "regions": cached["regions"]}
    regions = translation_handler(ocr["regions"], series, context)
    if not any(region.get("translation_error") for region in regions):
        persistent_cache().put("translation", key, {"regions": regions})
        emit("cache_store", cache_level="disk_translation")
    return {**ocr, "regions": regions}


def pipeline_scheduler() -> PipelineScheduler:
    global _pipeline_scheduler
    with _scheduler_lock:
        if _pipeline_scheduler is None:
            _pipeline_scheduler = PipelineScheduler()
        return _pipeline_scheduler


def shutdown_pipeline_scheduler() -> None:
    global _pipeline_scheduler
    with _scheduler_lock:
        scheduler = _pipeline_scheduler
        _pipeline_scheduler = None
    if scheduler is not None:
        scheduler.shutdown()


def reprioritize_request(request_id: str, priority: Any) -> bool:
    with _request_jobs_lock:
        job = _request_jobs.get(request_id)
    return bool(job and pipeline_scheduler().reprioritize(job.job_id, priority))


def cancel_request(request_id: str) -> bool:
    with _request_jobs_lock:
        job = _request_jobs.get(request_id)
    return bool(job and pipeline_scheduler().cancel(job.job_id, "browser_viewport_obsolete"))


atexit.register(shutdown_pipeline_scheduler)


def _scheduled_ocr(
    image_bytes: bytes,
    ocr_handler: Any,
    bubble_handler: Any,
) -> dict[str, Any]:
    regions = ocr_handler(image_bytes)
    detected_text_count = len(regions)
    filter_started = time.monotonic_ns()
    regions, filtered_text_count = bubble_handler(image_bytes, regions)
    emit(
        "filter_end",
        duration_ms=round((time.monotonic_ns() - filter_started) / 1_000_000, 3),
        filtered_text_count=filtered_text_count,
        region_count=len(regions),
    )
    return {
        "regions": regions,
        "detected_text_count": detected_text_count,
        "filtered_text_count": filtered_text_count,
    }


@pipeline_operation
def handle(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
    translation_handler: Any = translate_korean_regions,
    runtime_handler: Any = translation_runtime_status,
) -> dict[str, Any]:
    """Bind request correlation once so every nested stage uses the same IDs."""
    with bind_request(
        message.get("request_id"),
        message.get("image_id") or message.get("request_id"),
        message.get("priority", "visible"),
        translation_model_state(),
    ):
        result = _handle(
            message,
            ocr_handler,
            bubble_handler,
            translation_handler,
            runtime_handler,
        )
        context = current_context()
        if context is not None and context.metrics:
            result["performance"] = dict(context.metrics)
        return result


def _handle(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
    translation_handler: Any = translate_korean_regions,
    runtime_handler: Any = translation_runtime_status,
) -> dict[str, Any]:
    request_id = message.get("request_id")
    message_type = message.get("type")

    if message_type in {"translation_settings", "configure_translation"}:
        try:
            if message_type == "configure_translation":
                configure(message)
            return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id,
                    "status": "ok", "type": "translation_settings", "settings": describe()}
        except (ValueError, OSError) as error:
            return {"status": "error", "request_id": request_id,
                    "error": {"code": "translation_configuration_failed", "message": str(error)}}

    if message_type == "ping":
        from browser_bridge import status as bridge_status
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "pong",
            "runtime": runtime_handler(),
            "browser_bridge": bridge_status(),
            "translation_identity": translation_runtime_identity(),
        }

    if message_type == "translate":
        started = time.monotonic_ns()
        ocr_processing_time_ms = 0
        translation_processing_time_ms = 0
        cache_hit = False
        detected_text_count = 0
        filtered_text_count = 0
        encoded_image = message.get("image_base64", "")
        decode_started = time.monotonic_ns()
        emit("decode_start", byte_size=len(encoded_image))
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
        emit(
            "decode_end",
            duration_ms=round((time.monotonic_ns() - decode_started) / 1_000_000, 3),
            byte_size=len(image_bytes),
        )

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
            use_split_cache = (
                ocr_handler is recognize_korean
                and bubble_handler is filter_dialogue_regions
                and translation_handler is translate_korean_regions
            )
            hash_started = time.monotonic_ns()
            emit("hash_start", byte_size=len(image_bytes), cache_level="content_sha256")
            key = hashlib.sha256(image_bytes).hexdigest()
            emit(
                "hash_end",
                duration_ms=round((time.monotonic_ns() - hash_started) / 1_000_000, 3),
                byte_size=len(image_bytes),
                cache_level="content_sha256",
            )
            context = _translation_context(message.get("context"))
            cache_sources = {"ocr": "miss", "translation": "miss"}
            priority = JobPriority.parse(message.get("priority", "visible"))
            job = PipelineJob(
                request_id=str(request_id or ""),
                image_id=str(message.get("image_id") or request_id or ""),
                priority=priority,
                image_bytes=image_bytes,
                ocr_work=lambda _: _ocr_with_split_cache(
                    image_bytes, ocr_handler, bubble_handler, cache_sources
                ) if use_split_cache else _scheduled_ocr(image_bytes, ocr_handler, bubble_handler),
                translation_work=lambda _, ocr: _translation_with_split_cache(
                    ocr, series, context, translation_handler, cache_sources
                ) if use_split_cache else {
                    **ocr, "regions": translation_handler(ocr["regions"], series, context)
                },
                model_state=translation_model_state(),
            )
            with _request_jobs_lock:
                _request_jobs[str(request_id or "")] = job
            try:
                pipeline_scheduler().submit(job)
                pipeline_result = job.wait()
            except Exception as error:
                logging.exception("Pipeline failed for request %s", request_id)
                error_code = (
                    error.code
                    if isinstance(error, TranslationError)
                    else "ocr_failed"
                    if job.failed_stage == "ocr"
                    else "pipeline_failed"
                )
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "status": "error",
                    "error": {"code": error_code, "message": str(error)},
                }
            finally:
                with _request_jobs_lock:
                    _request_jobs.pop(str(request_id or ""), None)
            if job.state == JobState.CANCELLED:
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "image_id": message.get("image_id") or request_id,
                    "status": "error",
                    "error": {"code": "cancelled", "message": "Request became obsolete."},
                }
            regions = pipeline_result["regions"]
            detected_text_count = pipeline_result["detected_text_count"]
            filtered_text_count = pipeline_result["filtered_text_count"]
            ocr_processing_time_ms = round(job.ocr_duration_ms)
            translation_processing_time_ms = round(job.translation_duration_ms)
            cache_hit = cache_sources["translation"] != "miss"

        result = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "image_id": message.get("image_id") or request_id,
            "model_state": translation_model_state(),
            "status": "ok",
            "type": "translation",
            "regions": regions,
            "processing_time_ms": round(
                (time.monotonic_ns() - started) / 1_000_000
            ),
            "ocr_processing_time_ms": ocr_processing_time_ms,
            "translation_processing_time_ms": translation_processing_time_ms,
            "cache_hit": cache_hit,
            "cache_sources": cache_sources if image_bytes else {"ocr": "miss", "translation": "miss"},
            "detected_text_count": detected_text_count,
            "filtered_text_count": filtered_text_count,
            "received_image_bytes": len(image_bytes),
            "content_hash": hashlib.sha256(image_bytes).hexdigest(),
            "translation_runtime": translation_runtime_identity(),
            "translation_capabilities": translation_runtime_capabilities(),
            "context_count": len(context) if image_bytes else 0,
        }
        emit(
            "response_received",
            duration_ms=result["processing_time_ms"],
            byte_size=len(image_bytes),
            cache_level=cache_sources.get("translation", "miss") if image_bytes else "miss",
        )
        return result

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "error",
        "error": {
            "code": "unsupported_message",
            "message": f"Unsupported message type: {message_type!r}",
        },
    }


@pipeline_operation
def handle_batch(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
    translation_handler: Any = translate_korean_regions,
) -> dict[str, Any]:
    raw_images = message.get("images")
    image_ids = (
        [str(item.get("image_id", "")) for item in raw_images if isinstance(item, dict)]
        if isinstance(raw_images, list)
        else []
    )
    with bind_request(
        message.get("request_id"),
        ",".join(image_ids),
        message.get("priority", "visible"),
        translation_model_state(),
    ):
        result = _handle_batch(
            message,
            ocr_handler,
            bubble_handler,
            translation_handler,
        )
        context = current_context()
        if context is not None and context.metrics:
            result["performance"] = dict(context.metrics)
        return result


def _handle_batch(
    message: dict[str, Any],
    ocr_handler: Any = recognize_korean,
    bubble_handler: Any = filter_dialogue_regions,
    translation_handler: Any = translate_korean_regions,
) -> dict[str, Any]:
    """Pipeline up to three ordered images while preserving response order."""
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
        image_id = raw_image["image_id"]
        decode_started = time.monotonic_ns()
        emit(
            "decode_start",
            request_id=request_id,
            image_id=image_id,
            byte_size=len(str(raw_image.get("image_base64", ""))),
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
        emit(
            "decode_end",
            request_id=request_id,
            image_id=image_id,
            duration_ms=round((time.monotonic_ns() - decode_started) / 1_000_000, 3),
            byte_size=len(image_bytes),
        )

        hash_started = time.monotonic_ns()
        emit(
            "hash_start",
            request_id=request_id,
            image_id=image_id,
            cache_level="result_lru",
            byte_size=len(image_bytes),
        )
        key = _cache_key(image_bytes, series)
        emit(
            "hash_end",
            request_id=request_id,
            image_id=image_id,
            cache_level="result_lru",
            duration_ms=round((time.monotonic_ns() - hash_started) / 1_000_000, 3),
        )
        emit(
            "cache_lookup",
            request_id=request_id,
            image_id=image_id,
            cache_level="result_lru",
        )
        cached_result = _cached_result(key) if use_result_cache else None
        emit(
            "cache_hit" if cached_result is not None else "cache_miss",
            request_id=request_id,
            image_id=image_id,
            cache_level="result_lru",
        )
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

        try:
            with bind_request(
                request_id,
                image_id,
                message.get("priority", "visible"),
                translation_model_state(),
            ):
                preprocess_started = time.monotonic_ns()
                emit("preprocess_start", byte_size=len(image_bytes))
                processing_bytes, coordinate_scale = _resize_for_ocr(
                    image_bytes,
                    max_image_width,
                )
                emit(
                    "preprocess_end",
                    duration_ms=round(
                        (time.monotonic_ns() - preprocess_started) / 1_000_000, 3
                    ),
                    byte_size=len(processing_bytes),
                    coordinate_scale=coordinate_scale,
                )
        except Exception as error:
            logging.exception("Batched preprocessing failed for request %s", request_id)
            return _batch_error(request_id, "ocr_failed", str(error))
        entry["coordinate_scale"] = coordinate_scale
        context = _translation_context(message.get("context"))
        job = PipelineJob(
            request_id=str(request_id or ""),
            image_id=image_id,
            priority=JobPriority.parse(message.get("priority", "visible")),
            image_bytes=processing_bytes,
            ocr_work=lambda _, data=processing_bytes: _scheduled_ocr(
                data, ocr_handler, bubble_handler
            ),
            translation_work=lambda _, ocr, page_context=context: {
                **ocr,
                "regions": translation_handler(
                    ocr["regions"], series, page_context
                ),
            },
            model_state=translation_model_state(),
        )
        entry["job"] = job
        uncached_entries.append(entry)
        pipeline_scheduler().submit(job)

    translation_processing_time_ms = 0
    for entry in uncached_entries:
        job = entry["job"]
        try:
            pipeline_result = job.wait()
        except Exception as error:
            logging.exception("Batched pipeline failed for request %s", request_id)
            code = (
                error.code
                if isinstance(error, TranslationError)
                else "ocr_failed"
                if job.failed_stage == "ocr"
                else "pipeline_failed"
            )
            return _batch_error(request_id, code, str(error))
        entry.update(pipeline_result)
        entry["ocr_processing_time_ms"] = round(job.ocr_duration_ms)
        translation_processing_time_ms += round(job.translation_duration_ms)

    for entry in uncached_entries:
        scale = float(entry["coordinate_scale"])
        if scale == 1.0:
            continue
        for region in entry["regions"]:
            region["bbox"] = [round(float(value) * scale, 1) for value in region["bbox"]]

    response_images = []
    for entry in entries:
        if (
            use_result_cache
            and not entry["cached"]
            and not any(region.get("translation_error") for region in entry["regions"])
        ):
            _store_cached_result(
                entry["cache_key"],
                entry["regions"],
                entry["detected_text_count"],
                entry["filtered_text_count"],
            )
            emit(
                "cache_store",
                request_id=request_id,
                image_id=entry["image_id"],
                cache_level="result_lru",
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
        "model_state": translation_model_state(),
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
    # http_server imports main; alias the CLI module to preserve one scheduler,
    # model instance, and cache for both native IPC and extension requests.
    sys.modules.setdefault("main", sys.modules[__name__])
    if os.environ.get("PANELLENS_BROWSER_BRIDGE") == "1":
        from browser_bridge import start
        start()
    logging.info("PanelLens sidecar started with protocol version %s", PROTOCOL_VERSION)
    if os.environ.get("PANELLENS_DEFER_WARMUP") != "1":
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


@pipeline_operation
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
