"""Read-only media-library tools over the existing MediaIndex.

Every answer is derived from the scanned local filesystem index (plus optional
NFO metadata). Nothing here invents titles, genres, durations or counts, and
nothing here can execute a television command.

Kind detection is evidence-driven and never title-driven (V4.4):

1. a ``movie.nfo`` or ``<type>movie</type>`` declares a movie;
2. a ``<studio>`` mentioning 短剧, a ``<uniqueid type="hongguo">`` or a
   ``/短剧/`` path segment declares a short drama;
3. real ``SxxExx`` episode numbers declare a series;
4. a single unmarked video file is a movie;
5. anything else stays ``unknown`` instead of being reported as a series.

Metadata comes from real NFO fields only (``plot``/``genre``/``tag``/``studio``/
``uniqueid``/``runtime``/``year``/``country``). Missing metadata is reported as
missing, never filled in.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from .media_index import (KIND_LABELS, NfoCache, classify_kind, index_from_config,
                          nfo_evidence, normalize)

KINDS = ('any', 'movie', 'series', 'short_drama', 'unknown')
SORTS = ('title', 'year', 'episodes', 'recent')
MAX_LIMIT = 20
PLOT_LIMIT = 300
SHORT_EPISODES = 24
RECENT_DAYS = 7
RECENT_DAYS_WIDE = 30
_ITEM_ID = re.compile(r'[0-9a-f]{24}')

# The only fields a caller may ever see. Paths are deliberately absent.
_PROJECT_FIELDS = ('id', 'title', 'year', 'kind', 'kind_label', 'genres', 'studio',
                   'plot', 'region', 'episodes', 'seasons', 'runtime_minutes',
                   'updated_at', 'added_days_ago', 'season', 'episode')


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


def _episode_paths(group):
    return [path for entry in group for season in entry.seasons.values()
            for path in season.values()]


def _days_ago(mtime, now):
    if not mtime:
        return None
    return round(max(0.0, (now - mtime) / 86400.0), 2)


def _wanted_genres(values):
    return [normalize(value) for value in (values or [])
            if isinstance(value, str) and value.strip()]


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
        # NFO parses survive the 60 s rebuild, keyed by directory signature.
        self._nfo_cache = NfoCache()
        self._seen = set()

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
        wall = time.time()
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
                paths = _episode_paths(group)
                if not paths:
                    continue
                first = _first_existing(paths)
                if first is None:
                    continue
                evidence = nfo_evidence(Path(first).parent, self._nfo_cache)
                kind = classify_kind(entry, paths, evidence)
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
                raw_year = entry.year or evidence.get('year')
                if raw_year and str(raw_year).isdigit():
                    year = int(raw_year)
                items.append({
                    'id': _safe_id(key),
                    'title': entry.base,
                    'year': year,
                    'kind': kind,
                    'kind_label': KIND_LABELS.get(kind, KIND_LABELS['unknown']),
                    'genres': list(evidence.get('genres') or []),
                    'studio': evidence.get('studio'),
                    'plot': evidence.get('plot'),
                    'region': evidence.get('region'),
                    'episodes': episodes,
                    'seasons': sorted(season_counts),
                    'episode_count_by_season': season_counts,
                    'runtime_minutes': evidence.get('runtime'),
                    'updated_at': mtime,
                    'added_days_ago': _days_ago(mtime, wall),
                    # Metadata means a real NFO field; a year parsed out of the
                    # folder name is structure, not metadata.
                    'has_metadata': bool(evidence.get('plot') or evidence.get('genres')
                                         or evidence.get('studio') or evidence.get('year')),
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
        projected = {key: item.get(key) for key in _PROJECT_FIELDS}
        projected['genres'] = list(item.get('genres') or [])
        plot = item.get('plot')
        projected['plot'] = plot[:PLOT_LIMIT] if isinstance(plot, str) and plot else None
        return projected

    @staticmethod
    def _match_kind(item, kind):
        return kind in (None, 'any') or item['kind'] == kind

    @staticmethod
    def _filter_rows(rows, max_episodes=None, exclude_genres=None):
        if max_episodes is not None:
            if type(max_episodes) is not int or not 1 <= max_episodes <= 10000:
                raise ValueError('max_episodes must be 1..10000')
            rows = [item for item in rows if (item['episodes'] or 0) <= max_episodes]
        excluded = _wanted_genres(exclude_genres)
        if excluded:
            rows = [item for item in rows
                    if not any(want and want in normalize(genre)
                               for want in excluded for genre in item['genres'])]
        return rows

    def _sort(self, rows, sort):
        if sort == 'year':
            return sorted(rows, key=lambda r: (r['year'] or 0, normalize(r['title'])), reverse=True)
        if sort == 'episodes':
            return sorted(rows, key=lambda r: (-(r['episodes'] or 0), normalize(r['title'])))
        if sort == 'recent':
            return sorted(rows, key=lambda r: (-(r['updated_at'] or 0), normalize(r['title'])))
        return sorted(rows, key=lambda r: normalize(r['title']))

    @staticmethod
    def _state(summary):
        if not summary['available']:
            return 'unavailable'
        if summary['total'] == 0:
            return 'empty'
        return 'ok'

    def note_seen(self, item_id):
        """Remember an already-watched work for this session (id or title)."""
        if isinstance(item_id, str) and item_id.strip():
            self._seen.add(item_id.strip())
        return sorted(self._seen)

    # ---------- tools ----------
    def summary(self):
        self._build()
        items = self._items
        kinds = {'movie': 0, 'series': 0, 'short_drama': 0, 'unknown': 0}
        for item in items:
            kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
        metadata = sum(1 for item in items if item['has_metadata'])
        recent = [item for item in items if item['added_days_ago'] is not None]
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
            # V4.4 additions
            'short_dramas': kinds['short_drama'],
            'seasons': sum(len(item['seasons']) for item in items),
            'with_plot': sum(1 for item in items if item['plot']),
            # "有元数据" (plot/studio/year) is not "有题材标签": genre filtering
            # matches ``item['genres']``, so genre coverage must count that field.
            'with_genres': sum(1 for item in items if item['genres']),
            'recent_added_7d': sum(1 for item in recent if item['added_days_ago'] <= RECENT_DAYS),
            'recent_added_30d': sum(1 for item in recent
                                    if item['added_days_ago'] <= RECENT_DAYS_WIDE),
        }

    def overview(self):
        """Stable, path-free view for natural-language catalogue answers."""
        summary = self.summary()
        rows = self._sort([item for item in self._items if item['updated_at']], 'recent')
        return {
            'state': self._state(summary),
            'source': 'local_catalog',
            'read_only': True,
            'available': summary['available'],
            'total': summary['total'],
            'movies': summary['movies'],
            'series': summary['series'],
            'short_dramas': summary['short_dramas'],
            'unknown_kind': summary['unknown_kind'],
            'episodes': summary['episodes'],
            'seasons': summary['seasons'],
            'with_metadata': summary['with_metadata'],
            'metadata_missing': summary['metadata_missing'],
            'with_plot': summary['with_plot'],
            'with_genres': summary['with_genres'],
            'recent_added_7d': summary['recent_added_7d'],
            'recent_added_30d': summary['recent_added_30d'],
            'index_age_seconds': summary['index_age_seconds'],
            'roots_available': sum(1 for state in self._root_state
                                   if state['exists'] and state['readable']),
            'recent': [self._project(item) for item in rows[:5]],
            'errors': list(summary['errors']),
        }

    def status(self):
        """Machine-readable catalog state: ok / empty / unavailable.

        Distinguishes "the library really has nothing" from "the index could not
        be read", which callers must not conflate.
        """
        summary = self.summary()
        return {'state': self._state(summary), 'source': 'local_catalog', 'read_only': True,
                'available': summary['available'], 'total': summary['total'],
                'movies': summary['movies'], 'series': summary['series'],
                'short_dramas': summary['short_dramas'],
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
            'nfo_parses': self._nfo_cache.parses,
            'kind_support': {'movie': summary['movies'], 'series': summary['series'],
                             'short_drama': summary['short_dramas'],
                             'unknown': summary['unknown_kind']},
            'metadata_coverage': {'with_metadata': summary['with_metadata'],
                                  'with_plot': summary['with_plot'],
                                  'missing': summary['metadata_missing']},
            'ambiguous': {'folders': summary['ambiguous_folders'],
                          'videos': summary['ambiguous_videos']},
            'errors': summary['errors'],
            'read_only': True,
        }

    def browse(self, kind='any', limit=10, sort='title', max_episodes=None,
               exclude_genres=None):
        if kind not in KINDS:
            raise ValueError('kind must be any/movie/series/short_drama/unknown')
        if sort not in SORTS:
            raise ValueError('sort must be title/year/episodes/recent')
        limit = max(1, min(MAX_LIMIT, int(limit)))
        self._build()
        rows = [item for item in self._items if self._match_kind(item, kind)]
        rows = self._filter_rows(rows, max_episodes, exclude_genres)
        rows = self._sort(rows, sort)
        return {'kind': kind, 'sort': sort, 'total': len(rows),
                'max_episodes': max_episodes,
                'exclude_genres': list(exclude_genres or []),
                'items': [self._project(item) for item in rows[:limit]]}

    def search(self, query=None, genre=None, year=None, kind='any', limit=10,
               max_episodes=None, exclude_genres=None):
        if kind not in KINDS:
            raise ValueError('kind must be any/movie/series/short_drama/unknown')
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
        rows = self._filter_rows(rows, max_episodes, exclude_genres)
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
                  max_episodes=None, exclude_genres=None, include_seen=False, seen=None):
        """Deterministic ranking over real items; never invents a title or genre.

        ``reasons`` may only cite observed fields: real genres, real episode
        counts, real file mtimes and an explicit seen-list. With no metadata the
        reasons stay structural ("近期新增"/"集数较短"/"未观看") instead of
        guessing a genre.
        """
        limit = max(1, min(MAX_LIMIT, int(limit)))
        if kind not in KINDS:
            raise ValueError('kind must be any/movie/series/short_drama/unknown')
        self._build()
        prefs = [p.strip() for p in (preferences or []) if isinstance(p, str) and p.strip()]
        prefs_norm = [normalize(p) for p in prefs]
        banned = {str(x) for x in (exclusions or []) if isinstance(x, str) and x}
        banned_titles = {normalize(x) for x in banned if not _ITEM_ID.fullmatch(x)}
        seen_ids = {str(x) for x in (seen or []) if isinstance(x, str) and x} | set(self._seen)
        seen_titles = {normalize(x) for x in seen_ids if not _ITEM_ID.fullmatch(x)}
        rows = [item for item in self._items
                if self._match_kind(item, kind)
                and item['id'] not in banned and normalize(item['title']) not in banned_titles]
        if not include_seen:
            rows = [item for item in rows if item['id'] not in seen_ids
                    and normalize(item['title']) not in seen_titles]
        rows = self._filter_rows(rows, max_episodes, exclude_genres)
        scored = []
        for item in rows:
            score = 0.0
            reasons = []
            haystack = normalize(item['title']) + ' ' + ' '.join(
                normalize(g) for g in item['genres'])
            for pref, pref_norm in zip(prefs, prefs_norm):
                if pref_norm and pref_norm in haystack:
                    score += 3.0
                    reasons.append('符合偏好：' + pref)
            if not prefs:
                score += 1.0
                reasons.append('本地已入库')
            if item['genres']:
                score += min(1.0, 0.2 * len(item['genres']))
                reasons.append('类型：' + '、'.join(item['genres']))
            if item['studio']:
                reasons.append('出品：' + item['studio'])
            if item['episodes']:
                score += min(1.0, item['episodes'] / 200.0)
                if item['episodes'] <= SHORT_EPISODES:
                    reasons.append(f'共 {item["episodes"]} 集，集数较短')
                else:
                    reasons.append(f'共 {item["episodes"]} 集')
            if item['kind'] == 'movie':
                score += 0.5
                reasons.append('电影')
            days = item['added_days_ago']
            if days is not None and days <= RECENT_DAYS:
                reasons.append('7 天内新增')
            elif days is not None and days <= RECENT_DAYS_WIDE:
                reasons.append('30 天内新增')
            if seen_ids:
                if item['id'] in seen_ids or normalize(item['title']) in seen_titles:
                    reasons.append('已看过')
                else:
                    reasons.append('未观看')
            scored.append((score, item, reasons))
        scored.sort(key=lambda triple: (-triple[0], -(triple[1]['updated_at'] or 0),
                                        normalize(triple[1]['title'])))
        return {'preferences': preferences or [], 'kind': kind,
                'max_episodes': max_episodes, 'exclude_genres': list(exclude_genres or []),
                'include_seen': include_seen, 'excluded': len(banned),
                'total': len(rows),
                'items': [dict(self._project(item), reasons=reasons)
                          for _score, item, reasons in scored[:limit]]}

    def all_items(self, limit=None):
        """Every indexed work as a projection (uncapped internal accessor).

        ``browse`` caps its page at ``MAX_LIMIT`` for the model surface; the
        voice catalog needs the whole library for search/resolve, and must use
        exactly the same ids and kinds as the tool surface.
        """
        self._build()
        rows = [self._project(item) for item in self._items]
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        return {'total': len(self._items), 'items': rows}

    def recent(self, limit=5, kind='any'):
        if kind not in KINDS:
            raise ValueError('kind must be any/movie/series/short_drama/unknown')
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
        """Full path-free detail for one work (None when the id is unknown)."""
        self._build()
        for item in self._items:
            if item['id'] == item_id:
                detail = self._project(item)
                detail['has_metadata'] = item['has_metadata']
                detail['episode_count_by_season'] = dict(item['episode_count_by_season'])
                return detail
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
LibraryTools.catalog_overview = LibraryTools.overview
LibraryTools.catalog_detail = LibraryTools.item


def _loose(key, name):
    def collapse(text):
        return re.sub(r'(.)\1+', r'\1', text)
    short, long = collapse(key), collapse(name)
    if len(short) > len(long):
        short, long = long, short
    if len(short) < 2 or len(long) - len(short) > 14:
        return False
    return short in long
