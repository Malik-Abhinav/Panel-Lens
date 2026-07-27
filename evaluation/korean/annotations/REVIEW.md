# Human Review Record

The first terminology review is complete. Its decisions have been applied to the
matching annotation JSON files.

## Confirmed decisions

- `ko_001`, region 2: preserve the four visible lines instead of imposing
  normalized Korean spacing:
  - `담당 선생님께서`
  - `입원 수속 진행`
  - `시켜드리라고`
  - `말씀을...!`
- `ko_001`, region 3: `수납` refers to payment in this scene.
- `ko_002`, region 1: `지대호` is the correct character name and is recorded as
  “Ji Dae-ho.”
- `ko_004`, region 1: both “Hyungnim” and the localized “Bro” are acceptable.
- `ko_005`, region 1: `마력` refers to mana.

Bounding boxes currently surround text rather than the entire speech bubble.
They are approximate and will be refined with an annotation helper or OCR debug
overlay.
