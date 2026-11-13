#!/usr/bin/env python3
"""Read-only, bounded title suggestions from a real MediaIndex; never execute.

RapidFuzz is an optional experiment dependency (poc/requirements-eval.txt).
String similarity is not an ASR confidence probability. Original hypotheses are
returned unchanged, even when suggestions or validated aliases match.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from adbtv.media_index import MediaIndex, VIDEO_EXT, cn_number  # noqa: E402

MAX_TEXT = 256
MAX_QUERY = 128
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_EPISODES = 200000
MAX_ALIASES = 1000
MAX_CANDIDATES = 5


def _key(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def _digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def _name(value):
    return (isinstance(value, str) and 0 < len(value) <= MAX_TEXT
            and bool(_key(value)) and not any(unicodedata.category(c).startswith('C') for c in value)
            and not any(c in value for c in ('/', '\\', '<', '>', '`', '$', '|', '{', '}')))


class TitleSuggester:
    """Immutable snapshot; no cache writes, agents, ADB, or playback operations."""

    def __init__(self, index, *, aliases=None, min_score=70.0, min_margin=10.0):
        from rapidfuzz.fuzz import ratio
        self._ratio = ratio
        if not 0 <= min_score <= 100 or not 0 <= min_margin <= 100:
            raise ValueError('Invalid score thresholds')
        self.min_score, self.min_margin = float(min_score), float(min_margin)
        if len(index.entries) > MAX_ENTRIES:
            raise ValueError('Index exceeds entry limit')
        records, identities, episode_slots = {}, {}, 0
        for entry in index.entries:
            if not _name(entry.base) or not _name(entry.title) or not isinstance(entry.seasons, dict):
                raise ValueError('Invalid media entry')
            year = str(entry.year) if entry.year is not None else None
            if year is not None and not re.fullmatch(r'\d{4}', year):
                raise ValueError('Invalid media year')
            for season, episodes in entry.seasons.items():
                if not str(season).isdigit() or not 1 <= int(season) <= 999 or not isinstance(episodes, dict):
                    raise ValueError('Invalid media season')
                episode_slots += len(episodes)
                if episode_slots > MAX_EPISODES:
                    raise ValueError('Index exceeds episode limit')
                identity = (_key(entry.base), year, int(season))
                bucket = records.setdefault(identity, set())
                identities.setdefault(identity, set()).add(entry.base)
                for episode, raw_path in episodes.items():
                    if (not str(episode).isdigit() or not 1 <= int(episode) <= 99999
                            or not isinstance(raw_path, str) or len(raw_path) > 4096):
                        raise ValueError('Invalid media episode')
                    path = Path(raw_path).expanduser()
                    # Cache entries are evidence only if their media still exists.
                    # Do not open media contents, follow executables, or create files.
                    if path.suffix.lower() not in VIDEO_EXT or not path.is_file():
                        continue
                    info = path.stat()
                    bucket.add((int(episode), str(path.resolve()), info.st_size, info.st_mtime_ns))
        self._candidates = []
        canonical = []
        for identity in sorted(records, key=lambda i: (i[0], i[1] or '', i[2])):
            episodes = records[identity]
            if not episodes:
                continue
            title = sorted(identities[identity])[0]
            candidate_id = 'title-' + _digest(identity)[:24]
            self._candidates.append({'id': candidate_id, 'title': title, 'year': identity[1],
                                     'season': identity[2],
                                     'episode_count': len({e[0] for e in episodes})})
            canonical.append([identity, sorted(episodes)])
        self.index_fingerprint = _digest(canonical)
        self._names = {c['id']: [_key(c['title'])] for c in self._candidates}
        aliases = aliases if aliases is not None else index.aliases
        if not isinstance(aliases, dict) or len(aliases) > MAX_ALIASES:
            raise ValueError('Invalid aliases')
        for alias, target in aliases.items():
            if not _name(alias) or len(alias) > MAX_QUERY or not _name(target):
                raise ValueError('Invalid alias')
            matched = [c for c in self._candidates if target == c['id'] or _key(target) == _key(c['title'])]
            # A title alias can address its real seasons, but not different remakes.
            if not matched or len({(c['title'], c['year']) for c in matched}) != 1:
                raise ValueError('Alias target must identify one existing title')
            alias_key = _key(alias)
            for candidate in self._candidates:
                if _key(candidate['title']) == alias_key and candidate not in matched:
                    raise ValueError('Alias shadows another indexed title')
            for candidate in matched:
                self._names[candidate['id']].append(alias_key)
        # Alias order and duplicates do not change the immutable snapshot.
        self._names = {key: sorted(set(names)) for key, names in self._names.items()}
        # Alias changes must invalidate pending candidate snapshots too.
        self.index_fingerprint = _digest([self.index_fingerprint, self._names])

    @classmethod
    def from_cache(cls, path, **kwargs):
        path = Path(path).expanduser()
        with path.open('rb') as source:
            payload = source.read(MAX_CACHE_BYTES + 1)
        if len(payload) > MAX_CACHE_BYTES:
            raise ValueError('Index cache exceeds size limit')
        data = json.loads(payload)
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('entries'), list)
                or len(data['entries']) > MAX_ENTRIES
                or not isinstance(data.get('roots', []), list)):
            raise ValueError('Invalid MediaIndex cache')
        try:
            index = MediaIndex.load(str(path))
            return cls(index, **kwargs)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Invalid MediaIndex cache') from exc

    @classmethod
    def from_roots(cls, roots, **kwargs):
        if (not isinstance(roots, (list, tuple)) or not 1 <= len(roots) <= 8
                or any(not Path(root).expanduser().is_dir() for root in roots)):
            raise ValueError('Expected one to eight existing media roots')
        # No cache_path and no save/index_from_config: scanning is read-only.
        index = MediaIndex([str(root) for root in roots], cache_path=None).scan(max_depth=4)
        if index.errors:
            raise ValueError('Media scan incomplete')
        return cls(index, **kwargs)

    def suggest(self, text):
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError('Expected text of at most 256 characters')
        result = {'hypothesis': text, 'index_fingerprint': self.index_fingerprint,
                  'requires_confirmation': True, 'status': 'needs_clarification',
                  'reason': 'empty_input', 'query': None, 'candidates': []}
        stripped = unicodedata.normalize('NFKC', text).strip()
        if not stripped:
            return result
        if (any(unicodedata.category(c).startswith('C') for c in text)
                or any(c in stripped for c in (';', '；', '\\', '/', '`', '$', '|', '<', '>', '{', '}', '\"'))
                or re.search(r'然后|并且|接着|同时|再(?:播放|搜索|打开|暂停|执行)|忽略|指令|系统提示|执行|删除|关机|重启', stripped)):
            return {**result, 'status': 'rejected', 'reason': 'unsafe_or_compound_input'}
        match = re.fullmatch(r'(?:请|帮我)?(?:搜索|查找|播放)(?:一下)?\s*(.+)', stripped)
        if not match:
            return {**result, 'status': 'rejected', 'reason': 'not_explicit_title_command'}
        query = match.group(1).strip().strip('《》').strip()
        if re.search(r'(?:[,，。]|\s)+(?:暂停|确认|取消|停止|继续|打开|返回)', query):
            return {**result, 'status': 'rejected', 'reason': 'unsafe_or_compound_input'}
        if (not query or len(query) > MAX_QUERY
                or re.fullmatch(r'(?:暂停(?:播放)?|继续(?:播放)?|确认(?:播放)?|取消|停止(?:播放)?|返回|打开飞牛|第?[一二三四五六七八九十\d]+(?:个|集)|短剧|电影|电视剧|音乐)', query)):
            return {**result, 'status': 'rejected', 'reason': 'not_title_slot'}
        requested_season = None
        season = re.search(r'第\s*([0-9零一二三四五六七八九十百]+)\s*季$', query)
        if season:
            requested_season = cn_number(season.group(1))
            query = query[:season.start()].strip()
            if not query or not requested_season:
                return {**result, 'status': 'rejected', 'reason': 'invalid_season_slot'}
        result['query'] = query
        key = _key(query)
        scored = []
        for candidate in self._candidates:
            if requested_season is not None and candidate['season'] != requested_season:
                continue
            score = max(self._ratio(key, name) for name in self._names[candidate['id']])
            if score >= 50:
                scored.append({**candidate, 'score': round(score, 4), 'requires_confirmation': True})
        scored.sort(key=lambda c: (-c['score'], _key(c['title']), c['year'] or '', c['season'], c['id']))
        result['candidates'] = scored[:MAX_CANDIDATES]
        if not scored:
            result['reason'] = 'no_indexed_match'
            return result
        best = scored[0]
        if best['score'] < self.min_score:
            result['reason'] = 'low_score'
        elif len(scored) > 1 and (best['title'], best['year']) == (scored[1]['title'], scored[1]['year']) and requested_season is None:
            result['reason'] = 'ambiguous_seasons'
        elif len(scored) > 1 and best['score'] - scored[1]['score'] < self.min_margin:
            result['reason'] = 'small_score_margin'
        else:
            result.update(status='suggested', reason='candidate_requires_confirmation')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--index', help='Existing MediaIndex v1 cache; never rewritten')
    source.add_argument('--root', action='append', help='Read-only scan of media roots (up to eight)')
    parser.add_argument('--text', required=True)
    parser.add_argument('--aliases', help='JSON mapping of reviewed aliases to real indexed title or ID')
    args = parser.parse_args(argv)
    try:
        aliases = None
        if args.aliases:
            path = Path(args.aliases)
            if path.stat().st_size > 256 * 1024:
                raise ValueError('Aliases exceed size limit')
            aliases = json.loads(path.read_text(encoding='utf-8'))
        factory = TitleSuggester.from_cache if args.index else TitleSuggester.from_roots
        suggester = factory(args.index or args.root, aliases=aliases)
        print(json.dumps(suggester.suggest(args.text), ensure_ascii=False))
        return 0
    except (OSError, ValueError, ImportError):
        # Exceptions may include private filesystem paths; do not echo them.
        print(json.dumps({'status': 'unavailable_or_invalid', 'requires_confirmation': True,
                          'reason': 'check_index_input_and_optional_dependencies'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
