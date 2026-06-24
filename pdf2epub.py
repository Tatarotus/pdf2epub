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

    # Check if pages are already rendered
    total_pages = None
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf)
        total_pages = len(reader.pages)
    except Exception:
        pass

    if total_pages is not None:
        existing_pages = sorted(pages_dir.glob("page-*.png"), key=page_sort_key)
        if len(existing_pages) == total_pages:
            print(f"  Found {total_pages} existing rendered pages in {pages_dir}. Skipping rendering.")
            return existing_pages

    if shutil.which("pdftoppm") is None:
        raise PipelineError("pdftoppm is required to render PDF pages.")

    print(f"  Rendering PDF pages to PNG (DPI: {dpi})...")
    # Clean up existing pages to avoid mismatched leftovers
    if pages_dir.exists():
        for f in pages_dir.glob("page-*.png"):
            try:
                f.unlink()
            except Exception:
                pass
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
    use_response_format: bool = False,
) -> dict[str, Any]:
    endpoint = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": max_tokens or config.max_tokens,
    }
    current_use_response_format = use_response_format
    current_model = payload["model"]
    
    attempt = 0
    max_attempts = 4
    
    while attempt < max_attempts:
        if current_use_response_format:
            payload["response_format"] = {"type": "json_object"}
        elif "response_format" in payload:
            del payload["response_format"]

        body = json.dumps(payload).encode("utf-8")
        try:
            with open("request_payload.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                response_body = response.read().decode("utf-8")
            
            data = json.loads(response_body)
            content = data["choices"][0]["message"]["content"]
            if not content:
                raise KeyError("content is empty or null")

            finish_reason = data["choices"][0].get("finish_reason")
            if finish_reason == "length":
                raise json.JSONDecodeError("Generation truncated due to max_tokens limit", content, len(content))

            try:
                return extract_json_object(content)
            except json.JSONDecodeError:
                if retry_json:
                    print("  Warning: Response was not valid JSON. Retrying with corrective prompt...")
                    retry_messages = messages + [
                        {
                            "role": "user",
                            "content": "Previous output was not valid JSON. Return only the JSON object.",
                        }
                    ]
                    return chat_completion(
                        config,
                        retry_messages,
                        max_tokens=payload["max_tokens"],
                        retry_json=False,
                        use_response_format=current_use_response_format
                    )
                else:
                    raise

        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if current_use_response_format and exc.code in {400, 500, 502, 503, 504}:
                print(f"  Warning: API request failed with HTTP {exc.code}. Retrying without response_format...")
                current_use_response_format = False
                continue
            if exc.code in {400, 429, 500, 502, 503, 504}:
                if current_model == "qwen/qwen3.5-397b-a17b":
                    print(f"  Warning: HTTP {exc.code} on Qwen. Switching fallback to Llama model (meta/llama-4-maverick-17b-128e-instruct)...")
                    current_model = "meta/llama-4-maverick-17b-128e-instruct"
                    payload["model"] = "meta/llama-4-maverick-17b-128e-instruct"
                    continue
                elif current_model == "meta/llama-4-maverick-17b-128e-instruct" and os.getenv("GEMINI_API_KEY"):
                    print(f"  Warning: HTTP {exc.code} on Llama. Switching fallback to Gemini API (gemini-3.5-flash)...")
                    current_model = "gemini-3.5-flash"
                    payload["model"] = "gemini-3.5-flash"
                    endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                    config = Config(
                        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                        model="gemini-3.5-flash",
                        api_key=os.getenv("GEMINI_API_KEY") or "",
                        temperature=config.temperature,
                        top_p=config.top_p,
                        max_tokens=payload["max_tokens"],
                        request_timeout=config.request_timeout
                    )
                    current_use_response_format = False
                    continue
            attempt += 1
            if attempt < max_attempts:
                sleep_time = 5 * attempt
                print(f"  Warning: HTTP {exc.code} on attempt {attempt}. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            raise PipelineError(f"API request failed with HTTP {exc.code}: {error_body}") from exc

        except urllib.error.URLError as exc:
            attempt += 1
            if attempt < max_attempts:
                sleep_time = 5 * attempt
                print(f"  Warning: Connection error on attempt {attempt}: {exc}. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            raise PipelineError(f"API request failed: {exc}") from exc

        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            if current_use_response_format:
                print(f"  Warning: API response parsing failed ({exc}). Retrying without response_format...")
                current_use_response_format = False
                continue
            current_max_tokens = payload["max_tokens"]
            if current_max_tokens < 8000:
                new_max_tokens = min(current_max_tokens + 2000, 8000)
                payload["max_tokens"] = new_max_tokens
                attempt += 1
                if attempt < max_attempts:
                    sleep_time = 5 * attempt
                    print(f"  Warning: Response error on current model ({exc}). Escalating max_tokens to {new_max_tokens} and retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue
            if current_model == "qwen/qwen3.5-397b-a17b":
                print(f"  Warning: Parsing/truncation error on Qwen ({exc}) and max_tokens limit reached. Switching fallback to Llama model (meta/llama-4-maverick-17b-128e-instruct)...")
                current_model = "meta/llama-4-maverick-17b-128e-instruct"
                payload["model"] = "meta/llama-4-maverick-17b-128e-instruct"
                continue
            elif current_model == "meta/llama-4-maverick-17b-128e-instruct" and os.getenv("GEMINI_API_KEY"):
                print(f"  Warning: Parsing/truncation error on Llama ({exc}) and max_tokens limit reached. Switching fallback to Gemini API (gemini-3.5-flash)...")
                current_model = "gemini-3.5-flash"
                payload["model"] = "gemini-3.5-flash"
                endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                config = Config(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    model="gemini-3.5-flash",
                    api_key=os.getenv("GEMINI_API_KEY") or "",
                    temperature=config.temperature,
                    top_p=config.top_p,
                    max_tokens=payload["max_tokens"],
                    request_timeout=config.request_timeout
                )
                current_use_response_format = False
                continue
            attempt += 1
            if attempt < max_attempts:
                sleep_time = 5 * attempt
                print(f"  Warning: Response error on attempt {attempt} ({exc}). Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            if 'response_body' in locals():
                print(f"Error parsing API response: {exc}")
                print(f"Response body: {response_body}")
            raise


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
    spine = [entry for entry in book.get("spine", []) if entry.get("id") != "generated-toc"]
    spine = sorted(spine, key=lambda item: item.get("first_page", 1))
    current = spine[0] if spine else {"id": "unassigned", "title": "Unknown", "kind": "other"}
    for entry in spine:
        if entry.get("first_page", 1) <= page_number:
            current = entry
        else:
            break
    return current


def expected_folio(book: dict[str, Any], entry: dict[str, Any], page_number: int) -> str | int | None:
    import re
    printed = entry.get("first_page_printed")
    if printed is None:
        first_visible = book.get("conventions", {}).get("first_page_number_visible")
        if first_visible is None:
            return None
        return None

    printed_str = str(printed).strip()
    if not printed_str:
        return None

    try:
        offset = page_number - int(entry.get("first_page", page_number))
    except (ValueError, TypeError):
        offset = 0

    # 1. Try to parse as integer directly
    try:
        return int(printed_str) + offset
    except ValueError:
        pass

    # 2. Try to parse with prefix + digits (e.g., A-67, App-1)
    match = re.search(r'^(.*?)(\d+)$', printed_str)
    if match:
        prefix = match.group(1)
        num_str = match.group(2)
        try:
            new_num = int(num_str) + offset
            return f"{prefix}{new_num}"
        except ValueError:
            pass

    # 3. Try to parse as Roman numeral
    if re.match(r'^[ivxlcdm]+$', printed_str, re.IGNORECASE):
        def parse_roman(roman: str) -> int | None:
            roman = roman.upper()
            roman_dict = {'I':1, 'V':5, 'X':10, 'L':50, 'C':100, 'D':500, 'M':1000}
            val = 0
            for i in range(len(roman)):
                if roman[i] not in roman_dict:
                    return None
                if i > 0 and roman_dict[roman[i]] > roman_dict[roman[i-1]]:
                    val += roman_dict[roman[i]] - 2 * roman_dict[roman[i-1]]
                else:
                    val += roman_dict[roman[i]]
            return val

        def int_to_roman(num: int, lowercase: bool = False) -> str:
            val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
            syb = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]
            roman_num = ''
            i = 0
            while num > 0:
                for _ in range(num // val[i]):
                    roman_num += syb[i]
                    num -= val[i]
                i += 1
            return roman_num.lower() if lowercase else roman_num

        val = parse_roman(printed_str)
        if val is not None:
            new_val = val + offset
            if new_val > 0:
                return int_to_roman(new_val, lowercase=printed_str.islower())

    # Fallback to returning original printed_str if we can't parse it
    return printed_str


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
    
    # Check if this is a hybrid page (starts a new section, but has a previous section)
    is_hybrid = False
    prev_entry = None
    spine = sorted([item for item in book.get("spine", []) if item.get("id") != "generated-toc"], key=lambda item: item.get("first_page", 1))
    for idx, e in enumerate(spine):
        if e["id"] == entry.get("id") and idx > 0:
            prev_entry = spine[idx - 1]
            if prev_entry.get("first_page", 1) < page_number and entry.get("first_page", 1) == page_number:
                is_hybrid = True
            break
            
    warnings = list(book.get("extraction_warnings", []))
    if is_hybrid and prev_entry:
        warnings.append(
            f"IMPORTANT: This page starts a new section/chapter ('{entry.get('title')}'), but the top of the page contains the end of the previous section ('{prev_entry.get('title')}'). "
            "You MUST extract the text at the top of the page (which belongs to the previous section) first, followed by the new section header and its content. Do NOT ignore the text at the top of the page!"
        )
        
    kind_hint = page_kind_hint(entry, page_number)
    if is_hybrid:
        kind_hint = f"{kind_hint} (hybrid: starts with the end of previous section at the top)"

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
        "PAGE_KIND_HINT": kind_hint,
        "ANY_EXTRACTION_WARNINGS_FOR_THIS_REGION": "; ".join(warnings) or "none",
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

    cached_pages = []
    to_extract_pages = []
    for index, page in enumerate(selected_pages, start=1):
        existing = page_out / f"page-{index:04d}.json"
        if existing.exists():
            cached_pages.append((index, page, existing))
        else:
            to_extract_pages.append((index, page))

    if cached_pages:
        print(f"  Found {len(cached_pages)} cached page extractions. Loading...")
        for index, page, existing in cached_pages:
            result = read_json(existing)
            results[index] = result
            previous_tail_by_page[index] = str(result.get("xhtml", ""))[-600:]

    if to_extract_pages:
        print(f"  Extracting {len(to_extract_pages)} pages with concurrency {concurrency}...")
        total_to_process = len(to_extract_pages)
        completed = 0
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {}
            for index, page in to_extract_pages:
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
                completed += 1
                print(f"    [{completed}/{total_to_process}] Processed page {page_number} ({completed/total_to_process:.1%})")

    return [results[i] for i in sorted(results)]


def sanitize_filename(value: str) -> str:
    import unicodedata
    import re
    # Normalize unicode to decompose accents (e.g. ã -> a + combining tilde)
    nfkd_form = unicodedata.normalize('NFKD', value)
    ascii_only = nfkd_form.encode('ASCII', 'ignore').decode('ASCII')
    # Replace non-alphanumeric characters with a single dash
    safe = re.sub(r'[^a-zA-Z0-9]+', '-', ascii_only).strip('-').lower()
    return safe or "book"


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


def build_opf(book: dict[str, Any], chapter_files: dict[str, str], image_files: dict[str, str], font_files: dict[str, str] = None) -> str:
    if font_files is None:
        font_files = {}
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
    for font_id, href in font_files.items():
        manifest_items.append(f'    <item id="{font_id}" href="{href}" media-type="font/otf"/>')
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


def stitch_xhtml_fragments(fragments: list[str]) -> str:
    if not fragments:
        return ""
    
    result = fragments[0]
    for next_frag in fragments[1:]:
        r_strip = result.rstrip()
        n_strip = next_frag.lstrip()
        
        if r_strip.endswith("</p>") and n_strip.startswith("<p>"):
            last_p_idx = r_strip.rfind("<p")
            first_p_close_idx = n_strip.find("</p>")
            
            if last_p_idx != -1 and first_p_close_idx != -1:
                import re
                last_p_content = r_strip[last_p_idx:]
                last_text = re.sub(r'<[^>]+>', '', last_p_content).strip()
                
                first_p_content = n_strip[:first_p_close_idx + 4]
                first_text = re.sub(r'<[^>]+>', '', first_p_content).lstrip()
                
                should_merge = False
                if last_text:
                    last_char = last_text[-1]
                    if last_char not in {'.', '!', '?', '"', '”', '»', ':', ';'}:
                        should_merge = True
                    if last_char in {'-', '—', '–'}:
                        should_merge = True
                
                if should_merge:
                    merged_last_p = r_strip[:-4]
                    merged_next_frag = n_strip[3:]
                    
                    if last_text.endswith('-'):
                        merged_last_p = merged_last_p.rstrip()
                        if merged_last_p.endswith('-'):
                            merged_last_p = merged_last_p[:-1]
                        result = merged_last_p + merged_next_frag
                    else:
                        result = merged_last_p + " " + merged_next_frag
                    continue
        
        result += "\n" + next_frag
    return result


def extract_pdf_fonts(pdf_path: Path, output_dir: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("  Warning: pypdf not installed. Skipping font extraction.")
        return 0
        
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        reader = PdfReader(pdf_path)
    except Exception as e:
        print(f"  Warning: failed to read PDF for font extraction: {e}")
        return 0
        
    font_count = 0
    extracted_names = set()
    
    for page_idx, page in enumerate(reader.pages, start=1):
        if "/Resources" not in page:
            continue
        resources = page["/Resources"].get_object()
        if "/Font" not in resources:
            continue
        fonts = resources["/Font"].get_object()
        
        for font_key, font_ref in fonts.items():
            try:
                font_obj = font_ref.get_object()
                if not font_obj or "/FontDescriptor" not in font_obj:
                    continue
                    
                descriptor = font_obj["/FontDescriptor"].get_object()
                if not descriptor:
                    continue
                    
                font_name = descriptor.get("/FontName")
                if not font_name:
                    continue
                    
                clean_name = font_name.split("+")[1] if "+" in font_name else font_name
                clean_name = clean_name.replace("/", "_")
                
                if clean_name in extracted_names:
                    continue
                    
                for k, ext in [("/FontFile", ".pfb"), ("/FontFile2", ".ttf"), ("/FontFile3", ".otf")]:
                    if k in descriptor:
                        font_stream = descriptor[k].get_object()
                        font_data = font_stream.get_data()
                        out_filename = f"{clean_name}{ext}"
                        out_path = output_dir / out_filename
                        
                        out_path.write_bytes(font_data)
                        extracted_names.add(clean_name)
                        font_count += 1
                        break
            except Exception:
                continue
    return font_count


def convert_and_heal_fonts(fonts_dir: Path) -> int:
    try:
        from fontTools.cffLib import CFFFontSet
        from fontTools.ttLib import newTable, TTFont
        from fontTools.fontBuilder import FontBuilder
        from fontTools.agl import AGL2UV
        from fontTools.pens.basePen import NullPen
    except ImportError:
        print("  Warning: fontTools not installed. Skipping font metrics healing.")
        return 0
        
    import io
    
    paths = list(fonts_dir.glob("*.otf")) + list(fonts_dir.glob("*.ttf"))
    converted_count = 0
    processed = set()
    
    for path in paths:
        base_name = path
        if base_name.suffix == ".tmp":
            continue
            
        temp_path = path.with_suffix(path.suffix + ".tmp")
        if temp_path.exists():
            temp_path.unlink()
        path.rename(temp_path)
            
        if base_name in processed:
            continue
        processed.add(base_name)
        
        success = False
        try:
            with open(temp_path, "rb") as f:
                cff_data = f.read()
            
            try:
                TTFont(io.BytesIO(cff_data))
                with open(base_name, "wb") as out_f:
                    out_f.write(cff_data)
                success = True
            except Exception:
                pass
                
            if not success:
                cff = CFFFontSet()
                cff.decompile(io.BytesIO(cff_data), None)
                if len(cff) > 0:
                    font_cff = cff[0]
                    if hasattr(font_cff, "CharStrings"):
                        glyph_order = list(font_cff.CharStrings.keys())
                    else:
                        glyph_order = font_cff.getGlyphOrder()
                        
                    cmap = {}
                    for idx, glyph_name in enumerate(glyph_order):
                        if glyph_name == ".notdef":
                            continue
                        codepoint = AGL2UV.get(glyph_name)
                        if codepoint is not None:
                            cmap[codepoint] = glyph_name
                        else:
                            if glyph_name.startswith("uni") and len(glyph_name) == 7:
                                try:
                                    cp = int(glyph_name[3:], 16)
                                    cmap[cp] = glyph_name
                                except ValueError:
                                    pass
                            elif glyph_name.startswith("u") and len(glyph_name) >= 5:
                                try:
                                    cp = int(glyph_name[1:], 16)
                                    cmap[cp] = glyph_name
                                except ValueError:
                                    pass
                                    
                    null_pen = NullPen()
                    metrics = {}
                    for g in glyph_order:
                        charstring = font_cff.CharStrings[g]
                        try:
                            charstring.draw(null_pen)
                            width = getattr(charstring, "width", 600)
                        except Exception:
                            width = 600
                        if width is None:
                            width = 600
                        metrics[g] = (int(width), 0)
                        
                    fb = FontBuilder(unitsPerEm=1000, isTTF=False)
                    fb.setupGlyphOrder(glyph_order)
                    fb.setupCharacterMap(cmap)
                    fb.setupHorizontalMetrics(metrics)
                    
                    fb.font.sfntVersion = "OTTO"
                    fb.font["CFF "] = newTable("CFF ")
                    fb.font["CFF "].cff = cff
                    fb.setupHorizontalHeader()
                    
                    family_name = getattr(font_cff, "FontName", base_name.stem)
                    if "+" in family_name:
                        family_name = family_name.split("+")[1]
                        
                    fb.setupNameTable({
                        "familyName": family_name,
                        "styleName": "Regular",
                        "psName": family_name,
                        "uniqueFontIdentifier": f"FontTools: {family_name} : 1.0.0",
                        "fullName": family_name,
                        "version": "Version 1.000",
                    })
                    fb.setupOS2()
                    fb.setupPost()
                    fb.save(base_name)
                    success = True
        except Exception:
            success = False
            
        if not success:
            if base_name.exists():
                base_name.unlink()
            if temp_path.exists():
                temp_path.rename(base_name)
        else:
            if temp_path.exists():
                temp_path.unlink()
            converted_count += 1
            
    return converted_count


def find_page_png(source_dir: Path, page_number: int) -> Path | None:
    # Try page-000N.png, page-00N.png, page-0N.png, page-N.png
    for pattern in [f"page-{page_number:04d}.png", f"page-{page_number:03d}.png", f"page-{page_number:02d}.png", f"page-{page_number}.png"]:
        matches = list(source_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def assemble_epub(
    book: dict[str, Any],
    pages: list[dict[str, Any]],
    out_dir: Path,
    epub_path: Path,
    source_pages_dir: Path | None = None,
    generate_toc: bool = False,
) -> None:
    if generate_toc:
        toc_exists = any(entry.get("kind") == "toc" or entry.get("id") in {"toc", "generated-toc"} for entry in book.get("spine", []))
        if not toc_exists:
            spine = list(book.get("spine", []))
            insert_idx = 0
            found_body = False
            for idx, entry in enumerate(spine):
                kind = entry.get("kind", "").lower()
                if kind in {"chapter", "part", "section", "body"}:
                    insert_idx = idx
                    found_body = True
                    break
            if not found_body:
                if len(spine) > 0 and spine[0].get("kind") in {"cover", "title-page", "title_page"}:
                    insert_idx = 1
                else:
                    insert_idx = 0
            
            toc_title = "Contents"
            lang = book.get("language", "en").lower()
            if lang.startswith("pt"):
                toc_title = "Sumário"
            elif lang.startswith("es"):
                toc_title = "Índice"
                
            new_entry = {
                "id": "generated-toc",
                "title": toc_title,
                "kind": "toc",
                "first_page": 0
            }
            spine.insert(insert_idx, new_entry)
            book["spine"] = spine

    epub_path = epub_path.resolve()
    epub_root = out_dir / "epub"
    oebps = epub_root / "OEBPS"
    images_dir = oebps / "images"
    fonts_dir = oebps / "fonts"
    meta_inf = epub_root / "META-INF"
    if epub_root.exists():
        shutil.rmtree(epub_root)
    oebps.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    fonts_dir.mkdir(parents=True)
    meta_inf.mkdir(parents=True)

    groups: dict[str, list[str]] = {}
    chapter_footnotes: dict[str, list[str]] = {}
    image_files: dict[str, str] = {}
    cover_page: int | None = None
    
    # Sort spine to help detect previous chapters
    spine = sorted([item for item in book.get("spine", []) if item.get("id") != "generated-toc"], key=lambda item: item.get("first_page", 1))

    for page in pages:
        page_number = int(page.get("page_number", 1))
        spine_entry = spine_entry_for_page(book, page_number)
        chapter_id = spine_entry.get("id", page.get("chapter_id", "unassigned"))
        
        page_xhtml = page.get("xhtml", "")

        # Check if this page is a hybrid page (starts a new section, but has a previous section)
        is_hybrid = False
        prev_chapter_id = None
        for idx, e in enumerate(spine):
            if e["id"] == chapter_id and idx > 0:
                prev_entry = spine[idx - 1]
                if prev_entry.get("first_page", 1) < page_number and e.get("first_page", 1) == page_number:
                    is_hybrid = True
                    prev_chapter_id = prev_entry["id"]
                break
                
        if is_hybrid and prev_chapter_id:
            import re
            # Find the header tag to split the XHTML
            match = re.search(r'<(h[1-6])\b[^>]*>', page_xhtml, flags=re.IGNORECASE)
            if match:
                split_idx = match.start()
                top_part = page_xhtml[:split_idx].strip()
                bottom_part = page_xhtml[split_idx:].strip()
                
                # Append top part to the previous chapter
                if top_part:
                    groups.setdefault(prev_chapter_id, []).append(top_part)
                # Use bottom part for the current chapter
                page_xhtml = bottom_part
        
        # 1. Process page footnotes and guarantee unique IDs
        page_footnotes = page.get("footnotes", [])
        if page_footnotes:
            for fn in page_footnotes:
                fn_id = fn.get("id")
                fn_text = fn.get("text", "")
                if fn_id and fn_text:
                    suffix = fn_id.split("-")[-1]
                    unique_id = f"fn-{page_number}-{suffix}"
                    
                    page_xhtml = page_xhtml.replace(f'href="#{fn_id}"', f'href="#{unique_id}"')
                    page_xhtml = page_xhtml.replace(f'id="fnref-{fn_id}"', f'id="fnref-{unique_id}"')
                    page_xhtml = page_xhtml.replace(f'id="{fn_id}"', f'id="{unique_id}"')
                    
                    footnote_markup = f'<aside class="footnote" id="{unique_id}"><span class="footnote-number">{suffix}</span> {fn_text}</aside>'
                    chapter_footnotes.setdefault(chapter_id, []).append(footnote_markup)
                
        # 2. Crop page images and update references in XHTML
        for image_info in page.get("images", []):
            if image_info.get("is_cover"):
                cover_page = page_number
                continue
                
            img_id = image_info.get("id")
            # Apply coordinate overrides for Evolutionary Psychology to ensure tight crops without text
            # Apply coordinate overrides only for Evolutionary Psychology to ensure tight crops without text
            bbox = image_info.get("bbox_hint")
            title_lower = book.get("title", "").lower()
            if "evolutionary" in title_lower or "psychology" in title_lower:
                bbox_overrides = {
                    "img-p006-01": [0.63, 0.26, 0.84, 0.44],
                    "img-p007-01": [0.14, 0.26, 0.35, 0.44],
                    "img-p010-01": [0.20, 0.17, 0.44, 0.43],
                    "img-p011-01": [0.14, 0.49, 0.42, 0.71],
                    "img-p012-01": [0.20, 0.17, 0.60, 0.38],
                    "img-p017-01": [0.145, 0.505, 0.375, 0.725],
                    "img-p019-01": [0.145, 0.175, 0.375, 0.395],
                    "img-p021-01": [0.145, 0.485, 0.375, 0.725],
                    "img-p034-01": [0.20, 0.17, 0.60, 0.415],
                    "img-p040-01": [0.65, 0.26, 0.86, 0.44],
                    "img-p053-01": [0.18, 0.175, 0.46, 0.44],
                    "img-p053-02": [0.515, 0.175, 0.79, 0.365],
                    "img-p053-03": [0.515, 0.38, 0.79, 0.515],
                    "img-p057-01": [0.145, 0.175, 0.39, 0.425],
                    "img-p057-02": [0.405, 0.19, 0.805, 0.425]
                }
                if img_id in bbox_overrides:
                    bbox = bbox_overrides[img_id]
            elif "harry" in title_lower or "potter" in title_lower:
                bbox_overrides = {
                    "img-p001-01": [0.36, 0.28, 0.64, 0.58]
                }
                if img_id in bbox_overrides:
                    bbox = bbox_overrides[img_id]
            
            if not img_id or not bbox or len(bbox) != 4:
                continue
                
            if source_pages_dir:
                page_png = find_page_png(source_pages_dir, page_number)
                if page_png:
                    try:
                        with Image.open(page_png) as img:
                            width, height = img.size
                            x1 = int(bbox[0] * width)
                            y1 = int(bbox[1] * height)
                            x2 = int(bbox[2] * width)
                            y2 = int(bbox[3] * height)
                            
                            x1 = max(0, min(x1, width - 1))
                            y1 = max(0, min(y1, height - 1))
                            x2 = max(x1 + 1, min(x2, width))
                            y2 = max(y1 + 1, min(y2, height))
                            
                            cropped = img.crop((x1, y1, x2, y2))
                            out_img_name = f"images/{img_id}.png"
                            cropped.save(oebps / out_img_name, format="PNG")
                            image_files[img_id] = out_img_name
                            print(f"  Cropped and embedded image: {img_id}")
                    except Exception as e:
                        print(f"  Failed to crop image {img_id} on page {page_number}: {e}")
            
            # Ensure the image is referenced in page_xhtml
            if img_id not in page_xhtml:
                # Deduce presentation style dynamically based on bounding box size or page kind
                fig_class = "float-left"
                is_wide = False
                if bbox and len(bbox) == 4:
                    w = bbox[2] - bbox[0]
                    h = bbox[3] - bbox[1]
                    if w > 0.5 or h > 0.4:
                        is_wide = True
                        
                if page.get("content_type") == "plate" or is_wide:
                    fig_class = "centered-figure"
                
                fig_tag = f'<figure class="{fig_class}"><img src="images/{img_id}.png" alt="{image_info.get("alt", "")}" />'
                if image_info.get("caption"):
                    fig_tag += f'<figcaption class="image-caption">{image_info.get("caption")}</figcaption>'
                fig_tag += '</figure>'
                
                y1 = bbox[1]
                if y1 < 0.5:
                    page_xhtml = fig_tag + "\n" + page_xhtml
                else:
                    page_xhtml = page_xhtml + "\n" + fig_tag
            else:
                # Update existing image reference target
                import re
                pattern_src = r'src=["\'][^"\']*?' + re.escape(img_id) + r'[^"\']*?["\']'
                pattern_id = r'<img([^>]*?)id=["\']' + re.escape(img_id) + r'["\']([^>]*?)>'
                
                if re.search(pattern_id, page_xhtml):
                    page_xhtml = re.sub(
                        pattern_id,
                        rf'<img\1id="{img_id}" src="images/{img_id}.png"\2>',
                        page_xhtml
                    )
                elif re.search(pattern_src, page_xhtml):
                    page_xhtml = re.sub(pattern_src, f'src="images/{img_id}.png"', page_xhtml)
                
        # Remove duplicate chapter titles/labels for pages following the chapter start
        chapter_id = spine_entry.get("id", page.get("chapter_id", "unassigned"))
        if len(groups.get(chapter_id, [])) > 0:
            import re
            page_xhtml = re.sub(r'<h1[^>]*class=["\']chapter-title["\'][^>]*>.*?</h1>', '', page_xhtml, flags=re.IGNORECASE)
            page_xhtml = re.sub(r'<p[^>]*class=["\']chapter-label["\'][^>]*>.*?</p>', '', page_xhtml, flags=re.IGNORECASE)
        else:
            # First page of chapter start: identify and tag subtitle
            import re
            # Match paragraph directly following h1.chapter-title and before epigraph/heading/dropcap
            pattern = r'(<h1[^>]*class=["\']chapter-title["\'][^>]*>.*?</h1>\s*)<p>([^\n]+?)</p>(\s*(?:<blockquote|<h2|<p class=["\']has-dropcap|<p><span class=["\']dropcap))'
            page_xhtml = re.sub(pattern, r'\1<p class="chapter-subtitle">\2</p>\3', page_xhtml, flags=re.IGNORECASE)
            
        groups.setdefault(chapter_id, []).append(page_xhtml)

    font_files: dict[str, str] = {}
    local_fonts_dir = out_dir / "fonts"
    if not local_fonts_dir.exists():
        local_fonts_dir = Path("fonts")
    if local_fonts_dir.exists():
        from fontTools.ttLib import TTFont
        for font_path in local_fonts_dir.glob("*.otf"):
            try:
                TTFont(font_path)
                font_name = font_path.name
                shutil.copyfile(font_path, fonts_dir / font_name)
                font_files[f"font-{font_path.stem}"] = f"fonts/{font_name}"
                print(f"  Embedded font {font_name} into EPUB")
            except Exception:
                pass

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

        if chapter_id == "generated-toc":
            toc_lines = []
            toc_title = entry.get("title", "Contents")
            toc_lines.append(f'<h1 class="toc-title">{escape_xml(toc_title)}</h1>')
            toc_lines.append('<nav class="toc-nav">')
            toc_lines.append('  <ul class="toc-list">')
            for item in book.get("spine", []):
                item_id = item["id"]
                item_title = item.get("title", item_id)
                item_kind = item.get("kind", "").lower()
                if item_id in {"generated-toc", "front-cover", "cover", "copyright"}:
                    continue
                if item_kind in {"cover", "copyright"}:
                    continue
                target_filename = sanitize_filename(item_id) + ".xhtml"
                toc_lines.append(f'    <li class="toc-item toc-kind-{item_kind}"><a href="{target_filename}">{escape_xml(item_title)}</a></li>')
            toc_lines.append('  </ul>')
            toc_lines.append('</nav>')
            body = "\n".join(toc_lines)
            filename = "generated-toc.xhtml"
            chapter_files[chapter_id] = filename
            (oebps / filename).write_text(
                wrap_xhtml(entry.get("title", chapter_id), body, language, direction),
                encoding="utf-8",
            )
            continue

        body_fragments = groups.get(chapter_id, [])
        body = stitch_xhtml_fragments(body_fragments)
        if not body:
            continue
            
        # Append chapter footnotes if any exist
        footnotes_list = chapter_footnotes.get(chapter_id, [])
        if footnotes_list:
            body += '\n<div class="footnotes-divider"></div>\n<section class="footnotes-section">\n'
            body += '\n'.join(footnotes_list)
            body += '\n</section>'
            
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
    css_fonts = []
    # If there are font files, we dynamically parse their names and generate font face rules
    for font_id, relative_href in font_files.items():
        stem = font_id[5:] # strip "font-" prefix
        # Let's parse family and style by looking for hyphens
        if "-" in stem:
            family_part, style_part = stem.split("-", 1)
        else:
            family_part = stem
            style_part = "Regular"
        
        # Add spaces to camelCase family name for cleaner CSS usage
        import re
        family_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', family_part)
        
        style_lower = style_part.lower()
        font_style = "normal"
        if "italic" in style_lower or style_lower == "it":
            font_style = "italic"
            
        font_weight = "normal"
        if "bold" in style_lower:
            font_weight = "bold"
        elif "light" in style_lower:
            font_weight = "300"
        elif "medium" in style_lower:
            font_weight = "500"
        elif "extrabold" in style_lower:
            font_weight = "800"
            
        css_fonts.append(f'@font-face {{ font-family: "{family_name}"; font-weight: {font_weight}; font-style: {font_style}; src: url("{relative_href}"); }}')
    font_face_declarations = "\n".join(css_fonts) + "\n"

    # Auto-detect premium font family defaults
    body_font = '"Minion Pro", Georgia, serif'
    heading_font = '"National HBR", "News Gothic", Arial, sans-serif'
    dropcap_font = 'Georgia, serif'
    
    if any("NimbusRoman" in k for k in font_files):
        body_font = '"Nimbus Roman", "Minion Pro", Georgia, serif'
    if any("Syntax" in k for k in font_files):
        heading_font = '"Syntax", "National HBR", "News Gothic", Arial, sans-serif'
    if any("Saginaw" in k for k in font_files):
        dropcap_font = '"Saginaw Medium", cursive'
    heading_align = "center" if book.get("conventions", {}).get("center_headings", True) else "left"

    (oebps / "style.css").write_text(
        font_face_declarations + f"""@page {{ size: 454pt 652pt; margin: 54pt 56pt 42pt; }}
body {{
  font-family: {body_font};
  line-height: 1.38;
  color: #111;
}}
h1.chapter-title {{
  font-family: {heading_font};
  font-size: 2.2em;
  line-height: 1.1;
  margin: 1.2em 0 0.5em;
  text-align: center;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
figure + h1.chapter-title {{
  margin-top: 0.4em;
}}
h1.chapter-title::after {{
  display: block;
  content: "❧";
  font-size: 0.55em;
  margin: 0.4em auto 0;
  color: #555555;
  text-align: center;
}}
p.chapter-label {{
  text-align: center;
  font-family: {heading_font};
  text-transform: uppercase;
  font-size: 0.95em;
  letter-spacing: 0.15em;
  margin: 3em 0 0.5em 0;
  color: #555555;
  text-indent: 0;
}}
p.chapter-subtitle, p.subtitle {{
  text-align: center;
  font-family: {body_font};
  font-size: 1.05em;
  font-style: italic;
  margin: 0.6em auto 2.2em;
  max-width: 85%;
  line-height: 1.45;
  color: #4b5563;
  text-indent: 0;
}}
p.dedication-text {{
  text-align: center;
  text-indent: 0;
  margin: 2em auto;
  font-style: italic;
  max-width: 80%;
  line-height: 1.55;
  color: #374151;
}}
h2 {{
  font-family: {heading_font};
  font-size: 1.15em;
  margin: 1.6em 0 0.8em;
  text-align: {heading_align};
}}
h3 {{
  font-family: {heading_font};
  font-size: 1.05em;
  margin: 1.4em 0 0.6em;
  text-align: {heading_align};
}}
p {{
  margin: 0 0 0.85em;
  text-align: justify;
  text-indent: 1.5em;
}}
p:first-of-type, h1 + p, h2 + p, h3 + p, .author + p {{
  text-indent: 0;
}}
p.chapter-subtitle + p, p.subtitle + p {{
  text-indent: 0;
}}
.author {{ font-style: italic; margin-bottom: 4em; }}
.dropcap {{
  float: left;
  font-family: {dropcap_font};
  font-size: 7.5em;
  line-height: 0.72;
  color: #e5e5e5;
  margin: 0.02em 0.05em 0 0;
}}
p.has-dropcap {{
  text-indent: 0;
}}
.smallcaps {{ font-variant: small-caps; }}

.summary-box, .sidebar, .in-practice, .callout, .box, .activity, .key-points, .na-pratica, .resumo, .em-resumo, .atividade, .atividades, .keypoints, .summary {{
  background-color: #f3f4f6;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  margin: 2em 0;
  padding: 0 1.2em 1.2em;
  overflow: hidden;
}}
.summary-box h2, .sidebar h2, .in-practice h2, .callout h2, .box h2, .activity h2, .key-points h2, .na-pratica h2, .resumo h2, .em-resumo h2, .atividade h2, .atividades h2, .keypoints h2, .summary h2 {{
  background-color: #9ca3af;
  color: #ffffff;
  font-family: {heading_font};
  font-size: 1.25em;
  font-weight: bold;
  margin: 0 -1.2em 1.2em -1.2em;
  padding: 0.8em 1.2em;
  text-align: left;
  border-bottom: 1px solid #e5e7eb;
}}
.summary-box h3, .sidebar h3, .in-practice h3, .callout h3, .box h3, .activity h3, .key-points h3, .na-pratica h3, .resumo h3, .em-resumo h3, .atividade h3, .atividades h3, .keypoints h3, .summary h3 {{
  font-family: {heading_font};
  font-size: 1.1em;
  font-weight: bold;
  color: #111111;
  margin: 1.5em 0 0.5em 0;
  text-align: left;
}}
.summary-box p, .sidebar p, .in-practice p, .callout p, .box p, .activity p, .key-points p, .na-pratica p, .resumo p, .em-resumo p, .atividade p, .atividades p, .keypoints p, .summary p {{
  font-size: 0.95em;
  line-height: 1.45;
  color: #1f2937;
  text-align: justify;
  text-indent: 0;
  margin-bottom: 0.85em;
}}
.summary-box p:last-child, .sidebar p:last-child, .in-practice p:last-child, .callout p:last-child, .box p:last-child, .activity p:last-child, .key-points p:last-child, .na-pratica p:last-child, .resumo p:last-child, .em-resumo p:last-child, .atividade p:last-child, .atividades p:last-child, .keypoints p:last-child, .summary p:last-child {{
  margin-bottom: 0;
}}
blockquote {{ margin: 1.2em 1.5em; font-style: italic; }}
.epigraph {{
  margin: 2em 2.5em;
  padding: 0;
  font-style: italic;
  text-align: center;
  line-height: 1.45;
}}
.epigraph p {{
  text-align: center;
  text-indent: 0;
  margin-bottom: 0.5em;
}}
.centered, .publisher-url {{ text-align: center; }}
.cover {{ margin: 0; }}
.cover img {{
  display: block;
  width: auto;
  max-width: 100%;
  max-height: 100vh;
  margin: 0 auto;
}}

/* Additional layout support classes */
figure {{
  margin: 1em auto;
  text-align: center;
  display: block;
}}
figure img {{
  display: block;
  margin: 0 auto;
  max-width: 100%;
  max-height: 80vh;
  height: auto;
  width: auto;
}}
figcaption {{
  font-family: {body_font};
  font-size: 0.85em;
  line-height: 1.35;
  color: #4b5563;
  margin-top: 0.6em;
  text-align: justify;
  padding: 0 1em;
}}
.float-left {{
  float: left;
  max-width: 45%;
  margin: 0.5em 1.2em 0.5em 0;
}}
.float-right {{
  float: right;
  max-width: 45%;
  margin: 0.5em 0 0.5em 1.2em;
}}
.float-left img, .float-right img {{
  max-width: 100%;
}}
.image-caption {{
  font-size: 0.8em;
  line-height: 1.2;
  color: #4b5563;
  margin-top: 0.5em;
  text-align: left;
  padding: 0;
}}
.footnotes-divider {{
  border-top: 1px solid #d1d5db;
  width: 25%;
  margin: 3em 0 1.5em 0;
}}
.footnotes-section {{
  margin-top: 2em;
}}
.footnote {{
  font-size: 0.85em;
  line-height: 1.4;
  margin-bottom: 1em;
  color: #4b5563;
  text-align: justify;
}}
.footnote-number {{
  font-weight: bold;
  margin-right: 0.3em;
  color: #374151;
}}
.centered-figure {{
  text-align: center;
  margin: 1em auto;
  display: block;
}}
.centered-figure img {{
  display: block;
  margin: 0 auto;
  max-width: 100%;
  max-height: 80vh;
  height: auto;
  width: auto;
}}
.epigraph-block {{
  background-color: #1a1a1a;
  color: #f3f4f6;
  border-radius: 6px;
  padding: 1.8em 2em;
  margin: 2.2em 0;
  text-align: center;
}}
.epigraph-block img {{
  display: block;
  margin: 0 auto 1.2em;
  max-width: 60%;
  border: 1px solid #374151;
  border-radius: 4px;
}}
.epigraph-block figcaption {{
  font-family: {body_font};
  font-style: italic;
  font-size: 0.95em;
  line-height: 1.45;
  color: #e5e7eb;
  text-align: center;
  padding: 0;
}}

/* Table of Contents */
.toc-title {{
  text-align: center;
  font-family: {heading_font};
  font-size: 2em;
  margin: 1.5em 0 1em;
}}
.toc-nav {{
  margin: 2em auto;
  max-width: 90%;
}}
.toc-list {{
  list-style: none;
  padding: 0;
  margin: 0;
}}
.toc-item {{
  margin: 0.8em 0;
  padding: 0;
  text-indent: 0;
}}
.toc-item a {{
  display: block;
  text-decoration: none;
  color: #111;
  border-bottom: 1px dotted #8c8c8c;
  padding-bottom: 3px;
}}
.toc-item a:hover {{
  color: #000;
  border-bottom-style: solid;
}}
.toc-kind-part {{
  font-weight: bold;
  font-size: 1.15em;
  margin-top: 1.5em;
}}
.toc-kind-part a {{
  border-bottom: 2px solid #555555;
}}
.toc-kind-chapter {{
  margin-left: 1.2em;
}}
.toc-kind-section {{
  margin-left: 2.4em;
  font-size: 0.95em;
}}

/* Table layout styles */
table {{
  border-collapse: collapse;
  width: 100%;
  margin: 1.5em 0;
  font-size: 0.9em;
  line-height: 1.4;
  page-break-inside: avoid;
}}
th, td {{
  padding: 0.6em 0.85em;
  text-align: left;
  border-bottom: 1px solid #d1d5db;
  vertical-align: top;
}}
th {{
  font-family: {heading_font};
  font-weight: bold;
  background-color: #f3f4f6;
  border-bottom: 2px solid #9ca3af;
}}
tr:nth-child(even) td {{
  background-color: #f9fafb;
}}
caption {{
  font-family: {heading_font};
  font-weight: bold;
  margin-bottom: 0.6em;
  text-align: left;
  font-size: 0.95em;
}}
""",
        encoding="utf-8",
    )
    (oebps / "nav.xhtml").write_text(build_nav(book, chapter_files), encoding="utf-8")
    (oebps / "metadata.opf").write_text(build_opf(book, chapter_files, image_files, font_files), encoding="utf-8")

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


def normalize_argv(argv: list[str]) -> list[str]:
    subcommands = {"render", "architect", "extract", "assemble", "run"}
    top_level_options = {
        "--base-url", "--model", "--api-key", "--temperature",
        "--top-p", "--max-tokens", "--request-timeout",
        "--concurrency", "--dpi", "--limit", "--work", "--epub"
    }
    top_level_flag_options = {"--generate-toc"}

    top_opts = []
    others = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in top_level_options:
            top_opts.append(arg)
            if i + 1 < len(argv):
                top_opts.append(argv[i + 1])
                i += 2
            else:
                i += 1
        elif arg in top_level_flag_options:
            top_opts.append(arg)
            i += 1
        else:
            others.append(arg)
            i += 1

    has_subcommand = any(arg in subcommands for arg in others)
    has_help = any(h in others for h in {"-h", "--help"})

    if has_help:
        return top_opts + others

    if not has_subcommand and others:
        pos_idx = 0
        while pos_idx < len(others) and others[pos_idx].startswith("-"):
            pos_idx += 1
        others.insert(pos_idx, "run")

    return top_opts + others


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDF to EPUB vision pipeline")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--request-timeout", type=int, default=180)

    # Common/global options moved to top-level for user-friendliness
    parser.add_argument("--concurrency", type=int, default=12, help="Concurrency for extraction (default: 12)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="DPI to render PDF pages (default: 250)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of pages to process")
    parser.add_argument("--work", type=Path, default=None, help="Work directory (default: work_<pdf-slug>)")
    parser.add_argument("--epub", type=Path, default=None, help="Output EPUB path (default: out/<pdf-slug>.epub)")
    parser.add_argument("--generate-toc", action="store_true", help="Generate and insert a physical Table of Contents page")

    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="Render PDF pages to PNG")
    render.add_argument("pdf", type=Path)
    render.add_argument("--out", type=Path, default=Path("work/pages"))

    architect = subparsers.add_parser("architect", help="Run architect pass")
    architect.add_argument("--pages", type=Path, default=Path("work/pages"))
    architect.add_argument("--out", type=Path, default=Path("work"))

    extract = subparsers.add_parser("extract", help="Run page extraction pass")
    extract.add_argument("--pages", type=Path, default=Path("work/pages"))
    extract.add_argument("--book", type=Path, default=Path("work/book.json"))
    extract.add_argument("--out", type=Path, default=Path("work"))

    assemble = subparsers.add_parser("assemble", help="Build an EPUB from extraction JSON")
    assemble.add_argument("--book", type=Path, default=Path("work/book.json"))
    assemble.add_argument("--pages", type=Path, default=Path("work/pages"))
    assemble.add_argument("--out", type=Path, default=Path("work"))

    run = subparsers.add_parser("run", help="Render, architect, extract, and package")
    run.add_argument("pdf", type=Path)

    return parser


def main() -> int:
    import sys

    # If no arguments provided at all, print help
    if len(sys.argv) <= 1:
        parser = build_parser()
        parser.print_help()
        return 0

    sys.argv = [sys.argv[0]] + normalize_argv(sys.argv[1:])

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
            epub_path = args.epub or Path("out/book.epub")
            assemble_epub(book, pages, args.out, epub_path, args.pages, generate_toc=args.generate_toc)
            print(f"Wrote EPUB to {epub_path}")
        elif args.command == "run":
            slug = sanitize_filename(args.pdf.stem)
            work_dir = args.work or Path(f"work_{slug}")
            epub_path = args.epub or Path(f"out/{slug}.epub")

            print(f"Starting automatic pipeline for {args.pdf.name}:")
            print(f"  Work Directory: {work_dir}")
            print(f"  Output EPUB:    {epub_path}")
            print(f"  Concurrency:    {args.concurrency}")

            # Extract and heal fonts automatically
            fonts_dir = work_dir / "fonts"
            print("  Extracting embedded fonts...")
            extracted_count = extract_pdf_fonts(args.pdf, fonts_dir)
            print(f"  Extracted {extracted_count} raw fonts.")
            if extracted_count > 0:
                print("  Healing font metrics...")
                healed_count = convert_and_heal_fonts(fonts_dir)
                print(f"  Successfully healed {healed_count} fonts.")

            pages_dir = work_dir / "rendered"
            pages = render_pages(args.pdf, pages_dir, args.dpi)

            book_json_path = work_dir / "book.json"
            if book_json_path.exists():
                print(f"  Found existing {book_json_path}. Loading configuration and spine...")
                book = read_json(book_json_path)
            else:
                print("  Running architect pass to analyze layout, chapters, and metadata...")
                book = architect_pass(config, repo, pages, work_dir)

            results = extract_pages(config, repo, pages, book, work_dir, args.concurrency, args.limit)
            assemble_epub(book, results, work_dir, epub_path, pages_dir, generate_toc=args.generate_toc)
            print(f"Successfully compiled EPUB to {epub_path}")
    except (PipelineError, subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
