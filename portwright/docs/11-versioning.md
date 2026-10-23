---
title: Versioning and change detection
screen: loads
---
Every real load run fingerprints what it read – a hash of the rows, the file name, size and modified time,
the file's SHA-256 and the column list – and stores each distinct content as a numbered **version**
(v1, v2, …). A compressed copy of the last 30 versions is kept (`archive_keep`).

| Setting | Default | Meaning |
|---|---|---|
| Source unchanged (`on_unchanged`) | skip | same data as the last successful load: skip (exit code 3), load anyway, or fail |
| Columns changed (`on_header_change`) | fail | columns added or removed since that version: fail or warn |
| Wait until unchanged (`stable_seconds`) | 0 | wait until the file has not changed for N seconds (still being copied) |
| Max file age (`max_age_hours`) | none | fail if the file is older (e.g. yesterday's file was not replaced) |
| Copies to keep (`archive_keep`) | 30 | versions kept for comparing; 0 = fingerprint only |

<!-- studio -->
## In Studio

* **Loads > Check sources** reads every source and shows *changed since vN*, *unchanged* or *never loaded*.
* In the load editor, **5 · Source versions**: the settings above, **Check source now**, and the list of
  versions with the runs that loaded them. **Compare** (arrows button) shows rows added, removed and changed between a
  version and the previous one, by the load key.
* A run skipped because nothing changed shows **Run anyway**.

<!-- cli -->
## With the CLI

```powershell
portwright load check transfers-to-db           # exit 0 = changed or never loaded, 3 = unchanged
portwright load versions transfers-to-db
portwright load diff transfers-to-db 3 4         # rows added / removed / changed between v3 and v4
portwright load run transfers-to-db --force      # load even though unchanged / columns changed

portwright load map transfers-to-db --on-unchanged load          # always load
portwright load map transfers-to-db --stable-seconds 60 --max-age-hours 26 --archive-keep 10
portwright load map transfers-to-db --on-header-change warn
```

Files: `logs\loads\versions\<load>.jsonl` (one line per version) and
`logs\loads\archive\<load>\v0001.jsonl.gz` (the copies).
