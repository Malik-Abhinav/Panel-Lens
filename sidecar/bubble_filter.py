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
MINIMUM_TRANSLUCENT_BUBBLE_SCORE = float(
    os.environ.get("PANELLENS_MINIMUM_TRANSLUCENT_BUBBLE_SCORE", "0.68")
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
        translucent_score = _translucent_bubble_score(
            image,
            region["bbox"],
        )
        narration_score = _narration_score(region)
        decorative_colored_text = _is_decorative_colored_text(
            image,
            region,
        )
        translucent_dialogue = _is_translucent_dialogue(
            image,
            region,
            translucent_score,
            narration_score,
        )
        if (
            decorative_colored_text
            or (
                bubble_score < MINIMUM_BUBBLE_SCORE
                and narration_score < MINIMUM_NARRATION_SCORE
                and not translucent_dialogue
            )
        ):
            continue

        candidate = dict(region)
        if bubble_score >= MINIMUM_BUBBLE_SCORE:
            candidate["bubble_confidence"] = round(bubble_score, 4)
            candidate["region_type"] = "dialogue"
            candidate["bubble_style"] = "opaque"
        elif translucent_dialogue:
            candidate["bubble_confidence"] = round(translucent_score, 4)
            candidate["region_type"] = "dialogue"
            candidate["bubble_style"] = "translucent"
        else:
            candidate["bubble_confidence"] = round(narration_score, 4)
            candidate["region_type"] = "narration"
        kept.append(candidate)

    return kept, len(regions) - len(kept)


def _bubble_score(image: np.ndarray, bbox: list[float]) -> float:
    crop = _expanded_crop(image, bbox)
    if crop.size == 0:
        return 0.0

    darkest_channel = crop.min(axis=2)
    color_range = crop.max(axis=2) - darkest_channel
    bubble_pixels = (darkest_channel >= 225) & (color_range <= 25)
    return float(bubble_pixels.mean())


def _expanded_crop(
    image: np.ndarray,
    bbox: list[float],
) -> np.ndarray:
    image_height, image_width = image.shape[:2]
    left, top, width, height = bbox
    horizontal_margin = max(8, round(width * 0.15))
    vertical_margin = max(8, round(height * 0.20))

    x1 = max(0, round(left) - horizontal_margin)
    y1 = max(0, round(top) - vertical_margin)
    x2 = min(image_width, round(left + width) + horizontal_margin)
    y2 = min(image_height, round(top + height) + vertical_margin)
    return image[y1:y2, x1:x2].astype(np.int16)


def _translucent_bubble_score(
    image: np.ndarray,
    bbox: list[float],
) -> float:
    """Score a pale, locally uniform bubble without requiring pure white."""
    crop = _expanded_crop(image, bbox)
    if crop.size == 0:
        return 0.0

    luminance = (
        crop[:, :, 0] * 0.2126
        + crop[:, :, 1] * 0.7152
        + crop[:, :, 2] * 0.0722
    )
    cutoff = np.percentile(luminance, 35)
    background_mask = luminance >= max(100.0, cutoff)
    if background_mask.sum() < 16:
        return 0.0

    background_luminance = luminance[background_mask]
    background_chroma = (
        crop.max(axis=2) - crop.min(axis=2)
    )[background_mask]
    brightness = np.clip(
        (np.median(background_luminance) - 175.0) / 65.0,
        0.0,
        1.0,
    )
    uniformity = np.clip(
        1.0 - np.std(background_luminance) / 40.0,
        0.0,
        1.0,
    )
    neutrality = np.clip(
        1.0 - np.median(background_chroma) / 70.0,
        0.0,
        1.0,
    )
    return float(
        brightness * 0.45
        + uniformity * 0.35
        + neutrality * 0.20
    )


def _is_translucent_dialogue(
    image: np.ndarray,
    region: dict[str, Any],
    translucent_score: float,
    narration_score: float,
) -> bool:
    if translucent_score < MINIMUM_TRANSLUCENT_BUBBLE_SCORE:
        return False

    confidence = float(region.get("confidence", 0.0))
    if confidence < 0.80:
        return False

    text = re.sub(r"\s+", " ", str(region.get("original", "")).strip())
    hangul_count = len(re.findall(r"[가-힣]", text))
    if hangul_count == 0 or _ink_chroma(image, region["bbox"]) > 35:
        return False

    if hangul_count >= 5 and narration_score >= 0.25:
        return True

    return (
        confidence >= 0.88
        and bool(re.search(r"[,，.!?…~]$", text))
        and translucent_score >= 0.75
    )


def _is_decorative_colored_text(
    image: np.ndarray,
    region: dict[str, Any],
) -> bool:
    text = re.sub(r"\s+", "", str(region.get("original", "")).strip())
    hangul_count = len(re.findall(r"[가-힣]", text))
    return (
        hangul_count <= 3
        and not re.search(r"[,，.!?…~]$", text)
        and _ink_chroma(image, region["bbox"]) > 35
    )


def _ink_chroma(image: np.ndarray, bbox: list[float]) -> float:
    image_height, image_width = image.shape[:2]
    left, top, width, height = bbox
    x1 = max(0, round(left))
    y1 = max(0, round(top))
    x2 = min(image_width, round(left + width))
    y2 = min(image_height, round(top + height))
    crop = image[y1:y2, x1:x2].astype(np.int16)
    if crop.size == 0:
        return 0.0

    luminance = (
        crop[:, :, 0] * 0.2126
        + crop[:, :, 1] * 0.7152
        + crop[:, :, 2] * 0.0722
    )
    ink = crop[luminance < 160]
    if ink.size == 0:
        return 0.0
    return float(np.median(ink.max(axis=1) - ink.min(axis=1)))


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
