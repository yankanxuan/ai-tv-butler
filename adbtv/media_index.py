"""Local fnOS media-library index built from the library filesystem.

The fnOS media app keeps a Bleve index and an authenticated HTTP API; this module
instead scans the shared library directories on the same NAS. That makes title/
season/episode resolution deterministic and independent of TV UI scraping, while
staying a pure read-only operation over the user's own files.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".flv", ".wmv", ".webm"}
_YEAR = re.compile(r"\s*[\(（]\s*(19|20)\d{2}\s*[\)）]\s*$")
_CN = {c: i for i, c in enumerate("零一二三四五六七八九")}


def cn_number(text: str) -> int | None:
    """Parse 0..999 Chinese/Arabic numerals used in season/episode names."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in digits:
            number = digits[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        else:
            return None
    total = section + number
    # "零五" style: leading 零 already contributes nothing; number holds last digit
    return total if total != 0 or "零" in text else total


def normalize(text: str) -> str:
    """Case/punctuation/space-insensitive key; keeps CJK characters."""
    text = (text or "").casefold()
    return re.sub(r"[\s·:：,，。.、!！?？\-\u2010-\u2015'\"]", "", text)


def parse_show_name(name: str) -> tuple[str, int | None, str | None]:
    """Split '穿成野神…第十季 (2026)' -> (base, season, year)."""
    year = None
    m = _YEAR.search(name)
    raw = name
    if m:
        year = m.group(0).strip(" ()（）")
        raw = name[:m.start()]
    raw = raw.strip()
    season = None
    base = raw
    # 第N季 / 第N部 / N季 / 第N
    m = re.search(r"第\s*([0-9零一二三四五六七八九十百]+)\s*季$", raw)
    if m:
        season = cn_number(m.group(1))
        base = raw[:m.start()].strip()
    else:
        m = re.search(r"第\s*([0-9零一二三四五六七八九十百]+)\s*部$", raw)
        if m:
            season = cn_number(m.group(1))
            base = raw[:m.start()].strip()
        else:
            m = re.search(r"([0-9]{1,2})$", raw)
            if m and len(raw) > len(m.group(1)) + 1:
                season = int(m.group(1))
                base = raw[:m.start()].strip()
    return (base or raw), season, year


def parse_episode(name: str) -> tuple[int | None, int | None]:
    stem = Path(name).stem
    m = re.search(r"[Ss](\d{1,3})[\s._-]*[Ee](\d{1,4})", stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"第\s*([0-9]+)\s*集", stem)
    if m:
        return None, int(m.group(1))
    m = re.search(r"[Ee][Pp]?\s*(\d{1,4})$", stem)
    if m:
        return None, int(m.group(1))
    return None, None


# ---------------------------------------------------------------------------
# NFO metadata (V4.4)
#
# Real hongguo-style NFO files carry <plot>/<studio>/<uniqueid>/<tag>, not the
# <genre> element the older parser only looked at. Parsing stays defensive: a
# size ceiling, no external entities (stdlib ElementTree never resolves them,
# defusedxml is used when the deployment image happens to ship it), and every
# failure degrades to "no metadata" instead of raising.
# ---------------------------------------------------------------------------
NFO_NAMES = ('movie.nfo', 'tvshow.nfo')
_NFO_MAX_BYTES = 1024 * 1024
KIND_LABELS = {'movie': '电影', 'series': '电视剧', 'short_drama': '短剧', 'unknown': '未知'}
_GENRE_SPLIT = re.compile(r'[,，、/|;；]')

try:  # pragma: no cover - depends on the deployment image
    from defusedxml import ElementTree as _XML  # type: ignore
except Exception:  # pragma: no cover - the stdlib fallback is the normal case
    import xml.etree.ElementTree as _XML


def empty_evidence() -> dict[str, Any]:
    """Neutral evidence record: nothing known, nothing guessed."""
    return {'movie_nfo': False, 'tvshow_nfo': False, 'plot': None, 'genres': [],
            'studio': None, 'uniqueids': [], 'runtime': None, 'year': None,
            'region': None, 'type': None, 'sources': []}


def _local(tag: Any) -> str:
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def parse_nfo(path: str | Path) -> dict[str, Any] | None:
    """Parse one Kodi-style NFO. ``None`` when absent, oversized or malformed."""
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size > _NFO_MAX_BYTES:
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    try:
        root = _XML.fromstring(raw)
    except Exception:  # malformed XML, entity bombs, encoding errors
        return None

    def texts(name: str) -> list[str]:
        out = []
        for node in root.iter():
            if _local(node.tag) == name and node.text and node.text.strip():
                out.append(node.text.strip())
        return out

    uniqueids: list[tuple[str | None, str]] = []
    for node in root.iter():
        if _local(node.tag) != 'uniqueid':
            continue
        value = (node.text or '').strip()
        kind = (node.get('type') or '').strip().casefold() or None
        if value and (kind, value) not in uniqueids:
            uniqueids.append((kind, value))

    genres: list[str] = []
    for raw_genre in texts('genre') + texts('tag'):
        for part in _GENRE_SPLIT.split(raw_genre):
            part = part.strip()
            if part and part not in genres:
                genres.append(part)

    return {'plot': _first(texts('plot')),
            'genres': genres,
            'studio': _first(texts('studio')),
            'uniqueids': uniqueids,
            'runtime': _first(texts('runtime')),
            'year': _first(texts('year')),
            'region': _first(texts('country')) or _first(texts('region')),
            'type': (_first(texts('type')) or '').casefold() or None}


def _read_directory_nfo(directory: Path) -> dict[str, Any]:
    """Merge movie.nfo/tvshow.nfo found directly inside ``directory``."""
    evidence = empty_evidence()
    for name in NFO_NAMES:
        path = directory / name
        try:
            present = path.is_file()
        except OSError:
            present = False
        if not present:
            continue
        evidence['movie_nfo' if name == 'movie.nfo' else 'tvshow_nfo'] = True
        evidence['sources'].append(name)
        parsed = parse_nfo(path)
        if parsed is None:
            continue
        for field in ('plot', 'studio', 'runtime', 'year', 'region', 'type'):
            if not evidence[field] and parsed.get(field):
                evidence[field] = parsed[field]
        for genre in parsed.get('genres') or []:
            if genre not in evidence['genres']:
                evidence['genres'].append(genre)
        for pair in parsed.get('uniqueids') or []:
            if pair not in evidence['uniqueids']:
                evidence['uniqueids'].append(pair)
    return evidence


class NfoCache:
    """Per-directory NFO cache invalidated by the NFO files' (mtime_ns, size).

    Rebuilding the index must not re-read 45 unchanged files every minute; the
    signature check costs two ``stat`` calls per directory instead.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[tuple, dict[str, Any]]] = {}
        self.parses = 0

    @staticmethod
    def _signature(directory: Path) -> tuple:
        signature = []
        for name in NFO_NAMES:
            try:
                info = (directory / name).stat()
                signature.append((name, info.st_mtime_ns, info.st_size))
            except OSError:
                signature.append((name, None, None))
        return tuple(signature)

    def get(self, directory: str | Path) -> dict[str, Any]:
        directory = Path(directory)
        key = str(directory)
        signature = self._signature(directory)
        cached = self._data.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        evidence = _read_directory_nfo(directory)
        self.parses += 1
        self._data[key] = (signature, evidence)
        return evidence


def nfo_evidence(directory: str | Path, cache: "NfoCache | None" = None) -> dict[str, Any]:
    """Metadata for one work: its own video directory first, then the parent.

    ``tvshow.nfo`` normally sits one level above a ``Season NN`` folder, so the
    parent is consulted only when the video directory itself has no NFO at all.
    """
    directory = Path(directory)
    getter = cache.get if cache is not None else _read_directory_nfo
    own = getter(directory)
    if own['sources']:
        return own
    parent = directory.parent
    if parent == directory:
        return own
    return getter(parent)


def classify_kind(entry: "MediaEntry", paths: list[str], evidence: dict[str, Any]) -> str:
    """Evidence-only kind decision, in the V4.4 contract's priority order.

    Nothing here inspects the title: a work is only a short drama when the NFO
    or the folder path proves it.
    """
    if evidence.get('movie_nfo') or (evidence.get('type') or '') == 'movie':
        return 'movie'
    # A library-level category folder ("电影/…") is structural evidence, the same
    # way "短剧/…" is: fnOS keeps category folders at the root of the library.
    # It only applies to an exact category component, never to a title fragment,
    # and NFO evidence above always wins.
    if any(part in ('电影', '影片') for path in paths for part in Path(str(path)).parts):
        if not any('短剧' in Path(str(path)).parts for path in paths):
            return 'movie'
    studio = evidence.get('studio') or ''
    hongguo = any((kind or '') == 'hongguo' for kind, _value in evidence.get('uniqueids') or [])
    short_path = any('短剧' in Path(str(path)).parts for path in paths)
    if '短剧' in studio or hongguo or short_path:
        return 'short_drama'
    marked = sum(1 for path in paths if parse_episode(path)[1] is not None)
    if marked or len(getattr(entry, 'seasons', {}) or {}) > 1:
        return 'series'
    if len(paths) == 1:
        return 'movie'
    return 'unknown'


@dataclass
class MediaEntry:
    base: str
    title: str
    seasons: dict[str, dict[str, str]] = field(default_factory=dict)  # "1"->{"1": path}
    year: str | None = None
    season_hint: int | None = None
    # Video files that carry no episode marker in a multi-file folder. They are
    # counted for diagnostics only and never given invented episode numbers.
    unindexed: int = 0
    # number of episodes per season, for quick display
    def episode_count(self, season: int | None = None) -> int:
        if season is not None:
            return len(self.seasons.get(str(season), {}))
        return sum(len(v) for v in self.seasons.values())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaEntry":
        return cls(base=data["base"], title=data.get("title", data["base"]),
                   seasons=data.get("seasons", {}), year=data.get("year"),
                   season_hint=data.get("season_hint"),
                   unindexed=int(data.get("unindexed") or 0))


class MediaIndex:
    def __init__(self, roots: list[str], aliases: dict[str, str] | None = None,
                 cache_path: str | None = None):
        self.roots = [Path(r).expanduser() for r in roots if r]
        self.aliases = aliases or {}
        self.cache_path = Path(cache_path).expanduser() if cache_path else None
        self.entries: list[MediaEntry] = []
        self.scanned_at: float = 0.0
        self.errors: list[str] = []

    # ---------- 构建 ----------
    def scan(self, max_depth: int = 4) -> "MediaIndex":
        self.entries = []
        self.errors = []
        holders: list[MediaEntry] = []
        for root in self.roots:
            if not root.is_dir():
                self.errors.append(f"媒体根目录不存在: {root}")
                continue
            try:
                self._scan_root(root, max_depth, holders)
            except PermissionError as exc:
                self.errors.append(f"无权限读取 {root}: {exc}")
        self.entries = sorted(holders, key=lambda e: (normalize(e.base), e.season_hint or 0))
        self.scanned_at = time.time()
        return self

    @staticmethod
    def _looks_like_show(directory: Path) -> bool:
        try:
            children = list(directory.iterdir())
        except PermissionError:
            return False
        # A show contains video files directly or Season/第N季 subfolders.
        for c in children:
            if c.is_file() and c.suffix.lower() in VIDEO_EXT:
                return True
            if c.is_dir() and (re.fullmatch(r"[Ss]eason\s*\d{1,3}", c.name) or
                               re.fullmatch(r"第\s*[0-9零一二三四五六七八九十百]+\s*季", c.name) or
                               re.fullmatch(r"\d{1,3}", c.name)):
                return True
        return False

    def _scan_root(self, root: Path, max_depth: int, holders: list):
        def walk(directory: Path, depth: int):
            try:
                children = sorted(directory.iterdir())
            except PermissionError as exc:
                self.errors.append(f"无权限读取 {directory}: {exc}")
                return
            for child in children:
                if not child.is_dir() or child.name in ("backup_temp",):
                    continue
                if depth < max_depth and self._looks_like_show(child):
                    base, season_no, year = parse_show_name(child.name)
                    entry = MediaEntry(base=base, title=child.name, year=year, season_hint=season_no)
                    holders.append(entry)
                    season_dirs = [p for p in child.iterdir() if p.is_dir()]
                    if not season_dirs:
                        self._collect_episodes(entry, child, season_no, year)
                    for season_dir in season_dirs:
                        m = (re.fullmatch(r"[Ss]eason\s*(\d{1,3})", season_dir.name) or
                             re.fullmatch(r"第\s*([0-9零一二三四五六七八九十百]+)\s*季", season_dir.name) or
                             re.fullmatch(r"(\d{1,3})", season_dir.name))
                        sn = None
                        if m:
                            sn = int(m.group(1)) if m.group(1).isdigit() else cn_number(m.group(1))
                        self._collect_episodes(entry, season_dir, sn if sn is not None else season_no, year)
                elif depth < max_depth:
                    # Category folder (短剧/电影/电视剧…): recurse into it.
                    walk(child, depth + 1)

        walk(root, 0)

    def _collect_episodes(self, entry: MediaEntry, directory: Path,
                          season_default: int | None, year: str | None):
        try:
            files = list(directory.iterdir())
        except PermissionError:
            return
        videos = [f for f in files if f.is_file() and f.suffix.lower() in VIDEO_EXT]
        unmarked = []
        for f in videos:
            season, episode = parse_episode(f.name)
            season = season if season is not None else (season_default or 1)
            if episode is None:
                unmarked.append(f)
                continue
            entry.seasons.setdefault(str(season), {})[str(episode)] = str(f)
        # A single unmarked video in a folder is a movie-style item: it becomes
        # episode 1 so it stays resolvable. Several unmarked videos are
        # ambiguous (split parts or a collection) and are only counted, never
        # assigned invented episode numbers.
        if len(unmarked) == 1 and not entry.seasons and season_default in (None, 1):
            entry.seasons.setdefault('1', {})['1'] = str(unmarked[0])
        else:
            entry.unindexed += len(unmarked)

    # ---------- 查询 ----------
    def _aliases_for(self, base: str) -> list[str]:
        names = [base]
        for alias, target in self.aliases.items():
            if normalize(target) == normalize(base) or normalize(alias) == normalize(base):
                names.extend([alias, target])
        return list(dict.fromkeys(names))

    def _grouped(self) -> dict[str, list[MediaEntry]]:
        groups: dict[str, list[MediaEntry]] = {}
        for entry in self.entries:
            groups.setdefault(normalize(entry.base), []).append(entry)
        return groups

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q = normalize(query)
        if not q:
            return []
        scored = []
        for key, group in self._grouped().items():
            base = group[0].base
            best = 0
            for name in self._aliases_for(base):
                n = normalize(name)
                if n == q:
                    best = max(best, 100)
                elif q in n:
                    best = max(best, 80 - (len(n) - len(q)))
                elif n and n in q:
                    best = max(best, 60 - (len(q) - len(n)))
                elif _loose_match(q, n):
                    best = max(best, 40)
            if best > 0:
                scored.append((best, base, group))
        scored.sort(key=lambda x: (-x[0], normalize(x[1])))
        out = []
        for score, base, group in scored[:limit]:
            seasons = {}
            year = None
            for e in group:
                year = year or e.year
                for sn, eps in e.seasons.items():
                    seasons[int(sn)] = seasons.get(int(sn), 0) + len(eps)
            out.append({"title": base, "year": year, "score": score,
                        "seasons": [{"season": s, "episodes": seasons[s]} for s in sorted(seasons)]})
        return out

    def resolve(self, title: str, season: int | None = None,
                episode: int | None = None) -> dict[str, Any] | None:
        q = normalize(title)
        groups = self._grouped()
        # exact base match first, then substring match
        keys = [k for k, g in groups.items() if any(normalize(n) == q for n in self._aliases_for(g[0].base))]
        if not keys:
            keys = [k for k, g in groups.items() if any(q and q in normalize(n) for n in self._aliases_for(g[0].base))]
        if not keys:
            return None
        group = groups[sorted(keys, key=len)[0]]
        base = group[0].base
        year = next((e.year for e in group if e.year), None)

        def seasons_of(entry):
            return {int(k): v for k, v in entry.seasons.items()}

        # Choose the physical folder that actually owns the requested season.
        chosen = None
        if season is not None:
            preferred = [e for e in group if e.season_hint == season]
            chosen = preferred[0] if preferred else next((e for e in group if season in seasons_of(e)), None)
        if chosen is None:
            chosen = next((e for e in group if e.season_hint in (1, None) and 1 in seasons_of(e)), None) \
                or next(iter(sorted(group, key=lambda e: (e.season_hint or 0))), None)
        if chosen is None:
            return None
        available = sorted(seasons_of(chosen))
        if not available:
            return None
        # Season key within the chosen folder: prefer hint, else the requested key.
        chosen_season = season if season in available else (chosen.season_hint if chosen.season_hint in available else available[0])
        episodes = chosen.seasons[str(chosen_season)]
        available_eps = sorted(int(e) for e in episodes)
        if not available_eps:
            return None
        chosen_ep = episode if episode in available_eps else available_eps[0]
        all_seasons = sorted({int(k) for e in group for k in e.seasons} | {e.season_hint for e in group if e.season_hint})
        return {"title": base, "year": year, "season": chosen_season,
                "episode": chosen_ep, "path": episodes[str(chosen_ep)],
                "folder": chosen.title, "seasons": all_seasons,
                "episode_range": [available_eps[0], available_eps[-1]],
                "requested_season_found": (season in all_seasons) if season is not None else None,
                "requested_episode_found": (episode in available_eps) if episode is not None else None}

    def stats(self) -> dict[str, Any]:
        return {"series": len({normalize(e.base) for e in self.entries}),
                "episodes": sum(e.episode_count() for e in self.entries),
                "folders": len(self.entries),
                "roots": [str(r) for r in self.roots],
                "scanned_at": self.scanned_at, "errors": self.errors}

    # ---------- 缓存 ----------
    def save(self, path: str | None = None) -> str:
        target = Path(path).expanduser() if path else self.cache_path
        if target is None:
            raise ValueError("未指定媒体索引缓存路径")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"version": 1, "scanned_at": self.scanned_at,
                   "roots": [str(r) for r in self.roots],
                   "entries": [e.to_dict() for e in self.entries]}
        temp = target.with_suffix(target.suffix + ".tmp")
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        temp.replace(target)
        return str(target)

    @classmethod
    def load(cls, path: str, aliases: dict[str, str] | None = None) -> "MediaIndex":
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        index = cls(data.get("roots", []), aliases=aliases, cache_path=path)
        index.scanned_at = data.get("scanned_at", 0.0)
        index.entries = [MediaEntry.from_dict(e) for e in data.get("entries", [])]
        return index


def _loose_match(q: str, n: str) -> bool:
    """Allow a repeated/omitted character, which is common in spoken titles."""
    def collapse(s):
        return re.sub(r"(.)\1+", r"\1", s)
    q2, n2 = collapse(q), collapse(n)
    # shorter side must be reasonably contained, not an unrelated long title
    short, long = (q2, n2) if len(q2) <= len(n2) else (n2, q2)
    if len(short) < 2 or len(long) - len(short) > 14:
        return False
    return short in long


def index_from_config(cfg: dict, *, rebuild_if_empty: bool = True):
    """Build/load the media index described by cfg['media']; None if disabled."""
    media = cfg.get("media", {}) or {}
    if not media.get("enabled", False):
        return None
    aliases: dict[str, str] = {}
    for app in (cfg.get("apps", {}) or {}).values():
        for alias, target in (app.get("title_aliases") or {}).items():
            aliases[alias] = target
    cache = media.get("cache") or ""
    index = None
    if cache:
        try:
            index = MediaIndex.load(cache, aliases=aliases)
        except (OSError, ValueError, KeyError):
            index = None
    # V4.4: a cached index must not outlive the library it describes, otherwise
    # "最近新增" answers with stale data forever. The scan is read-only and cheap
    # (~0.6 s for 35 works), so a bounded TTL is the safe default.
    try:
        refresh = float(media.get("refresh_seconds") or 900)
    except (TypeError, ValueError):
        refresh = 900.0
    stale = (index is not None and refresh > 0
             and time.time() - float(getattr(index, "scanned_at", 0) or 0) > refresh)
    if index is None or (rebuild_if_empty and not index.entries) or stale:
        index = MediaIndex(media.get("roots") or [], aliases=aliases, cache_path=cache or None).scan()
        if cache:
            try:
                index.save(cache)
            except OSError:
                pass
    return index
