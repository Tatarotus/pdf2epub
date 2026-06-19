# Extract prompt — Pass 2 (the workhorse).

Run this **once per page**. The system prompt (`prompts/system.md`) is the
system message. This file is the user message template — the engine fills in
the bracketed slots.

The model receives **one page image** plus the **full book.json** plus the
**previous page's emitted `<section>`** (so it can keep a paragraph whole if
the paragraph broke across the page boundary).

---

## User message template

```
You are extracting page {PAGE_NUMBER} of "{BOOK_TITLE}" by {AUTHOR}.

### Current book context (from Pass 1)

{BOOK_JSON_INLINE}

### Previous page's last element (so you can continue paragraphs that broke across pages)

{PREVIOUS_PAGE_TAIL_XHTML}

If the previous page ended mid-paragraph, your first <p> on this page
continues that paragraph — do not start a new <p>, just continue the prose
and close the </p> on this page.

### This page

You are looking at page {PAGE_NUMBER} of the original PDF (image attached).

Emit the JSON object per the system schema.

### Hints for this page

- Current chapter id: {CURRENT_CHAPTER_ID} ({CURRENT_CHAPTER_TITLE})
- Expected running header on this page: {EXPECTED_RUNNING_HEADER_OR_null}
  → If you see this on the page, strip it (do NOT include in xhtml) and
    record it under chrome_detected.
- Expected folio (printed page number): {EXPECTED_FOLIO_OR_null}
  → Same: strip from body, record under chrome_detected.
- This page is: {PAGE_KIND_HINT}  // e.g. "chapter_body", "chapter_start",
  // "plate", "cover", "blank", "front-matter-toc", etc.
- Watch out: {ANY_EXTRACTION_WARNINGS_FOR_THIS_REGION}

If PAGE_KIND_HINT is "chapter_start", make sure:
- You render the chapter title as <h1 class="chapter-title">{TITLE}</h1>
- You render "Chapter N" or "CHAPTER N" above it as <p class="chapter-label">Chapter N</p>
  if it appears on the page
- You render any epigraph as <blockquote class="epigraph">…</blockquote>
- The first body paragraph may have a drop cap — wrap it accordingly
- Set is_chapter_start: true

If PAGE_KIND_HINT is "plate":
- xhtml is ""
- images[] has one entry with is_cover: false, full bbox_hint
- content_type: "plate"

If PAGE_KIND_HINT is "blank":
- xhtml is ""
- content_type: "blank"
- images: []

### Output

JSON only. No fences. No commentary.
```

---

## Worked input/output

### Input image (described): a page from a novel

- Page 87 of the PDF.
- Top of page: small running header "CHAPTER 7 — THE CROSSING" right-aligned.
- Centered page number "112" at the bottom.
- Body: single column, justified. Two paragraphs. The second paragraph
  starts with a large ornate "T" drop cap followed by "he river was high
  that spring and the willows along the bank were still bare."
- Halfway down the second paragraph, an italicized phrase: "*he thought of
  his father*".
- At the very bottom, a footnote separator rule and a small footnote:
  "¹ Smith, op. cit., p. 14." with a superscript "1" inside the second
  paragraph after the word "spring".

### Expected output

```json
{
  "page_number": 87,
  "chapter_id": "ch07",
  "is_chapter_start": false,
  "content_type": "chapter_body",
  "xhtml": "<p>First paragraph text, fully merged across any line breaks…</p><p class=\"has-dropcap\"><span class=\"dropcap\">T</span>he river was high that spring<sup><a href=\"#fn-87-1\" id=\"fnref-87-1\">1</a></sup> and the willows along the bank were still bare. He <em>thought of his father</em> as he watched the current take a branch downriver.</p>",
  "images": [],
  "chrome_detected": [
    { "kind": "running_header", "text": "CHAPTER 7 — THE CROSSING" },
    { "kind": "page_number", "text": "112" }
  ],
  "footnotes": [
    { "id": "fn-87-1", "text": "Smith, op. cit., p. 14." }
  ],
  "confidence": 0.95,
  "notes": "Single column, justified, one drop cap on second paragraph, one footnote, one italicized phrase."
}
```

Note how:
- The running header and the page number are stripped from `xhtml`.
- The drop cap is wrapped in `<span class="dropcap">`.
- The footnote anchor in the body uses `<sup><a href="#fn-87-1">1</a></sup>`.
- The footnote text is in `footnotes[]`, not in `xhtml` body.
- The italic is preserved as `<em>`.
- No CSS except the agreed class names.

---

## Engine-side notes (not part of the prompt)

- **`{PREVIOUS_PAGE_TAIL_XHTML}`**: send the last ~600 chars of the previous
  page's emitted `xhtml`, **wrapped in a single root** so the model can see
  the open `<p>` etc. Don't send the whole previous page; tokens matter.
- **`{BOOK_JSON_INLINE}`**: inline the full book.json. It's small (~2–5KB)
  and gives the model crucial context.
- **One image per call.** Don't batch pages; layout varies too much.
- **Temperature 0.1**, `response_format: {"type": "json_object"}` if the
  endpoint supports it.
- **Max output tokens 3000** is plenty for almost all pages; bump to 4000
  for chapters with dense tables or long quoted passages.
- **Retry policy**: if the response isn't valid JSON, retry once with the
  same prompt + a one-line "previous output was not valid JSON, return only
  the JSON object". If still broken, log and skip the page (mark as
  `confidence: 0`).
- **Images on pages**: the engine renders the page once for the LLM, and
  also crops the original page render using the `bbox_hint` from the
  response. The crop becomes the actual `<img>` in the EPUB. The LLM never
  has to embed image data — just coordinates and a description.
