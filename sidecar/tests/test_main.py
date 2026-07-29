import base64

from main import handle


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
        }
    )

    assert result["status"] == "ok"
    assert result["type"] == "translation"
    assert result["request_id"] == "translate-1"
    assert result["received_image_bytes"] == 8
    assert result["regions"] == [
        {
            "bbox": [120, 160, 240, 100],
            "original": "테스트 대사",
            "translation": "Test dialogue",
            "language": "ko",
            "confidence": 0.99,
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
