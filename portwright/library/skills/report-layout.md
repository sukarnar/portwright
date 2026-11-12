---
title: Report layout for PDF
description: HTML and CSS that render well in Portwright's PDF backends, with master-detail patterns.
category: reports
---
# Report layout for PDF

Portwright renders report templates (Jinja2 + HTML) to PDF with WeasyPrint, or with xhtml2pdf when WeasyPrint
is not available. Write templates that look right in both.

## Structure
- One file. All CSS in one `<style>` element in `<head>`. No scripts, no external files, no `{% include %}`.
- Page: `@page { size: A4; margin: 15mm; }` (`A4 landscape` for wide tables).
- Fonts: `Arial, Helvetica, sans-serif`. No web fonts.
- Layout with tables and block elements. Avoid flexbox and grid: xhtml2pdf ignores them.
- Long tables: `thead { display: table-header-group; }` repeats the header on each page, and
  `tr { page-break-inside: avoid; }` keeps rows whole.

## Data
- Rows are in `rows` (alias `data`); `row_count`, `report_name`, `generated_at` are always there.
- Guard optional values: `{{ r.amount | default_if_none('-') }}`; format with `| number(2)`, `| currency`,
  `| date`.
- Always handle no rows: `{% if rows %} ... {% else %}<p>No records found.</p>{% endif %}`.

## Master-detail
When the report has datasets attached to its rows (for example `order.lines`), nest the loops:

```
{% for o in rows %}
  <h2>Order {{ o.id }}</h2>
  <table>
  {% for l in o.lines %}<tr><td>{{ l.product }}</td><td>{{ l.qty }}</td></tr>
  {% else %}<tr><td colspan="2">No lines.</td></tr>{% endfor %}
  </table>
{% endfor %}
```

A lookup attached with `one: true` is a single row or nothing: `{{ o.customer.name if o.customer else '-' }}`.
Every dataset is also available on its own as `datasets.<name>`.

## Totals and groups
- `{{ rows | sum_col('amount') | number(2) }}` for a total.
- `{% for key, group in rows | groupby_col('dept') %}` for sections per group.
