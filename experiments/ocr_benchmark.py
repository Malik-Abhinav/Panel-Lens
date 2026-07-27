"""Benchmark local Korean PaddleOCR against PanelLens annotations."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "PADDLE_PDX_CACHE_HOME", str(PROJECT_ROOT / ".cache" / "paddlex")
)
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import paddle
import paddleocr
from paddleocr import PaddleOCR
from PIL import Image, ImageDraw


DEFAULT_IMAGES = PROJECT_ROOT / "evaluation" / "korean" / "images"
DEFAULT_ANNOTATIONS = PROJECT_ROOT / "evaluation" / "korean" / "annotations"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiments" / "results"
DETECTION_MODELS = {
    "server": "PP-OCRv5_server_det",
    "mobile": "PP-OCRv5_mobile_det",
}
KOREAN_RECOGNITION_MODEL = "korean_PP-OCRv5_mobile_rec"


@dataclass(frozen=True)
class OCRLine:
    text: str
    score: float
    polygon: list[list[float]]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def center(self) -> tuple[float, float]:
        left, top, right, bottom = self.bbox
        return (left + right) / 2, (top + bottom) / 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGES)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--detector", choices=DETECTION_MODELS, default="server")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def normalize_text(text: str) -> str:
    """Normalize layout and punctuation so CER focuses on Korean transcription."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text).lower()


def levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    return max(0.0, right - left) * max(0.0, bottom - top)


def annotation_bbox(region: dict[str, Any]) -> tuple[float, float, float, float]:
    x, y, width, height = region["bbox"]
    return x, y, x + width, y + height


def line_matches_region(line: OCRLine, region: dict[str, Any]) -> bool:
    target = annotation_bbox(region)
    center_x, center_y = line.center
    center_inside = (
        target[0] <= center_x <= target[2] and target[1] <= center_y <= target[3]
    )
    line_box = line.bbox
    line_area = max(1.0, (line_box[2] - line_box[0]) * (line_box[3] - line_box[1]))
    overlap = intersection_area(line_box, target) / line_area
    return center_inside or overlap >= 0.5


def extract_lines(result: Any) -> list[OCRLine]:
    payload = result.json["res"]
    texts = payload.get("rec_texts", [])
    scores = payload.get("rec_scores", [])
    polygons = payload.get("rec_polys", payload.get("dt_polys", []))
    return [
        OCRLine(
            text=str(text),
            score=float(score),
            polygon=[[float(x), float(y)] for x, y in polygon],
        )
        for text, score, polygon in zip(texts, scores, polygons, strict=True)
    ]


def order_lines(lines: Iterable[OCRLine]) -> list[OCRLine]:
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))


