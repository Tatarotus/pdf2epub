# Architect prompt — Pass 1.

Run this **once** at the start of the pipeline, on a curated set of pages:
first 6 + last 4 + 8–12 evenly-spaced interior pages (total ~18–22 pages).

The system prompt (`prompts/system.md`) is already loaded as the system
message. This file is the **user** message for the architect call.

---

## User message

You are doing the **architect pass** for a book. You will see ~20 sampled
pages from a PDF rendered as images. Your job is to look at the whole book
through these samples and decide:

1. The book's bibliographic metadata.
2. The **spine**: ordered list of every chapter / front-matter / back-matter
   unit, with the **first page number** of each unit in the original PDF.
3. The reading conventions used in this book (language, direction, has
   running headers, has folios, has drop caps, has two-column, etc.).
4. Any tricky structural elements the extractor must watch out for.

You may receive images labeled like `page-0001.png`, `page-0042.png`, etc.
The number is the 1-indexed page number in the original PDF.

### Output (strict JSON, no fences)

```jsonc
{
  "title": "The Crossing",
  "subtitle": null,
  "authors": ["Cormac McCarthy"],
  "language": "en",                       // BCP-47, lowercase
  "direction": "ltr",                     // "rtl" for Arabic, Hebrew, Persian, Urdu
  "publisher": "Knopf",
  "published_year": 1994,
  "isbn": null,
  "description": "One sentence summary if inferable from back cover; null otherwise.",

  "spine": [
    {
      "id": "front-cover",
      "title": "Cover",
      "first_page": 1,
      "kind": "cover",
      "notes": null
    },
    {
      "id": "front-half-title",
      "title": "Half Title",
      "first_page": 2,
      "kind": "half_title"
    },
    {
      "id": "front-title",
      "title": "Title Page",
      "first_page": 3,
      "kind": "title_page"
    },
    {
      "id": "front-copyright",
      "title": "Copyright",
      "first_page": 4,
      "kind": "copyright"
    },
    {
      "id": "front-dedication",
      "title": "Dedication",
      "first_page": 5,
      "kind": "dedication"
    },
    {
      "id": "front-toc",
      "title": "Contents",
      "first_page": 6,
      "kind": "toc"
    },
    {
      "id": "ch01",
      "title": "The Wild Horses",
      "first_page": 13,
      "kind": "chapter",
      "running_header": "The Wild Horses"
    },
    {
      "id": "ch02",
      "title": "A Beach Beyond",
      "first_page": 41,
      "kind": "chapter",
      "running_header": "A Beach Beyond"
    }
    // … continue through end
  ],

  "conventions": {
    "has_running_headers": true,
    "has_page_numbers": true,
    "page_number_position": "bottom_center",   // top_outer | bottom_outer | bottom_center | top_center
    "has_drop_caps": true,
    "has_two_column": false,
    "has_footnotes": true,
    "footnote_style": "numbered_per_page",     // numbered_per_page | numbered_per_chapter | symbols
    "first_page_number_visible": 13            // the printed folio of the first body page (PDF page may differ)
  },

  "extraction_warnings": [
    "Chapter 7 has 4 full-page plates inserted — flag any page that is mostly image as content_type=plate.",
    "Page 220 has a fold-out map — it spans pages 220-222 in the PDF.",
    "The book switches from English to Spanish in chapter 12 — preserve Spanish text exactly."
  ],

  "confidence": 0.88,
  "notes": "Spine inferred from running headers and large centered titles on pages 13, 41, 70, 102, 135, 174, 220, 256."
}
```

### Rules for this pass

1. **Spine ordering is sacred.** List everything from page 1 to the last page,
   in order. If you cannot confidently identify a unit, give it a generic id
   like `front-matter-unknown-3` and a best-guess `title`, and flag it in
   `extraction_warnings`.

2. **`first_page` is the PDF page number**, not the printed folio. The engine
   maps between them later. If the printed folio is the more useful anchor
   for the user, also fill `first_page_printed`.

3. **`running_header` per chapter**: copy it verbatim from a real page. If the
   book has no running headers, set to `null`. The extractor uses this to
   detect and strip headers from body pages.

4. **Be conservative.** If you only see the first 6 pages, don't claim you
   know all chapter titles — flag what's confirmed and what's inferred. Lower
   `confidence` accordingly.

5. **Do not include blank pages** in the spine unless they clearly serve a
   function (e.g. blank verso before a chapter start). Otherwise the EPUB
   spine gets full of noise.

6. **Plates and fold-outs** go in the spine as their own items with
   `kind: "plate"` or `kind: "foldout"`, with a clear `title` and
   `first_page`.

7. **Front matter and back matter** are real units. Tag them with
   `kind: "preface" | "foreword" | "introduction" | "acknowledgments" |
   "dedication" | "epigraph" | "toc" | "half_title" | "title_page" |
   "copyright" | "contents" | "appendix" | "glossary" | "bibliography" |
   "index" | "colophon" | "about_author" | "other"`.

8. **Output only the JSON object.** No prose, no fences, no commentary.

### What you do NOT do in this pass

- You do not extract body text. That comes in Pass 2.
- You do not describe images in detail. Just enough to identify them.
- You do not guess page numbers for the printed TOC — those are on the page
  and Pass 2 will copy them faithfully.
