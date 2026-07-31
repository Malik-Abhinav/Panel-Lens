# Translation Model Selection

PanelLens evaluates translation models on meaning, fluency, consistency, and
local performance. Exact agreement with an official translation is not
required; synonyms are acceptable when meaning, tone, names, and continuity are
preserved.

## Human scoring

Score every region in the generated `review.md`.

Meaning:

- `0` — wrong or unrelated
- `1` — major missing, invented, or reversed meaning
- `2` — mostly correct with a meaningful nuance error
- `3` — correct meaning, including acceptable synonyms

Fluency:

- `0` — broken or incomprehensible English
- `1` — understandable but noticeably awkward
- `2` — natural English suitable for a comic

Treat a model as ineligible if it frequently:

- invents a subject, location, name, or action;
- loses negation, quantities, names, or sentence fragments;
- mistakes dialect words for character names;
- copies one block into another;
- produces untranslated Hangul.

## Decision rule

Prefer the model with the highest average human meaning score, provided its
latency and memory use are acceptable on the target Mac. Use fluency as the
second criterion. Wording-overlap proxies are diagnostic only and must not
override human meaning review.

Do not add failed benchmark phrases to production code. Keep failures in the
evaluation dataset so future models are tested against them.

## Model adapters

General chat models use PanelLens's production page-level structured prompt.
TranslateGemma uses its direct Korean-to-English prompt with every page block
numbered in one request, preserving nearby dialogue and narration context. If
the model does not return every numbered block exactly once, the benchmark
falls back to translating each region separately so a malformed response does
not invalidate the entire run.

Tencent Hy-MT2 uses Tencent's official English translation instruction and the
same numbered page format. Its inference settings follow the official
recommendation: temperature 0.7, top-p 0.6, top-k 20, and repetition penalty
1.05.

## Preliminary local results

All models below were tested on the same 11 pages and 29 human-reviewed Korean
regions. Times are warm local page latency on the development Mac. Similarity
scores are wording proxies, not semantic grades.

| Model | Mean page time | Structurally valid | Sequence proxy | Token F1 |
| --- | ---: | ---: | ---: | ---: |
| Qwen 2.5 3B | 3.04 s | 29/29 | 0.5811 | 0.4279 |
| Qwen 2.5 7B | 6.52 s | 28/29 | 0.6859 | 0.5542 |
| TranslateGemma 4B | 3.59 s | 29/29 | 0.6028 | 0.4589 |
| Tencent Hy-MT2 1.8B Q4 | 1.49 s | 29/29 | 0.6343 | 0.4902 |
| Tencent Hy-MT2 7B Q4 | 5.03 s | 29/29 | 0.6847 | 0.5317 |

Preliminary qualitative review:

- Hy-MT2 1.8B is the speed leader but still makes major terminology, dialect,
  and slang errors. It is not accurate enough to become the default.
- Hy-MT2 7B is currently the strongest candidate. It improves ordinary
  dialogue, names, honorifics, payment context, narration, and slang while
  remaining faster than Qwen 2.5 7B.
- Every tested model still needs help with dialect-only words and ambiguous
  recurring terms whose intended meaning depends on series context. These
  failures should be addressed by generic context and terminology profiles,
  not phrase-specific production corrections.
- Do not switch the production default until the Hy-MT2 7B output has received
  human meaning and fluency scores in its generated `review.md`.

## Expanded live-regression comparison

Two failures observed on real manhwa pages were added as generic regression
cases: an omitted-subject narration case and a capability-versus-intent
dialogue case. The source screenshots remain local and ignored; the Korean text,
acceptable meanings, and failure categories are tracked in the benchmark.

The strongest Hy-MT2 candidate and the larger TranslateGemma model were rerun
on the expanded, identical suite of 13 pages and 32 regions:

| Model | Mean page time | Structurally valid | Sequence proxy | Token F1 |
| --- | ---: | ---: | ---: | ---: |
| Tencent Hy-MT2 7B Q4 | 7.13 s | 32/32 | 0.7086 | 0.5895 |
| Qwen 3.5 9B Q4 | 9.42 s | 32/32 | 0.7034 | 0.5871 |
| LG EXAONE 3.5 7.8B Q4 | 6.04 s | 32/32 | 0.6608 | 0.5129 |
| TranslateGemma 12B | 15.79 s | 32/32 | 0.6687 | 0.5764 |

TranslateGemma 12B did not resolve either live semantic failure. It inferred
`you` instead of the visually implied omitted first-person subject, and
rendered a statement about the speaker's level/capability as the awkward
“how much I'm capable of killing you.” Hy-MT2 7B also failed these exact
ambiguities, but remained more than twice as fast and scored higher across the
expanded suite.

Decision: retain Hy-MT2 7B as the current production candidate and keep
TranslateGemma 12B available locally as a replaceable benchmark candidate. Do
not switch the default to TranslateGemma 12B. These regressions require better
visual/page context or a future model; they must not be patched with
series-specific phrase rules.

Qwen 3.5 9B is the closest alternative to Hy-MT2 7B. It is structurally
reliable and nearly matches both wording proxies, but is about 32% slower on
this Mac. On the omitted-subject regression it invented plural actors and
changed the object from singular to plural (“we can catch them”). On the
capability regression it changed the speaker's strength/level into willingness
(“how far I'll go”). Its multimodal support remains interesting for a future
image-aware experiment, but the current text-only benchmark does not justify
replacing Hy-MT2.

EXAONE 3.5 7.8B is the fastest of these three similarly sized candidates, but
its aggregate proxies are substantially lower. It also guessed a second-person
subject in the omitted-subject regression and produced the awkward “how much I
am capable of killing you” for the capability regression. In addition, the
published EXAONE 3.5 model license is non-commercial, which makes it a poor
production default if PanelLens may later be commercialized.

Current ranking for the text-only pipeline:

1. Hy-MT2 7B — best accuracy/latency balance and Apache-2.0 licensing.
2. Qwen 3.5 9B — close accuracy and Apache-2.0 licensing, but slower; retain as
   the leading multimodal experiment candidate.
3. EXAONE 3.5 7.8B — fast and Korean-focused, but weaker here and
   non-commercially licensed.
4. TranslateGemma 12B — slowest and no accuracy advantage on this suite.

Official model sources:

- Hy-MT2 repository and prompting:
  <https://github.com/Tencent-Hunyuan/Hy-MT2>
- Hy-MT2 Apache 2.0 license:
  <https://github.com/Tencent-Hunyuan/Hy-MT2/blob/main/LICENSE.txt>
- Official 1.8B GGUF:
  <https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF>
- Official 7B GGUF:
  <https://huggingface.co/tencent/Hy-MT2-7B-GGUF>
- Official TranslateGemma 12B:
  <https://huggingface.co/google/translategemma-12b-it>
- Official Qwen 3.5 9B:
  <https://huggingface.co/Qwen/Qwen3.5-9B>
- Official EXAONE 3.5 7.8B:
  <https://huggingface.co/LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct>

The GGUF files and temporary Ollama Modelfiles belong under the ignored
`models/` directory. Import the Q4 builds into Ollama as `hy-mt2:1.8b` and
`hy-mt2:7b`, then run:

```bash
python experiments/translation_benchmark.py \
  --model hy-mt2:1.8b \
  --model hy-mt2:7b \
  --source all
```