def draw_debug_image(
    image_path: Path,
    output_path: Path,
    lines: list[OCRLine],
    regions: list[dict[str, Any]],
) -> None:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    for index, region in enumerate(regions):
        left, top, right, bottom = annotation_bbox(region)
        draw.rectangle((left, top, right, bottom), outline="#2ECC71", width=3)
        draw.text((left + 3, top + 3), f"GT {index}", fill="#168A48")

    for index, line in enumerate(lines):
        left, top, right, bottom = line.bbox
        draw.rectangle((left, top, right, bottom), outline="#E74C3C", width=2)
        draw.text((left + 3, max(0, top - 12)), f"OCR {index}", fill="#B03A2E")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def evaluate_image(
    ocr: PaddleOCR,
    image_path: Path,
    annotation_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    annotation = json.loads(annotation_path.read_text())
    started = time.perf_counter()
    predictions = list(
        ocr.predict(
            str(image_path),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if len(predictions) != 1:
        raise RuntimeError(
            f"Expected one OCR result for {image_path}, got {len(predictions)}"
        )

    lines = extract_lines(predictions[0])
    region_results: list[dict[str, Any]] = []
    total_edits = 0
    total_characters = 0
    detected_regions = 0

    for region in annotation["regions"]:
        matches = order_lines(
            line for line in lines if line_matches_region(line, region)
        )
        predicted = "".join(line.text for line in matches)
        expected_normalized = normalize_text(region["source_text"])
        predicted_normalized = normalize_text(predicted)
        edits = levenshtein(expected_normalized, predicted_normalized)
        total_edits += edits
        total_characters += len(expected_normalized)
        detected_regions += bool(matches)
        region_results.append(
            {
                "id": region["id"],
                "expected": region["source_text"],
                "predicted": predicted,
                "normalized_expected": expected_normalized,
                "normalized_predicted": predicted_normalized,
                "character_errors": edits,
                "characters": len(expected_normalized),
                "cer": round(edits / max(1, len(expected_normalized)), 4),
                "matched_line_indexes": [lines.index(line) for line in matches],
            }
        )

    draw_debug_image(
        image_path,
        output_dir / "debug" / image_path.name,
        lines,
        annotation["regions"],
    )

    return {
        "image": image_path.name,
        "elapsed_ms": elapsed_ms,
        "annotated_regions": len(annotation["regions"]),
        "detected_regions": detected_regions,
        "region_recall": round(detected_regions / len(annotation["regions"]), 4),
        "character_errors": total_edits,
        "characters": total_characters,
        "cer": round(total_edits / max(1, total_characters), 4),
        "ocr_lines": [
            {
                "text": line.text,
                "score": round(line.score, 4),
                "polygon": line.polygon,
                "bbox": [round(value, 1) for value in line.bbox],
            }
            for line in lines
        ],
        "regions": region_results,
    }


def main() -> int:
    args = parse_args()
    if args.output is None:
        args.output = (
            DEFAULT_OUTPUT_ROOT / f"paddleocr-korean-{args.detector}"
        )
    args.output.mkdir(parents=True, exist_ok=True)

    print("Loading Korean PaddleOCR model...", file=sys.stderr)
    load_started = time.perf_counter()
    ocr = PaddleOCR(
        lang="korean",
        ocr_version="PP-OCRv5",
        text_detection_model_name=DETECTION_MODELS[args.detector],
        text_recognition_model_name=KOREAN_RECOGNITION_MODEL,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    load_ms = round((time.perf_counter() - load_started) * 1000, 1)

    image_results = []
    for annotation_path in sorted(args.annotations.glob("ko_*.json")):
        image_path = args.images / annotation_path.with_suffix(".png").name
        print(f"Processing {image_path.name}...", file=sys.stderr)
        image_results.append(
            evaluate_image(ocr, image_path, annotation_path, args.output)
        )

    total_regions = sum(item["annotated_regions"] for item in image_results)
    detected_regions = sum(item["detected_regions"] for item in image_results)
    total_edits = sum(item["character_errors"] for item in image_results)
    total_characters = sum(item["characters"] for item in image_results)

    report = {
        "benchmark": f"paddleocr-korean-{args.detector}-v1",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "paddlepaddle": paddle.__version__,
            "paddleocr": paddleocr.__version__,
            "paddlex_cache": os.environ.get("PADDLE_PDX_CACHE_HOME"),
        },
        "model": {
            "language": "korean",
            "ocr_version": "PP-OCRv5",
            "text_detection_model": DETECTION_MODELS[args.detector],
            "text_recognition_model": KOREAN_RECOGNITION_MODEL,
            "load_ms": load_ms,
        },
        "summary": {
            "images": len(image_results),
            "annotated_regions": total_regions,
            "detected_regions": detected_regions,
            "region_recall": round(detected_regions / total_regions, 4),
            "character_errors": total_edits,
            "characters": total_characters,
            "cer": round(total_edits / max(1, total_characters), 4),
            "total_inference_ms": round(
                sum(item["elapsed_ms"] for item in image_results), 1
            ),
            "average_inference_ms": round(
                sum(item["elapsed_ms"] for item in image_results)
                / len(image_results),
                1,
            ),
        },
        "images": image_results,
    }

    report_path = args.output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
