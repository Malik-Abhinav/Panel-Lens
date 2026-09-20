import base64
import io

import numpy as np
from PIL import Image

from main import _translation_context
from main import handle
from main import handle_batch
from main import _resize_for_ocr
from ocr_pipeline import group_ocr_lines
from translation_pipeline import TranslationError


def test_ping_returns_pong(monkeypatch) -> None:
    import translation_pipeline as pipeline
    monkeypatch.setattr(pipeline, "OLLAMA_MODEL", "hy-mt2:7b")
    runtime = {
        "ready": True,
        "code": "ready",
        "model": "hy-mt2:7b",
        "message": "Local OCR and hy-mt2:7b are ready.",
    }
    result = handle(
        {"type": "ping", "request_id": "test-1"},
        runtime_handler=lambda: runtime,
    )

    assert result["browser_bridge"]["session"]
    assert result["translation_identity"]["model"]
    assert {key: result[key] for key in ("protocol_version", "request_id", "status", "type", "runtime")} == {
        "protocol_version": 1,
        "request_id": "test-1",
        "status": "ok",
        "type": "pong",
        "runtime": runtime,
    }


def test_translate_returns_fake_region() -> None:
    result = handle(
        {
            "type": "translate",
            "request_id": "translate-1",
            "image_base64": base64.b64encode(b"fake-png").decode("ascii"),
            "series": "Test Series",
            "chapter": 1,
        },
        ocr_handler=lambda _: [
            {
                "bbox": [10, 20, 30, 40],
                "original": "안녕",
                "translation": "",
                "language": "ko",
                "confidence": 0.98,
            }
        ],
        bubble_handler=lambda _, regions: (regions, 0),
        translation_handler=lambda regions, _, __: [
            {
                **regions[0],
                "translation": "Hello",
                "tone": "casual",
                "translation_confidence": 0.97,
            }
        ],
    )

    assert result["status"] == "ok"
    assert result["type"] == "translation"
    assert result["request_id"] == "translate-1"
    assert result["received_image_bytes"] == 8
    assert result["cache_hit"] is False
    assert result["ocr_processing_time_ms"] >= 0
    assert result["translation_processing_time_ms"] >= 0
    assert result["regions"] == [
        {
            "bbox": [10, 20, 30, 40],
            "original": "안녕",
            "translation": "Hello",
            "language": "ko",
            "confidence": 0.98,
            "tone": "casual",
            "translation_confidence": 0.97,
        }
    ]


