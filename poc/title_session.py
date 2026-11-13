#!/usr/bin/env python3
"""Work-level title resolution with explicit season/episode confirmation.

Reuses the real MediaIndex and RapidFuzz. A work is a normalized title/year pair;
its seasons never consume separate work-candidate slots. Original ASR text is
returned unchanged, and no method executes television commands.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from adbtv.media_index import MediaIndex, VIDEO_EXT, cn_number  # noqa: E402

MAX_TEXT = 256
MAX_QUERY = 128
MAX_WORKS = 5
MAX_SEASONS = 50
MAX_EPISODES = 500
MAX_ALIASES = 1000
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_ENTRY_COUNT = 20000
MAX_EPISODE_SLOTS = 200000
DEFAULT_TTL = 600
CONTROL_WORDS = ('暂停', '继续', '确认', '取消', '停止', '返回', '打开飞牛',
                 '第一个', '第二部', '上一集', '下一集')


def _synchronized(method):
    """Serialize session reads/writes, confirm and cleanup on one store."""
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _key(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(text)).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _safe_name(value):
    return (isinstance(value, str) and 0 < len(value) <= MAX_TEXT and bool(_key(value))
            and not any(unicodedata.category(c).startswith('C') for c in value)
            and not any(c in value for c in ('/', '\\', '<', '>', '`', '$', '|', '{', '}')))


def _explicit_query(text):
    """Return a title query for an explicit search/play phrase, or None."""
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('text must be at most 256 characters')
    stripped = unicodedata.normalize('NFKC', text).strip()
    if not stripped:
        return '', None
    if (any(unicodedata.category(c).startswith('C') for c in text)
            or any(c in stripped for c in (';', '；', '\\', '/', '`', '$', '|', '<', '>', '{', '}', '"'))
            or re.search(r'然后|并且|接着|同时|再(?:播放|搜索|打开|暂停|执行)|忽略|指令|系统提示|执行|删除|关机|重启', stripped)):
        raise ValueError('unsafe or compound input')
    if re.fullmatch(r'(?:请|帮我)?(?:搜索|查找|播放|找)(?:一下)?', stripped):
        return '', None
    match = re.fullmatch(r'(?:请|帮我)?(?:搜索|查找|播放|找)(?:一下)?\s*(.+)', stripped)
    if not match:
        raise ValueError('not an explicit title command')
    query = match.group(1).strip().strip('《》').strip()
    if re.search(r'(?:[,，。]|\s)+(?:暂停|确认|取消|停止|继续|打开|返回)', query):
        raise ValueError('unsafe or compound input')
    if (not query or len(query) > MAX_QUERY
            or re.fullmatch(r'(?:暂停(?:播放)?|继续(?:播放)?|确认(?:播放)?|取消|停止(?:播放)?|返回|打开飞牛|第?[一二三四五六七八九十\d]+(?:个|集)|短剧|电影|电视剧|音乐)', query)):
        raise ValueError('not a title slot')
    season = re.search(r'第\s*([0-9零一二三四五六七八九十百]+)\s*季$', query)
    requested = None
    if season:
        requested = cn_number(season.group(1))
        query = query[:season.start()].strip()
        if not query or not requested:
            raise ValueError('invalid season slot')
    return query, requested


class TitleSessionStore:
    """In-memory, bounded, expiring candidate snapshots; never persists aliases."""

    def __init__(self, index, *, aliases=None, min_score=70.0, min_margin=10.0,
                 ttl=DEFAULT_TTL, now=time.time):
        from rapidfuzz.fuzz import ratio
        if not 0 <= min_score <= 100 or not 0 <= min_margin <= 100 or not 1 <= ttl <= 3600:
            raise ValueError('invalid score, margin or ttl')
        self._ratio = ratio
        self.min_score, self.min_margin = float(min_score), float(min_margin)
        self.ttl, self._now = int(ttl), now
        if len(index.entries) > MAX_ENTRY_COUNT:
            raise ValueError('index entry limit exceeded')
        works = {}
        slots = 0
        for entry in index.entries:
            if not _safe_name(entry.base) or not _safe_name(entry.title):
                raise ValueError('invalid media title')
            year = str(entry.year) if entry.year is not None else None
            if year is not None and not re.fullmatch(r'\d{4}', year):
                raise ValueError('invalid media year')
            title_key = _key(entry.base)
            work_key = (title_key, year)
            work = works.setdefault(work_key, {'title': entry.base, 'year': year, 'seasons': {}})
            # Multiple index entries can describe the same work; merge seasons.
            for season, episodes in entry.seasons.items():
                if not str(season).isdigit() or not 1 <= int(season) <= 999 or not isinstance(episodes, dict):
                    raise ValueError('invalid media season')
                # MediaEntry.seasons is keyed by a folder-local season index; the
                # show's own season_hint distinguishes e.g. Season 01..11.
                effective_season = entry.season_hint if entry.season_hint is not None else int(season)
                if not 1 <= effective_season <= 999:
                    raise ValueError('invalid media season hint')
                slots += len(episodes)
                if slots > MAX_EPISODE_SLOTS:
                    raise ValueError('index episode limit exceeded')
                bucket = work['seasons'].setdefault(effective_season, {})
                for episode, raw_path in episodes.items():
                    if (not str(episode).isdigit() or not 1 <= int(episode) <= 99999
                            or not isinstance(raw_path, str) or len(raw_path) > 4096):
                        raise ValueError('invalid media episode')
                    path = Path(raw_path).expanduser()
                    if path.suffix.lower() not in VIDEO_EXT or not path.is_file():
                        continue
                    info = path.stat()
                    bucket[int(episode)] = (str(path.resolve()), info.st_size, info.st_mtime_ns)
        self._works = []
        for (title_key, year), work in sorted(works.items(), key=lambda item: (item[0][0], item[0][1] or '')):
            seasons = {s: e for s, e in work['seasons'].items() if e}
            if not seasons:
                continue
            work_id = 'work-' + _digest([title_key, year])[:24]
            seasons_view = []
            for season in sorted(seasons):
                episodes = sorted(seasons[season])
                seasons_view.append({'season': season, 'episode_count': len(episodes),
                                     'first_episode': episodes[0], 'last_episode': episodes[-1]})
            self._works.append({'work_id': work_id, 'title': work['title'], 'year': year,
                                'title_key': title_key, 'seasons': seasons,
                                'season_view': seasons_view,
                                'episode_count': sum(len(v) for v in seasons.values())})
        self._by_id = {work['work_id']: work for work in self._works}
        raw_aliases = aliases if aliases is not None else index.aliases
        if not isinstance(raw_aliases, dict) or len(raw_aliases) > MAX_ALIASES:
            raise ValueError('invalid aliases')
        self._aliases = {}
        for alias, target in raw_aliases.items():
            if not _safe_name(alias) or len(alias) > MAX_QUERY or not _safe_name(target):
                raise ValueError('invalid alias')
            matches = [w for w in self._works if target == w['work_id'] or _key(target) == w['title_key']]
            if len(matches) != 1:
                raise ValueError('alias target must identify exactly one existing work')
            if any(w['title_key'] == _key(alias) and w is not matches[0] for w in self._works):
                raise ValueError('alias shadows another indexed title')
            self._aliases[_key(alias)] = (matches[0]['work_id'], alias)
        self._lock = threading.RLock()
        self._sessions = {}
        self._cleanup_stop = None
        self._cleanup_thread = None
        self.index_fingerprint = _digest([
            (w['work_id'], w['title'], w['year'],
             [(s, sorted((ep, v[1], v[2]) for ep, v in episodes.items()))
              for s, episodes in sorted(w['seasons'].items())])
            for w in self._works] + [sorted(self._aliases)])

    @classmethod
    def from_cache(cls, path, *, path_map=None, aliases=None, **kwargs):
        cache = Path(path).expanduser()
        with cache.open('rb') as stream:
            payload = stream.read(MAX_CACHE_BYTES + 1)
        if len(payload) > MAX_CACHE_BYTES:
            raise ValueError('index cache exceeds size limit')
        data = json.loads(payload)
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('entries'), list)
                or len(data['entries']) > MAX_ENTRY_COUNT):
            raise ValueError('invalid MediaIndex cache')
        index = MediaIndex.load(str(cache))
        if path_map:
            if not isinstance(path_map, dict) or len(path_map) > 8:
                raise ValueError('invalid path map')
            for entry in index.entries:
                for season in entry.seasons.values():
                    for episode, path in list(season.items()):
                        for source, target in path_map.items():
                            if isinstance(path, str) and path.startswith(source.rstrip('/') + '/'):
                                season[episode] = target.rstrip('/') + path[len(source.rstrip('/')):]
                                break
        return cls(index, aliases=aliases, **kwargs)

    @classmethod
    def from_roots(cls, roots, **kwargs):
        if (not isinstance(roots, (list, tuple)) or not 1 <= len(roots) <= 8
                or any(not Path(root).expanduser().is_dir() for root in roots)):
            raise ValueError('expected one to eight existing media roots')
        index = MediaIndex([str(r) for r in roots], cache_path=None).scan(max_depth=4)
        if index.errors:
            raise ValueError('media scan incomplete')
        return cls(index, **kwargs)

    def _live(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError('unknown session')
        if self._now() > session['expires_at']:
            self._sessions.pop(session_id, None)
            raise ValueError('candidate snapshot expired')
        return session

    def _touch(self, session):
        session['expires_at'] = self._now() + self.ttl

    @_synchronized
    def cleanup(self):
        before = len(self._sessions)
        self._sessions = {k: v for k, v in self._sessions.items()
                          if self._now() <= v['expires_at']}
        return before - len(self._sessions)

    def start_cleanup(self, interval=60):
        """Start one bounded daemon sweep; idempotent and explicitly stoppable."""
        if not isinstance(interval, (int, float)) or not 0.01 <= interval <= 3600:
            raise ValueError('cleanup interval must be 0.01..3600 seconds')
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return self._cleanup_thread
        stop = threading.Event()

        def sweep():
            while not stop.wait(interval):
                self.cleanup()

        self._cleanup_stop = stop
        self._cleanup_thread = threading.Thread(target=sweep, daemon=True,
                                                name='title-session-cleanup')
        self._cleanup_thread.start()
        return self._cleanup_thread

    def stop_cleanup(self):
        if self._cleanup_stop is not None:
            self._cleanup_stop.set()
        thread = self._cleanup_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._cleanup_stop = None
        self._cleanup_thread = None

    def _present(self, work, score, alias_used=None):
        return {'work_id': work['work_id'], 'title': work['title'], 'year': work['year'],
                'season_count': len(work['season_view']), 'episode_count': work['episode_count'],
                'seasons': [dict(s) for s in work['season_view']],
                'score': round(score, 4), 'alias_used': alias_used,
                'requires_confirmation': True}

    @_synchronized
    def suggest(self, session_id, text, *, hypothesis=None, current=None):
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 96:
            raise ValueError('invalid session id')
        try:
            query, requested_season = _explicit_query(text)
        except ValueError as exc:
            self._sessions[session_id] = {'created_at': self._now(), 'expires_at': self._now() + self.ttl,
                                          'hypothesis': text if isinstance(text, str) else None,
                                          'works': [], 'selected': None, 'confirmed': False}
            return {'session_id': session_id, 'status': 'rejected', 'reason': str(exc),
                    'hypothesis': text, 'query': None, 'works': [],
                    'requires_confirmation': True, 'index_fingerprint': self.index_fingerprint,
                    'expires_at': self._sessions[session_id]['expires_at']}
        if not query:
            result = {'session_id': session_id, 'status': 'needs_clarification',
                      'reason': 'empty_input', 'hypothesis': text, 'query': None, 'works': [],
                      'requires_confirmation': True, 'index_fingerprint': self.index_fingerprint}
            self._sessions[session_id] = {'created_at': self._now(), 'expires_at': self._now() + self.ttl,
                                          'hypothesis': text, 'works': [], 'selected': None,
                                          'confirmed': False}
            result['expires_at'] = self._sessions[session_id]['expires_at']
            return result
        query_key = _key(query)
        scored = []
        for work in self._works:
            if requested_season is not None and requested_season not in work['seasons']:
                continue
            alias_used = None
            score = self._ratio(query_key, work['title_key'])
            for alias_key, (work_id, alias) in self._aliases.items():
                if work_id == work['work_id']:
                    value = self._ratio(query_key, alias_key)
                    if value > score:
                        score, alias_used = value, alias
            if current and isinstance(current, dict):
                current_title = _key(current.get('title') or '')
                if current_title and (current_title == work['title_key']
                                      or current_title in work['title_key']
                                      or work['title_key'] in current_title):
                    score = min(100.0, score + 5.0)
                    # A partial title spoken while that work is playing should
                    # favour the current work over a longer similarly named show.
                    if query_key and current_title.startswith(query_key):
                        score = max(score, 95.0)
            if score >= 50:
                scored.append((score, work, alias_used))
        scored.sort(key=lambda item: (-item[0], item[1]['title_key'], item[1]['year'] or '', item[1]['work_id']))
        works = [self._present(work, score, alias) for score, work, alias in scored[:MAX_WORKS]]
        session = {'created_at': self._now(), 'expires_at': self._now() + self.ttl,
                   'hypothesis': hypothesis if isinstance(hypothesis, str) else text,
                   'query': query, 'requested_season': requested_season,
                   'works': [w['work_id'] for w in works], 'selected': None, 'confirmed': False}
        self._sessions[session_id] = session
        result = {'session_id': session_id, 'status': 'needs_clarification', 'reason': 'no_indexed_match',
                  'hypothesis': session['hypothesis'], 'query': query, 'works': works,
                  'requires_confirmation': True, 'index_fingerprint': self.index_fingerprint,
                  'expires_at': session['expires_at']}
        if not works:
            return result
        best, second = scored[0], scored[1] if len(scored) > 1 else None
        if best[0] < self.min_score:
            result['reason'] = 'low_score'
        elif second and best[1]['title_key'] == second[1]['title_key'] and best[1]['year'] != second[1]['year']:
            result['status'], result['reason'] = 'needs_clarification', 'ambiguous_remakes'
        elif second and best[0] - second[0] < self.min_margin:
            result['status'], result['reason'] = 'needs_clarification', 'small_score_margin'
        elif len(works) == 1 and requested_season is not None:
            result['status'], result['reason'] = 'suggested', 'candidate_requires_confirmation'
        else:
            result['status'], result['reason'] = 'suggested', 'candidate_requires_confirmation'
        return result

    @_synchronized
    def seasons(self, session_id, work_id):
        session = self._live(session_id)
        if work_id not in session['works'] or work_id not in self._by_id:
            raise ValueError('work is not part of this candidate snapshot')
        work = self._by_id[work_id]
        self._touch(session)
        return {'session_id': session_id, 'status': 'needs_season', 'work': self._present(work, 100),
                'seasons': [dict(s) for s in work['season_view']],
                'requires_confirmation': True, 'expires_at': session['expires_at']}

    @_synchronized
    def episodes(self, session_id, work_id, season):
        session = self._live(session_id)
        if work_id not in session['works'] or work_id not in self._by_id:
            raise ValueError('work is not part of this candidate snapshot')
        if type(season) is not int or season not in self._by_id[work_id]['seasons']:
            raise ValueError('season does not exist')
        episodes = sorted(self._by_id[work_id]['seasons'][season])
        self._touch(session)
        return {'session_id': session_id, 'status': 'needs_episode',
                'work_id': work_id, 'season': season,
                'episodes': episodes[:MAX_EPISODES], 'episode_count': len(episodes),
                'truncated': len(episodes) > MAX_EPISODES, 'requires_confirmation': True,
                'expires_at': session['expires_at']}

    @_synchronized
    def select(self, session_id, work_id, season, episode):
        session = self._live(session_id)
        if work_id not in session['works'] or work_id not in self._by_id:
            raise ValueError('work is not part of this candidate snapshot')
        if type(season) is not int or type(episode) is not int:
            raise ValueError('season and episode must be integers')
        if episode not in self._by_id[work_id]['seasons'].get(season, {}):
            raise ValueError('episode does not exist')
        work = self._by_id[work_id]
        path, size, mtime = work['seasons'][season][episode]
        self._touch(session)
        session['selected'] = {'work_id': work_id, 'title': work['title'], 'year': work['year'],
                               'season': season, 'episode': episode}
        session['selected_media'] = {'path': path, 'size_bytes': size, 'mtime_ns': mtime}
        session['confirmed'] = False
        return {'session_id': session_id, 'status': 'needs_confirmation',
                'selection': dict(session['selected']),
                'command': f'播放《{work["title"]}》第{season}季第{episode}集',
                'requires_confirmation': True, 'expires_at': session['expires_at']}

    @_synchronized
    def confirm(self, session_id, *, confirm=False):
        if confirm is not True:
            raise ValueError('explicit confirm:true is required')
        session = self._live(session_id)
        if session.get('confirmed'):
            raise ValueError('candidate snapshot was already confirmed')
        selected = session.get('selected')
        if not selected:
            raise ValueError('select a work, season and episode first')
        self._revalidate_selected(session, selected)
        self._touch(session)
        session['confirmed'] = True
        return {'session_id': session_id, 'status': 'confirmed',
                'selection': dict(selected),
                'command': f'播放《{selected["title"]}》第{selected["season"]}季第{selected["episode"]}集',
                'requires_confirmation': True, 'confirmed': True,
                'expires_at': session['expires_at']}

    def _revalidate_selected(self, session, selected):
        """Require the chosen media file and snapshot to be unchanged."""
        work = self._by_id.get(selected.get('work_id'))
        media = session.get('selected_media')
        if (work is None or selected.get('work_id') not in session.get('works', [])
                or not isinstance(media, dict)):
            session['selected'] = None
            session['selected_media'] = None
            raise ValueError('candidate state changed; reselect work, season and episode')
        season, episode = selected.get('season'), selected.get('episode')
        current = work.get('seasons', {}).get(season, {}).get(episode)
        if current is None:
            session['selected'] = None
            session['selected_media'] = None
            raise ValueError('selected episode no longer exists; reselect')
        path, size, mtime = current
        if (path != media.get('path') or size != media.get('size_bytes')
                or mtime != media.get('mtime_ns')):
            session['selected'] = None
            session['selected_media'] = None
            raise ValueError('media changed since selection; reselect')
        target = Path(path)
        if not target.is_file() or target.is_symlink():
            session['selected'] = None
            session['selected_media'] = None
            raise ValueError('media file missing or replaced; reselect')
        info = target.stat()
        if info.st_size != size or info.st_mtime_ns != mtime:
            session['selected'] = None
            session['selected_media'] = None
            raise ValueError('media file changed since selection; reselect')

    @_synchronized
    def cancel(self, session_id):
        session = self._sessions.pop(session_id, None)
        return {'session_id': session_id, 'status': 'cancelled',
                'had_pending': bool(session and session.get('selected'))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--index')
    source.add_argument('--root', action='append')
    parser.add_argument('--path-map', help='JSON object mapping index prefixes to local prefixes')
    parser.add_argument('--aliases')
    parser.add_argument('--text', required=True)
    parser.add_argument('--current-title')
    args = parser.parse_args(argv)
    try:
        aliases = json.loads(Path(args.aliases).read_text()) if args.aliases else None
        path_map = json.loads(args.path_map) if args.path_map else None
        store = (TitleSessionStore.from_cache(args.index, path_map=path_map, aliases=aliases)
                 if args.index else TitleSessionStore.from_roots(args.root, aliases=aliases))
        result = store.suggest('cli-session', args.text,
                               current={'title': args.current_title} if args.current_title else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print('title resolution failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())