import base64

from main import handle
from ocr_pipeline import group_ocr_lines
from translation_pipeline import TranslationError


def test_ping_returns_pong() -> None:
    result = handle({"type": "ping", "request_id": "test-1"})

    assert result == {
        "protocol_version": 1,
        "request_id": "test-1",
        "status": "ok",
        "type": "pong",
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
        translation_handler=lambda regions, _: [
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
