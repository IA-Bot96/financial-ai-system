SYSTEM:
You classify financial tables extracted from an annual report. Assign each
table EXACTLY one type from the allowed list, based on its title and line-item
labels. If a table does not clearly fit any type, use "other". Classify ALL
tables given in a single response.

USER:
Allowed types: {allowed_types}

Tables (id :: signature):
{tables}

Return JSON: {{ "classifications": [ {{ "table_id": "...", "statement_type": "..." }} ] }}
