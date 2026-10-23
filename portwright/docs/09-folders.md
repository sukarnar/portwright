---
title: Folders of files (BLOB / CLOB)
screen: loads
---
A **folder source** is a directory of files. A load reads it in one of two ways:

| `files.read` | Each file becomes |
|---|---|
| `document` (default) | **one row**: its name, path, size, dates, SHA-256, MIME type and its **content** |
| `records` | **its rows**: every CSV, Excel or JSON file is parsed like a file source, many files into one table |

**Source columns of a file**: `file_name`, `file_path` (relative to the folder), `folder`, `extension`,
`size_bytes`, `modified`, `created`, `sha256`, `mime_type`, `content` (the bytes) and `content_text` (the
content decoded as text). In records mode every row also has these columns, apart from `content` and
`content_text`.

**Large objects**: map `content` with type **binary** and `content_text` with type **text**:

| type | Oracle | SQL Server | PostgreSQL | MySQL | SQLite |
|---|---|---|---|---|---|
| `binary` | BLOB (RAW) | VARBINARY(MAX) | BYTEA | LONGBLOB | BLOB |
| `text` | CLOB / NCLOB | NVARCHAR(MAX) | TEXT | LONGTEXT | TEXT |

Reading the table shows these columns as binary / text, and auto-map picks the type for you. Each file is
read into memory once, so keep `max_file_mb` (default 100 MB) within what the server can hold. Text is
decoded with `files.encoding`: `auto` uses the byte-order mark, then UTF-8, then Windows-1252.

**Never the same file twice** – each load keeps a **ledger** (`logs\loads\<load>.files.db`) of every file it
loaded: path, size, time, SHA-256, run, where it was moved.

- A file whose content was already loaded is **skipped**, even when it is renamed or copied back.
- With `check_target` (on by default) and a column mapped from `sha256`, a file whose hash is already in
  the table is skipped too, even if another tool loaded it.
- A known file name with **new content** is a *changed* file. `on_changed: version` loads it as another row;
  `on_changed: replace` first deletes the rows whose `file_key` column has that file's value
  (`file_path` or `file_name`), in the same transaction.
- If nothing is new, the run is *skipped* with exit code **3**. `--force` loads files already loaded again.
- `stable_seconds` (load setting) leaves files that are still being copied for the next run;
  `max_files` limits how many files one run takes.

**One transaction per file**: a file that fails leaves no rows behind. It is moved to the failed-files
folder (`rejected\`) with a `.reason.txt` next to it, and the other files still load. The run then
ends with exit code **2**. Rows rejected in records mode go to the rejects file, and a file with more
than `max_rejects` rejected rows is not loaded.

**After loading**: `after_load: move` (default) moves each loaded file to `processed\<date>\`, keeping
sub-folders. `leave` keeps it in place (the ledger still prevents reloads). `delete` removes it after the
commit. Copies of files already loaded are tidied away the same way.

**Where a folder may be**: a folder inside the project's data folder (`inbound`), or an absolute or UNC path
(`D:\inbound`, `\\fileserver\hr\in`) that is listed in `security.allowed_folders`. The account that runs
Portwright – the service or the Stonebranch agent – needs read access, and modify access when files are
moved or deleted.

<!-- studio -->
## In Studio

1. **Sources > Add source**, type **Folder of files**.
   - **Folder**: a name inside the data folder, or `D:\inbound` / `\\server\share\hr`. Tick *Allow
     Portwright to read this folder* for a path outside the data folder.
   - **Files to load**: patterns such as `*.pdf` or `HR_*.csv`. **Skip**: patterns such as `~*` or `*.tmp`.
   - Sub-folders, and the maximum file size.
   - **Test** lists the matching files.
2. **Loads > New load**. Choose the folder under *File or folder*, the SQL source and the table, then
   **Read columns** and **Auto-map**. Map `content` to the BLOB column (type *binary*) or `content_text` to
   the CLOB column (type *text*).
3. Card **2 · Files**:
   - **Each file becomes**: document or records.
   - **Same file name, new content**: *new version* or *replace*. Replace needs a **File key column**.
   - *Skip files whose sha256 is already in the table*.
   - **After a file is loaded**: move, leave or delete; the archive folder; the folder for failed files.
   - Encoding, max files per run, and the wait time for files that are still being copied.
4. **Preview (dry run)** lists every file with what would happen to it. **5 · Files in the folder > Check
   folder now** does the same at any time. **Loaded files** shows the ledger; the replay button lets a file
   be loaded again.
5. **Save**, then **Run**. The report lists each file: loaded, skipped, failed, waiting, and where it was moved.

<!-- cli -->
## With the CLI

```powershell
pw source add hr_docs -t folder --path inbound --include "*.pdf" --include "*.docx" --exclude "~*" --recursive
pw source allow-folder \\fileserver\hr\scans            # a folder outside the data folder
pw source add scans -t folder --path \\fileserver\hr\scans --include "*.pdf" --max-file-mb 200
pw source test hr_docs                                  # the matching files

pw load create docs --from-source hr_docs --to hr_rw --table HR.EMP_DOCS --auto-map
pw load map docs --set "content=DOC_BLOB:binary" --set "sha256=DOC_SHA" --value "LOADED_BY=${run_id}"
pw load map docs --on-changed replace --file-key FILE_PATH --after-load move --archive-folder processed
pw load files docs                    # what a run would do with each file now
pw load run docs --dry-run
pw load run docs                      # exit 0 ok, 2 some files failed, 1 nothing loaded, 3 nothing new
pw load files docs --ledger           # what was loaded, when, moved where
pw load forget docs scans/2026/emp123.pdf    # let one file be loaded again (or --all)
```

A folder of CSV files into one table:

```powershell
pw source add daily -t folder --path \\etl\exports\daily --include "transfers_*.csv"
pw load create daily-transfers --from-source daily --to hr_rw --table HR.TRANSFERS --auto-map
pw load map daily-transfers --read records --set "file_name=SOURCE_FILE"
```

In `portwright.yaml`:

```yaml
sources:
  hr_docs: {type: folder, path: inbound, include: ["*.pdf"], recursive: true, max_file_mb: 100}
loads:
  - name: docs
    from: {source: hr_docs}
    to: {source: hr_rw, table: HR.EMP_DOCS}
    files: {read: document, on_changed: replace, file_key: FILE_PATH, after_load: move,
            archive_folder: processed, rejected_folder: rejected, check_target: true}
    columns:
      - {source: file_path, target: FILE_PATH}
      - {source: sha256, target: DOC_SHA}
      - {source: content, target: DOC_BLOB, type: binary}
      - {source: mime_type, target: MIME_TYPE}
```
