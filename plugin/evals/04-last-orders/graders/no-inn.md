---
type: regex
pattern: "(?<![0-9A-Fa-f-])\\d{10}(?:\\d{2})?(?![0-9A-Fa-f-])"
match: not_contains
target: last_message
---
