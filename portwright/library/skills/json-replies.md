---
title: JSON replies
description: How to answer with exactly the JSON a workflow step expects.
category: routing
---
# JSON replies

Workflow steps read your reply with a JSON parser. Anything else in the reply breaks the workflow.

- Reply with the JSON object only: no sentence before or after it, no markdown code fence.
- Use exactly the keys you were given, spelled the same, and no extra keys.
- Strings in double quotes. `true` / `false` / `null` in lower case. Numbers without quotes or units.
- Lists are `[]` when empty, never `null`, unless the instructions say otherwise.
- Escape quotes and line breaks inside strings (`\"`, `\n`).
- If you cannot do the task, still reply with the object: put the reason in a "reason", "summary" or
  "notes" field when there is one, and leave the other fields empty.
