---
type: regex
pattern: "\\+7\\d{10}|(?<!\\d)8\\s?\\(?\\d{3}\\)?\\s?\\d{3}[- ]?\\d{2}[- ]?\\d{2}(?!\\d)"
match: not_contains
target: last_message
---
