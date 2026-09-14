---
type: regex
pattern: "(?<![0-9A-Fa-f-])\\d{20}(?![0-9A-Fa-f-])"
match: not_contains
target: last_message
---
