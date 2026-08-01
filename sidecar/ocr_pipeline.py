"""Korean OCR pipeline used by the long-running PanelLens sidecar."""

from __future__ import annotations

import io
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "PADDLE_PDX_CACHE_HOME", str(PROJECT_ROOT / ".cache" / "paddlex")
)
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

KOREAN_TEXT = re.compile(r"[가-힣]")
MINIMUM_CONFIDENCE = 0.4


def group_ocr_lines(
    lines: list[dict[str, Any]],
    image: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    """Combine nearby wrapped OCR lines into dialogue-sized text blocks."""
    component_ids = (
        _light_background_components(image, lines)
        if image is not None
        else [None] * len(lines)
    )
    prepared_lines = [
        {**line, "_bubble_component": component_id}
        for line, component_id in zip(lines, component_ids, strict=True)
    ]
    ordered = sorted(
        prepared_lines,
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
    group_components = {
        item.get("_bubble_component")
        for item in group
        if item.get("_bubble_component") is not None
    }
    line_component = line.get("_bubble_component")
    if (
        group_components
        and line_component is not None
        and line_component not in group_components
    ):
        return False

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
    group_average_height = (
        sum(item["bbox"][3] for item in group) / len(group)
    )
    reference_height = max(
        group_average_height,
        min(height, group_average_height * 1.5),
    )
    if vertical_gap < -reference_height or vertical_gap > reference_height:
        return False

    vertical_overlap = max(
        0.0,
        min(group_bottom, top + height) - max(group_top, top),
    )
    minimum_height = max(1.0, min(group_bottom - group_top, height))
    horizontal_gap = max(
        0.0,
        max(left - group_right, group_left - right),
    )
    same_text_row = (
        vertical_overlap / minimum_height >= 0.5
        and horizontal_gap <= reference_height * 0.75
    )
    if same_text_row:
        return True

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


def _light_background_components(
    image: np.ndarray,
    lines: list[dict[str, Any]],
) -> list[int | None]:
    """Identify the light connected area surrounding each OCR line."""
    import cv2

    darkest_channel = image.min(axis=2)
    color_range = image.max(axis=2) - darkest_channel
    light_pixels = (
        (darkest_channel >= 225) & (color_range <= 25)
    ).astype(np.uint8)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        light_pixels,
        connectivity=8,
    )
    minimum_area = max(64, round(image.shape[0] * image.shape[1] * 0.0001))
    valid_components = {
        component_id
        for component_id in range(1, component_count)
        if stats[component_id, cv2.CC_STAT_AREA] >= minimum_area
    }

    return [
        _surrounding_component(labels, valid_components, line["bbox"])
        for line in lines
    ]


def _surrounding_component(
    labels: np.ndarray,
    valid_components: set[int],
    bbox: list[float],
) -> int | None:
    image_height, image_width = labels.shape
    left, top, width, height = bbox
    horizontal_margin = max(4, round(width * 0.08))
    vertical_margin = max(4, round(height * 0.15))
    x1 = max(0, round(left) - horizontal_margin)
    y1 = max(0, round(top) - vertical_margin)
    x2 = min(image_width, round(left + width) + horizontal_margin)
    y2 = min(image_height, round(top + height) + vertical_margin)
    nearby_labels = labels[y1:y2, x1:x2]
    if nearby_labels.size == 0:
        return None

    ring_mask = np.ones(nearby_labels.shape, dtype=bool)
    inner_x1 = max(0, round(left) - x1)
    inner_y1 = max(0, round(top) - y1)
    inner_x2 = min(nearby_labels.shape[1], round(left + width) - x1)
    inner_y2 = min(nearby_labels.shape[0], round(top + height) - y1)
    ring_mask[inner_y1:inner_y2, inner_x1:inner_x2] = False
    ring_labels = nearby_labels[ring_mask]
    component_ids, counts = np.unique(ring_labels, return_counts=True)
    minimum_ring_pixels = max(8, round(ring_labels.size * 0.15))
    candidates = [
        (int(count), int(component_id))
        for component_id, count in zip(component_ids, counts, strict=True)
        if (
            int(component_id) in valid_components
            and int(count) >= minimum_ring_pixels
        )
    ]
    if not candidates:
        return None
    return max(candidates)[1]


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
            normalized_text = re.sub(
                r"(?<=[가-힣])['’]$",
                "",
                normalized_text,
            )
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

        return group_ocr_lines(regions, image)


_korean_pipeline: KoreanOCRPipeline | None = None


def recognize_korean(image_bytes: bytes) -> list[dict[str, Any]]:
    global _korean_pipeline

    if _korean_pipeline is None:
        _korean_pipeline = KoreanOCRPipeline()
    return _korean_pipeline.recognize(image_bytes)
