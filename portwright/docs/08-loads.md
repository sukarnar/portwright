---
title: Loads and mapping
screen: loads
---
A load copies rows from a **file source** or a **GET endpoint** into a **database table**:
read, map the columns, check every row, write everything in **one transaction**, report.

**Modes**

| Mode | What it does |
|---|---|
| Append (`insert`) | inserts the rows; with a load key, rows whose key is already in the table are rejected (or skipped) |
| Kill & fill (`replace`) | deletes every row of the table, then inserts – in one transaction, so a failure keeps the old rows |
| Upsert (`upsert`) | updates rows whose key matches, inserts the others (Oracle/SQL Server MERGE) |

**Load key** – the columns that identify a row. Checked before writing, even if the table has no primary
key: empty keys are rejected, repeated keys are rejected (or keep first/last), existing keys in Append mode
are rejected (or skipped, or fail the load).

**Mapping** – one line per table column: a source column or a constant (`${now}`, `${today}`, `${run_id}`),
a type (string, integer, number, boolean, date, timestamp), a date format such as `%m/%d/%Y`, clean-up
(trim, upper, lower, null_if_empty, replace, code lookup), a default for blank cells, *required*.

Rows that fail go to `logs\loads\<run>_rejects.csv` with the reason. If more rows fail than
**Max rejects**, nothing is written.

<!-- studio -->
## In Studio

1. **Loads > New load**. Choose *File source* or *Endpoint*, the target SQL source (writes allowed) and the
   table (**Browse tables**). **Read columns** lists the table's columns.
2. **How rows are written**: mode, batch size, max rejects, what to do with duplicate / existing keys,
   and for kill & fill the safety limits (minimum rows, minimum % of the current rows).
3. **Column mapping**: **Auto-map** matches names; red lines must be fixed, yellow lines are guesses –
   check and **Confirm**. Tick the **Key** columns. The gear button opens clean-up, lookup, default and length options.
4. **Check and preview**: **Validate** checks against the real file and table; **Preview** maps every row
   without writing and shows rows before and after mapping.
5. **Save**, then **Run**. The result shows read / mapped / rejected / inserted / updated / upserted,
   and **View rejected rows** lists each bad row with its reason.
6. The Loads list shows the last run of every load; **History** lists all runs.

<!-- cli -->
## With the CLI

```powershell
# 1. create, with an auto-suggested mapping
portwright load create transfers-to-db --from-source transfers --to hr_rw --table hr.emp_transfers --auto-map

# 2. map where names differ, constants, clean-up, key and mode
portwright load map transfers-to-db --set "Emp No=emp_no:integer" --set "Eff Date=effective_date:date:%m/%d/%Y" `
  --set "Dept=dept_code" --transform "dept_code=trim,upper" --default "dept_code=UNK" --required emp_no `
  --value 'loaded_by=${run_id}' --value 'loaded_at=${now}' --key emp_no --mode upsert

# Append with key checks, keep the last copy of a repeated key, skip keys already loaded
portwright load map transfers-to-db --mode insert --key emp_no --duplicate-keys last --existing-keys skip

# Kill & fill, refuse if the file has fewer than 100 rows or under 50 % of the current rows
portwright load map transfers-to-db --mode replace --min-rows 100 --min-percent 50

# 3. check, preview, run
portwright load show transfers-to-db
portwright load validate transfers-to-db
portwright load run transfers-to-db --dry-run --preview 3
portwright load run transfers-to-db
portwright load history transfers-to-db --last 20
portwright load rejects 20261017-070035-419658
portwright load list
portwright load remove old-load
```

Exit codes of `load run`: **0** loaded · **2** loaded, some rows rejected · **1** failed, nothing written ·
**3** skipped, the source has not changed since the last successful load.
