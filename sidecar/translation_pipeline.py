"""Context-aware Korean-to-English translation through local Ollama."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


OLLAMA_BASE_URL = os.environ.get(
    "PANELLENS_OLLAMA_URL", "http://127.0.0.1:11434"
)
OLLAMA_MODEL = os.environ.get("PANELLENS_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_KEEP_ALIVE = os.environ.get("PANELLENS_OLLAMA_KEEP_ALIVE", "30m")


class TranslationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def warm_translation_model() -> bool:
    """Ask Ollama to load the model without generating any text."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": "",
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90):
            return True
    except (urllib.error.URLError, TimeoutError):
        return False


def translate_korean_regions(
    regions: list[dict[str, Any]],
    series: str = "",
) -> list[dict[str, Any]]:
    if not regions:
        return []

    prompt = _build_prompt(regions, series)
    last_error: TranslationError | None = None

    for _ in range(2):
        try:
            translations = _request_translations(prompt, len(regions))
            translations = _recover_missing_translations(
                regions,
                translations,
                series,
            )
            return _attach_translations(regions, translations)
        except TranslationError as error:
            last_error = error
            if error.code not in {"invalid_translation_response"}:
                raise

    assert last_error is not None
    raise last_error


def _recover_missing_translations(
    regions: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    series: str,
) -> list[dict[str, Any]]:
    missing_indexes = [
        index
        for index, translation in enumerate(translations)
        if not translation["translation"]
    ]
    if not missing_indexes:
        return translations

    missing_regions = [regions[index] for index in missing_indexes]
    try:
        recovered = _request_translations(
            _build_prompt(missing_regions, series),
            len(missing_regions),
        )
    except TranslationError:
        recovered = []

    repaired = [dict(translation) for translation in translations]
    for recovery_index, original_index in enumerate(missing_indexes):
        if (
            recovery_index < len(recovered)
            and recovered[recovery_index]["translation"]
        ):
            repaired[original_index] = recovered[recovery_index]
        else:
            repaired[original_index] = {
                "index": original_index,
                "translation": str(regions[original_index]["original"]),
                "tone": "untranslated",
                "confidence": 0.0,
            }

    return repaired


def _build_prompt(regions: list[dict[str, Any]], series: str) -> str:
    numbered_text = "\n".join(
        f"{index}. {region['original']}"
        for index, region in enumerate(regions)
    )
    series_context = series.strip() or "Unknown series"

    return f"""You are an expert Korean manhwa translator. Translate dialogue and
narration into natural, concise English that fits speech bubbles.

Series: {series_context}

Translate every numbered Korean block below in order. Preserve character names,
tone, honorific intent, and continuity between adjacent blocks. Resolve words
from scene context rather than using their most literal dictionary meaning
(for example, 선생님 in a hospital usually means doctor). Translate signs and
sound effects briefly rather than skipping them. Never combine adjacent indexes.
Do not add explanations.

Context glossary when applicable:
- 담당 선생님 or 선생님 in a hospital: attending doctor or doctor
- 입원 수속: hospital admission procedure
- 수납 in a hospital or store: payment or billing
- 저벅: a footstep sound, translated as "step"

Korean blocks:
{numbered_text}

Return one JSON object with a "translations" array containing exactly
{len(regions)} English strings, one for every input index in the same order."""


def _request_translations(
    prompt: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    response_schema = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "minItems": expected_count,
                "maxItems": expected_count,
                "items": {"type": "string"},
            }
        },
        "required": ["translations"],
    }
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": response_schema,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "messages": [{"role": "user", "content": prompt}],
        "options": {
            "temperature": 0.2,
            "num_ctx": 4096,
            "num_predict": max(96, min(512, expected_count * 64)),
        },
    }
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            envelope = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if error.code == 404:
            raise TranslationError(
                "ollama_model_missing",
                f"Ollama model {OLLAMA_MODEL!r} is not installed.",
            ) from error
        raise TranslationError(
            "ollama_request_failed",
            f"Ollama returned HTTP {error.code}: {body[:200]}",
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise TranslationError(
            "ollama_offline",
            "Ollama is not reachable. Start it with `ollama serve`.",
        ) from error
    except json.JSONDecodeError as error:
        raise TranslationError(
            "invalid_translation_response",
            "Ollama returned an invalid HTTP JSON response.",
        ) from error

    try:
        content = envelope["message"]["content"]
        result = json.loads(content)
        translations = result["translations"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise TranslationError(
            "invalid_translation_response",
            "Ollama returned malformed translation JSON.",
        ) from error

    if not isinstance(translations, list) or len(translations) != expected_count:
        raise TranslationError(
            "invalid_translation_response",
            f"Expected {expected_count} translations, received "
            f"{len(translations) if isinstance(translations, list) else 0}.",
        )

    normalized = []
    for expected_index, item in enumerate(translations):
        if not isinstance(item, str):
            raise TranslationError(
                "invalid_translation_response",
                f"Translation item {expected_index} is invalid.",
            )

        normalized.append(
            {
                "index": expected_index,
                "translation": item.strip(),
                "tone": "neutral",
                "confidence": 0.8 if item.strip() else 0.0,
            }
        )

    return normalized


def _attach_translations(
    regions: list[dict[str, Any]],
    translations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    translated_regions = []
    for region, translation in zip(regions, translations, strict=True):
        translated_region = dict(region)
        translated_region["translation"] = translation["translation"]
        translated_region["tone"] = translation["tone"]
        translated_region["translation_confidence"] = translation["confidence"]
        translated_regions.append(translated_region)
    return translated_regions
