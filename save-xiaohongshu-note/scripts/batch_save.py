#!/usr/bin/env python3
"""Batch archive Xiaohongshu notes from CSV, TSV, TXT, or XLSX."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

from save_note import ArchiveError, archive_note, parse_tag_value


URL_ALIASES = {"url", "link", "链接", "笔记链接", "小红书链接"}
OPERATION_ALIASES = {"运营标签", "运营tag", "operationtags", "operationtag"}
CATEGORY_ALIASES = {"内容分类", "内容标签", "分类", "contentcategories", "contentcategory"}
REPORT_FIELDS = [
    "row",
    "status",
    "url",
    "运营标签",
    "内容分类",
    "原始标题",
    "归档标题",
    "作者",
    "图片数",
    "已下载图片数",
    "Markdown",
    "错误",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "outputs" / "小红书批量归档")
    parser.add_argument("--authorized", action="store_true", help="Export full text and images")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between notes")
    parser.add_argument("--retries", type=int, default=2, help="Retries per failed note")
    parser.add_argument("--force", action="store_true", help="Re-export completed rows")
    return parser.parse_args()


def normalized_header(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).lower()


def find_column(headers: list[str], aliases: set[str], required: bool = False) -> str | None:
    mapping = {normalized_header(header): header for header in headers}
    for alias in aliases:
        key = normalized_header(alias)
        if key in mapping:
            return mapping[key]
    if required:
        raise ArchiveError(f"Missing required URL column. Accepted names: {', '.join(sorted(URL_ALIASES))}")
    return None


def read_delimited(path: Path, delimiter: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ArchiveError("The table has no header row")
        return [{str(k or ""): str(v or "") for k, v in row.items()} for row in reader]


def column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - 64
    return value - 1


def xml_text(node: ET.Element) -> str:
    return "".join(item.text or "" for item in node.iter() if item.tag.endswith("}t"))


def read_xlsx(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = [xml_text(item) for item in root if item.tag.endswith("}si")]

        workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
        first_sheet = next((item for item in workbook_root.iter() if item.tag.endswith("}sheet")), None)
        if first_sheet is None:
            raise ArchiveError("The XLSX workbook has no worksheets")
        relation_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        target = None
        for relation in rels_root:
            if relation.attrib.get("Id") == relation_id:
                target = relation.attrib.get("Target")
                break
        if not target:
            raise ArchiveError("Could not locate the first XLSX worksheet")
        target_path = str(PurePosixPath("xl") / target.lstrip("/"))
        if target_path.startswith("xl/xl/"):
            target_path = target_path[3:]
        sheet_root = ET.fromstring(workbook.read(target_path))
        grid: list[list[str]] = []
        for row_node in (item for item in sheet_root.iter() if item.tag.endswith("}row")):
            values: dict[int, str] = {}
            for cell in (item for item in row_node if item.tag.endswith("}c")):
                index = column_index(cell.attrib.get("r", "A1"))
                kind = cell.attrib.get("t", "")
                value_node = next((item for item in cell if item.tag.endswith("}v")), None)
                if kind == "inlineStr":
                    value = xml_text(cell)
                elif value_node is None:
                    value = ""
                elif kind == "s":
                    value = shared[int(value_node.text or "0")]
                else:
                    value = value_node.text or ""
                values[index] = value
            if values:
                width = max(values) + 1
                grid.append([values.get(index, "") for index in range(width)])
        if not grid:
            return []
        headers = [str(value).strip() for value in grid[0]]
        rows: list[dict[str, str]] = []
        for values in grid[1:]:
            padded = values + [""] * (len(headers) - len(values))
            rows.append({headers[index]: str(padded[index]) for index in range(len(headers))})
        return rows


def read_txt(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            rows.append({"url": value})
    return rows


def read_input(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_delimited(path, ",")
    if suffix == ".tsv":
        return read_delimited(path, "\t")
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix in {".txt", ".md"}:
        return read_txt(path)
    raise ArchiveError("Supported input formats: .csv, .tsv, .txt, .md, .xlsx")


def normalize_rows(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not raw_rows:
        return []
    headers = list(raw_rows[0])
    url_column = find_column(headers, URL_ALIASES, required=True)
    operation_column = find_column(headers, OPERATION_ALIASES)
    category_column = find_column(headers, CATEGORY_ALIASES)
    rows: list[dict[str, Any]] = []
    for source_row, raw in enumerate(raw_rows, start=2):
        url = str(raw.get(url_column or "", "")).strip()
        if not url:
            continue
        operation_tags = parse_tag_value(raw.get(operation_column or "", ""))
        content_categories = parse_tag_value(raw.get(category_column or "", ""))
        row_key = hashlib.sha256(
            json.dumps([url, operation_tags, content_categories], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20]
        rows.append(
            {
                "source_row": source_row,
                "key": row_key,
                "url": url,
                "operation_tags": operation_tags,
                "content_categories": content_categories,
            }
        )
    return rows


def load_completed(progress_path: Path) -> dict[str, dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    if not progress_path.exists():
        return completed
    for line in progress_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "success" and Path(record.get("markdown", "")).exists():
            completed[str(record.get("key"))] = record
    return completed


def append_progress(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def report_row(row: dict[str, Any], status: str, summary: dict[str, Any] | None, error: str = "") -> dict[str, str]:
    summary = summary or {}
    return {
        "row": str(row["source_row"]),
        "status": status,
        "url": str(row["url"]),
        "运营标签": "/".join(row["operation_tags"]),
        "内容分类": "/".join(row["content_categories"]),
        "原始标题": str(summary.get("title", "")),
        "归档标题": str(summary.get("archive_title", "")),
        "作者": str(summary.get("author", "")),
        "图片数": str(summary.get("image_count", "")),
        "已下载图片数": str(summary.get("downloaded_images", "")),
        "Markdown": str(summary.get("markdown", "")),
        "错误": error,
    }


def write_report(path: Path, records: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(records)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def escape_link_label(value: str) -> str:
    return escape_table(value).replace("[", "\\[").replace("]", "\\]")


def write_index(path: Path, records: list[dict[str, str]], root: Path) -> None:
    successes = [record for record in records if record["status"] in {"success", "skipped"}]
    lines = [
        "# 小红书批量归档",
        "",
        f"成功：{len(successes)}｜失败：{sum(record['status'] == 'failed' for record in records)}",
        "",
        "| 归档标题 | 运营标签 | 内容分类 | 作者 |",
        "|---|---|---|---|",
    ]
    for record in successes:
        markdown = Path(record["Markdown"])
        try:
            relative = markdown.relative_to(root).as_posix()
        except ValueError:
            relative = markdown.as_posix()
        title = escape_link_label(record["归档标题"] or record["原始标题"])
        encoded_relative = quote(relative, safe="/-_.~")
        lines.append(
            f"| [{title}]({encoded_relative}) | {escape_table(record['运营标签'])} | "
            f"{escape_table(record['内容分类'])} | {escape_table(record['作者'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        raw_rows = read_input(args.input_file)
        rows = normalize_rows(raw_rows)
        if not rows:
            raise ArchiveError("No Xiaohongshu links were found in the input file")
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        progress_path = output_dir / "batch-progress.jsonl"
        report_path = output_dir / "export-report.csv"
        index_path = output_dir / "index.md"
        completed = {} if args.force else load_completed(progress_path)
        records: list[dict[str, str]] = []

        for position, row in enumerate(rows):
            previous = completed.get(row["key"])
            if previous:
                records.append(report_row(row, "skipped", previous))
                continue
            summary: dict[str, Any] | None = None
            error = ""
            for attempt in range(args.retries + 1):
                try:
                    summary = archive_note(
                        row["url"],
                        output_dir,
                        authorized=args.authorized,
                        operation_tags=row["operation_tags"],
                        content_categories=row["content_categories"],
                    )
                    break
                except (ArchiveError, OSError) as exc:
                    error = str(exc)
                    if attempt < args.retries:
                        time.sleep(min(2 ** attempt * 2, 15))
            if summary:
                progress_record = {
                    "key": row["key"],
                    "status": "success",
                    "row": row["source_row"],
                    **summary,
                }
                append_progress(progress_path, progress_record)
                records.append(report_row(row, "success", summary))
            else:
                append_progress(
                    progress_path,
                    {"key": row["key"], "status": "failed", "row": row["source_row"], "url": row["url"], "error": error},
                )
                records.append(report_row(row, "failed", None, error))
            write_report(report_path, records)
            write_index(index_path, records, output_dir)
            if position < len(rows) - 1 and args.delay > 0:
                time.sleep(args.delay)

        write_report(report_path, records)
        write_index(index_path, records, output_dir)
        result = {
            "ok": True,
            "total": len(records),
            "success": sum(record["status"] in {"success", "skipped"} for record in records),
            "failed": sum(record["status"] == "failed" for record in records),
            "index": str(index_path),
            "report": str(report_path),
            "progress": str(progress_path),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["failed"] == 0 else 2
    except (ArchiveError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
