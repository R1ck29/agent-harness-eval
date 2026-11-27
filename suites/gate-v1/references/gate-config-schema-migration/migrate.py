"""Migrate a v1 config to v2 (see SPEC.md)."""
import json
import sys

V1_KEYS = {"version", "host", "port", "debug", "timeout"}
V2_KEYS = {"version", "server", "log_level", "timeout_ms"}
SERVER_KEYS = {"host", "port"}


class ConfigError(Exception):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise ConfigError(f"{where} must be an object")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def migrate_v1(cfg):
    _check_keys(cfg, V1_KEYS, "config")
    host = cfg.get("host")
    port = cfg.get("port", 8080)
    debug = cfg.get("debug", False)
    timeout = cfg.get("timeout", 30)
    if not isinstance(host, str) or not host:
        raise ConfigError("host is required and must be a string")
    if not _is_int(port) or not isinstance(debug, bool) or not _is_number(timeout):
        raise ConfigError("wrong type for port, debug or timeout")
    return {"version": 2, "server": {"host": host, "port": port},
            "log_level": "debug" if debug else "info", "timeout_ms": int(round(timeout * 1000))}


def check_v2(cfg):
    _check_keys(cfg, V2_KEYS, "config")
    server = cfg.get("server")
    _check_keys(server, SERVER_KEYS, "server")
    if (not isinstance(server.get("host"), str) or not server["host"] or not _is_int(server.get("port"))
            or cfg.get("log_level") not in ("debug", "info") or not _is_int(cfg.get("timeout_ms"))):
        raise ConfigError("invalid v2 config")
    return {"version": 2, "server": {"host": server["host"], "port": server["port"]},
            "log_level": cfg["log_level"], "timeout_ms": cfg["timeout_ms"]}


def migrate(cfg):
    if not isinstance(cfg, dict):
        raise ConfigError("config must be an object")
    version = cfg.get("version")
    if version == 1 and _is_int(version):
        return migrate_v1(cfg)
    if version == 2 and _is_int(version):
        return check_v2(cfg)
    raise ConfigError(f"unsupported version: {version!r}")


def main(argv):
    if len(argv) != 2:
        print("error: usage: migrate.py INPUT OUTPUT", file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as f:
            result = migrate(json.load(f))
    except (OSError, ValueError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with open(argv[1], "w", encoding="utf-8") as f:
        f.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
