#!/usr/bin/env python3
"""Build either a public source package or a private backup archive.

Two very different artifacts must not be confused:

* **public** — source only. No local ``config.yaml`` (real TV IP, media paths,
  tokens), no runtime state, no keys. This is what can be shared or published.
* **private** — same source plus the operator's own configuration and (without
  ``--with-state``) deliberately *no* runtime data. For personal restore only.

Usage::

    python3 scripts/package.py public  --out /tmp/ai-tv-butler-3.4.2.tar.gz
    python3 scripts/package.py private --out ~/backups/private-3.4.2.tar.gz
    python3 scripts/package.py public  --check      # 只做内容审计，不打包

The audit fails the build when anything sensitive is found, so a mistake cannot
silently ship a device address or a token.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_EXCLUDE = (
    "config.yaml", "config.local.yaml", "*.token", "*.sqlite3", "*.sqlite3-*",
    "state/*", "logs/*", "deploy/data/*", "*.bak", "*.premigration-*",
    ".git/*", "__pycache__/*", "*/__pycache__/*", "*.pyc", "build/*",
    "*.egg-info/*", "dist/*", ".venv/*", "*~", "*.swp", ".DS_Store",
)
PRIVATE_EXCLUDE = tuple(x for x in PUBLIC_EXCLUDE if x != "config.yaml")
# 只保留目录占位文件
KEEP_SUFFIX = (".gitkeep",)
# 公开包中不允许出现的敏感模式
SECRET_PATTERNS = (
    (re.compile(rb"192\.168\.\d{1,3}\.\d{1,3}"), "局域网设备地址"),
    (re.compile(rb"\b(?:sk|ak)-[A-Za-z0-9]{12,}\b"), "疑似 API Key"),
    (re.compile(rb"MA_[A-Za-z0-9_\-]{20,}"), "控制台令牌"),
    (re.compile(rb"serve_token\s*:\s*[\"']?[A-Za-z0-9]"), "媒体服务令牌"),
)

# Match literal assignments in YAML, dotenv, JSON and source without depending
# on a provider's current token format. Expressions and explicit templates are
# handled separately so source references to configuration remain publishable.
CREDENTIAL_ASSIGNMENT = re.compile(
    rb'''(?im)(?:^|[,{;])\s*(?:export\s+)?["']?(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|token|password|passwd|secret|credential|secret[_-]?access[_-]?key)["']?\s*[:=]\s*([^\r\n]+)'''
)
HOME_PATH = re.compile(
    rb'''(?<![A-Za-z0-9_/])(?:/(?:home|Users)/[^/\s"'`<>${}\\]+|[A-Za-z]:[\\/]Users[\\/][^\\/\s"'`<>${}]+)'''
)


def _private_env(relative: str) -> bool:
    return any(part.startswith(".env") and part != ".env.example"
               for part in PurePosixPath(relative).parts)


# Existing offline tests deliberately exercise authentication with these exact
# public fixtures. This exception applies only to Python tests, never configs or
# arbitrary newly planted credentials, and does not disable the other scans.
TEST_CREDENTIAL_FIXTURES = frozenset({
    b"secret-token", b"permanent-secret", b"abc", b"private-model-secret",
    b"private", b"token", b"new-key", b"123", b"wrong",
})


def _literal_credential(value: bytes, *, test_source: bool = False) -> bool:
    value = value.strip()
    if value.startswith((b'"', b"'")):
        quote = value[:1]
        end = value.find(quote, 1)
        if end < 0:
            return False
        tail = value[end + 1:].lstrip()
        if tail and not tail.startswith((b"#", b",", b"}", b"]", b";")):
            return False  # concatenation or another source expression
        value = value[1:end]
    else:
        value = value.split(b" #", 1)[0].strip().rstrip(b",};")
        if re.search(rb"[\s()\[\]]", value):
            return False  # source expression rather than a literal
    if test_source and value in TEST_CREDENTIAL_FIXTURES:
        return False
    lower = value.lower()
    if lower in (b"", b"null", b"none", b"false", b"true", b"~"):
        return False
    if value.startswith((b"$", b"<", b"{{")):
        return False
    if re.fullmatch(rb"(?:your[_-].*|replace[_-].*|change[_-]?me|example|placeholder)", lower):
        return False
    return True


def _audit_data(data: bytes, relative: str) -> list[str]:
    problems = []
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(data):
            problems.append(f"{relative}: 含{label}")
    if any(_literal_credential(match.group(1), test_source=(
               relative.startswith("tests/") and relative.endswith(".py")))
           for match in CREDENTIAL_ASSIGNMENT.finditer(data)):
        problems.append(f"{relative}: 含凭据赋值")
    if HOME_PATH.search(data) or HOME_PATH.search(relative.encode()):
        problems.append(f"{relative}: 含用户主目录绝对路径")
    return problems


def version() -> str:
    text = (ROOT / "adbtv" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("无法确定版本号")
    return match.group(1)


def iter_files(mode: str):
    patterns = PUBLIC_EXCLUDE if mode == "public" else PRIVATE_EXCLUDE
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        if mode == "public" and _private_env(relative):
            continue
        if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            # 运行期目录只保留 .gitkeep 占位
            if relative.startswith(("state/", "logs/")):
                if path.name in KEEP_SUFFIX:
                    yield path, relative
            continue
        yield path, relative


def audit(files, mode: str) -> list[str]:
    problems: list[str] = []
    for path, relative in files:
        if mode != "public":
            continue
        if relative == "config.yaml" or _private_env(relative):
            problems.append(f"公开包不得包含 {relative}")
            continue
        if path.is_symlink():
            problems.append(f"{relative}: 公开包不得包含符号链接")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(f"{relative}: 读取失败 {exc}")
            continue
        problems.extend(_audit_data(data, relative))
    return problems


def audit_archive(out: Path, mode: str) -> list[str]:
    """Inspect member names, types and actual payloads without extracting them."""
    if mode != "public":
        return []
    problems: list[str] = []
    try:
        with tarfile.open(out, "r:gz") as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                relative = "/".join(parts[1:])
                if (len(parts) < 2 or member.name.startswith("/")
                        or ".." in parts or "\\" in member.name):
                    problems.append(f"{member.name}: 非法归档路径")
                    continue
                if not member.isfile():
                    problems.append(f"{relative}: 公开包仅允许普通文件")
                    continue
                if (relative == "config.yaml" or _private_env(relative)
                        or any(fnmatch.fnmatch(relative, p) for p in PUBLIC_EXCLUDE)
                        and not (relative.startswith(("state/", "logs/"))
                                 and parts[-1] in KEEP_SUFFIX)):
                    problems.append(f"公开包不得包含 {relative}")
                stream = archive.extractfile(member)
                if stream is None:
                    problems.append(f"{relative}: 无法读取归档内容")
                else:
                    with stream:
                        problems.extend(_audit_data(stream.read(), relative))
    except (OSError, tarfile.TarError, EOFError) as exc:
        problems.append(f"归档读取失败: {exc}")
    return problems


def build(mode: str, out: Path) -> Path:
    files = [(path, relative) for path, relative in iter_files(mode)
             if path.resolve() != out.resolve()]
    problems = audit(files, mode)
    if problems:
        raise SystemExit("内容审计失败，拒绝打包：\n  " + "\n  ".join(problems))
    name = f"ai-tv-butler-{version()}"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the destination so only a completed, audited archive becomes
    # visible at --out. Failure also preserves any previous artifact.
    with tempfile.TemporaryDirectory(prefix=".package-", dir=out.parent) as staging:
        candidate = Path(staging) / "archive.tar.gz"
        with tarfile.open(candidate, "w:gz") as archive:
            for path, relative in files:
                archive.add(path, arcname=f"{name}/{relative}")
        problems = audit_archive(candidate, mode)
        if problems:
            raise SystemExit("归档审计失败，拒绝发布：\n  " + "\n  ".join(problems))
        candidate.replace(out)
    return out


def check(mode: str) -> int:
    files = list(iter_files(mode))
    problems = audit(files, mode)
    print(f"模式={mode} 文件数={len(files)}")
    if problems:
        print("发现敏感内容：")
        for problem in problems:
            print("  -", problem)
        return 1
    print("审计通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("public", "private"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true", help="仅审计，不打包")
    args = parser.parse_args()
    if args.check:
        return check(args.mode)
    if not args.out:
        parser.error("打包需要 --out")
    artifact = build(args.mode, args.out)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    print(json.dumps({"mode": args.mode, "archive": str(artifact),
                      "bytes": artifact.stat().st_size, "sha256": digest},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())