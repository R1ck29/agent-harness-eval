# tally — word frequency CLI

Write `tally.py` (standard library only).

```
python tally.py [--top N] FILE
```

- Counts words in FILE. A word is a run of ASCII letters `a-z` / `A-Z`; matching is case-insensitive and the output is lowercase.
- FILE may be `-` to read standard input.
- Output (stdout): one line per word, `WORD COUNT` (a single space), most frequent first, ties broken alphabetically. Exit `0`.
- `--top N` prints only the first N lines. N must be a positive integer.
- An empty file (or a file with no words) prints nothing and exits `0`.
- `--help` prints a usage message to stdout and exits `0`.

Errors (stdout stays empty; one message on stderr that starts with `error:` or `usage:`):

| situation | exit code |
|---|---|
| FILE is missing / cannot be read | `1` |
| no FILE argument | `2` |
| `--top` without a value, or N is not a positive integer | `2` |
| unknown option | `2` |
