#!/usr/bin/env python3
"""Archive one user-supplied Xiaohongshu note as Markdown."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
ALLOWED_HOSTS = ("xiaohongshu.com", "xhslink.com")
SHANGHAI = timezone(timedelta(hours=8))
TAG_SPLIT = re.compile(r"[/|,，;；、\n]+")


class ArchiveError(RuntimeError):
    pass


def parse_tag_value(value: str | None) -> list[str]:
    if not value:
        return []
    tags: list[str] = []
    for item in TAG_SPLIT.split(str(value)):
        cleaned = item.strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Xiaohongshu note URL")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "outputs")
    parser.add_argument("--authorized", action="store_true", help="Export full text and images")
    parser.add_argument("--inspect", action="store_true", help="Parse only; do not write files")
    parser.add_argument("--html-file", type=Path, help="Parse an already saved HTML page")
    parser.add_argument("--operation-tags", default="", help="运营标签，使用 / 分隔")
    parser.add_argument("--content-categories", default="", help="内容分类，使用 / 分隔")
    return parser.parse_args()


def validate_url(value: str) -> None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOSTS
    ):
        raise ArchiveError("Only xiaohongshu.com and xhslink.com URLs are accepted")


def fetch_text(url: str) -> tuple[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
    except HTTPError as exc:
        raise ArchiveError(f"Page returned HTTP {exc.code}; sign-in or access may be required") from exc
    except URLError as exc:
        raise ArchiveError(f"Could not open page: {exc.reason}") from exc
    return raw.decode(charset, errors="replace"), final_url


def extract_state(page: str) -> dict[str, Any]:
    match = re.search(
        r"<script>\s*window\.__INITIAL_STATE__\s*=\s*(.*?)</script>",
        page,
        flags=re.DOTALL,
    )
    if not match:
        raise ArchiveError("The page did not expose note data; it may require sign-in")
    payload = html.unescape(match.group(1)).strip().rstrip(";")
    payload = re.sub(r"(?<![\w\"'])undefined(?![\w\"'])", "null", payload)
    payload = re.sub(r"(?<![\w\"'])NaN(?![\w\"'])", "null", payload)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"The embedded page data could not be decoded at offset {exc.pos}") from exc


def find_note(state: dict[str, Any], url: str) -> dict[str, Any]:
    note_store = state.get("note", {})
    detail_map = note_store.get("noteDetailMap", {})
    candidates = re.findall(r"[0-9a-f]{24}", url, flags=re.IGNORECASE)
    candidates.extend(
        value for value in [note_store.get("currentNoteId"), note_store.get("firstNoteId")] if value
    )
    for note_id in candidates:
        record = detail_map.get(note_id)
        if isinstance(record, dict) and isinstance(record.get("note"), dict):
            return record["note"]
    for record in detail_map.values():
        if isinstance(record, dict) and isinstance(record.get("note"), dict):
            return record["note"]
    raise ArchiveError("No note detail was found in the page data")


def load_note(url: str, html_file: Path | None = None) -> tuple[dict[str, Any], str]:
    validate_url(url)
    if html_file:
        page = html_file.read_text(encoding="utf-8")
        final_url = url
    else:
        page, final_url = fetch_text(url)
        validate_url(final_url)
    return find_note(extract_state(page), final_url), final_url


def safe_name(value: str, fallback: str = "xiaohongshu-note") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120].rstrip() or fallback


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def format_time(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, tz=SHANGHAI).isoformat()


def canonical_url(note: dict[str, Any], fallback: str) -> str:
    note_id = str(note.get("noteId") or "")
    if note_id:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    return fallback


def image_urls(note: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for image in note.get("imageList") or []:
        if not isinstance(image, dict):
            continue
        url = image.get("urlDefault") or image.get("urlPre") or image.get("url")
        if not url:
            for info in image.get("infoList") or []:
                if isinstance(info, dict) and info.get("url"):
                    url = info["url"]
                    if info.get("imageScene") == "WB_DFT":
                        break
        if url:
            normalized = str(url).replace("http://", "https://", 1)
            if normalized not in urls:
                urls.append(normalized)
    return urls


def infer_extension(content_type: str, data: bytes) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    known = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    if mime in known:
        return known[mime]
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return mimetypes.guess_extension(mime) or ".img"


def download_image(url: str, target_stem: Path, referer: str) -> Path:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
    except (HTTPError, URLError) as exc:
        raise ArchiveError(f"Image download failed for {url}: {exc}") from exc
    if not data:
        raise ArchiveError(f"Image download returned no data for {url}")
    path = target_stem.with_suffix(infer_extension(content_type, data))
    path.write_bytes(data)
    return path


def display_title(original_title: str, operation_tags: list[str], categories: list[str]) -> str:
    prefixes: list[str] = []
    if operation_tags:
        prefixes.append(f"[{'·'.join(operation_tags)}]")
    if categories:
        prefixes.append(f"[{'·'.join(categories)}]")
    return f"{''.join(prefixes)} {original_title}".strip()


def yaml_list(name: str, values: list[str]) -> list[str]:
    return [f"{name}:", *[f"  - {yaml_string(value)}" for value in values]]


def build_markdown(
    note: dict[str, Any],
    source_url: str,
    authorized: bool,
    local_images: list[Path],
    operation_tags: list[str] | None = None,
    content_categories: list[str] | None = None,
) -> str:
    operation_tags = operation_tags or []
    content_categories = content_categories or []
    original_title = str(note.get("title") or "小红书笔记")
    title = display_title(original_title, operation_tags, content_categories)
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    author = str(user.get("nickname") or "")
    source_tags = [
        str(item.get("name"))
        for item in note.get("tagList") or []
        if isinstance(item, dict) and item.get("name")
    ]
    lines = [
        "---",
        f"title: {yaml_string(title)}",
        f"original_title: {yaml_string(original_title)}",
        f"author: {yaml_string(author)}",
        f"source: {yaml_string(source_url)}",
        f"note_id: {yaml_string(str(note.get('noteId') or ''))}",
        f"published_at: {yaml_string(format_time(note.get('time')))}",
        f"archived_at: {yaml_string(datetime.now(tz=SHANGHAI).isoformat())}",
        *yaml_list("operation_tags", operation_tags),
        *yaml_list("content_categories", content_categories),
        *yaml_list("source_tags", source_tags),
        "---",
        "",
        f"# {title}",
        "",
        f"作者：{author or '未知'}  ",
        f"来源：[{source_url}]({source_url})",
        "",
    ]
    if not authorized:
        lines.extend(
            [
                "> 此归档仅保存来源与元数据。完整正文和图片需由作品权利人或获授权用户导出。",
                "",
            ]
        )
        return "\n".join(lines)
    body = str(note.get("desc") or "").strip()
    if body:
        lines.extend([body, ""])
    for index, path in enumerate(local_images, start=1):
        lines.extend([f"![{original_title} - 图片 {index}](images/{path.name})", ""])
    return "\n".join(lines)


def archive_note(
    url: str,
    output_dir: Path,
    authorized: bool = False,
    operation_tags: list[str] | None = None,
    content_categories: list[str] | None = None,
    html_file: Path | None = None,
    inspect: bool = False,
) -> dict[str, Any]:
    operation_tags = operation_tags or []
    content_categories = content_categories or []
    note, final_url = load_note(url, html_file)
    urls = image_urls(note)
    original_title = str(note.get("title") or "小红书笔记")
    title = display_title(original_title, operation_tags, content_categories)
    source_url = canonical_url(note, final_url)
    summary: dict[str, Any] = {
        "ok": True,
        "note_id": str(note.get("noteId") or ""),
        "title": original_title,
        "archive_title": title,
        "author": str((note.get("user") or {}).get("nickname") or ""),
        "image_count": len(urls),
        "authorized": bool(authorized),
        "source": source_url,
        "operation_tags": operation_tags,
        "content_categories": content_categories,
    }
    if inspect:
        return summary

    note_id = summary["note_id"] or "unknown"
    base = safe_name(title)
    archive_dir = output_dir.expanduser().resolve() / f"{base}-{note_id}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    local_images: list[Path] = []
    if authorized and urls:
        images_dir = archive_dir / "images"
        images_dir.mkdir(exist_ok=True)
        for index, image_url in enumerate(urls, start=1):
            local_images.append(download_image(image_url, images_dir / f"{index:02d}", final_url))
    markdown_path = archive_dir / f"{base}.md"
    markdown_path.write_text(
        build_markdown(
            note,
            source_url,
            authorized,
            local_images,
            operation_tags=operation_tags,
            content_categories=content_categories,
        ),
        encoding="utf-8",
    )
    summary["markdown"] = str(markdown_path)
    summary["downloaded_images"] = len(local_images)
    return summary


def main() -> int:
    args = parse_args()
    try:
        summary = archive_note(
            args.url,
            args.output_dir,
            authorized=args.authorized,
            operation_tags=parse_tag_value(args.operation_tags),
            content_categories=parse_tag_value(args.content_categories),
            html_file=args.html_file,
            inspect=args.inspect,
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (ArchiveError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
