#!/usr/bin/env python3
"""Safe local config migration. Does not print secrets or overwrite a backup."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

import yaml


def main():
    parser = argparse.ArgumentParser(description="迁移 AI TV Butler 配置到V3")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    path = Path(args.config).expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("配置不是YAML对象")
    backup = path.with_suffix(path.suffix + f".v2-backup-{time.time_ns()}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    data.setdefault("memory", {"enabled": False, "path": ""})
    data.setdefault("voice", {"enabled": False})
    data.setdefault("execution", {"enabled": True, "timeout": 180, "max_actions": 100})
    data.setdefault("perception", {"cache_ttl": .3, "vision_cache_size": 16})
    data.setdefault("recovery", {"max_attempts": 4, "per_failure": 2})
    server = data.setdefault("server", {})
    # Migrate anonymous remote access to loopback unless the operator explicitly
    # supplies a strong token. No anonymous open console after upgrade.
    if server.get("host") in ("0.0.0.0", "::", "") and not server.get("token"):
        server["host"] = "127.0.0.1"
    server.setdefault("host", "127.0.0.1")
    server.setdefault("port", 8770)
    server.setdefault("token", "")
    descriptor, temp = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    print(json.dumps({"migrated": str(path), "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
