import json
from unittest.mock import patch

from translation_pipeline import _attach_translations
from translation_pipeline import _request_translations
from translation_pipeline import translate_korean_regions


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


def test_ollama_request_keeps_model_warm_and_limits_output() -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    response = FakeResponse()
    response.read = lambda: b""

    def fake_json_load(_: object) -> dict[str, object]:
        return {
            "message": {
                "content": json.dumps(
                    {
                        "translations": ["Hello"]
                    }
                )
            }
        }

    with (
        patch("translation_pipeline.urllib.request.urlopen") as urlopen,
        patch("translation_pipeline.json.load", side_effect=fake_json_load),
    ):
        urlopen.return_value = response
        result = _request_translations("prompt", 1)

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["keep_alive"] == "30m"
    assert payload["options"]["num_predict"] == 96
    assert result[0]["translation"] == "Hello"


def test_empty_translation_retries_only_missing_region() -> None:
    regions = [{"original": "안녕"}, {"original": "효과음"}]
    first_result = [
        {
            "index": 0,
            "translation": "Hello",
            "tone": "neutral",
            "confidence": 0.8,
        },
        {
            "index": 1,
            "translation": "",
            "tone": "neutral",
            "confidence": 0.0,
        },
    ]
    recovered_result = [
        {
            "index": 0,
            "translation": "Sound effect",
            "tone": "neutral",
            "confidence": 0.8,
        }
    ]

    with patch(
        "translation_pipeline._request_translations",
        side_effect=[first_result, recovered_result],
    ) as request_translations:
        result = translate_korean_regions(regions)

    assert request_translations.call_count == 2
    assert [item["translation"] for item in result] == [
        "Hello",
        "Sound effect",
    ]


def test_untranslatable_region_falls_back_without_failing_page() -> None:
    regions = [{"original": "안녕"}, {"original": "♬"}]
    empty_results = [
        {
            "index": 0,
            "translation": "Hello",
            "tone": "neutral",
            "confidence": 0.8,
        },
        {
            "index": 1,
            "translation": "",
            "tone": "neutral",
            "confidence": 0.0,
        },
    ]

    with patch(
        "translation_pipeline._request_translations",
        side_effect=[
            empty_results,
            [
                {
                    "index": 0,
                    "translation": "",
                    "tone": "neutral",
                    "confidence": 0.0,
                }
            ],
        ],
    ):
        result = translate_korean_regions(regions)

    assert result[0]["translation"] == "Hello"
    assert result[1]["translation"] == "♬"
    assert result[1]["translation_confidence"] == 0.0
