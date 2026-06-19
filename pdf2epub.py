#!/usr/bin/env python3
"""PDF to EPUB vision pipeline engine.

This is a small orchestrator for the prompt pack in this repository. It renders
PDF pages to images, calls an OpenAI-compatible vision chat-completions API, and
writes the intermediate JSON files needed to assemble an EPUB.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


DEFAULT_BASE_URL = "https://dav.smre.run.place/v1"
DEFAULT_MODEL = "qwen/qwen3.5-397b-a17b"
DEFAULT_DPI = 250


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    model: str
    api_key: str
    temperature: float
    top_p: float
    max_tokens: int
    request_timeout: int


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(read_text(path))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def page_sort_key(path: Path) -> tuple[int, str]:
    digits = "".join(ch for ch in path.stem if ch.isdigit())
    return (int(digits) if digits else 0, path.name)


def image_data_url(path: Path, max_edge: int = 650, quality: int = 65) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
    media_type = "image/jpeg"
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def render_pages(pdf: Path, pages_dir: Path, dpi: int) -> list[Path]:
    if not pdf.exists():
        raise PipelineError(f"PDF not found: {pdf}")
    if shutil.which("pdftoppm") is None:
        raise PipelineError("pdftoppm is required to render PDF pages.")

    pages_dir.mkdir(parents=True, exist_ok=True)
    prefix = pages_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(pdf), str(prefix)],
        check=True,
    )
    pages = sorted(pages_dir.glob("page-*.png"), key=page_sort_key)
    if not pages:
        raise PipelineError(f"No rendered pages found in {pages_dir}")
    return pages


def discover_pages(pages_dir: Path) -> list[Path]:
    pages = sorted(pages_dir.glob("page-*.png"), key=page_sort_key)
    if not pages:
        raise PipelineError(f"No page PNG files found in {pages_dir}")
    return pages


def sample_architect_pages(pages: list[Path], interior_samples: int = 10) -> list[Path]:
    total = len(pages)
    if total <= 30:
        indexes = set(range(min(6, total)))
        indexes.update(range(max(0, total - 2), total))
        return [pages[i] for i in sorted(indexes)]
    front_count = 4 if total <= 30 else 6
    back_count = 2 if total <= 30 else 4
    indexes = set(range(min(front_count, total)))
    indexes.update(range(max(0, total - back_count), total))
    interior_count = min(interior_samples, 2 if total <= 30 else interior_samples)
    if total > 10 and interior_count > 0:
        for i in range(1, interior_count + 1):
            indexes.add(round((total - 1) * i / (interior_count + 1)))
    return [pages[i] for i in sorted(indexes)]


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def chat_completion(
    config: Config,
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
    retry_json: bool = True,
) -> dict[str, Any]:
    endpoint = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": max_tokens or config.max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                response_body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if exc.code in {502, 503, 504} and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise PipelineError(f"API request failed with HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise PipelineError(f"API request failed: {exc}") from exc

    data = json.loads(response_body)
    content = data["choices"][0]["message"]["content"]
    try:
        return extract_json_object(content)
    except json.JSONDecodeError:
        if not retry_json:
            raise
        retry_messages = messages + [
            {
                "role": "user",
                "content": "Previous output was not valid JSON. Return only the JSON object.",
            }
        ]
        return chat_completion(config, retry_messages, max_tokens=max_tokens, retry_json=False)


def architect_pass(config: Config, repo: Path, pages: list[Path], out_dir: Path) -> dict[str, Any]:
    system_prompt = read_text(repo / "prompts/system.md")
    architect_prompt = read_text(repo / "prompts/architect.md")
    content: list[dict[str, Any]] = [{"type": "text", "text": architect_prompt}]
    for page in sample_architect_pages(pages):
        content.append({"type": "text", "text": f"Image label: {page.name}"})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(page, max_edge=450, quality=58)}})
    book = chat_completion(
        config,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        max_tokens=6000,
    )
    write_json(out_dir / "book.json", book)
    return book


def spine_entry_for_page(book: dict[str, Any], page_number: int) -> dict[str, Any]:
    spine = sorted(book.get("spine", []), key=lambda item: item.get("first_page", 1))
    current = spine[0] if spine else {"id": "unassigned", "title": "Unknown", "kind": "other"}
    for entry in spine:
        if entry.get("first_page", 1) <= page_number:
            current = entry
        else:
            break
    return current


def expected_folio(book: dict[str, Any], entry: dict[str, Any], page_number: int) -> int | None:
    printed = entry.get("first_page_printed")
    if printed is not None:
        return int(printed) + (page_number - int(entry.get("first_page", page_number)))
    first_visible = book.get("conventions", {}).get("first_page_number_visible")
    if first_visible is None:
        return None
    return None


def page_kind_hint(entry: dict[str, Any], page_number: int) -> str:
    kind = entry.get("kind", "other")
    if kind == "chapter":
        return "chapter_start" if entry.get("first_page") == page_number else "chapter_body"
    return str(kind)


def render_extract_prompt(template_text: str, values: dict[str, Any]) -> str:
    rendered = template_text
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def extract_one_page(
    config: Config,
    repo: Path,
    book: dict[str, Any],
    page_path: Path,
    page_number: int,
    previous_tail: str,
    out_dir: Path,
) -> dict[str, Any]:
    system_prompt = read_text(repo / "prompts/system.md")
    extract_prompt = read_text(repo / "prompts/extract.md")
    entry = spine_entry_for_page(book, page_number)
    values = {
        "PAGE_NUMBER": page_number,
        "BOOK_TITLE": book.get("title", "Unknown"),
        "AUTHOR": ", ".join(book.get("authors", ["Unknown"])),
        "BOOK_JSON_INLINE": json.dumps(book, ensure_ascii=False, indent=2),
        "PREVIOUS_PAGE_TAIL_XHTML": previous_tail or "",
        "CURRENT_CHAPTER_ID": entry.get("id", "unassigned"),
        "CURRENT_CHAPTER_TITLE": entry.get("title", "Unknown"),
        "EXPECTED_RUNNING_HEADER_OR_null": entry.get("running_header") or "null",
        "EXPECTED_FOLIO_OR_null": expected_folio(book, entry, page_number) or "null",
        "PAGE_KIND_HINT": page_kind_hint(entry, page_number),
        "ANY_EXTRACTION_WARNINGS_FOR_THIS_REGION": "; ".join(book.get("extraction_warnings", [])) or "none",
        "TITLE": entry.get("title", "Unknown"),
    }
    prompt = render_extract_prompt(extract_prompt, values)
    time.sleep(random.uniform(0, 0.25))
    result = chat_completion(
        config,
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(page_path)}},
                ],
            },
        ],
    )
    result["page_number"] = page_number
    write_json(out_dir / f"page-{page_number:04d}.json", result)
    return result


def extract_pages(
    config: Config,
    repo: Path,
    pages: list[Path],
    book: dict[str, Any],
    out_dir: Path,
    concurrency: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    page_out = out_dir / "pages"
    selected_pages = pages[:limit] if limit else pages
    previous_tail_by_page: dict[int, str] = {}
    results: dict[int, dict[str, Any]] = {}

    # Prompts benefit from previous-page context, so submit in bounded batches
    # and use the last completed prior page when available.
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}
        for index, page in enumerate(selected_pages, start=1):
            existing = page_out / f"page-{index:04d}.json"
            if existing.exists():
                result = read_json(existing)
                results[index] = result
                previous_tail_by_page[index] = str(result.get("xhtml", ""))[-600:]
                continue
            previous_tail = previous_tail_by_page.get(index - 1, "")
            future = executor.submit(
                extract_one_page,
                config,
                repo,
                book,
                page,
                index,
                previous_tail,
                page_out,
            )
            futures[future] = index
        for future in as_completed(futures):
            page_number = futures[future]
            result = future.result()
            results[page_number] = result
            previous_tail_by_page[page_number] = str(result.get("xhtml", ""))[-600:]

    return [results[i] for i in sorted(results)]


def sanitize_filename(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    return safe or "item"


def wrap_xhtml(title: str, body: str, language: str, direction: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}" dir="{direction}">
<head>
  <title>{escape_xml(title)}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
<section>
{body}
</section>
</body>
</html>
'''


def escape_xml(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_nav(book: dict[str, Any], chapter_files: dict[str, str]) -> str:
    items = []
    for entry in book.get("spine", []):
        href = chapter_files.get(entry["id"])
        if href:
            items.append(f'    <li><a href="{href}">{escape_xml(entry["title"])}</a></li>')
    toc = "\n".join(items)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Navigation</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
{toc}
    </ol>
  </nav>
</body>
</html>
'''


def build_opf(book: dict[str, Any], chapter_files: dict[str, str], image_files: dict[str, str]) -> str:
    identifier = book.get("isbn") or f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_items = ['    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine_items = []
    for idx, href in enumerate(chapter_files.values(), start=1):
        item_id = f"item-{idx}"
        manifest_items.append(f'    <item id="{item_id}" href="{href}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'    <itemref idref="{item_id}"/>')
    for image_id, href in image_files.items():
        media_type = mimetypes.guess_type(href)[0] or "image/png"
        manifest_items.append(f'    <item id="{image_id}" href="{href}" media-type="{media_type}"/>')
    manifest = "\n".join(manifest_items)
    spine = "\n".join(spine_items)
    creators = "\n".join(f"    <dc:creator>{escape_xml(author)}</dc:creator>" for author in book.get("authors", []))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{escape_xml(identifier)}</dc:identifier>
    <dc:title>{escape_xml(book.get("title", "Untitled"))}</dc:title>
{creators}
    <dc:language>{escape_xml(book.get("language", "en"))}</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
{manifest}
    <item id="style" href="style.css" media-type="text/css"/>
  </manifest>
  <spine>
{spine}
  </spine>
</package>
'''


def assemble_epub(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    out_dir: Path,
    epub_path: Path,
    source_pages_dir: Path | None = None,
) -> None:
    epub_path = epub_path.resolve()
    epub_root = out_dir / "epub"
    oebps = epub_root / "OEBPS"
    images_dir = oebps / "images"
    meta_inf = epub_root / "META-INF"
    if epub_root.exists():
        shutil.rmtree(epub_root)
    oebps.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    meta_inf.mkdir(parents=True)

    groups: dict[str, list[str]] = {}
    image_files: dict[str, str] = {}
    cover_page: int | None = None
    for page in pages:
        page_number = int(page.get("page_number", 1))
        spine_entry = spine_entry_for_page(book, page_number)
        groups.setdefault(spine_entry.get("id", page.get("chapter_id", "unassigned")), []).append(page.get("xhtml", ""))
        for image in page.get("images", []):
            if image.get("is_cover"):
                cover_page = page_number

    chapter_files: dict[str, str] = {}
    language = book.get("language", "en")
    direction = book.get("direction", "ltr")
    if cover_page and source_pages_dir:
        source = sorted(source_pages_dir.glob(f"page-{cover_page:02d}.png"), key=page_sort_key)
        if not source:
            source = sorted(source_pages_dir.glob(f"page-{cover_page:03d}.png"), key=page_sort_key)
        if source:
            cover_name = "images/cover.png"
            shutil.copyfile(source[0], oebps / cover_name)
            image_files["cover-image"] = cover_name
            chapter_files["front-cover"] = "cover.xhtml"
            (oebps / "cover.xhtml").write_text(
                wrap_xhtml(
                    book.get("title", "Cover"),
                    '<figure class="cover"><img src="images/cover.png" alt="Cover"/></figure>',
                    language,
                    direction,
                ),
                encoding="utf-8",
            )

    for entry in book.get("spine", []):
        chapter_id = entry["id"]
        if chapter_id in chapter_files:
            continue
        body = "\n".join(groups.get(chapter_id, []))
        if not body:
            continue
        filename = sanitize_filename(chapter_id) + ".xhtml"
        chapter_files[chapter_id] = filename
        (oebps / filename).write_text(
            wrap_xhtml(entry.get("title", chapter_id), body, language, direction),
            encoding="utf-8",
        )

    (epub_root / "mimetype").write_text("application/epub+zip", encoding="ascii")
    (meta_inf / "container.xml").write_text(
        """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/metadata.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    (oebps / "style.css").write_text(
        """@page { size: 454pt 652pt; margin: 54pt 56pt 42pt; }
body {
  font-family: "Minion Pro", Georgia, serif;
  line-height: 1.38;
  color: #111;
}
h1.chapter-title {
  font-family: "Prensa", Georgia, serif;
  font-size: 2.4em;
  line-height: 1.05;
  margin: 2.2em 0 0.8em;
}
h2 {
  font-family: "National HBR", "News Gothic", Arial, sans-serif;
  font-size: 1.15em;
  margin: 1.4em 0 0.6em;
}
p { margin: 0 0 0.85em; text-align: justify; }
.author { font-style: italic; margin-bottom: 4em; }
.dropcap {
  float: left;
  font-size: 7.5em;
  line-height: 0.72;
  color: #e5e5e5;
  margin: 0.02em 0.05em 0 0;
}
.smallcaps { font-variant: small-caps; }
.summary-box, .sidebar {
  border-top: 1px solid #999;
  border-bottom: 1px solid #999;
  margin: 1.2em 0;
  padding: 0.8em 0;
}
blockquote { margin: 1.2em 1.5em; font-style: italic; }
.centered, .publisher-url { text-align: center; }
.cover { margin: 0; }
.cover img {
  display: block;
  width: auto;
  max-width: 100%;
  max-height: 100vh;
  margin: 0 auto;
}
""",
        encoding="utf-8",
    )
    (oebps / "nav.xhtml").write_text(build_nav(book, chapter_files), encoding="utf-8")
    (oebps / "metadata.opf").write_text(build_opf(book, chapter_files, image_files), encoding="utf-8")

    if shutil.which("zip") is None:
        raise PipelineError("zip is required to package the EPUB.")
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    if epub_path.exists():
        epub_path.unlink()
    subprocess.run(["zip", "-X0", str(epub_path), "mimetype"], cwd=epub_root, check=True)
    subprocess.run(["zip", "-rX9", str(epub_path), "META-INF", "OEBPS"], cwd=epub_root, check=True)


