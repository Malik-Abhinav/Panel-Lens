import io

import numpy as np
from PIL import Image

from bubble_filter import filter_dialogue_regions


def _png_with_background(color: tuple[int, int, int]) -> bytes:
    pixels = np.full((160, 200, 3), color, dtype=np.uint8)
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return output.getvalue()


def _region() -> dict[str, object]:
    return {
        "bbox": [50, 40, 100, 60],
        "original": "안녕하세요",
        "translation": "",
        "language": "ko",
        "confidence": 0.98,
    }


def test_white_bubble_region_is_kept() -> None:
    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((250, 250, 250)),
        [_region()],
    )

    assert filtered_count == 0
    assert len(kept) == 1
    assert kept[0]["region_type"] == "dialogue"
    assert kept[0]["bubble_confidence"] > 0.9


def test_colored_artwork_region_is_filtered() -> None:
    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((220, 150, 170)),
        [_region()],
    )

    assert kept == []
    assert filtered_count == 1


def test_mature_dialogue_in_white_bubble_is_kept_without_euphemizing() -> None:
    region = _region()
    region["original"] = "보지 만지지 마."

    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((250, 250, 250)),
        [region],
    )

    assert filtered_count == 0
    assert kept[0]["original"] == "보지 만지지 마."
    assert kept[0]["region_type"] == "dialogue"


def test_long_narration_over_artwork_is_kept() -> None:
    region = _region()
    region["original"] = (
        "그렇게 명예롭지 못한 피를 뒤집어쓰며 여기까지 왔건만."
    )
    region["bbox"] = [40, 20, 140, 90]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((40, 25, 20)),
        [region],
    )

    assert filtered_count == 0
    assert kept[0]["region_type"] == "narration"


def test_short_question_narration_over_artwork_is_kept() -> None:
    region = _region()
    region["original"] = "어째서또"
    region["bbox"] = [40, 20, 140, 50]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((10, 10, 10)),
        [region],
    )

    assert filtered_count == 0
    assert kept[0]["region_type"] == "narration"


def test_short_sound_effect_over_artwork_is_filtered() -> None:
    region = _region()
    region["original"] = "쿠구구구구"
    region["bbox"] = [40, 20, 140, 90]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((80, 30, 30)),
        [region],
    )

    assert kept == []
    assert filtered_count == 1


def test_small_webpage_label_is_filtered() -> None:
    region = _region()
    region["original"] = "홈으로 돌아가기"
    region["bbox"] = [40, 20, 140, 18]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_background((30, 30, 30)),
        [region],
    )

    assert kept == []
    assert filtered_count == 1
