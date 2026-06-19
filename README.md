# PDF → EPUB Vision Pipeline

A prompt + orchestration design that converts a PDF book into a clean EPUB by
treating each rendered page as an **image a human reads**, not as a text stream
to reverse-engineer.

## Why this is different

Every mainstream PDF→EPUB tool (Calibre, Pandoc, pdf2epub, online converters)
parses the PDF's content stream — text spans, fonts, coordinates — and tries to
re-construct paragraphs, headings, and reading order geometrically. That breaks
on:

- multi-column layouts
- drop caps + small caps + italic lead-ins
- figures that wrap text
- chapter starts on arbitrary pages
- watermarks, running heads, page numbers bleeding into body text
- scanned books (no text stream at all)

This pipeline **renders each page to an image** at 200–300 DPI and hands it to a
vision LLM (`qwen/qwen3.5-397b-a17b`) along with the running book context. The
model sees what a reader sees, decides what is body / heading / figure / chrome,
and emits **semantic XHTML** wrapped in a strict JSON envelope.

A small orchestrator on your end stitches the pages into an EPUB, builds the
nav document, and runs a final QA pass.

## Pipeline at a glance

```
            ┌─────────────────────┐
            │   PDF (input book)  │
            └──────────┬──────────┘
                       │
        render pages @ 250 dpi → page-NN.png
                       │
        ┌──────────────┴──────────────┐
        │                             │
   PASS 1: ARCHITECT             PASS 2..N: EXTRACT
   (first 6 + last 4 +           (page-by-page,
    sampled interior)              with book context)
        │                             │
   book.json                       page-NN.json  (×N)
        │                             │
        └──────────────┬──────────────┘
                       │
                 PASS 3: ASSEMBLE
              (build EPUB, fix order,
               verify TOC, render check)
                       │
                  output.epub
```

## Files in this pack

| File                  | Role                                                |
|-----------------------|-----------------------------------------------------|
| `prompts/system.md`   | System prompt — persona, global rules, JSON schema  |
| `prompts/architect.md`| Pass 1 prompt — book metadata + chapter spine       |
| `prompts/extract.md`  | Pass 2 prompt — per-page extraction                 |
| `prompts/assemble.md` | Pass 3 prompt — final EPUB assembly + QA            |
| `schema/page.json`    | Output schema for a single extracted page           |
| `schema/book.json`    | Output schema for the architect pass                |
| `examples/page-001.json`  | Worked example: a normal body page               |
| `examples/page-042.json`  | Worked example: chapter start with drop cap      |
| `examples/book.json`      | Worked example: book.json from Pass 1           |

## Quick start for an engine builder

1. Render every PDF page to PNG at 250 DPI. Name them `page-0001.png` …
2. Pick the architect sample set:
   - pages 1–6 (front matter + cover + first chapter start)
   - last 4 pages (back matter, last chapter end)
   - 8–12 evenly-spaced interior pages (sampling for structure confirmation)
3. Send those as one multi-image call → `prompts/architect.md` → parse → `book.json`
4. For each page in order, send a single-image call with `prompts/extract.md`
   (page image + `book.json` + the previous page's `<section>` so the model
   can continue mid-paragraph if a paragraph breaks across pages) → `page-NNNN.json`
5. Send the assembled chapter bundles to `prompts/assemble.md` → emits the
   final nav.xhtml + chapter XHTML files + cover image + metadata.opf
6. Zip the result with mimetype as the first entry → `.epub`

## Included engine

This repository includes a small Python engine, `pdf2epub.py`, wired for the
OpenAI-compatible vision endpoint:

- API base URL: `https://dav.smre.run.place/v1`
- Vision model: `qwen/qwen3.5-397b-a17b`

Set an API key with either `DAV_API_KEY` or `OPENAI_API_KEY`, then run:

```bash
python3 pdf2epub.py run "input.pdf" --work work --epub out/book.epub
```

Useful subcommands:

```bash
python3 pdf2epub.py render "input.pdf" --out work/pages --dpi 250
python3 pdf2epub.py architect --pages work/pages --out work
python3 pdf2epub.py extract --pages work/pages --book work/book.json --out work --concurrency 8
python3 pdf2epub.py assemble --book work/book.json --pages work/pages --epub out/book.epub
```

The engine sends page images as base64 `data:` URLs to
`/chat/completions`, requests `response_format: {"type": "json_object"}`, and
writes intermediate artifacts to `work/book.json` and `work/pages/page-NNNN.json`.

## Model notes — qwen/qwen3.5-397b-a17b

This is a 397B MoE with 17B active, vision-capable. Practical properties:

- **Strong** at: structured JSON output, layout reasoning, OCR, multilingual.
- **Watch out for**: occasional JSON drift on long outputs, occasional
  hallucinated text when image is low-contrast, sometimes folds a header
  into body if the system prompt is weak.
- **Mitigations baked into these prompts**: rigid schema, explicit
  `[illegible]` marker, low temperature, `response_format: json_object`,
  short output budget per page, hard "no invented text" rule.

## Token budget

- Page image (250 dpi, A5-ish): ~800–1500 image tokens.
- Page extraction prompt (with context): ~1500–2500 text tokens.
- Page extraction response: ~600–2500 tokens.
- Total per page: ~3000–6000 tokens. For a 400-page book that's roughly
  1.5–2.4M tokens total. Plan a concurrency of 8–16 parallel page calls.
