"""Read-only media-library tools over the existing MediaIndex.

Every answer is derived from the scanned local filesystem index (plus optional
NFO metadata). Nothing here invents titles, genres, durations or counts, and
nothing here can execute a television command.

Kind detection deliberately does **not** depend on a directory literally named
"电影": NFO ``<movie>``/``<tvshow>`` wins, then explicit episode/season
structure, then a single unmarked video file. Anything else stays ``unknown``
instead of being reported as a series.
"""
from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .media_index import MediaIndex, VIDEO_EXT, index_from_config, normalize, parse_episode

KINDS = ('any', 'movie', 'series', 'unknown')
SORTS = ('title', 'year', 'episodes', 'recent')
MAX_LIMIT = 20
_NFO_MAX_BYTES = 1024 * 1024
_GENRE_HINTS = ('悬疑', '科幻', '喜剧', '爱情', '动作', '犯罪', '历史', '战争',
                '恐怖', '家庭', '动画', '纪录片', '奇幻', '武侠', '都市', '古装')


def _safe_id(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]


def _first_existing(paths):
    for candidate in paths:
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    return None


def _nfo_kind_and_metadata(directory: Path):
    """Return (kind, metadata) from movie.nfo/tvshow.nfo, never guessed."""
    for name, kind in (('movie.nfo', 'movie'), ('tvshow.nfo', 'series')):
        path = directory / name
        try:
            if not path.is_file() or path.stat().st_size > _NFO_MAX_BYTES:
                continue
            node = ET.fromstring(path.read_bytes())
        except (OSError, ValueError, ET.ParseError):
            continue
        declared = (node.findtext('type') or '').strip().casefold()
        if declared in ('movie', 'series'):
            kind = declared
        metadata = {
            'genres': [e.text.strip() for e in node.findall('genre') if e.text and e.text.strip()],
            'region': (node.findtext('country') or node.findtext('region') or '').strip() or None,
            'year': (node.findtext('year') or '').strip() or None,
            'runtime_minutes': (node.findtext('runtime') or '').strip() or None,
        }
        return kind, metadata
    return None, {}


def _structural_kind(entry, paths):
    """Infer kind from real structure; ``unknown`` when evidence is insufficient."""
    marked = 0
    for path in paths:
        _season, episode = parse_episode(path)
        if episode is not None:
            marked += 1
    if marked:
        return 'series'
    if len(entry.seasons) > 1:
        return 'series'
    if len(paths) == 1:
        return 'movie'
    return 'unknown'


