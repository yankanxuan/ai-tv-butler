#!/usr/bin/env python3
"""Create private rollback snapshots, or restore into a new empty directory."""
import argparse
import hashlib
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def snapshot(version, output=None):
    if not version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in version):
        raise ValueError("Invalid release name")
    directory = Path(output).expanduser().resolve() if output else ROOT.parent / "ai-tv-butler-backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"ai-tv-butler-{version}.tar.gz"
    if target.exists():
        raise FileExistsError(target)
    with tarfile.open(target, "x:gz") as archive:
        def exclude(info):
            parts = Path(info.name).parts
            if "__pycache__" in parts or ".git" in parts or ".venv" in parts:
                return None
            # Runtime state is separately backed up by the SQLite/JSON APIs;
            # release archives contain source + private local config only.
            if len(parts) > 1 and parts[1] in ("state", "logs") and not info.name.endswith(".gitkeep"):
                return info if info.isdir() and len(parts)==2 else None
            return info
        archive.add(ROOT, arcname="ai-tv-butler", filter=exclude)
    target.chmod(0o600)
    print(json.dumps({"archive": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}))
    return target


def restore(archive_path, destination):
    target = Path(destination).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore destination must be a new or empty directory")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            resolved = (target / member.name).resolve()
            if not resolved.is_relative_to(target) or member.issym() or member.islnk() or member.isdev():
                raise ValueError("Unsafe archive member")
        archive.extractall(target)
    print(target / "ai-tv-butler")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("version", nargs="?")
    parser.add_argument("--output")
    parser.add_argument("--restore")
    parser.add_argument("--destination")
    args = parser.parse_args()
    if args.restore:
        if not args.destination:
            parser.error("--restore requires --destination")
        restore(args.restore, args.destination)
    elif args.version:
        snapshot(args.version, args.output)
    else:
        parser.error("supply a version or --restore")