def load_config(args: argparse.Namespace) -> Config:
    api_key = args.api_key or os.getenv("DAV_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key and args.command in {"architect", "extract", "run"}:
        raise PipelineError("Set DAV_API_KEY or OPENAI_API_KEY, or pass --api-key.")
    return Config(
        base_url=args.base_url,
        model=args.model,
        api_key=api_key or "",
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
        request_timeout=args.request_timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDF to EPUB vision pipeline")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--request-timeout", type=int, default=180)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="Render PDF pages to PNG")
    render.add_argument("pdf", type=Path)
    render.add_argument("--out", type=Path, default=Path("work/pages"))
    render.add_argument("--dpi", type=int, default=DEFAULT_DPI)

    architect = subparsers.add_parser("architect", help="Run architect pass")
    architect.add_argument("--pages", type=Path, default=Path("work/pages"))
    architect.add_argument("--out", type=Path, default=Path("work"))

    extract = subparsers.add_parser("extract", help="Run page extraction pass")
    extract.add_argument("--pages", type=Path, default=Path("work/pages"))
    extract.add_argument("--book", type=Path, default=Path("work/book.json"))
    extract.add_argument("--out", type=Path, default=Path("work"))
    extract.add_argument("--concurrency", type=int, default=8)
    extract.add_argument("--limit", type=int, default=None)

    assemble = subparsers.add_parser("assemble", help="Build an EPUB from extraction JSON")
    assemble.add_argument("--book", type=Path, default=Path("work/book.json"))
    assemble.add_argument("--pages", type=Path, default=Path("work/pages"))
    assemble.add_argument("--out", type=Path, default=Path("work"))
    assemble.add_argument("--epub", type=Path, default=Path("out/book.epub"))

    run = subparsers.add_parser("run", help="Render, architect, extract, and package")
    run.add_argument("pdf", type=Path)
    run.add_argument("--work", type=Path, default=Path("work"))
    run.add_argument("--epub", type=Path, default=Path("out/book.epub"))
    run.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--limit", type=int, default=None)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent
    try:
        config = load_config(args)
        if args.command == "render":
            pages = render_pages(args.pdf, args.out, args.dpi)
            print(f"Rendered {len(pages)} pages to {args.out}")
        elif args.command == "architect":
            pages = discover_pages(args.pages)
            book = architect_pass(config, repo, pages, args.out)
            print(f"Wrote architect output for {book.get('title', 'Untitled')} to {args.out / 'book.json'}")
        elif args.command == "extract":
            pages = discover_pages(args.pages)
            book = read_json(args.book)
            results = extract_pages(config, repo, pages, book, args.out, args.concurrency, args.limit)
            print(f"Wrote {len(results)} page extraction files to {args.out / 'pages'}")
        elif args.command == "assemble":
            book = read_json(args.book)
            page_files = sorted(args.pages.glob("page-*.json"))
            pages = []
            for path in page_files:
                page = read_json(path)
                page["page_number"] = page_sort_key(path)[0]
                pages.append(page)
            assemble_epub(book, pages, args.out, args.epub, args.pages)
            print(f"Wrote EPUB to {args.epub}")
        elif args.command == "run":
            pages_dir = args.work / "rendered"
            pages = render_pages(args.pdf, pages_dir, args.dpi)
            book = architect_pass(config, repo, pages, args.work)
            results = extract_pages(config, repo, pages, book, args.work, args.concurrency, args.limit)
            assemble_epub(book, results, args.work, args.epub, pages_dir)
            print(f"Wrote EPUB to {args.epub}")
    except (PipelineError, subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
