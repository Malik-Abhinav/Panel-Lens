"""Korean OCR pipeline used by the long-running PanelLens sidecar."""

from __future__ import annotations

import io
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "PADDLE_PDX_CACHE_HOME", str(PROJECT_ROOT / ".cache" / "paddlex")
)
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

KOREAN_TEXT = re.compile(r"[가-힣]")
MINIMUM_CONFIDENCE = 0.4


def group_ocr_lines(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine nearby wrapped OCR lines into dialogue-sized text blocks."""
    ordered = sorted(
        lines,
        key=lambda line: (line["bbox"][1], line["bbox"][0]),
    )
    groups: list[list[dict[str, Any]]] = []

    for line in ordered:
        if groups and _belongs_to_group(groups[-1], line):
            groups[-1].append(line)
        else:
            groups.append([line])

    return [_merge_group(group) for group in groups]


def _belongs_to_group(
    group: list[dict[str, Any]],
    line: dict[str, Any],
) -> bool:
    group_left = min(item["bbox"][0] for item in group)
    group_top = min(item["bbox"][1] for item in group)
    group_right = max(
        item["bbox"][0] + item["bbox"][2] for item in group
    )
    group_bottom = max(
        item["bbox"][1] + item["bbox"][3] for item in group
    )

    left, top, width, height = line["bbox"]
    right = left + width
    vertical_gap = top - group_bottom
    reference_height = max(
        height,
        sum(item["bbox"][3] for item in group) / len(group),
    )
    if vertical_gap < -reference_height or vertical_gap > reference_height:
        return False

    overlap = max(0.0, min(group_right, right) - max(group_left, left))
    minimum_width = max(1.0, min(group_right - group_left, width))
    horizontal_overlap_ratio = overlap / minimum_width
    group_center = (group_left + group_right) / 2
    line_center = (left + right) / 2
    centers_are_close = abs(group_center - line_center) <= max(
        group_right - group_left, width
    ) * 0.45

    return horizontal_overlap_ratio >= 0.35 or centers_are_close


def _merge_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    left = min(item["bbox"][0] for item in group)
    top = min(item["bbox"][1] for item in group)
    right = max(item["bbox"][0] + item["bbox"][2] for item in group)
    bottom = max(item["bbox"][1] + item["bbox"][3] for item in group)

    return {
        "bbox": [
            round(left, 1),
            round(top, 1),
            round(right - left, 1),
            round(bottom - top, 1),
        ],
        "original": " ".join(item["original"] for item in group),
        "translation": "",
        "language": "ko",
        "confidence": round(
            sum(item["confidence"] for item in group) / len(group),
            4,
        ),
        "line_count": len(group),
    }


class KoreanOCRPipeline:
    """Loads PaddleOCR once and reuses it for every captured frame."""

    def __init__(self) -> None:
        with redirect_stdout(sys.stderr):
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(
                lang="korean",
                ocr_version="PP-OCRv5",
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]:
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as source:
            image = np.asarray(source.convert("RGB"))

        with redirect_stdout(sys.stderr):
            predictions = list(
                self._ocr.predict(
                    image,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                )
            )
        if len(predictions) != 1:
            raise RuntimeError(
                f"Expected one OCR result, received {len(predictions)}"
            )

        payload = predictions[0].json["res"]
        texts = payload.get("rec_texts", [])
        scores = payload.get("rec_scores", [])
        polygons = payload.get("rec_polys", payload.get("dt_polys", []))

        regions = []
        for text, score, polygon in zip(
            texts, scores, polygons, strict=True
        ):
            normalized_text = str(text).strip()
            confidence = float(score)
            if (
                confidence < MINIMUM_CONFIDENCE
                or not KOREAN_TEXT.search(normalized_text)
            ):
                continue

            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            left = min(xs)
            top = min(ys)
            right = max(xs)
            bottom = max(ys)
            regions.append(
                {
                    "bbox": [
                        round(left, 1),
                        round(top, 1),
                        round(right - left, 1),
                        round(bottom - top, 1),
                    ],
                    "original": normalized_text,
                    "translation": "",
                    "language": "ko",
                    "confidence": round(confidence, 4),
                }
            )

        return group_ocr_lines(regions)


_korean_pipeline: KoreanOCRPipeline | None = None


def recognize_korean(image_bytes: bytes) -> list[dict[str, Any]]:
    global _korean_pipeline

    if _korean_pipeline is None:
        _korean_pipeline = KoreanOCRPipeline()
    return _korean_pipeline.recognize(image_bytes)
