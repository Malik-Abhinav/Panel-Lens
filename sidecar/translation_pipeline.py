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
OLLAMA_MODEL = os.environ.get("PANELLENS_OLLAMA_MODEL", "hy-mt2:7b")
TRANSLATION_ADAPTER = os.environ.get(
    "PANELLENS_TRANSLATION_ADAPTER", "auto"
)
OLLAMA_KEEP_ALIVE = os.environ.get("PANELLENS_OLLAMA_KEEP_ALIVE", "30m")
TRANSLATION_CACHE_SIZE = max(
    1, int(os.environ.get("PANELLENS_TRANSLATION_CACHE_SIZE", "64"))
)
TRANSLATION_CONTEXT_SIZE = 20
TRANSLATION_CONTEXT_CHARACTER_BUDGET = 6000
TRANSLATION_PROMPT_VERSION = "2026-07-31.rolling-context-v1"
_translation_cache: OrderedDict[
    tuple[str, str, str, str, tuple[tuple[str, str], ...], tuple[str, ...]],
    list[dict[str, Any]],
] = OrderedDict()


class TranslationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def translation_runtime_status() -> dict[str, Any]:
    """Report whether Ollama and the configured local model are available."""
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/tags",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {
            "ready": False,
            "code": "ollama_offline",
            "model": OLLAMA_MODEL,
            "message": "Ollama is not running. Start Ollama, then retry.",
        }

    installed = {
        str(item.get("name") or item.get("model") or "")
        for item in payload.get("models", [])
        if isinstance(item, dict)
    }
    if OLLAMA_MODEL not in installed:
        return {
            "ready": False,
            "code": "model_missing",
            "model": OLLAMA_MODEL,
            "message": (
                f"The local model {OLLAMA_MODEL} is not installed. "
                "Install it in Ollama, then retry."
            ),
        }

    return {
        "ready": True,
        "code": "ready",
        "model": OLLAMA_MODEL,
        "message": f"Local OCR and {OLLAMA_MODEL} are ready.",
    }


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
    context: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not regions:
        return []

    bounded_context = _bounded_context(context)
    cache_key = (
        TRANSLATION_PROMPT_VERSION,
        OLLAMA_MODEL,
        _active_adapter(),
        series.strip(),
        tuple(
            (item["korean"], item["english"])
            for item in bounded_context
        ),
        tuple(
            re.sub(r"\s+", "", str(region["original"]))
            for region in regions
        ),
    )
    cached = _translation_cache.get(cache_key)
    if cached is not None:
        _translation_cache.move_to_end(cache_key)
        return _attach_translations(regions, cached)

    try:
        translations = _request_page_translations(
            regions,
            series,
            bounded_context,
        )
    except TranslationError as error:
        if error.code != "invalid_translation_response":
            raise
        translations = _request_page_translations(
            regions,
            series,
            bounded_context,
        )

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
            bounded_context,
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

    if re.search(r"[\u3400-\u4DBF\u4E00-\u9FFF가-힣]", translation):
        problems.append(
            "The English output contains untranslated Korean or CJK text."
        )

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


