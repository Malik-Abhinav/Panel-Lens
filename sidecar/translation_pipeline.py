"""Context-aware Korean-to-English translation through local Ollama."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any


OLLAMA_BASE_URL = os.environ.get(
    "PANELLENS_OLLAMA_URL", "http://127.0.0.1:11434"
)
OLLAMA_MODEL = os.environ.get("PANELLENS_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_KEEP_ALIVE = os.environ.get("PANELLENS_OLLAMA_KEEP_ALIVE", "30m")
TRANSLATION_CACHE_SIZE = max(
    1, int(os.environ.get("PANELLENS_TRANSLATION_CACHE_SIZE", "64"))
)
TRANSLATION_PROMPT_VERSION = "2026-07-29.page-v1"
_translation_cache: OrderedDict[
    tuple[str, str, tuple[str, ...]],
    list[dict[str, Any]],
] = OrderedDict()


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

    cache_key = (
        TRANSLATION_PROMPT_VERSION,
        series.strip(),
        tuple(
            re.sub(r"\s+", "", str(region["original"]))
            for region in regions
        ),
    )
    cached = _translation_cache.get(cache_key)
    if cached is not None:
        _translation_cache.move_to_end(cache_key)
        return _attach_translations(regions, cached)

    prompt = _build_page_prompt(regions, series)
    try:
        translations = _request_translations(prompt, len(regions))
    except TranslationError as error:
        if error.code != "invalid_translation_response":
            raise
        translations = _request_translations(prompt, len(regions))

    for index, region in enumerate(regions):
        source = str(region["original"])
        vocative_name = _extract_vocative_name(source)
        if vocative_name:
            translations[index] = {
                "index": index,
                "translation": f"{_romanize_korean_name(vocative_name)}.",
                "tone": "neutral",
                "confidence": 0.95,
            }
            continue

        translations[index]["translation"] = _normalize_translation(
            translations[index]["translation"]
        )

    for index, region in enumerate(regions):
        source = str(region["original"])
        problems = _translation_problems(
            regions,
            translations,
            index,
        )
        if not problems:
            translations[index]["index"] = index
            continue

        repaired = _repair_translation(
            regions,
            translations[index]["translation"],
            index,
            series,
            problems,
        )
        repaired["translation"] = _normalize_translation(
            repaired["translation"]
        )
        candidate_translations = [
            dict(translation) for translation in translations
        ]
        candidate_translations[index] = repaired
        remaining_problems = _translation_problems(
            regions,
            candidate_translations,
            index,
        )
        if remaining_problems:
            translations[index] = {
                "index": index,
                "translation": source,
                "tone": "untranslated",
                "confidence": 0.0,
            }
        else:
            repaired["index"] = index
            translations[index] = repaired

    _translation_cache[cache_key] = [
        dict(translation) for translation in translations
    ]
    _translation_cache.move_to_end(cache_key)
    while len(_translation_cache) > TRANSLATION_CACHE_SIZE:
        _translation_cache.popitem(last=False)
    return _attach_translations(regions, translations)


def _looks_like_name_vocative(source: str) -> bool:
    return _extract_vocative_name(source) is not None


def _extract_vocative_name(source: str) -> str | None:
    compact = re.sub(r"[\s.!?,~…]+", "", source)
    match = re.fullmatch(r"([가-힣]{2,4})[아야]", compact)
    return match.group(1) if match else None


_HANGUL_INITIALS = (
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp",
    "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
)
_HANGUL_VOWELS = (
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o",
    "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu",
    "eu", "ui", "i",
)
_HANGUL_FINALS = (
    "", "k", "k", "ks", "n", "nj", "nh", "t", "l", "lk",
    "lm", "lb", "ls", "lt", "lp", "lh", "m", "p", "ps", "t",
    "t", "ng", "t", "t", "k", "t", "p", "h",
)
_COMMON_SURNAMES = {
    "김": "Kim",
    "이": "Lee",
    "박": "Park",
    "최": "Choi",
    "정": "Jeong",
    "강": "Kang",
    "조": "Jo",
    "윤": "Yun",
    "장": "Jang",
    "임": "Im",
    "한": "Han",
    "오": "Oh",
    "서": "Seo",
    "신": "Shin",
    "권": "Kwon",
    "황": "Hwang",
    "안": "Ahn",
    "송": "Song",
    "전": "Jeon",
    "홍": "Hong",
}


def _romanize_hangul_syllable(character: str) -> str:
    offset = ord(character) - 0xAC00
    if not 0 <= offset < 11172:
        return character
    initial = offset // 588
    vowel = (offset % 588) // 28
    final = offset % 28
    return (
        _HANGUL_INITIALS[initial]
        + _HANGUL_VOWELS[vowel]
        + _HANGUL_FINALS[final]
    )


def _romanize_korean_name(name: str) -> str:
    syllables = [_romanize_hangul_syllable(char) for char in name]
    if len(name) == 3 and name[0] in _COMMON_SURNAMES:
        return f"{_COMMON_SURNAMES[name[0]]} {syllables[1].capitalize()}-{syllables[2]}"
    return "-".join(syllables).capitalize()


def _normalize_translation(translation: str) -> str:
    """Apply formatting-only normalization, never phrase-specific rewrites."""
    return re.sub(r"\s+", " ", translation).strip()


def _translation_problems(
    regions: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    target_index: int,
) -> list[str]:
    source = str(regions[target_index]["original"])
    translation = str(translations[target_index].get("translation", "")).strip()
    problems: list[str] = []

    if not translation:
        problems.append("The translation is empty.")
        return problems

    if re.search(r"[가-힣]", translation):
        problems.append("The English output contains untranslated Hangul.")

    source_hangul = len(re.findall(r"[가-힣]", source))
    english_words = len(
        re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", translation)
    )
    if source_hangul <= 5 and english_words > max(8, source_hangul * 3):
        problems.append(
            "The short source expanded into implausibly long English."
        )

    if _looks_like_name_vocative(source) and english_words > 4:
        problems.append(
            "A name-only direct address contains unsupported dialogue."
        )

    normalized_translation = re.sub(
        r"[^a-z0-9]+",
        " ",
        translation.casefold(),
    ).strip()
    if english_words >= 4 and normalized_translation:
        for index, other in enumerate(translations):
            if index == target_index:
                continue
            other_source = re.sub(
                r"\s+",
                "",
                str(regions[index]["original"]),
            )
            this_source = re.sub(r"\s+", "", source)
            other_translation = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(other.get("translation", "")).casefold(),
            ).strip()
            if (
                this_source != other_source
                and normalized_translation == other_translation
            ):
                problems.append(
                    "This output duplicates a different source block."
                )
                break

    return problems


def _format_page_blocks(regions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[id={index} type={region.get('region_type', 'unknown')}] "
        f"{region['original']}"
        for index, region in enumerate(regions)
    )


def _build_page_prompt(
    regions: list[dict[str, Any]],
    series: str,
) -> str:
    series_context = series.strip() or "Unknown series"
    return f"""You are an expert Korean-to-English comics translator.
