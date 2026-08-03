import io

import numpy as np
from PIL import Image

from bubble_filter import filter_dialogue_regions


def _png_with_background(color: tuple[int, int, int]) -> bytes:
    pixels = np.full((160, 200, 3), color, dtype=np.uint8)
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return output.getvalue()


def _png_with_translucent_bubble() -> bytes:
    y, x = np.indices((160, 200))
    artwork = np.stack(
        (
            70 + (x % 60),
            45 + (y % 70),
            80 + ((x + y) % 80),
        ),
        axis=2,
    ).astype(np.uint8)
    bubble = ((x - 100) / 72) ** 2 + ((y - 80) / 58) ** 2 <= 1
    blended = artwork.astype(np.float32)
    blended[bubble] = blended[bubble] * 0.22 + 255 * 0.78
    output = io.BytesIO()
    Image.fromarray(blended.astype(np.uint8)).save(output, format="PNG")
    return output.getvalue()


def _png_with_colored_sound_effect() -> bytes:
    pixels = np.full((160, 200, 3), 250, dtype=np.uint8)
    pixels[55:105, 75:125] = (170, 25, 35)
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return output.getvalue()


def _png_with_inverse_bubble() -> bytes:
    pixels = np.full((160, 200, 3), (245, 225, 230), dtype=np.uint8)
    y, x = np.indices((160, 200))
    bubble = ((x - 100) / 78) ** 2 + ((y - 80) / 65) ** 2 <= 1
    pixels[bubble] = (48, 22, 29)
    pixels[62:68, 70:130] = (248, 225, 220)
    pixels[91:97, 62:138] = (248, 225, 220)
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


def test_translucent_bubble_dialogue_is_kept() -> None:
    region = _region()
    region["original"] = "아랫배가 너무 아파!!"
    region["bbox"] = [58, 58, 84, 44]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_translucent_bubble(),
        [region],
    )

    assert filtered_count == 0
    assert kept[0]["region_type"] == "dialogue"
    assert kept[0]["bubble_style"] == "translucent"


def test_light_text_in_dark_bubble_is_kept() -> None:
    region = _region()
    region["original"] = "에리카 황녀 전하."
    region["line_count"] = 2

    kept, filtered_count = filter_dialogue_regions(
        _png_with_inverse_bubble(),
        [region],
    )

    assert filtered_count == 0
    assert kept[0]["region_type"] == "dialogue"
    assert kept[0]["bubble_style"] == "inverse"


def test_short_light_sound_effect_in_dark_art_is_filtered() -> None:
    region = _region()
    region["original"] = "번쩍"
    region["bbox"] = [70, 55, 60, 50]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_inverse_bubble(),
        [region],
    )

    assert kept == []
    assert filtered_count == 1


def test_short_colored_sound_effect_on_white_is_filtered() -> None:
    region = _region()
    region["original"] = "툭"
    region["bbox"] = [75, 55, 50, 50]

    kept, filtered_count = filter_dialogue_regions(
        _png_with_colored_sound_effect(),
        [region],
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
