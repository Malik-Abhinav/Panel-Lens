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


class TranslationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
            return _attach_translations(regions, translations)
        except TranslationError as error:
            last_error = error
            if error.code not in {"invalid_translation_response"}:
                raise

    assert last_error is not None
    raise last_error


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
(for example, 선생님 in a hospital usually means doctor). Do not add explanations.

Korean blocks:
{numbered_text}

Return one JSON object in exactly this shape:
{{
  "translations": [
    {{
      "index": 0,
      "translation": "Natural English text",
      "tone": "neutral",
      "confidence": 0.95
    }}
  ]
}}

Return exactly one item for every input index."""


def _request_translations(
    prompt: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0.2},
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
        if (
            not isinstance(item, dict)
            or item.get("index") != expected_index
            or not isinstance(item.get("translation"), str)
            or not item["translation"].strip()
        ):
            raise TranslationError(
                "invalid_translation_response",
                f"Translation item {expected_index} is invalid.",
            )

        confidence = item.get("confidence", 0.75)
        if not isinstance(confidence, (int, float)):
            confidence = 0.75

        normalized.append(
            {
                "index": expected_index,
                "translation": item["translation"].strip(),
                "tone": str(item.get("tone", "neutral")),
                "confidence": max(0.0, min(1.0, float(confidence))),
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
