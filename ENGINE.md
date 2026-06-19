# Engine notes — orchestrating the pipeline

This document is for the engineer wiring the prompts to `qwen/qwen3.5-397b-a17b`.

## 0. Stack assumptions

- Node 20+ or Python 3.11+.
- A PDF rasterizer: `pdftoppm -r 250 -png input.pdf page` works.
- An EPUB packager: `zip` with the EPUB mimetype convention.
- Image cropping: `sharp` (Node) or `Pillow` (Python).
- LLM client: OpenAI-compatible chat-completions with vision (`image_url` content parts).

## 1. Render pages

```bash
mkdir -p work/pages
pdftoppm -r 250 -png input.pdf work/pages/page
# produces work/pages/page-001.png ... page-387.png
```

Pick DPI empirically per book:
- 200 dpi for plain novels (faster, sufficient).
- 250 dpi default.
- 350+ dpi for heavily illustrated books, fold-outs, or scans.

## 2. Build the architect sample set

```text
sample = pages[0..5]  +  pages[total-4..total-1]  +  every(total/10)
```

Pass these ~18–22 images in a single chat-completions call with
`prompts/architect.md` as the user message. Parse the JSON response with a
strict schema check. If it fails, retry once with a one-line nudge.

## 3. Page loop (the bulk of the cost)

For each page in `1..total`, in order, but **with concurrency 12** and a small
random jitter to avoid lockstep:

```text
messages = [
  { role: "system", content: load("prompts/system.md") },
  { role: "user", content: [
      { type: "text", text: render("prompts/extract.md", {
        PAGE_NUMBER: page_num,
        BOOK_TITLE: book.title,
        AUTHOR: book.authors[0],
        BOOK_JSON_INLINE: JSON.stringify(book, null, 2),
        PREVIOUS_PAGE_TAIL_XHTML: previous_tail,   // last 600 chars of prev xhtml
        CURRENT_CHAPTER_ID: current_chapter_id,
        CURRENT_CHAPTER_TITLE: current_chapter_title,
        EXPECTED_RUNNING_HEADER: expected_running_header,
        EXPECTED_FOLIO: expected_folio,
        PAGE_KIND_HINT: page_kind_hint,
        ANY_EXTRACTION_WARNINGS_FOR_THIS_REGION: warnings_for_region
      }) },
      { type: "image_url", image_url: { url: `file://${abs_path_to_page_png}` } }
  ]}
]
```

Settings: `temperature: 0.1`, `response_format: { type: "json_object" }`,
`max_tokens: 3000`, `top_p: 0.95`.

Track state per page:
- `current_chapter_id` updates when a previous page sets
  `is_chapter_start: true`.
- `expected_running_header` updates to the matching spine entry's value.
- `expected_folio` updates from `first_page_printed + offset`.
- `previous_tail` updates to the last ~600 chars of the current page's
  emitted `xhtml`.

## 4. Stitch pages into chapters

Group pages by `chapter_id`. Concatenate `xhtml` per chapter, preserving
order. Wrap in `<section class="chapter" id="…">` if not already wrapped.
Inline the images at the page offset indicated by `images[]` — the engine
should crop the original page render using `bbox_hint` and insert `<img>`
just before the next non-figure content.

## 5. Assemble + QA

Run `prompts/assemble.md` once with:
- all chapter bundles,
- the original book.json,
- the full list of `page-NNNN.json` extractions.

The response gives you `nav_xhtml`, `metadata_opf`, audit results, and human-
review items. Apply auto-fixes for `warn`s if the fix is mechanical
(orphan anchor → drop ref). Surface `error`s as blockers.

## 6. Package the EPUB

```bash
mkdir -p out/epub/{META-INF,OEBPS,OEBPS/images}

# mimetype must be the first entry, stored uncompressed
printf 'application/epub+zip' > out/epub/mimetype

# container.xml
cat > out/epub/META-INF/container.xml <<'XML'
<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/metadata.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
XML

# write all chapter XHTML, nav, opf, images
cp out/chapters/*.xhtml out/epub/OEBPS/
cp out/nav.xhtml out/epub/OEBPS/
cp out/metadata.opf out/epub/OEBPS/
cp out/images/* out/epub/OEBPS/images/

cd out/epub && zip -X0 ../book.epub mimetype
cd out/epub && zip -rX9 ../book.epub META-INF OEBPS
```

Validate with `epubcheck` if available. Fail loud on any structural error.

## 7. Failure modes & mitigations

| Symptom                                            | Likely cause                                   | Fix                                                                                  |
|----------------------------------------------------|------------------------------------------------|--------------------------------------------------------------------------------------|
| Model wraps body text in `<html><body>…`           | System prompt not loaded / too long extraction | Re-assert: "Output JSON only, xhtml is a fragment, no wrapper."                       |
| Model invents text in scanned pages                | DPI too low                                    | Re-render at 350 dpi; ask user to confirm or accept with `[illegible]` markers.      |
| Model drops running header into body               | Hint missing                                   | Always pass `EXPECTED_RUNNING_HEADER`. If not in book.json, set to "unknown".         |
| Model splits a paragraph mid-sentence at page break| `previous_tail` not provided                   | Always pass the previous page's tail.                                                |
| Footnote anchors have no matching footnote         | Footnote separator rule misread                | Audit pass catches this. Treat `warn` severity, drop the anchor, log it.              |
| Page is mostly image, model hallucinates text      | True plate                                     | Mark `content_type: "plate"` in the page-kind hint and `xhtml: ""`.                   |
| Two-column page rendered as one stream             | Layout not detected                            | Pass a hint: "this is a two-column academic page — read column 1 fully, then column 2". |
| RTL book rendered LTR                              | `direction` not threaded through               | Pass `direction: "rtl"` and add to extract prompt: "read right to left".              |
| Confidence < 0.5 on many pages                     | Bad scans, low DPI, mixed languages            | Bump DPI; flag the book for human review; ship what you have with notes in metadata. |

## 8. Cost & time estimate

For a 400-page book:
- Render: ~30s.
- Architect call: ~20s, ~30k tokens.
- Page loop at concurrency 12: ~6–10 minutes, ~1.5–2.4M tokens.
- Assemble: ~30s, ~80k tokens.
- Pack & validate: ~5s.

Total wall time: roughly **8–12 minutes** per book, dominated by the page
loop. Tokens dominate cost: ~$5–15 per book depending on your endpoint
pricing.
