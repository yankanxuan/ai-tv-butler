#!/usr/bin/env python3
"""Prepare only directories owned and writable by the calling user; never chown/sudo."""
import argparse
import os
from pathlib import Path
import stat


def prepare(destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    # Validate all existing ancestors before any mutation. Do not repair ownership.
    current = destination
    while not current.exists():
        current = current.parent
    if not os.access(current, os.W_OK | os.X_OK):
        raise PermissionError(f"目录不可写: {current}")
    if destination.exists() and destination.stat().st_uid != os.getuid():
        raise PermissionError(f"目录不属于当前 UID {os.getuid()}: {destination}")
    for item in (destination, destination / ".android"):
        if item.is_symlink():
            raise PermissionError(f"拒绝符号链接目录: {item}")
        if item.exists() and (item.stat().st_uid != os.getuid() or not item.is_dir()):
            raise PermissionError(f"目录不属于当前用户或不是目录: {item}")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    (destination / ".android").mkdir(mode=0o700, exist_ok=True)
    config = destination / "config.yaml"
    if not config.exists():
        template = Path(__file__).resolve().parent.parent / "deploy" / "config.yaml.example"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(template.read_text(encoding="utf-8"))
    print(f"BUTLER_UID={os.getuid()}\nBUTLER_GID={os.getgid()}\nBUTLER_DATA_DIR={destination}")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("deploy/data"))
    args = parser.parse_args()
    prepare(args.data_dir)
