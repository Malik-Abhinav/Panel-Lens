from translation_pipeline import _attach_translations


def test_attach_translations_preserves_ocr_geometry() -> None:
    regions = [
        {
            "bbox": [10, 20, 30, 40],
            "original": "안녕하세요",
            "translation": "",
            "language": "ko",
            "confidence": 0.98,
        }
    ]
    translations = [
        {
            "index": 0,
            "translation": "Hello",
            "tone": "polite",
            "confidence": 0.96,
        }
    ]

    result = _attach_translations(regions, translations)

    assert result == [
        {
            "bbox": [10, 20, 30, 40],
            "original": "안녕하세요",
            "translation": "Hello",
            "language": "ko",
            "confidence": 0.98,
            "tone": "polite",
            "translation_confidence": 0.96,
        }
    ]
