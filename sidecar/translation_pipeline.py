"""Context-aware Korean-to-English translation through local Ollama."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict
import threading
from typing import Any

from performance_instrumentation import add_metrics, emit, set_model_state
from translation_settings import saved_settings


OLLAMA_BASE_URL = os.environ.get(
    "PANELLENS_OLLAMA_URL", "http://127.0.0.1:11434"
)
OLLAMA_MODEL = os.environ.get("PANELLENS_OLLAMA_MODEL", saved_settings().get("model", ""))
TRANSLATION_ADAPTER = os.environ.get(
    "PANELLENS_TRANSLATION_ADAPTER", "auto"
)
OLLAMA_KEEP_ALIVE = os.environ.get("PANELLENS_OLLAMA_KEEP_ALIVE", "30m")
TRANSLATION_RUNTIME = "ollama"
TRANSLATION_CACHE_SIZE = max(
    1, int(os.environ.get("PANELLENS_TRANSLATION_CACHE_SIZE", "64"))
)
TRANSLATION_CONTEXT_SIZE = 20
TRANSLATION_CONTEXT_CHARACTER_BUDGET = 6000
TRANSLATION_PROMPT_VERSION = "2026-08-08.series-glossary-v3"
_translation_cache: OrderedDict[
    tuple[str, str, str, str, tuple[tuple[str, str], ...], tuple[str, ...]],
    list[dict[str, Any]],
] = OrderedDict()
_model_condition = threading.Condition()
_model_state = "cold"


class TranslationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def translation_runtime_identity() -> dict[str, Any]:
    return {
        "model": OLLAMA_MODEL,
        "model_version": OLLAMA_MODEL,
        "artifact_sha256": "",
        "model_repository_sha": "",
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "runtime": "ollama",
    }


def translation_runtime_capabilities() -> dict[str, Any]:
    return {"supports_multi_region_generation": True, "max_concurrent_generations": 1}


def translation_runtime_status() -> dict[str, Any]:
    """Report whether Ollama and the configured local model are available."""
    if not OLLAMA_MODEL:
        return {
            "ready": False,
            "code": "model_unselected",
            "model": "",
            "message": "Choose a model installed in Ollama in Browser Setup & Models.",
        }
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
    global _model_state
    if not OLLAMA_MODEL:
        return False
    with _model_condition:
        if _model_state == "warm":
            return True
        if _model_state == "loading":
            _model_condition.wait_for(lambda: _model_state != "loading", timeout=90)
            return _model_state == "warm"
        _model_state = "loading"
    set_model_state("loading")
    emit("translation_queue_enter", model=OLLAMA_MODEL, model_state="loading")
    started = time.monotonic_ns()
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

    succeeded = False
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            envelope = json.load(response)
            if isinstance(envelope, dict):
                metrics = ollama_metrics_from_envelope(envelope)
                add_metrics(**metrics)
                emit(
                    "translation_model_metrics",
                    model=OLLAMA_MODEL,
                    model_state="loading",
                    warmup=True,
                    **metrics,
                )
            succeeded = True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        succeeded = False
    finally:
        with _model_condition:
            _model_state = "warm" if succeeded else "cold"
            _model_condition.notify_all()
        set_model_state(_model_state)
        emit(
            "translation_end",
            duration_ms=round((time.monotonic_ns() - started) / 1_000_000, 3),
            model=OLLAMA_MODEL,
            model_state=_model_state,
            warmup=True,
        )
    return succeeded


def translation_model_state() -> str:
    with _model_condition:
        return _model_state


def _ensure_translation_model_ready() -> str:
    """Wait for background warm-up, or perform it once if it has not started."""
    if not OLLAMA_MODEL:
        raise TranslationError("model_unselected", "Choose an installed Ollama model in Browser Setup & Models.")
    initial_state = translation_model_state()
    if initial_state != "warm":
        warm_translation_model()
    state = translation_model_state()
    set_model_state(state)
    return state


def translate_korean_regions(
    regions: list[dict[str, Any]],
    series: str = "",
    context: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not regions:
        return []

    _ensure_translation_model_ready()

    bounded_context = _bounded_context(context)
    identity = translation_runtime_identity()
    cache_key = (
        identity["prompt_version"],
        identity["model"],
        identity["model_version"],
        identity["artifact_sha256"],
        identity["model_repository_sha"],
        identity["runtime"],
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
    emit("cache_lookup", cache_level="translation_lru", **identity)
    cached = _translation_cache.get(cache_key)
    if cached is not None:
        emit("cache_hit", cache_level="translation_lru", cache_status="hit", **identity)
        _translation_cache.move_to_end(cache_key)
        return _attach_translations(regions, cached)
    emit("cache_miss", cache_level="translation_lru", cache_status="miss", **identity)

    try:
        translations = _request_page_translations(regions, series, bounded_context)
    except TranslationError as error:
        if error.code != "invalid_translation_response":
            raise
        translations = _request_page_translations(regions, series, bounded_context)

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
        if translations[index].get("translation_error"):
            continue
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

    if not any(item.get("translation_error") for item in translations):
        _translation_cache[cache_key] = [
            dict(translation) for translation in translations
        ]
        _translation_cache.move_to_end(cache_key)
        while len(_translation_cache) > TRANSLATION_CACHE_SIZE:
            _translation_cache.popitem(last=False)
        emit("cache_store", cache_level="translation_lru")
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
    lines = []
    previous_index = 0
    for item in context:
        if item["korean"].casefold().startswith("[glossary] "):
            korean = item["korean"][len("[glossary] ") :]
            lines.append(
                f"[series glossary] Korean: {korean}\n"
                f"[series glossary] Required English: {item['english']}"
            )
            continue
        previous_index += 1
        lines.append(
            f"[previous {previous_index}] Korean: {item['korean']}\n"
            f"[previous {previous_index}] English: {item['english']}"
        )
    return "\n".join(lines)


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
        "words. Korean often omits subjects and gender. Never guess he, she, "
        "or a speaker identity unless the current text or established context "
        "supports it; prefer natural gender-neutral wording. Reuse an "
        "established romanized name and every series-glossary spelling "
        "exactly. Parenthetical gender in the glossary is metadata only; do "
        "not output the parenthetical text. Do not invent or omit "
        "information. Output only the numbered "
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
- Reuse every series-glossary spelling exactly. Parenthetical gender in a
  glossary entry is metadata only and must not appear in translated dialogue.
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
    metrics = ollama_metrics_from_envelope(envelope)
    add_metrics(**metrics)
    emit("translation_first_token", measured=False, reason="ollama_non_streaming")
    emit(
        "translation_model_metrics",
        model=str(payload.get("model", OLLAMA_MODEL)),
        model_state=translation_model_state(),
        **metrics,
    )
    return envelope


def ollama_metrics_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Normalize Ollama nanosecond counters without retaining generated content."""
    def milliseconds(key: str) -> float | None:
        value = envelope.get(key)
        if not isinstance(value, (int, float)):
            return None
        return round(float(value) / 1_000_000, 3)

    prompt_tokens = envelope.get("prompt_eval_count")
    generated_tokens = envelope.get("eval_count")
    generation_duration = envelope.get("eval_duration")
    tokens_per_second = None
    if (
        isinstance(generated_tokens, (int, float))
        and isinstance(generation_duration, (int, float))
        and generation_duration > 0
    ):
        tokens_per_second = round(
            float(generated_tokens) / (float(generation_duration) / 1_000_000_000),
            3,
        )
    return {
        "ollama_load_duration_ms": milliseconds("load_duration"),
        "ollama_prompt_eval_duration_ms": milliseconds("prompt_eval_duration"),
        "ollama_generation_duration_ms": milliseconds("eval_duration"),
        "ollama_total_duration_ms": milliseconds("total_duration"),
        "ollama_prompt_token_count": prompt_tokens
        if isinstance(prompt_tokens, int)
        else None,
        "ollama_generated_token_count": generated_tokens
        if isinstance(generated_tokens, int)
        else None,
        "ollama_tokens_per_second": tokens_per_second,
    }


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
        for field in (
            "generation_latency_ms",
            "output_tokens",
            "translation_error",
        ):
            if field in translation:
                translated_region[field] = translation[field]
        translated_regions.append(translated_region)
    return translated_regions