def test_translate_rejects_invalid_base64() -> None:
    result = handle(
        {
            "type": "translate",
            "request_id": "bad-image",
            "image_base64": "not valid base64",
        }
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_image"


def test_batch_pipelines_each_image_as_soon_as_ocr_completes() -> None:
    translated_batches: list[list[str]] = []
    ocr_calls = 0

    def ocr(image: bytes) -> list[dict[str, object]]:
        nonlocal ocr_calls
        number = ocr_calls
        ocr_calls += 1
        return [
            {
                "bbox": [1, 2, 3, 4],
                "original": f"대사 {number}",
                "translation": "",
                "language": "ko",
                "confidence": 0.99,
            }
        ]

    def translate(
        regions: list[dict[str, object]],
        _: str,
        __: list[dict[str, str]],
    ) -> list[dict[str, object]]:
        translated_batches.append([str(region["original"]) for region in regions])
        return [
            {
                **region,
                "translation": f"English {str(region['original']).split()[-1]}",
            }
            for region in regions
        ]

    result = handle_batch(
        {
            "request_id": "batch-1",
            "images": [
                    {
                        "image_id": f"image-{index}",
                        "image_base64": base64.b64encode(
                            _test_png_bytes()
                        ).decode(),
                }
                for index in range(3)
            ],
        },
        ocr_handler=ocr,
        bubble_handler=lambda _, regions: (regions, 0),
        translation_handler=translate,
    )

    assert result["status"] == "ok"
    assert translated_batches == [["대사 0"], ["대사 1"], ["대사 2"]]
    assert [
        image["regions"][0]["translation"] for image in result["images"]
    ] == ["English 0", "English 1", "English 2"]


def _test_png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(output, format="PNG")
    return output.getvalue()


def test_batch_rejects_more_than_three_images() -> None:
    result = handle_batch(
        {
            "request_id": "too-many",
            "images": [
                {"image_id": str(index), "image_base64": "aW1hZ2U="}
                for index in range(4)
            ],
        }
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_batch"


def test_resize_for_ocr_only_downscales_wide_images() -> None:
    output = io.BytesIO()
    Image.new("RGB", (2000, 1000), "white").save(output, format="PNG")

    resized, coordinate_scale = _resize_for_ocr(output.getvalue(), 1000)

    with Image.open(io.BytesIO(resized)) as image:
        assert image.size == (1000, 500)
    assert coordinate_scale == 2.0


def test_translate_reports_ocr_failure() -> None:
    def fail(_: bytes) -> list[dict[str, object]]:
        raise RuntimeError("model failed")

    result = handle(
        {
            "type": "translate",
            "request_id": "ocr-error",
            "image_base64": base64.b64encode(b"image").decode("ascii"),
        },
        ocr_handler=fail,
    )

    assert result["status"] == "error"
    assert result["error"] == {
        "code": "ocr_failed",
        "message": "model failed",
    }


def test_translate_reports_ollama_offline() -> None:
    def fail_translation(
        _: list[dict[str, object]],
        __: str,
        ___: list[dict[str, str]],
    ) -> list[dict[str, object]]:
        raise TranslationError("ollama_offline", "Ollama is offline")

    result = handle(
        {
            "type": "translate",
            "request_id": "translation-error",
            "image_base64": base64.b64encode(b"image").decode("ascii"),
        },
        ocr_handler=lambda _: [
            {
                "bbox": [1, 2, 3, 4],
                "original": "안녕",
                "translation": "",
                "language": "ko",
                "confidence": 0.99,
            }
        ],
        bubble_handler=lambda _, regions: (regions, 0),
        translation_handler=fail_translation,
    )

    assert result["status"] == "error"
    assert result["error"] == {
        "code": "ollama_offline",
        "message": "Ollama is offline",
    }


def test_translate_passes_validated_bounded_context() -> None:
    received_context: list[dict[str, str]] = []

    def translate(
        regions: list[dict[str, object]],
        _: str,
        context: list[dict[str, str]],
    ) -> list[dict[str, object]]:
        received_context.extend(context)
        return regions

    raw_context: list[object] = [
        {"korean": f"이전 {index}", "english": f"Previous {index}"}
        for index in range(22)
    ]
    raw_context.extend(
        [
            {"korean": "", "english": "missing source"},
            "invalid",
        ]
    )
    result = handle(
        {
            "type": "translate",
            "request_id": "context",
            "image_base64": base64.b64encode(b"image").decode("ascii"),
            "context": raw_context,
        },
        ocr_handler=lambda _: [
            {
                "bbox": [1, 2, 3, 4],
                "original": "현재",
                "translation": "Current",
                "language": "ko",
                "confidence": 0.99,
            }
        ],
        bubble_handler=lambda _, regions: (regions, 0),
        translation_handler=translate,
    )

    assert result["status"] == "ok"
    assert len(received_context) == 18
    assert received_context[0]["korean"] == "이전 4"
    assert received_context[-1]["english"] == "Previous 21"


def test_translation_context_rejects_non_list_payload() -> None:
    assert _translation_context({"korean": "안녕", "english": "Hello"}) == []


def test_group_ocr_lines_combines_wrapped_dialogue() -> None:
    lines = [
        {
            "bbox": [250, 110, 140, 50],
            "original": "안돼요!",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [248, 157, 150, 54],
            "original": "환자분!",
            "translation": "",
            "language": "ko",
            "confidence": 0.98,
        },
        {
            "bbox": [224, 205, 195, 57],
            "original": "잠시만요!!",
            "translation": "",
            "language": "ko",
            "confidence": 0.97,
        },
        {
            "bbox": [190, 700, 255, 48],
            "original": "담당 선생님께서",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
    ]

    groups = group_ocr_lines(lines)

    assert len(groups) == 2
    assert groups[0]["original"] == "안돼요! 환자분! 잠시만요!!"
    assert groups[0]["line_count"] == 3
    assert groups[1]["original"] == "담당 선생님께서"


def test_group_ocr_lines_combines_adjacent_fragments_on_same_row() -> None:
    lines = [
        {
            "bbox": [105, 61, 88, 45],
            "original": "담당'",
            "translation": "",
            "language": "ko",
            "confidence": 0.97,
        },
        {
            "bbox": [177, 62, 179, 42],
            "original": "선생님께서",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
    ]

    groups = group_ocr_lines(lines)

    assert len(groups) == 1
    assert groups[0]["original"] == "담당' 선생님께서"


def test_group_ocr_lines_does_not_merge_tall_distant_sound_effect() -> None:
    lines = [
        {
            "bbox": [90, 1048, 224, 48],
            "original": "병원비 더럽게",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [138, 1091, 133, 49],
            "original": "비싸네..",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [33, 1245, 114, 110],
            "original": "저벅",
            "translation": "",
            "language": "ko",
            "confidence": 0.90,
        },
    ]

    groups = group_ocr_lines(lines)

    assert len(groups) == 2
    assert groups[0]["original"] == "병원비 더럽게 비싸네.."
    assert groups[1]["original"] == "저벅"


def test_group_ocr_lines_keeps_nearby_bubbles_separate() -> None:
    image = np.full((220, 240, 3), 70, dtype=np.uint8)
    image[20:94, 40:200] = 250
    image[102:180, 40:200] = 250
    lines = [
        {
            "bbox": [72, 48, 96, 30],
            "original": "첫 번째 말",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [72, 108, 96, 30],
            "original": "두 번째 말",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
    ]

    groups = group_ocr_lines(lines, image)

    assert [group["original"] for group in groups] == [
        "첫 번째 말",
        "두 번째 말",
    ]


def test_group_ocr_lines_preserves_multiline_text_in_one_bubble() -> None:
    image = np.full((220, 240, 3), 70, dtype=np.uint8)
    image[20:180, 40:200] = 250
    lines = [
        {
            "bbox": [72, 48, 96, 30],
            "original": "이어지는",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [68, 80, 104, 30],
            "original": "대화입니다",
            "translation": "",
            "language": "ko",
            "confidence": 0.98,
        },
    ]

    groups = group_ocr_lines(lines, image)

    assert len(groups) == 1
    assert groups[0]["original"] == "이어지는 대화입니다"
    assert groups[0]["line_count"] == 2


def test_group_ocr_lines_preserves_connected_narration_over_artwork() -> None:
    image = np.full((220, 240, 3), (60, 35, 45), dtype=np.uint8)
    lines = [
        {
            "bbox": [54, 48, 132, 30],
            "original": "그날의 기억은",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [60, 80, 120, 30],
            "original": "아직 선명했다.",
            "translation": "",
            "language": "ko",
            "confidence": 0.98,
        },
    ]

    groups = group_ocr_lines(lines, image)

    assert len(groups) == 1
    assert groups[0]["original"] == "그날의 기억은 아직 선명했다."


def test_group_ocr_lines_does_not_treat_light_lettering_as_bubble_component() -> None:
    image = np.full((180, 300, 3), (65, 35, 55), dtype=np.uint8)
    lines = [
        {
            "bbox": [82, 35, 136, 30],
            "original": "내가 정말",
            "translation": "",
            "language": "ko",
            "confidence": 0.99,
        },
        {
            "bbox": [48, 65, 204, 30],
            "original": "공작 부인이",
            "translation": "",
            "language": "ko",
            "confidence": 0.98,
        },
        {
            "bbox": [70, 95, 160, 30],
            "original": "맞느냐?",
            "translation": "",
            "language": "ko",
            "confidence": 0.97,
        },
    ]
    for left, top, width, height in (line["bbox"] for line in lines):
        x1 = round(left + width * 0.15)
        x2 = round(left + width * 0.85)
        y1 = round(top + height * 0.3)
        y2 = round(top + height * 0.7)
        image[y1:y2, x1:x2] = 245

    groups = group_ocr_lines(lines, image)

    assert len(groups) == 1
    assert groups[0]["original"] == "내가 정말 공작 부인이 맞느냐?"
