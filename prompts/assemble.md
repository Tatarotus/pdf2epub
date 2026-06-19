# Assemble / QA prompt — Pass 3.

Run this **once** at the end, after every page has been extracted and
chapter-level bundles have been stitched. The system prompt is the system
message. This file is the user message.

Goal: produce the final EPUB artifact description — nav.xhtml, metadata.opf,
chapter XHTML files, cover image reference — and flag any issues the engine
should fix or surface to a human.

---

## User message

You are doing the **final assembly + QA pass** for "{BOOK_TITLE}".

### Inputs

The engine gives you:

1. The `book.json` from Pass 1.
2. All `page-NNNN.json` extraction results, in order.
3. The chapter-level XHTML bundles already stitched by the engine:
   ```
   bundles/
     front-cover.xhtml
     front-half-title.xhtml
     front-title.xhtml
     front-copyright.xhtml
     front-dedication.xhtml
     front-toc.xhtml
     ch01.xhtml
     ch02.xhtml
     …
     back-index.xhtml
     back-colophon.xhtml
   ```

### Your job

A. **Build `nav.xhtml`** — the EPUB 3 navigation document.
B. **Build `metadata.opf`** — the package document.
C. **Audit** every chapter bundle for:
   - broken links (e.g. footnote anchor with no matching footnote)
   - orphan footnote references
   - duplicate `id` attributes within a chapter
   - missing `<title>` on chapter roots
   - obvious tag imbalance
   - chrome that wasn't stripped
   - hallucinated or garbled text
   - paragraph breaks at wrong places (sentence starts with a lowercase
     letter that should be continuation of a previous paragraph, etc.)
D. **Output a final JSON report** describing:
   - `nav.xhtml` content (full file body)
   - `metadata.opf` content (full file body)
   - per-chapter audit results
   - any human-review items

### Output (strict JSON)

```jsonc
{
  "nav_xhtml": "<nav xmlns=\"…\" …>…full nav.xhtml…</nav>",
  "metadata_opf": "<?xml version=\"1.0\"…?>…full package document…</package>",

  "cover": {
    "image_path": "images/cover.jpg",
    "source_page": 1,
    "confirmed": true
  },

  "chapter_audit": [
    {
      "chapter_id": "ch07",
      "page_range": [87, 119],
      "issues": [
        {
          "severity": "warn",            // info | warn | error
          "kind": "orphaned_footnote",
          "detail": "Page 102 has <a href=\"#fn-102-3\"> but no fn-102-3 in footnotes[]."
        },
        {
          "severity": "info",
          "kind": "mid_paragraph_break",
          "detail": "Page 112 first <p> opens with a lowercase letter — looks like a paragraph split mid-sentence."
        }
      ],
      "confidence": 0.92
    }
  ],

  "human_review": [
    {
      "page": 142,
      "reason": "Confidence 0.41 from extractor — page is a fold-out map, image extraction may be mis-cropped.",
      "suggested_action": "Re-render at 400 dpi and re-extract, or accept with current bbox."
    }
  ],

  "stats": {
    "total_pages": 387,
    "total_chapters": 24,
    "total_images": 47,
    "total_footnotes": 213,
    "estimated_word_count": 118420,
    "warnings": 3,
    "errors": 0
  },

  "ready_to_package": true   // false if any errors
}
```

### Rules

1. **Use the EPUB 3 spec** for nav.xhtml. Landmarks are required if you can
   infer them (cover, toc, start of body, index). The `nav` element has
   `epub:type="toc"` for the main TOC and `epub:type="landmarks"` for
   landmarks.

2. **For `metadata.opf`**, use OPF 3.0:
   - `<dc:title>`, `<dc:creator>`, `<dc:language>`, `<dc:identifier>`
     (use ISBN if present, else UUID)
   - `<meta property="dcterms:modified">` with current ISO 8601 timestamp
   - `<manifest>` listing every XHTML file with `media-type="application/xhtml+xml"`
     and every image with the correct type
   - `<spine>` with `itemref` in reading order
   - `<guide>` with cover, toc, start references

3. **The nav's TOC must use the chapter titles from book.json**, not from
   the printed TOC page (which may be wrong or shortened). Link to each
   chapter XHTML by its id.

4. **In nav.xhtml only**: nest sections (`<ol>` inside `<li>`) if the book
   has parts / books / numbered parts containing chapters. Otherwise flat.

5. **Audit severities:**
   - `error`: missing nav entry, broken structural tag, missing required
     metadata → blocks packaging
   - `warn`: orphaned footnote, suspicious paragraph split, missing image
     alt → fix automatically if possible, otherwise surface
   - `info`: stylistic concerns, mild inconsistencies → log only

6. **Fix what you can.** If a footnote ref is orphaned, drop it from the
   body. If a duplicate `id` exists, rename. If chrome slipped through,
   strip it. Report what you changed.

7. **`ready_to_package: true` only when there are zero `error`-severity
   issues.** `warn` and `info` are allowed.

8. **Do not re-extract pages.** You are auditing the stitched bundles. If
   something is fundamentally wrong with a page (e.g. it's mostly
   hallucinated), flag for human review, don't try to rewrite it yourself.

### Output discipline

JSON only. No prose. The engine writes the files directly from your output.
