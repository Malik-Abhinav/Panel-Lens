import json
from unittest.mock import patch

from translation_pipeline import _attach_translations
from translation_pipeline import _build_page_prompt
from translation_pipeline import _normalize_translation
from translation_pipeline import _request_translations
from translation_pipeline import _romanize_korean_name
from translation_pipeline import _translation_problems
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


def test_page_prompt_includes_all_blocks_in_id_order() -> None:
    regions = [
        {"original": "첫 번째", "region_type": "narration"},
        {"original": "두 번째", "region_type": "dialogue"},
        {"original": "세 번째", "region_type": "dialogue"},
    ]

    prompt = _build_page_prompt(regions, "")

    first = prompt.index("[id=0 type=narration] 첫 번째")
    second = prompt.index("[id=1 type=dialogue] 두 번째")
    third = prompt.index("[id=2 type=dialogue] 세 번째")
    assert first < second < third
    assert "exactly\n3 English strings" in prompt


def test_page_prompt_uses_general_fidelity_rules() -> None:
    prompt = _build_page_prompt(
        [{"original": "아직 끝난 게 아닌데.", "region_type": "dialogue"}],
        "",
    )

    assert "Preserve names, quantities, negation, subject/object direction" in prompt
    assert "Do not invent I/he/she/they" in prompt
    assert "Preserve fragments as fragments" in prompt


def test_page_prompt_protects_name_only_vocatives() -> None:
    prompt = _build_page_prompt(
        [
            {"original": "왜 이런 일이 생긴 거야?"},
            {"original": "민수야."},
        ],
        "",
    )

    assert "vocative -아/-야" in prompt
    assert "do not expand it into surrounding dialogue" in prompt


def test_translation_problems_rejects_context_added_to_name() -> None:
    regions = [{"original": "병희야."}]
    translations = [
        {
            "translation": "Byeong-hui, what happened because of that guy?",
        }
    ]
    assert _translation_problems(regions, translations, 0)

    translations[0]["translation"] = "Byeong-hui."
    assert _translation_problems(regions, translations, 0) == []


def test_translation_problems_rejects_untranslated_hangul() -> None:
    regions = [{"original": "이게 뭐야?"}]
    translations = [{"translation": "What is this, 뭐야?"}]
    problems = _translation_problems(regions, translations, 0)
    assert "The English output contains untranslated Hangul." in problems


def test_translation_problems_rejects_duplicate_outputs() -> None:
    regions = [
        {"original": "오늘은 집에 가자."},
        {"original": "내일 다시 만나자."},
    ]
    translations = [
        {"translation": "Let's go home today."},
        {"translation": "Let's go home today."},
    ]

    assert any(
        "duplicates" in problem
        for problem in _translation_problems(regions, translations, 1)
    )


def test_romanize_korean_names_without_series_specific_data() -> None:
    assert _romanize_korean_name("병희") == "Byeong-hui"
    assert _romanize_korean_name("형남") == "Hyeong-nam"
    assert _romanize_korean_name("이형태") == "Lee Hyeong-tae"


def test_normalize_translation_only_changes_formatting() -> None:
    assert _normalize_translation(
        "  Keep   the model's meaning.  "
    ) == "Keep the model's meaning."


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


def test_valid_page_uses_one_batched_model_request() -> None:
    regions = [{"original": "안녕"}, {"original": "잘 가"}]
    batch = [
        {
            "index": 0,
            "translation": "Hello",
            "tone": "neutral",
            "confidence": 0.8,
        },
        {
            "index": 1,
            "translation": "Goodbye",
            "tone": "neutral",
            "confidence": 0.8,
        },
    ]

    with patch(
        "translation_pipeline._request_translations",
        return_value=batch,
    ) as request_translations:
        result = translate_korean_regions(regions, "batch-test-series")

    request_translations.assert_called_once()
    assert [item["translation"] for item in result] == ["Hello", "Goodbye"]


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
