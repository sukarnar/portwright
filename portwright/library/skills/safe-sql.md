---
title: Safe SQL
description: How to write read-only SELECT queries that are correct, small and fast.
category: data
---
# Safe SQL

Use this whenever you write SQL for a SQL tool.

## The rules
- One statement, `SELECT` (or `WITH ... SELECT`) only. The tool refuses anything else.
- Never `SELECT *` from a large table to "have a look". To see the columns:
  `SELECT * FROM orders WHERE 1 = 0`.
- Filter and aggregate in SQL (`WHERE`, `GROUP BY`, `COUNT`, `SUM`), so the result is the answer, not the
  raw table. Ask for at most a few hundred rows.
- Order results (`ORDER BY`) whenever you show "top" or "latest" rows.

## Correctness traps
- `NULL`: `x = NULL` is never true; use `x IS NULL`. `COUNT(column)` skips NULLs, `COUNT(*)` does not.
  `AVG` ignores NULLs.
- Joins: a join to a table with several rows per key multiplies rows; check counts before and after, or
  aggregate the detail table first.
- Dates: compare against a range (`placed >= '2026-10-01' AND placed < '2026-11-01'`) instead of
  functions on the column, so indexes are used and time-of-day is not lost.
- Text matching: `LIKE` is case-sensitive on some databases; say which one you assumed.
- Integer division: `7 / 2` can be `3`; multiply by `1.0` for a decimal result.

## Dialects
- Row limits: `FETCH FIRST 100 ROWS ONLY` (Oracle 12c+, PostgreSQL), `LIMIT 100` (PostgreSQL, MySQL,
  SQLite), `SELECT TOP 100 ...` or `ORDER BY ... OFFSET 0 ROWS FETCH NEXT 100 ROWS ONLY` (SQL Server).
- Current date: `CURRENT_DATE` (most), `SYSDATE` / `TRUNC(SYSDATE)` (Oracle), `GETDATE()` (SQL Server).

## When the result looks wrong
Re-run with a smaller filter and check a few rows by hand before answering. Say so if you are unsure.
