"""Compare Ollama models on PanelLens Korean page translation fixtures."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = PROJECT_ROOT / "sidecar"
sys.path.insert(0, str(SIDECAR_ROOT))

from translation_pipeline import OLLAMA_BASE_URL  # noqa: E402
from translation_pipeline import OLLAMA_MODEL  # noqa: E402
from translation_pipeline import _build_page_prompt  # noqa: E402
from translation_pipeline import _request_translations  # noqa: E402
from translation_pipeline import _translation_problems  # noqa: E402


DEFAULT_ANNOTATIONS = (
    PROJECT_ROOT / "evaluation" / "korean" / "annotations"
)
DEFAULT_REGRESSIONS = (
    PROJECT_ROOT / "evaluation" / "korean" / "translation-regressions.json"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiments" / "results" / "translation"


@dataclass(frozen=True)
class TranslationCase:
    page_id: str
    series_alias: str
    categories: tuple[str, ...]
    regions: tuple[dict[str, Any], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark one or more Ollama models on human-reviewed Korean "
            "translation fixtures."
        )
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help=(
            "Ollama model to evaluate. Repeat for multiple models. "
            f"Defaults to {OLLAMA_MODEL!r}."
        ),
    )
    parser.add_argument(
        "--source",
        choices=("annotations", "regressions", "all"),
        default="all",
        help="Which fixture collection to run.",
    )
    parser.add_argument(
        "--adapter",
        choices=("auto", "panelens", "translategemma", "hy-mt2"),
        default="auto",
        help=(
            "Prompt/output adapter. Auto selects TranslateGemma's official "
            "direct-translation prompts for TranslateGemma and "
            "Tencent Hy-MT2 models."
        ),
    )
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--regressions", type=Path, default=DEFAULT_REGRESSIONS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-warmup", action="store_true")
    return parser.parse_args()


def load_annotation_cases(directory: Path) -> list[TranslationCase]:
    cases: list[TranslationCase] = []
    for path in sorted(directory.glob("ko_*.json")):
        payload = json.loads(path.read_text())
        regions = []
        region_types: set[str] = set()
        for region in sorted(
            payload["regions"],
            key=lambda item: item["reading_order"],
        ):
            region_type = str(region["region_type"])
            region_types.add(region_type)
            regions.append(
                {
                    "original": region["source_text"],
                    "region_type": region_type,
                    "references": _references(region),
                }
            )
        cases.append(
            TranslationCase(
                page_id=path.stem,
                series_alias=str(payload["image"]["series_alias"]),
                categories=("annotated", *sorted(region_types)),
                regions=tuple(regions),
            )
        )
    return cases


def load_regression_cases(path: Path) -> list[TranslationCase]:
    payload = json.loads(path.read_text())
    cases = []
    for page in payload["pages"]:
        regions = [
            {
                "original": region["source_text"],
                "region_type": region["region_type"],
                "references": _references(region),
            }
            for region in page["regions"]
        ]
        cases.append(
            TranslationCase(
                page_id=str(page["id"]),
                series_alias=str(page["series_alias"]),
                categories=tuple(str(item) for item in page["category"]),
                regions=tuple(regions),
            )
        )
    return cases


def _references(region: dict[str, Any]) -> list[str]:
    references = [str(region["acceptable_english"])]
    references.extend(
        str(item) for item in region.get("alternate_english", [])
    )
    return references


def normalize_english(text: str) -> str:
    return re.sub(r"[^a-z0-9']+", " ", text.casefold()).strip()


def token_f1(prediction: str, reference: str) -> float:
    predicted_tokens = normalize_english(prediction).split()
    reference_tokens = normalize_english(reference).split()
    if not predicted_tokens or not reference_tokens:
        return float(predicted_tokens == reference_tokens)

    remaining = list(reference_tokens)
    overlap = 0
    for token in predicted_tokens:
        if token in remaining:
            overlap += 1
            remaining.remove(token)
    precision = overlap / len(predicted_tokens)
    recall = overlap / len(reference_tokens)
    if not precision or not recall:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def reference_proxies(
    prediction: str,
    references: list[str],
) -> dict[str, float]:
    normalized_prediction = normalize_english(prediction)
    sequence_scores = [
        SequenceMatcher(
            None,
            normalized_prediction,
            normalize_english(reference),
        ).ratio()
        for reference in references
    ]
    token_scores = [
        token_f1(prediction, reference) for reference in references
    ]
    return {
        "sequence_similarity": round(max(sequence_scores, default=0.0), 4),
        "token_f1": round(max(token_scores, default=0.0), 4),
    }


def warm_model(model: str) -> float:
    payload = {
        "model": model,
        "prompt": "",
        "stream": False,
        "keep_alive": "30m",
    }
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180):
            pass
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Could not warm {model!r}: HTTP {error.code}: {body[:200]}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_BASE_URL}: {error}"
        ) from error
    return round((time.perf_counter() - started) * 1000, 1)


def adapter_for_model(model: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if model.casefold().startswith("translategemma"):
        return "translategemma"
    if model.casefold().startswith(("hy-mt2", "hymt2")):
        return "hy-mt2"
    return "panelens"


def build_translategemma_prompt(text: str) -> str:
    return (
        "You are a professional Korean (ko) to English (en) translator. "
        "Your goal is to accurately convey the meaning and nuances of the "
        "original Korean text while adhering to English grammar, vocabulary, "
        "and cultural sensitivities.\n"
        "Produce only the English translation, without any additional "
        "explanations or commentary. Please translate the following Korean "
        "text into English:\n\n"
        f"{text}"
    )


def build_hymt_prompt(text: str) -> str:
    return (
        "Translate the following text into English. Note that you should only "
        "output the translated result without any additional explanation:\n"
        f"{text}"
    )


def request_direct_translation(
    model: str,
    text: str,
    adapter: str = "translategemma",
) -> dict[str, Any]:
    if adapter == "translategemma":
        prompt = build_translategemma_prompt(text)
        options = {
            "temperature": 0.0,
            "num_ctx": 2048,
            "num_predict": 192,
        }
    elif adapter == "hy-mt2":
        prompt = build_hymt_prompt(text)
        options = {
            "temperature": 0.7,
            "top_p": 0.6,
            "top_k": 20,
            "repeat_penalty": 1.05,
            "num_ctx": 2048,
            "num_predict": 192,
        }
    else:
        raise ValueError(f"Unsupported direct translation adapter: {adapter}")

    payload = {
        "model": model,
        "stream": False,
        "keep_alive": "30m",
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "options": options,
    }
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            envelope = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{model!r} returned HTTP {error.code}: {body[:200]}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_BASE_URL}: {error}"
        ) from error

    try:
        translation = str(envelope["message"]["content"]).strip()
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            f"{model!r} returned malformed chat output."
        ) from error
    return {
        "index": 0,
        "translation": translation,
        "tone": "neutral",
        "confidence": 0.8 if translation else 0.0,
    }


def parse_numbered_translations(
    output: str,
    expected_count: int,
) -> list[str] | None:
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
                return None
            translations[current_index] = [match.group(2).strip()]
        elif current_index is not None:
            translations[current_index].append(line)
        else:
            return None

    if set(translations) != set(range(expected_count)):
        return None

    parsed = [
        " ".join(translations[index]).strip()
        for index in range(expected_count)
    ]
    return parsed if all(parsed) else None


def request_numbered_page(
    model: str,
    regions: list[dict[str, Any]],
    adapter: str,
) -> list[dict[str, Any]]:
    numbered_source = "\n".join(
        f"[{index + 1}] {region['original']}"
        for index, region in enumerate(regions)
    )
    page_result = request_direct_translation(
        model,
        numbered_source,
        adapter=adapter,
    )
    parsed = parse_numbered_translations(
        str(page_result["translation"]),
        len(regions),
    )
    if parsed is None:
        return [
            {
                **request_direct_translation(
                    model,
                    str(region["original"]),
                    adapter=adapter,
                ),
                "index": index,
            }
            for index, region in enumerate(regions)
        ]

    return [
        {
            "index": index,
            "translation": translation,
            "tone": "neutral",
            "confidence": 0.8,
        }
        for index, translation in enumerate(parsed)
    ]


def evaluate_case(
    case: TranslationCase,
    model: str,
    adapter: str,
) -> dict[str, Any]:
    regions = [
        {
            "original": region["original"],
            "region_type": region["region_type"],
        }
        for region in case.regions
    ]
    started = time.perf_counter()
    if adapter in {"translategemma", "hy-mt2"}:
        translations = request_numbered_page(model, regions, adapter)
    else:
        prompt = _build_page_prompt(regions, case.series_alias)
        translations = _request_translations(
            prompt,
            len(regions),
            model=model,
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    region_results = []
    for index, (region, translation) in enumerate(
        zip(case.regions, translations, strict=True)
    ):
        prediction = str(translation["translation"])
        problems = _translation_problems(regions, translations, index)
        region_results.append(
            {
                "index": index,
                "region_type": region["region_type"],
                "source": region["original"],
                "prediction": prediction,
                "references": region["references"],
                "structural_problems": problems,
                "reference_similarity_proxy": reference_proxies(
                    prediction,
                    region["references"],
                ),
                "human_review": {
                    "meaning_0_to_3": None,
                    "fluency_0_to_2": None,
                    "notes": "",
                },
            }
        )

    return {
        "page_id": case.page_id,
        "series_alias": case.series_alias,
        "categories": list(case.categories),
        "adapter": adapter,
        "elapsed_ms": elapsed_ms,
        "regions": region_results,
    }


def summarize(pages: list[dict[str, Any]]) -> dict[str, Any]:
    regions = [
        region for page in pages for region in page["regions"]
    ]
    return {
        "pages": len(pages),
        "regions": len(regions),
        "total_elapsed_ms": round(
            sum(float(page["elapsed_ms"]) for page in pages),
            1,
        ),
        "mean_page_elapsed_ms": round(
            sum(float(page["elapsed_ms"]) for page in pages)
            / max(1, len(pages)),
            1,
        ),
        "structurally_valid_regions": sum(
            not region["structural_problems"] for region in regions
        ),
        "mean_sequence_similarity_proxy": round(
            sum(
                region["reference_similarity_proxy"]["sequence_similarity"]
                for region in regions
            )
            / max(1, len(regions)),
            4,
        ),
        "mean_token_f1_proxy": round(
            sum(
                region["reference_similarity_proxy"]["token_f1"]
                for region in regions
            )
            / max(1, len(regions)),
            4,
        ),
    }


def safe_model_name(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")


def write_review_markdown(
    path: Path,
    model: str,
    pages: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    lines = [
        f"# Translation Review — `{model}`",
        "",
        "Reference similarity is only a wording proxy. Fill in the human "
        "meaning and fluency scores before choosing a model.",
        "",
        "Meaning: 0 wrong, 1 major errors, 2 mostly correct, 3 correct.",
        "",
        "Fluency: 0 broken, 1 understandable, 2 natural.",
        "",
        f"- Pages: {summary['pages']}",
        f"- Regions: {summary['regions']}",
        f"- Mean page time: {summary['mean_page_elapsed_ms']} ms",
        "",
    ]
    for page in pages:
        lines.extend(
            [
                f"## {page['page_id']}",
                "",
                f"Categories: {', '.join(page['categories'])}",
                "",
            ]
        )
        for region in page["regions"]:
            lines.extend(
                [
                    f"### Region {region['index']}",
                    "",
                    f"- Korean: `{region['source']}`",
                    f"- Prediction: **{region['prediction']}**",
                    f"- References: {' / '.join(region['references'])}",
                    "- Meaning (0–3): ",
                    "- Fluency (0–2): ",
                    "- Notes: ",
                    "",
                ]
            )
    path.write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    models = args.models or [OLLAMA_MODEL]
    cases: list[TranslationCase] = []
    if args.source in {"annotations", "all"}:
        cases.extend(load_annotation_cases(args.annotations))
    if args.source in {"regressions", "all"}:
        cases.extend(load_regression_cases(args.regressions))
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    if not cases:
        raise SystemExit("No translation benchmark cases were found.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    for model in models:
        adapter = adapter_for_model(model, args.adapter)
        print(
            f"Evaluating {model} with {adapter} adapter "
            f"on {len(cases)} pages...",
            file=sys.stderr,
        )
        warmup_ms = None
        if not args.skip_warmup:
            warmup_ms = warm_model(model)

        pages = []
        for case in cases:
            print(f"  {case.page_id}", file=sys.stderr)
            pages.append(evaluate_case(case, model, adapter))

        model_summary = summarize(pages)
        report = {
            "benchmark": "panellens-korean-translation-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "adapter": adapter,
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "ollama_url": OLLAMA_BASE_URL,
            },
            "warmup_ms": warmup_ms,
            "summary": model_summary,
            "pages": pages,
            "metric_warning": (
                "Reference similarity proxies measure wording overlap, not "
                "semantic correctness. Human meaning review is authoritative."
            ),
        }

        output_dir = args.output_root / safe_model_name(model)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        write_review_markdown(
            output_dir / "review.md",
            model,
            pages,
            model_summary,
        )
        print(
            json.dumps(
                {"model": model, **model_summary},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
