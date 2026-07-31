# Experiments

Short-lived OCR and translation comparisons belong here. Record inputs, model
versions, timing, memory use, and results so technology choices are evidence-based.

## Translation model benchmark

`translation_benchmark.py` compares one or more installed Ollama models using
the same structured page prompt as PanelLens:

```sh
sidecar/.venv/bin/python experiments/translation_benchmark.py \
  --model qwen2.5:7b
```

Repeat `--model` to compare several models in one run:

```sh
sidecar/.venv/bin/python experiments/translation_benchmark.py \
  --model qwen2.5:3b \
  --model qwen2.5:7b
```

The default `auto` adapter uses PanelLens's structured page prompt for general
chat models. For model names beginning with `translategemma`, it uses
TranslateGemma's official Korean-to-English direct-translation prompt and
evaluates each region separately. Override this only for experiments with
`--adapter panelens` or `--adapter translategemma`.

The default `all` source includes the five annotated Korean pages and the
evaluation-only cases in
`evaluation/korean/translation-regressions.json`. Those fixtures exercise
dialogue, narration, names, dialect, slang, fragments, and terminology. They
are never imported by the production sidecar.

Reports are written under `experiments/results/translation/<model>/`:

- `report.json` contains predictions, references, structural warnings, timing,
  and wording-overlap proxies.
- `review.md` is a worksheet for human meaning and fluency scores.

Reference similarity is not an accuracy score: correct synonyms can have low
overlap and fluent mistranslations can have high overlap. Select a model using
human meaning scores first, followed by fluency, latency, and memory use.

Use `--limit 1` for a quick integration smoke test or
`--source regressions` to run only the difficult manual-test cases.