Translate every OCR block from one visible manhwa page in reading order.

Series: {series_context}

Requirements:
- Return exactly one concise English string per input ID, in the same order.
- Use the entire page only to resolve genuine context and sentence continuity.
- Translate each block only from words supported by that block. Never copy,
  repeat, or import actions, locations, names, or pronouns from another block.
- Preserve names, quantities, negation, subject/object direction, politeness,
  slang strength, and tone.
- Preserve fragments as fragments when a sentence continues across blocks.
- Korean often omits its subject. Do not invent I/he/she/they when the page does
  not establish one; prefer a natural subject-neutral fragment.
- Romanize Korean personal names consistently. Do not leave Hangul in English
  output. If an official spelling is unknown, use standard romanization.
- A block containing only a name with vocative -아/-야 only calls that person;
  do not expand it into surrounding dialogue.
- OCR may contain spacing or syllable errors. Correct only when grammar and page
  context make the intended Korean clear.
- Do not add explanations or translator notes.

Page blocks:
{_format_page_blocks(regions)}

Return one JSON object with a "translations" array containing exactly
{len(regions)} English strings in ID order."""


def _repair_translation(
    regions: list[dict[str, Any]],
    rejected_translation: str,
    target_index: int,
    series: str,
    problems: list[str],
) -> dict[str, Any]:
    source = str(regions[target_index]["original"])
    prompt = f"""You are repairing one Korean-to-English comics translation.

Series: {series.strip() or "Unknown series"}

Page context:
{_format_page_blocks(regions)}

Target ID: {target_index}
Target Korean: {source}
Rejected English: {rejected_translation}
Validation problems:
{chr(10).join(f"- {problem}" for problem in problems)}

Translate only the target Korean. Context may clarify references, but do not
borrow words or meaning from other IDs. Preserve names, quantities, negation,
tone, subject/object direction, and incomplete sentence structure. Do not
invent an omitted subject. Romanize names and output no Hangul.

Return one JSON object with a "translations" array containing exactly one
concise English string."""
    try:
        return _request_translations(prompt, 1)[0]
    except TranslationError as error:
        if error.code != "invalid_translation_response":
            raise
        return {
            "index": target_index,
            "translation": "",
            "tone": "untranslated",
            "confidence": 0.0,
        }


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
            "temperature": 0.0,
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
