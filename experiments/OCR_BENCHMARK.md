# Korean OCR Benchmark — Baseline

Date: 2026-07-27

## Dataset

- 5 Korean webtoon screenshots
- 13 annotated dialogue or narration regions
- 145 normalized Korean/alphanumeric characters
- Sound effects intentionally excluded from the MVP target

Character Error Rate (CER) removes whitespace and punctuation before comparison,
so the measurement focuses on transcription rather than line wrapping.

## Results

| Configuration | Region recall | Character errors | CER | Average/image |
|---|---:|---:|---:|---:|
| PP-OCRv5 server detector + Korean mobile recognizer | 13/13 (100%) | 2/145 | 1.38% | 8.86 s |
| PP-OCRv5 mobile detector + Korean mobile recognizer | 13/13 (100%) | 2/145 | 1.38% | 1.81 s |

The mobile detector produced the same measured target-text accuracy while running
about 4.9 times faster. It is the current MVP choice.

## Errors

- `ko_003`, region 2: expected `큭`; recognized `클`.
- `ko_004`, region 1: expected `형님아`; recognized `형남아`.

Both errors are plausible candidates for correction through series context or a
translation-time uncertainty path. The short `큭` result also had a low OCR
confidence score.

## Text outside target regions

The mobile configuration returned 38 text lines:

- 33 lines fell inside annotated dialogue/narration regions.
- 5 lines fell outside them.

The five outside lines came from background signage, a visual mark, and English
shirt lettering. This is not a formal detection-precision score because the
dataset does not annotate every visible piece of non-target text. It demonstrates
that OCR alone cannot decide which text should receive a translation overlay;
PanelLens still needs region classification or filtering.

The mobile detector did not return the large red `쾅` effects in `ko_005` on this
run, but five images are not enough to claim reliable sound-effect exclusion.

## Per-image mobile latency

| Image | Time | CER |
|---|---:|---:|
| `ko_001.png` | 2.22 s | 0% |
| `ko_002.png` | 2.38 s | 0% |
| `ko_003.png` | 1.57 s | 7.14% |
| `ko_004.png` | 1.84 s | 3.33% |
| `ko_005.png` | 1.06 s | 0% |

Model construction from an already downloaded cache took approximately 0.46
seconds for the mobile configuration in this run.

## Decision

Proceed with:

- `PP-OCRv5_mobile_det`
- `korean_PP-OCRv5_mobile_rec`
- PaddlePaddle 3.2.0
- PaddleOCR 3.7.0

Keep the server detector available only as an experiment. Before making a
resume-level accuracy claim, expand the dataset and repeat the benchmark.

## Reproduce

From the project root:

```sh
experiments/run_korean_ocr_benchmark.sh --detector mobile
```

Generated reports and debug images are written under
`experiments/results/paddleocr-korean-mobile/` and are ignored by Git.

