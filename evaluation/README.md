# PanelLens Evaluation Dataset

This folder contains the small, permission-safe dataset used to test PanelLens.
Do not commit copyrighted screenshots unless their license permits redistribution.

## First dataset

Collect five Korean screenshots:

1. Three screenshots from one primary series.
2. One more difficult screenshot from that primary series.
3. One screenshot from a different series with a noticeably different visual style.

The five screenshots should collectively include:

- a plain speech bubble;
- a narration box;
- text over artwork or a non-white background;
- two dialogue regions at similar vertical positions;
- small, decorative, or low-contrast lettering.

It is fine for one screenshot to cover several categories. Prefer screenshots from
normal reading sessions rather than deliberately choosing only easy examples.

## File naming

Use anonymous, stable names:

```text
evaluation/korean/images/ko_001.png
evaluation/korean/images/ko_002.png
...
```

Create a matching JSON annotation:

```text
evaluation/korean/annotations/ko_001.json
```

Copy `annotation-template.json` to create each annotation file.

## What “manual annotation” means

For every dialogue bubble or narration box that PanelLens should translate:

1. Type the source text exactly as it appears.
2. Type an acceptable English translation.
3. Draw or estimate a rectangle around it.
4. Assign its expected reading-order number, beginning at zero.
5. Label it `dialogue` or `narration`.

The rectangle is `[x, y, width, height]` in image pixels. The origin `[0, 0]` is
the top-left corner. An annotation does not have to be pixel-perfect initially;
it only needs to surround the intended text.

This is the answer key. Later, PanelLens output can be compared with it to measure
whether detection, OCR, reading order, and translation are improving.

## Finding rectangle coordinates on macOS

For the first five images, open the screenshot in Preview:

1. Choose **Tools → Rectangular Selection**.
2. Drag over the text region.
3. Use the selection size shown by Preview for width and height.
4. Estimate the top-left `x` and `y` using the image dimensions.

An annotation helper will be built later so this does not remain a manual process.

