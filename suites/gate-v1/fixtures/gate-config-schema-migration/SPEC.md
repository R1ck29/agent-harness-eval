# Config schema migration (v1 -> v2)

Write `migrate.py`, a command-line tool: `python migrate.py INPUT OUTPUT`.

## v1 schema (flat)

| key | type | default | note |
|---|---|---|---|
| `version` | int | - | must be `1` |
| `host` | string | - | required |
| `port` | int | `8080` | |
| `debug` | bool | `false` | |
| `timeout` | number (seconds) | `30` | |

## v2 schema (nested)

```json
{"version": 2, "server": {"host": "...", "port": 8080}, "log_level": "info", "timeout_ms": 30000}
```

- `log_level` is `"debug"` when v1 `debug` is true, otherwise `"info"`.
- `timeout_ms` is v1 `timeout` x 1000, as an integer (round to the nearest integer).

## Rules

1. Keys that are not part of the schema (at any level) are an error. Never ignore them silently.
2. If INPUT is already v2 and valid, OUTPUT gets the same content (the tool is idempotent: migrating its own output changes nothing).
3. INPUT is never modified or deleted. OUTPUT is written only on success.
4. Write OUTPUT as JSON with 2-space indentation and a trailing newline.
5. Exit codes: `0` success; `2` invalid input (unreadable file, invalid JSON, unknown key, missing `host`, wrong type, unsupported `version`).
   On exit `2` print one line to stderr that starts with `error:`, print nothing to stdout, and do not create OUTPUT.
