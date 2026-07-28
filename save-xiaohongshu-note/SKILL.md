---
name: save-xiaohongshu-note
description: Save one or many Xiaohongshu or RedNote posts supplied by URL or a CSV, TSV, TXT, Markdown, or XLSX list as local Markdown archives with source metadata and downloaded images. Preserve spreadsheet operation tags and content categories in filenames, Markdown titles, frontmatter, index, and reports. Use when the user asks to clip, archive, batch export, download, back up, or export 小红书笔记, 正文, 原图, 表格链接, 运营标签, 内容分类, or Xiaohongshu links to Markdown or Obsidian. Export complete text and images only when the user states they own the posts or have permission; otherwise create citation-only archives or provide summaries.
---

# Save Xiaohongshu Note

Archive user-supplied Xiaohongshu links with deterministic local files. Preserve attribution and never bypass access controls.

## Authorization

- Treat an explicit statement such as “这些是我的笔记”, “我有作者授权”, or “可完整归档” as authorization.
- Do not infer authorization from possession of public links.
- Without authorization, omit full bodies and images.
- Process only links explicitly placed in scope. Do not discover or crawl profiles, search results, followers, favorites, or collections unless separately requested and authorized.

## Single note

Run:

```bash
python3 scripts/save_note.py "<url>" --output-dir "<output-root>"
```

After authorization, add `--authorized`. Add spreadsheet-style labels when provided:

```bash
python3 scripts/save_note.py "<url>" \
  --output-dir "<output-root>" \
  --authorized \
  --operation-tags "优质内容/爆款笔记" \
  --content-categories "唱片推荐/新品上市"
```

Use `--inspect` to parse without writing. Use `--html-file <path>` for an HTML page the user already saved.

## Batch export

Accept `.csv`, `.tsv`, `.txt`, `.md`, and `.xlsx`. For tables, recognize these columns:

- URL: `url`, `link`, `链接`, `笔记链接`, or `小红书链接` (required)
- Operation tags: `运营标签` (optional)
- Content categories: `内容分类`, `内容标签`, or `分类` (optional)

Split multiple labels on `/`, `|`, commas, Chinese commas, semicolons, `、`, or line breaks.

Run:

```bash
python3 scripts/batch_save.py "<table-or-list>" \
  --output-dir "<output-root>" \
  --authorized \
  --delay 3 \
  --retries 2
```

Only add `--authorized` after confirming ownership or permission. Keep the default delay unless the user requests a slower rate. Use `--force` only when the user wants already completed rows re-exported.

## Batch behavior

- Process sequentially with a configurable delay and retries.
- Resume completed rows from `batch-progress.jsonl` when rerun.
- Write `export-report.csv` after every processed row.
- Write `index.md` with links, operation tags, categories, and authors.
- Prefix filenames and Markdown headings as `[运营标签][内容分类] 原始标题`.
- Preserve the original title separately in YAML frontmatter.
- Keep complete labels in frontmatter even when a filesystem-safe filename is shortened.

## Verification

1. Inspect the final JSON summary.
2. Confirm `index.md` and `export-report.csv` exist for a batch.
3. Compare successful note counts and downloaded image counts with the report.
4. Link the generated `index.md` and report in the final response. Link a Markdown file for a single note.
5. Mention partial failures, sign-in requirements, or access-denied pages.

## Guardrails

- Accept only `xiaohongshu.com` and `xhslink.com` URLs.
- Do not obtain, expose, or reuse browser cookies without explicit user instruction.
- Do not bypass sign-in, CAPTCHA, private posts, deleted posts, rate limits, or platform protections.
- Preserve a canonical source URL and the author in every archive.
- Use moderate request frequency and stop on repeated access-denied responses.

## Output

Single note:

```text
<output-root>/<labeled-title>-<note-id>/
├── <labeled-title>.md
└── images/
```

Batch:

```text
<output-root>/
├── index.md
├── export-report.csv
├── batch-progress.jsonl
└── <labeled-title>-<note-id>/
    ├── <labeled-title>.md
    └── images/
```