def _bounded_context(
    context: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    if not context:
        return []

    newest_first: list[dict[str, str]] = []
    used_characters = 0
    for item in reversed(context[-TRANSLATION_CONTEXT_SIZE:]):
        if not isinstance(item, dict):
            continue
        korean = str(item.get("korean", "")).strip()[:500]
        english = str(item.get("english", "")).strip()[:1000]
        item_size = len(korean) + len(english)
        if not korean or not english:
            continue
        if (
            used_characters
            and used_characters + item_size
            > TRANSLATION_CONTEXT_CHARACTER_BUDGET
        ):
            break
        newest_first.append({"korean": korean, "english": english})
        used_characters += item_size
    return list(reversed(newest_first))


def _format_previous_context(context: list[dict[str, str]]) -> str:
    if not context:
        return "(none)"
    return "\n".join(
        f"[previous {index + 1}] Korean: {item['korean']}\n"
        f"[previous {index + 1}] English: {item['english']}"
        for index, item in enumerate(context)
    )


def _active_adapter(
    model: str | None = None,
    configured: str | None = None,
) -> str:
    selected_model = (model or OLLAMA_MODEL).casefold()
    selected_adapter = (configured or TRANSLATION_ADAPTER).casefold()
    if selected_adapter == "auto":
        return (
            "hy-mt2"
            if selected_model.startswith(("hy-mt2", "hymt2"))
            else "panelens-json"
        )
    if selected_adapter not in {"hy-mt2", "panelens-json"}:
        raise TranslationError(
            "invalid_translation_adapter",
            f"Unsupported translation adapter {selected_adapter!r}.",
        )
    return selected_adapter


def _request_page_translations(
    regions: list[dict[str, Any]],
    series: str,
    context: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if _active_adapter() == "hy-mt2":
        return _request_hymt_page(regions, series, context)
    return _request_translations(
        _build_page_prompt(regions, series, context),
        len(regions),
    )


def _build_hymt_page_prompt(
    regions: list[dict[str, Any]],
    series: str,
    context: list[dict[str, str]] | None = None,
) -> str:
    series_line = (
        f"The series is {series.strip()}.\n" if series.strip() else ""
    )
    blocks = "\n".join(
        f"[{index + 1}] {region['original']}"
        for index, region in enumerate(regions)
    )
    previous_context = _format_previous_context(_bounded_context(context))
    return (
        "Translate the following numbered Korean comic text blocks into "
        "natural English. The blocks appear in reading order on the same "
        "visible page, so use neighboring blocks only to resolve context and "
        "sentence continuity.\n"
        f"{series_line}"
        "Previous translated context is reference only. Use it to resolve "
        "names, omitted subjects, pronouns, terminology, and continuity. "
        "Never output or translate a previous block again.\n\n"
        "Previous translated context:\n"
        f"{previous_context}\n\n"
        "Preserve every [number] exactly and output one concise translation "
        "per block. Preserve names, quantities, negation, pronouns, sentence "
        "fragments, politeness, slang strength, and tone. Translate the "
        "intended meaning of dialect rather than transliterating dialect "
        "words. Do not invent or omit information. Output only the numbered "
        "English translations without explanations.\n\n"
        "Current blocks to translate:\n"
        f"{blocks}"
    )


def _parse_numbered_translations(
    output: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    stripped_output = output.strip()
    if (
        expected_count == 1
        and stripped_output
        and not re.match(r"^\[\d+\]", stripped_output)
    ):
        return [
            {
                "index": 0,
                "translation": stripped_output,
                "tone": "neutral",
                "confidence": 0.8,
            }
        ]

    translations: dict[int, list[str]] = {}
    current_index: int | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^\[(\d+)\]\s*(.*)$", line)
        if match:
            current_index = int(match.group(1)) - 1
            if current_index in translations:
                raise TranslationError(
                    "invalid_translation_response",
                    "The translation response repeated a numbered block.",
                )
            translations[current_index] = [match.group(2).strip()]
        elif current_index is not None:
            translations[current_index].append(line)
        else:
            raise TranslationError(
                "invalid_translation_response",
                "The translation response did not preserve numbered blocks.",
            )

    if set(translations) != set(range(expected_count)):
        raise TranslationError(
            "invalid_translation_response",
            f"Expected {expected_count} numbered translations, received "
            f"{len(translations)}.",
        )

    normalized = []
    for index in range(expected_count):
        translation = " ".join(translations[index]).strip()
        if not translation:
            raise TranslationError(
                "invalid_translation_response",
                f"Translation item {index} is empty.",
            )
        normalized.append(
            {
                "index": index,
                "translation": translation,
                "tone": "neutral",
                "confidence": 0.8,
            }
        )
    return normalized


def _request_hymt_page(
    regions: list[dict[str, Any]],
    series: str,
    context: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    content = _request_hymt_content(
        _build_hymt_page_prompt(regions, series, context),
        max_tokens=max(96, min(512, len(regions) * 64)),
    )
    return _parse_numbered_translations(content, len(regions))


def _request_hymt_content(prompt: str, max_tokens: int) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "messages": [{"role": "user", "content": prompt}],
        "options": {
            "temperature": 0.7,
            "top_p": 0.6,
            "top_k": 20,
            "repeat_penalty": 1.05,
            "num_ctx": 4096,
            "num_predict": max_tokens,
        },
    }
    envelope = _send_ollama_chat(payload)
    try:
        content = str(envelope["message"]["content"]).strip()
    except (KeyError, TypeError) as error:
        raise TranslationError(
            "invalid_translation_response",
            "Ollama returned malformed translation output.",
        ) from error
    if not content:
        raise TranslationError(
            "invalid_translation_response",
            "Ollama returned an empty translation response.",
        )
    return content


def _build_page_prompt(
    regions: list[dict[str, Any]],
    series: str,
    context: list[dict[str, str]] | None = None,
) -> str:
    series_context = series.strip() or "Unknown series"
    previous_context = _format_previous_context(_bounded_context(context))
    return f"""You are an expert Korean-to-English comics translator.
Translate every OCR block from one visible manhwa page in reading order.

Series: {series_context}

Previous translated context (reference only; never output these blocks):
{previous_context}

Current page blocks to translate:
{_format_page_blocks(regions)}

Requirements:
- Return exactly one concise English string per input ID, in the same order.
- Use the previous context and current page only to resolve genuine context,
  names, omitted subjects, pronouns, terminology, and sentence continuity.
- Never return a translation for a previous context block.
- Translate each block only from words supported by that block. Never copy,
  repeat, or import actions, locations, names, or pronouns from another block.
- Preserve names, quantities, negation, subject/object direction, politeness,
  slang strength, and tone.
- Preserve fragments as fragments when a sentence continues across blocks.
- Korean often omits its subject. Do not invent I/he/she/they when the page does
  not establish one; prefer a natural subject-neutral fragment.
- Romanize Korean personal names consistently. Do not leave Korean, Chinese, or
  other CJK characters in English output. If an official spelling is unknown,
  use standard romanization.
- A block containing only a name with vocative -아/-야 only calls that person;
  do not expand it into surrounding dialogue.
- OCR may contain spacing or syllable errors. Correct only when grammar and page
  context make the intended Korean clear.
- Do not add explanations or translator notes.

Return one JSON object with a "translations" array containing exactly
{len(regions)} English strings in ID order."""


def _repair_translation(
    regions: list[dict[str, Any]],
    rejected_translation: str,
    target_index: int,
    series: str,
    problems: list[str],
    context: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    source = str(regions[target_index]["original"])
    repair_context = f"""You are repairing one Korean-to-English comics translation.

Series: {series.strip() or "Unknown series"}

Previous translated context (reference only):
{_format_previous_context(_bounded_context(context))}

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
invent an omitted subject. Romanize names and output no Korean or CJK text.
"""
    if _active_adapter() == "hy-mt2":
        try:
            translation = _request_hymt_content(
                repair_context
                + "\nOutput only the corrected English translation without "
                "a number, explanation, or translator note.",
                max_tokens=128,
            )
        except TranslationError as error:
            if error.code != "invalid_translation_response":
                raise
            translation = ""
        return {
            "index": target_index,
            "translation": translation,
            "tone": "neutral" if translation else "untranslated",
            "confidence": 0.8 if translation else 0.0,
        }

    prompt = repair_context + """
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
    model: str | None = None,
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
        "model": model or OLLAMA_MODEL,
        "stream": False,
        "think": False,
        "format": response_schema,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "messages": [{"role": "user", "content": prompt}],
        "options": {
            "temperature": 0.0,
            "num_ctx": 4096,
            "num_predict": max(96, min(512, expected_count * 64)),
        },
    }
    envelope = _send_ollama_chat(payload)

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


def _send_ollama_chat(payload: dict[str, Any]) -> dict[str, Any]:
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
                f"Ollama model {payload['model']!r} is not installed.",
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
    if not isinstance(envelope, dict):
        raise TranslationError(
            "invalid_translation_response",
            "Ollama returned an invalid response envelope.",
        )
    return envelope


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
