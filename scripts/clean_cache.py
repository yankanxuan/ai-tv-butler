#!/usr/bin/env python3
"""Remove only Python bytecode caches under known code directories.

No runtime-data cleanup is performed. --dry-run prints the exact file targets.
Symlinks and non-bytecode files are never removed.
"""
from pathlib import Path
import argparse


def cache_targets(root: Path) -> list[Path]:
    root = root.resolve()
    targets = []
    for name in ("adbtv", "tests", "scripts"):
        code = root / name
        if code.is_symlink() or not code.is_dir():
            continue
        for candidate in code.rglob("*"):
            if (candidate.is_symlink() or not candidate.is_file() or
                    candidate.suffix not in (".pyc", ".pyo") or
                    candidate.parent.name != "__pycache__"):
                continue
            resolved = candidate.resolve()
            if code.resolve() not in resolved.parents:
                continue
            # A symlink anywhere in the path must not redirect cleanup.
            if any(parent.is_symlink() for parent in candidate.parents if parent != root):
                continue
            targets.append(candidate)
    return sorted(targets)


def clean(root: Path, dry_run: bool = False) -> list[Path]:
    targets = cache_targets(root)
    for target in targets:
        print(target)
        if not dry_run:
            target.unlink()
    if not dry_run:
        for directory in sorted({target.parent for target in targets}):
            try:
                directory.rmdir()  # only succeeds for an empty cache directory
            except OSError:
                pass
    return targets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    clean(Path(__file__).resolve().parent.parent, args.dry_run)
