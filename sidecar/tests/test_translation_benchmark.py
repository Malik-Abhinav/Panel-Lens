import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "experiments"))

from translation_benchmark import load_annotation_cases
from translation_benchmark import load_regression_cases
from translation_benchmark import adapter_for_model
from translation_benchmark import build_translategemma_prompt
from translation_benchmark import build_hymt_prompt
from translation_benchmark import parse_numbered_translations
from translation_benchmark import reference_proxies
from translation_benchmark import token_f1


def test_load_translation_fixtures() -> None:
    annotations = load_annotation_cases(
        PROJECT_ROOT / "evaluation" / "korean" / "annotations"
    )
    regressions = load_regression_cases(
        PROJECT_ROOT
        / "evaluation"
        / "korean"
        / "translation-regressions.json"
    )

    assert len(annotations) == 5
    assert sum(len(case.regions) for case in annotations) == 13
    assert len(regressions) >= 8
    assert any("dialect" in case.categories for case in regressions)
    assert any(
        case.page_id == "subject-ambiguity-from-live-page"
        for case in regressions
    )
    assert any(
        case.page_id == "capability-versus-intent-from-live-page"
        for case in regressions
    )


def test_reference_proxy_accepts_alternate_wording() -> None:
    proxies = reference_proxies(
        "Bro! Are you okay?!",
        ["Hyungnim! Are you okay?!", "Bro! Are you okay?!"],
    )

    assert proxies == {
        "sequence_similarity": 1.0,
        "token_f1": 1.0,
    }


def test_token_f1_is_a_wording_proxy_not_binary_exact_match() -> None:
    score = token_f1(
        "That is the best news I've heard.",
        "That's the best news I've heard.",
    )

    assert 0.7 < score < 1.0


def test_translategemma_uses_its_official_direct_prompt_adapter() -> None:
    assert adapter_for_model("translategemma:4b", "auto") == "translategemma"
    assert adapter_for_model("hy-mt2:1.8b", "auto") == "hy-mt2"
    assert adapter_for_model("qwen2.5:7b", "auto") == "panelens"

    prompt = build_translategemma_prompt("안녕하세요")
    assert "professional Korean (ko) to English (en) translator" in prompt
    assert prompt.endswith("\n\n안녕하세요")

    hymt_prompt = build_hymt_prompt("안녕하세요")
    assert "Translate the following text into English" in hymt_prompt
    assert hymt_prompt.endswith("\n안녕하세요")


def test_parse_numbered_translations_preserves_page_alignment() -> None:
    parsed = parse_numbered_translations(
        "[1] Please wait, patient!\n"
        "[2] The attending physician asked me\n"
        "to handle your admission.\n"
        "[3] Can I pay here?",
        3,
    )

    assert parsed == [
        "Please wait, patient!",
        "The attending physician asked me to handle your admission.",
        "Can I pay here?",
    ]


def test_parse_numbered_translations_rejects_missing_or_duplicate_ids() -> None:
    assert parse_numbered_translations("[1] One\n[3] Three", 3) is None
    assert parse_numbered_translations("[1] One\n[1] Again", 1) is None