class LibraryTools:
    """Bounded, read-only projections of the local media index."""

    def __init__(self, cfg, *, index=None, clock=time.monotonic):
        self.cfg = cfg
        self._index = index
        self.clock = clock
        self._items = None
        self._built_at = 0.0
        self._build_seconds = 0.0
        self._errors = []
        self._root_state = []

    # ---------- build ----------
    def _build(self):
        now = self.clock()
        if self._items is not None and now - self._built_at < 60:
            return
        started = self.clock()
        index = self._index or index_from_config(self.cfg)
        self._index = index
        items = []
        errors = list(getattr(index, 'errors', []) or []) if index is not None else []
        root_state = []
        if index is not None:
            for root in index.roots:
                exists = root.is_dir()
                readable = False
                if exists:
                    try:
                        next(root.iterdir(), None)
                        readable = True
                    except (PermissionError, OSError):
                        readable = False
                root_state.append({'root': str(root), 'exists': exists, 'readable': readable})
            for key, group in index._grouped().items():
                entry = group[0]
                paths = [p for item in group for season in item.seasons.values()
                         for p in season.values()]
                if not paths:
                    continue
                first = _first_existing(paths)
                if first is None:
                    continue
                directory = Path(first).parent
                kind, metadata = _nfo_kind_and_metadata(directory)
                if kind is None:
                    # tvshow.nfo may live one level above the Season folder.
                    kind, metadata = _nfo_kind_and_metadata(directory.parent)
                if kind is None:
                    kind = _structural_kind(entry, paths)
                season_counts = {}
                for item in group:
                    for season_no, episodes in item.seasons.items():
                        try:
                            number = int(season_no)
                        except (TypeError, ValueError):
                            continue
                        season_counts[number] = season_counts.get(number, 0) + len(episodes)
                episodes = sum(season_counts.values())
                mtime = 0.0
                for candidate in paths[:64]:
                    try:
                        mtime = max(mtime, Path(candidate).stat().st_mtime)
                    except OSError:
                        continue
                year = None
                raw_year = entry.year or metadata.get('year')
                if raw_year and str(raw_year).isdigit():
                    year = int(raw_year)
                genres = list(metadata.get('genres') or [])
                items.append({
                    'id': _safe_id(key),
                    'title': entry.base,
                    'year': year,
                    'kind': kind,
                    'genres': genres,
                    'region': metadata.get('region'),
                    'episodes': episodes,
                    'seasons': sorted(season_counts),
                    'episode_count_by_season': season_counts,
                    'runtime_minutes': metadata.get('runtime_minutes'),
                    'updated_at': mtime,
                    'season': None,
                    'episode': None,
                })
            items.sort(key=lambda item: normalize(item['title']))
        self._items = items
        self._errors = errors
        self._root_state = root_state
        self._built_at = now
        self._build_seconds = max(0.0, self.clock() - started)

    # ---------- helpers ----------
    def _project(self, item):
        """Only fields a caller may see; no filesystem paths leave this module."""
        return {key: item.get(key) for key in
                ('id', 'title', 'year', 'kind', 'genres', 'region', 'episodes',
                 'seasons', 'runtime_minutes', 'updated_at', 'season', 'episode')}

    @staticmethod
    def _match_kind(item, kind):
        return kind in (None, 'any') or item['kind'] == kind

    def _sort(self, rows, sort):
        if sort == 'year':
            return sorted(rows, key=lambda r: (r['year'] or 0, normalize(r['title'])), reverse=True)
        if sort == 'episodes':
            return sorted(rows, key=lambda r: (-(r['episodes'] or 0), normalize(r['title'])))
        if sort == 'recent':
            return sorted(rows, key=lambda r: (-(r['updated_at'] or 0), normalize(r['title'])))
        return sorted(rows, key=lambda r: normalize(r['title']))

    # ---------- tools ----------
    def summary(self):
        self._build()
        items = self._items
        kinds = {'movie': 0, 'series': 0, 'unknown': 0}
        for item in items:
            kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
        metadata = sum(1 for item in items if item['genres'] or item['region'])
        age = None
        if self._index is not None and getattr(self._index, 'scanned_at', 0):
            age = max(0.0, time.time() - self._index.scanned_at)
        # "available" means the mount is readable; an empty but mounted library
        # is still available and must not be reported as a broken index.
        has_mount = any(state['exists'] and state['readable'] for state in self._root_state)
        return {
            'available': has_mount,
            'total': len(items),
            'movies': kinds['movie'],
            'series': kinds['series'],
            'unknown_kind': kinds['unknown'],
            'episodes': sum(item['episodes'] for item in items),
            'with_metadata': metadata,
            'metadata_missing': len(items) - metadata,
            'ambiguous_folders': sum(1 for entry in (self._index.entries if self._index else [])
                                     if getattr(entry, 'unindexed', 0)),
            'ambiguous_videos': sum(getattr(entry, 'unindexed', 0)
                                    for entry in (self._index.entries if self._index else [])),
            'roots': [state['root'] for state in self._root_state],
            'index_age_seconds': age,
            'errors': list(self._errors),
        }

    def status(self):
        """Machine-readable catalog state: ok / empty / unavailable.

        Distinguishes "the library really has nothing" from "the index could not
        be read", which callers must not conflate.
        """
        summary = self.summary()
        if not summary['available']:
            state = 'unavailable'
        elif summary['total'] == 0:
            state = 'empty'
        else:
            state = 'ok'
        return {'state': state, 'source': 'local_catalog', 'read_only': True,
                'available': summary['available'], 'total': summary['total'],
                'movies': summary['movies'], 'series': summary['series'],
                'unknown_kind': summary['unknown_kind'], 'episodes': summary['episodes'],
                'metadata_missing': summary['metadata_missing'],
                'ambiguous_folders': summary['ambiguous_folders'],
                'roots': [{'root': r['root'], 'exists': r['exists'], 'readable': r['readable']}
                          for r in self._root_state],
                'errors': summary['errors']}

    def episodes(self, item_id, season=None):
        """Real season/episode numbers for one indexed work (no invention)."""
        if not isinstance(item_id, str) or not item_id:
            raise ValueError('item_id required')
        self._build()
        target = next((item for item in self._items if item['id'] == item_id), None)
        if target is None:
            raise ValueError('unknown catalog item')
        seasons = list(target['seasons'])
        if season is not None:
            if type(season) is not int:
                raise ValueError('season must be an integer')
            if season not in seasons:
                raise ValueError('season not present in this item')
        picked = [season] if season is not None else seasons
        detail = []
        for number in picked:
            count = target['episode_count_by_season'].get(number, 0)
            detail.append({'season': number, 'episode_count': count,
                           'first_episode': 1 if count else None,
                           'last_episode': count or None})
        return {'item': self._project(target), 'source': 'local_catalog',
                'seasons': seasons, 'episodes': detail, 'season': season}

    def health(self):
        self._build()
        summary = self.summary()
        return {
            'roots': list(self._root_state),
            'available': summary['available'],
            'entries': summary['total'],
            'index_age_seconds': summary['index_age_seconds'],
            'build_ms': round(self._build_seconds * 1000, 1),
            'kind_support': {'movie': summary['movies'], 'series': summary['series'],
                             'unknown': summary['unknown_kind']},
            'metadata_coverage': {'with_metadata': summary['with_metadata'],
                                  'missing': summary['metadata_missing']},
            'ambiguous': {'folders': summary['ambiguous_folders'],
                          'videos': summary['ambiguous_videos']},
            'errors': summary['errors'],
            'read_only': True,
        }

    def browse(self, kind='any', limit=10, sort='title'):
        if kind not in KINDS:
            raise ValueError('kind must be any/movie/series/unknown')
        if sort not in SORTS:
            raise ValueError('sort must be title/year/episodes/recent')
        limit = max(1, min(MAX_LIMIT, int(limit)))
        self._build()
        rows = [item for item in self._items if self._match_kind(item, kind)]
        rows = self._sort(rows, sort)
        return {'kind': kind, 'sort': sort, 'total': len(rows),
                'items': [self._project(item) for item in rows[:limit]]}

    def search(self, query=None, genre=None, year=None, kind='any', limit=10):
        if kind not in KINDS:
            raise ValueError('kind must be any/movie/series/unknown')
        limit = max(1, min(MAX_LIMIT, int(limit)))
        self._build()
        rows = [item for item in self._items if self._match_kind(item, kind)]
        if isinstance(year, str) and year.isdigit():
            year = int(year)
        if year is not None:
            if type(year) is not int:
                raise ValueError('year must be an integer')
            rows = [item for item in rows if item['year'] == year]
        if genre:
            wanted = normalize(genre)
            rows = [item for item in rows
                    if any(wanted and wanted in normalize(g) for g in item['genres'])]
        matched_by = 'filter'
        if query:
            scored = []
            key = normalize(query)
            for item in rows:
                name = normalize(item['title'])
                score = 0
                if name == key:
                    score = 100
                elif key and key in name:
                    score = 80
                elif name and name in key:
                    score = 60
                elif key and len(key) >= 2 and _loose(key, name):
                    score = 40
                if score:
                    scored.append((score, item))
            scored.sort(key=lambda pair: (-pair[0], normalize(pair[1]['title'])))
            rows = [item for _score, item in scored]
            matched_by = 'title'
        return {'query': query, 'genre': genre, 'year': year, 'kind': kind,
                'matched_by': matched_by, 'total': len(rows),
                'items': [self._project(item) for item in rows[:limit]]}

    def recommend(self, preferences=None, exclusions=None, limit=5, kind='any',
                  max_episodes=None):
        """Deterministic ranking over real items; never invents a title."""
        limit = max(1, min(MAX_LIMIT, int(limit)))
        self._build()
        prefs = [normalize(p) for p in (preferences or []) if isinstance(p, str) and p.strip()]
        banned = {str(x) for x in (exclusions or []) if isinstance(x, str) and x}
        banned_titles = {normalize(x) for x in banned if not re.fullmatch(r'[0-9a-f]{24}', x)}
        rows = [item for item in self._items
                if self._match_kind(item, kind)
                and item['id'] not in banned and normalize(item['title']) not in banned_titles]
        if max_episodes is not None:
            if type(max_episodes) is not int or not 1 <= max_episodes <= 10000:
                raise ValueError('max_episodes must be 1..10000')
            rows = [item for item in rows if (item['episodes'] or 0) <= max_episodes]
        scored = []
        for item in rows:
            score = 0.0
            reasons = []
            haystack = normalize(item['title']) + ' ' + ' '.join(normalize(g) for g in item['genres'])
            for pref in prefs:
                if pref and pref in haystack:
                    score += 3.0
                    reasons.append('符合“' + pref + '”')
            if not prefs:
                score += 1.0
                reasons.append('本地已入库')
            if item['kind'] == 'movie':
                score += 0.5
            if item['episodes']:
                score += min(1.0, item['episodes'] / 200.0)
            scored.append((score, item, reasons))
        scored.sort(key=lambda triple: (-triple[0], -(triple[1]['updated_at'] or 0),
                                        normalize(triple[1]['title'])))
        return {'preferences': preferences or [], 'kind': kind,
                'max_episodes': max_episodes, 'excluded': len(banned),
                'total': len(rows),
                'items': [dict(self._project(item), reasons=reasons)
                          for _score, item, reasons in scored[:limit]]}

    def recent(self, limit=5, kind='any'):
        limit = max(1, min(MAX_LIMIT, int(limit)))
        self._build()
        rows = [item for item in self._items
                if self._match_kind(item, kind) and item['updated_at']]
        rows = self._sort(rows, 'recent')
        return {'kind': kind, 'total': len(rows),
                'items': [self._project(item) for item in rows[:limit]]}

    def find(self, title):
        """Exact normalized lookup used before any playback proposal."""
        if not isinstance(title, str) or not title.strip():
            return None
        self._build()
        key = normalize(title)
        exact = [item for item in self._items if normalize(item['title']) == key]
        if len(exact) == 1:
            return self._project(exact[0])
        return None

    def item(self, item_id):
        self._build()
        for item in self._items:
            if item['id'] == item_id:
                return self._project(item)
        return None


# Unified catalog naming (V4.3). The older ``library_*`` names stay as thin
# aliases so existing callers and tests keep working against one implementation.
LibraryTools.list_items = LibraryTools.browse
LibraryTools.catalog_status = LibraryTools.status
LibraryTools.catalog_summary = LibraryTools.summary
LibraryTools.catalog_list = LibraryTools.browse
LibraryTools.catalog_search = LibraryTools.search
LibraryTools.catalog_recommend = LibraryTools.recommend
LibraryTools.catalog_recent = LibraryTools.recent
LibraryTools.catalog_health = LibraryTools.health
LibraryTools.catalog_episodes = LibraryTools.episodes
LibraryTools.catalog_get_episodes = LibraryTools.episodes


def _loose(key, name):
    def collapse(text):
        return re.sub(r'(.)\1+', r'\1', text)
    short, long = collapse(key), collapse(name)
    if len(short) > len(long):
        short, long = long, short
    if len(short) < 2 or len(long) - len(short) > 14:
        return False
    return short in long
