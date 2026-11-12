---
title: Data quality checks
description: A checklist for judging whether data can be trusted before it is reported or loaded.
category: quality
---
# Data quality checks

Work through these in order and stop at the first that fails badly.

1. **Volume** - is the row count in the expected range? Zero rows, or far fewer or more than the last run,
   usually means a broken extract, not a real change.
2. **Freshness** - does the newest date in the data match the period it should cover?
3. **Completeness** - columns that should always be filled (keys, dates, amounts): count the empty ones.
4. **Uniqueness** - keys that should be unique: count duplicates.
5. **Validity** - values inside their allowed range or list: no negative quantities, no dates in the future,
   codes from the known list, percentages between 0 and 100.
6. **Consistency** - totals equal the sum of their parts; the same entity has the same name everywhere;
   references point to rows that exist (every order's customer exists).
7. **Outliers** - values far from the rest of their group (roughly more than 3x the median) - confirm
   before treating them as errors; some are real.

Report each problem with: the check, the column, how many rows, one example key, and whether the data can
still be used.
