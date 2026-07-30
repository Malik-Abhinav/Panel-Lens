"""Lightweight dialogue and narration filtering for OCR regions."""

from __future__ import annotations

import io
import os
import re
from typing import Any

import numpy as np
from PIL import Image


MINIMUM_BUBBLE_SCORE = float(
    os.environ.get("PANELLENS_MINIMUM_BUBBLE_SCORE", "0.55")
)
MINIMUM_NARRATION_SCORE = float(
    os.environ.get("PANELLENS_MINIMUM_NARRATION_SCORE", "0.60")
)


def filter_dialogue_regions(
    image_bytes: bytes,
    regions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep speech bubbles and sentence-like narration over manhwa artwork."""
    if not regions:
        return [], 0

    with Image.open(io.BytesIO(image_bytes)) as source:
        image = np.asarray(source.convert("RGB"))

    kept: list[dict[str, Any]] = []
    for region in regions:
        bubble_score = _bubble_score(image, region["bbox"])
        narration_score = _narration_score(region)
        if (
            bubble_score < MINIMUM_BUBBLE_SCORE
            and narration_score < MINIMUM_NARRATION_SCORE
        ):
            continue

        candidate = dict(region)
        if bubble_score >= MINIMUM_BUBBLE_SCORE:
            candidate["bubble_confidence"] = round(bubble_score, 4)
            candidate["region_type"] = "dialogue"
        else:
            candidate["bubble_confidence"] = round(narration_score, 4)
            candidate["region_type"] = "narration"
        kept.append(candidate)

    return kept, len(regions) - len(kept)


def _bubble_score(image: np.ndarray, bbox: list[float]) -> float:
    image_height, image_width = image.shape[:2]
    left, top, width, height = bbox
    horizontal_margin = max(8, round(width * 0.15))
    vertical_margin = max(8, round(height * 0.20))

    x1 = max(0, round(left) - horizontal_margin)
    y1 = max(0, round(top) - vertical_margin)
    x2 = min(image_width, round(left + width) + horizontal_margin)
    y2 = min(image_height, round(top + height) + vertical_margin)
    crop = image[y1:y2, x1:x2].astype(np.int16)
    if crop.size == 0:
        return 0.0

    darkest_channel = crop.min(axis=2)
    color_range = crop.max(axis=2) - darkest_channel
    bubble_pixels = (darkest_channel >= 225) & (color_range <= 25)
    return float(bubble_pixels.mean())


def _narration_score(region: dict[str, Any]) -> float:
    """Estimate whether non-bubble Korean text is prose rather than UI/SFX."""
    text = re.sub(r"\s+", " ", str(region.get("original", "")).strip())
    hangul_count = len(re.findall(r"[가-힣]", text))
    if hangul_count < 4:
        return 0.0

    score = 0.0
    if hangul_count >= 10:
        score += 0.55
    else:
        score += 0.25

    if " " in text:
        score += 0.20

    if re.search(
        r"(?:다|요|까|나|네|군|건만|는데|지만|어서|해서)[.!?…]*$",
        text,
    ):
        score += 0.20

    if re.match(r"(?:왜|어째서|어떻게|무슨|누가|무엇|뭐)", text):
        score += 0.40

    bbox = region.get("bbox", [0, 0, 0, 0])
    if len(bbox) == 4 and float(bbox[3]) >= 30:
        score += 0.10

    confidence = float(region.get("confidence", 0.0))
    if confidence < 0.75:
        score -= 0.20

    return min(1.0, max(0.0, score))
