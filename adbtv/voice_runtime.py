"""V4 phase 1/2 wrapper around the existing Agent, not a new executor.

Catalog facts and ephemeral sessions are separate from model proposals. Only
finite, re-parsed command templates reach Agent.run. Xiaomi's native assistant
cannot be suppressed: relative/state-changing controls require confirmation,
while idempotent controls use a brief settle period and state coordination.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from .assistant_loop import AssistantLoop
from .conversation import ConversationManager
from .intent import parse_request
from .library_tools import LibraryTools
from .media_index import cn_number, normalize, index_from_config
from .voice_intelligence import IntelligentVoiceRouter, VoiceDecision, validate_decision


def _clean_reply(text, limit=1000):
    """Collapse model-produced whitespace/control characters into one line.

    The response schema rejects control characters, and a model answer may
    contain newlines. This keeps the fact content intact while making the reply
    a single bounded line.
    """
    if not isinstance(text, str):
        return ''
    cleaned = ''.join(' ' if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:limit]


_CAPABILITY=('我可以查询本地影视库（总数、按类型浏览、按片名/年份搜索、推荐、最近添加），'
              '也可以打开应用、暂停/继续、查当前播放；播放前会先请你确认。')

GENRES=('悬疑','科幻','喜剧','爱情','动作','犯罪','历史','战争','恐怖','家庭','动画','纪录片')


class LibraryVoiceCatalog:
    """Factual candidates from the existing local index and optional NFO metadata.

    No model-generated recommendations enter this adapter. Missing genre/region
    metadata does not match a genre/region filter. No title-to-genre guessing.
    """
    def __init__(self,cfg,index=None):
        self.cfg=cfg
        self.index=index
        self._loaded_at=0.0
        self._rows=[]

    def _load(self):
        if self._loaded_at and time.monotonic()-self._loaded_at<60:return
        index=self.index or index_from_config(self.cfg)
        self.index=index
        self._rows=[]
        if index is None:
            self._loaded_at=time.monotonic()
            return
        for key,group in index._grouped().items():
            entry=group[0]; metadata={}; paths=[]
            for item in group:
                for episodes in item.seasons.values():paths.extend(episodes.values())
            # The persisted index may outlive moved/unmounted files. Verify a
            # real local path before reporting the title as currently present.
            first_video=next((video for video in paths if Path(video).is_file()),None)
            if first_video is None:continue
            for video in (first_video,):
                current=Path(video).parent
                for directory in (current,current.parent):
                    nfo=directory/'tvshow.nfo'
                    try:
                        if not nfo.is_file() or nfo.stat().st_size>1024*1024:continue
                        node=ET.fromstring(nfo.read_bytes())
                        metadata={'genres':[e.text.strip() for e in node.findall('genre') if e.text],
                                  'region':node.findtext('country') or node.findtext('region')}
                        break
                    except (OSError,ValueError,ET.ParseError):continue
                if metadata:break
            kind='movie' if '电影' in Path(first_video).parts else 'series'
            row={'id':hashlib.sha256(key.encode()).hexdigest()[:24],'title':entry.base,
                 'year':int(entry.year) if entry.year and str(entry.year).isdigit() else None,
                 'genres':metadata.get('genres',[]),'region':metadata.get('region'),
                 'season':None,'episode':None,'kind':kind}
            self._rows.append(row)
        self._loaded_at=time.monotonic()

    def search(self,query='',genre=None,region=None,kind=None):
        self._load()
        matching=None
        if query and self.index:
            matching={normalize(r['title']) for r in self.index.search(query,limit=32)}
        rows=[]
        for row in self._rows:
            if matching is not None and normalize(row['title']) not in matching:continue
            if genre and genre not in row['genres']:continue
            if kind and kind!='any' and kind!=row['kind']:continue
            # Region filtering uses the same explicit normalization as sessions.
            if region:
                wanted='中国大陆' if region in ('国产','大陆','中国','内地') else region
                have='中国大陆' if row['region'] in ('国产','大陆','中国','内地') else row['region']
                if have!=wanted:continue
            rows.append(dict(row))
        return rows[:32]

    def overview(self,kind='any',limit=8):
        """Factual, bounded summary; search() is intentionally capped at 32.

        Only the local mounted media index is visible. Online fnmedia-ext and
        other external catalogs are not claimed to be linked.
        """
        if kind not in ('any','movie','series'):
            raise ValueError('invalid library kind')
        self._load()
        rows=[dict(item) for item in self._rows if kind=='any' or item['kind']==kind]
        has_mount=bool(self.index is not None and any(root.is_dir() for root in self.index.roots))
        return {'available': has_mount,
                'total':len(rows),
                'movies':sum(1 for item in self._rows if item['kind']=='movie'),
                'series':sum(1 for item in self._rows if item['kind']=='series'),
                'rows':rows[:min(8,max(0,int(limit)))]}

    def exact_title(self,requested,actual):
        if normalize(requested)==normalize(actual):return True
        self._load()
        aliases=self.index.aliases if self.index else {}
        return normalize(aliases.get(requested,''))==normalize(actual)

    def resolve(self,title,season=None,episode=None):
        self._load()
        if self.index is None:return None
        match=self.index.resolve(title,season,episode)
        if not match or match.get('requested_season_found') is False or match.get('requested_episode_found') is False:return None
        return match


def _quoted(text):
    if not isinstance(text,str) or not text or len(text)>200 or re.search(r'[《》\x00-\x1f]',text):
        raise ValueError('unsafe content label')
    return '《'+text+'》'


class VoiceDialogue:
    def __init__(self,cfg,*,agent,device=None,catalog=None,router=None,sessions=None,
                 responder=None,clock=time.monotonic):
        self.cfg=cfg
        self.agent=agent
        self.device=device
        self.catalog=catalog if catalog is not None else LibraryVoiceCatalog(cfg)
        self.router=router if router is not None else IntelligentVoiceRouter(cfg)
        options=(cfg.get('voice') or {}).get('conversation') or {}
        self.sessions=sessions or ConversationManager(options.get('ttl_seconds',180),options.get('max_turns',8),clock)
        self.clock=clock
        self._extras={}
        self._last_observe={}
        self._lock=threading.RLock()
        self.observe_ttl=max(0.0,min(10.0,float(((cfg.get('voice') or {}).get('intelligence') or {}).get('observe_ttl_seconds',1.5))))
        self.settle=max(0,min(2,float(((cfg.get('voice') or {}).get('intelligence') or {}).get('native_settle_seconds',.25))))
        if responder is None:
            from .voice_response import VoiceResponder
            responder=VoiceResponder(cfg)
        self.responder=responder
        # V4.2 read-only library tools + natural-language loop. Both are optional:
        # when disabled, the pre-existing deterministic routing is unchanged.
        assistant_cfg=(cfg.get('voice') or {}).get('assistant') or {}
        self.assistant_enabled=bool(assistant_cfg.get('enabled',False))
        self.tools=LibraryTools(cfg,index=getattr(self.catalog,'index',None))
        self.assistant=AssistantLoop(
            cfg,self.tools,getattr(self.router,'llm',None),
            max_steps=int(assistant_cfg.get('max_steps',4)),
            budget_seconds=float(assistant_cfg.get('budget_seconds',14)),
            clock=clock) if self.assistant_enabled else None
        self.persist_preferences=bool(assistant_cfg.get('remember_preferences',False))
        self.preferences_path=None
        if self.persist_preferences:
            configured=assistant_cfg.get('preferences_path') or ''
            if configured:
                self.preferences_path=Path(str(configured)).expanduser()
            else:
                from .execution import runtime_dir
                self.preferences_path=runtime_dir(cfg)/'voice_preferences.json'
        self._preferences={}

    def _extra(self,tv):
        now=self.clock()
        for key in list(self._extras):
            if self._extras[key]['expires']<=now:del self._extras[key]
        session=self.sessions.get(tv)
        if tv not in self._extras or self._extras[tv]['sid']!=session.session_id:
            if len(self._extras)>=32:self._extras.pop(next(iter(self._extras)))
            self._extras[tv]={'sid':session.session_id,'criteria':{},'expires':0}
        state=self._extras[tv];state['expires']=now+self.sessions.ttl_seconds
        return state

    def _ask(self,tv,reply,kind='clarify',candidates=None,status='NEEDS_CLARIFICATION'):
        reply=_clean_reply(reply)
        self.sessions.ask(tv,kind,reply,{'count':len(candidates or [])})
        stage='confirm' if kind=='confirmation' else 'clarify'
        spoken=self.responder.respond(stage,reply,status=status)
        return {'route':'clarify','status':status,'reply':reply,'candidates':candidates or [],
                'speech':spoken}

    @staticmethod
    def _sequence_tail(app,tail):
        """Map the action half of "打开<app><action>" to one finite step."""
        tail=(tail or '').strip(' 的了，,')
        if not tail:
            return VoiceDecision('agent','open',target=app,parameters={'app':app},
                                 confidence=1,source='context_rule')
        sets=re.fullmatch(r'(?:把|将)?(?:音量|声音)\s*(?:设置|调整|调|设)?\s*(?:成|到|为|至)?\s*'
                          r'([0-9一二三四五六七八九十百]+)\s*(?:格|级)?',tail)
        if sets:
            text=sets.group(1)
            level=int(text) if text.isdigit() else cn_number(text)
            if level is None or not 0<=level<=100:
                return VoiceDecision('clarify',reply='请说明 0 到 100 的音量。',confidence=1,source='context_rule')
            step={'intent':'volume_set','target':'','parameters':{'level':level}}
        elif re.fullmatch(r'(?:静音|关掉声音)',tail):
            step={'intent':'volume_set','target':'','parameters':{'level':0}}
        elif re.fullmatch(r'(?:暂停|暂停一下|停一下)',tail):
            step={'intent':'pause','target':'','parameters':{}}
        elif re.fullmatch(r'(?:继续|继续播放|接着播)',tail):
            step={'intent':'resume','target':'','parameters':{}}
        elif re.search(r'播放|放|看|搜|找|来',tail):
            step={'intent':'search','target':'','parameters':{'query':tail}}
        else:
            return None
        return VoiceDecision('agent','sequence',parameters={'steps':[
            {'intent':'open','target':app,'parameters':{'app':app}}, step]},
            confidence=1,source='context_rule')

    @staticmethod
    def _split_combination(clean):
        """Deterministic "打开X，<simple action>" -> finite sequence, if possible."""
        names=('飞牛TV','飞牛tv','飞牛','B站','b站','哔哩哔哩','小电视','爱奇艺','腾讯视频','优酷','芒果TV','芒果','抖音')
        app=next((n for n in names if clean.startswith('打开'+n)),None)
        if app is not None:
            tail=clean[len('打开'+app):].strip(' ，,')
            match=re.fullmatch(r'(.*)',tail) if tail else None
            if match:
                return VoiceDialogue._sequence_tail(app,match.group(1))
        match=re.fullmatch(r'打开\s*(.+?)\s*[，,]?\s*(?:把|将)?\s*(.+)',clean)
        if not match:
            return None
        app=match.group(1).replace(' ','')
        tail=match.group(2).strip()
        level=None
        sets=re.fullmatch(r'(?:音量|声音)\s*(?:设置|调整|调|设)?\s*(?:成|到|为|至)\s*([0-9一二三四五六七八九十百]+)\s*(?:格|级)?',tail)
        if sets:
            text=sets.group(1)
            level=int(text) if text.isdigit() else cn_number(text)
            if level is None or not 0<=level<=100:
                return VoiceDecision('clarify',reply='请说明 0 到 100 的音量。',confidence=1,source='context_rule')
            step={'intent':'volume_set','target':'','parameters':{'level':level}}
        elif re.fullmatch(r'(?:静音|关掉声音|声音关掉)',tail):
            step={'intent':'volume_set','target':'','parameters':{'level':0}}
        elif re.fullmatch(r'(?:把)?(?:声音|音量)?(?:调小|小一点|调低|小点)(?:一点|点)?',tail):
            step={'intent':'volume_down','target':'','parameters':{'delta':2}}
        elif re.fullmatch(r'(?:把)?(?:声音|音量)?(?:调大|大一点|调高|大点)(?:一点|点)?',tail):
            step={'intent':'volume_up','target':'','parameters':{'delta':2}}
        elif tail in ('暂停','暂停一下','停一下'):
            step={'intent':'pause','target':'','parameters':{}}
        elif tail in ('继续','继续播放','接着播'):
            step={'intent':'resume','target':'','parameters':{}}
        elif re.search(r'播放|放点|开个|来点|搜|找',tail):
            step={'intent':'search','target':'','parameters':{'query':tail}}
        else:
            return None
        return VoiceDecision('agent','sequence',parameters={'steps':[
            {'intent':'open','target':app,'parameters':{'app':app}}, step]},
            confidence=1,source='context_rule')

    def _context_decision(self,text,tv,pending=None):
        """Finite answers to questions, used only after semantic routing/fallback."""
        from .voice import VoiceRouter
        clean=VoiceRouter.normalize(text).strip().rstrip('。！!？?')
        state=self.sessions.get(tv)
        # The device observation this turn clears ``waiting_question``; ``pending``
        # is the copy captured before that, so a one-word answer can still resolve.
        waiting=pending or state.waiting_question or {}
        if waiting.get('kind') in ('selection','episode'):
            slot=re.fullmatch(r'(?:第)?([0-9]+|[零〇一二三四五六七八九十百两]+)\s*(?:部|个|项|集)?',clean)
            if slot:
                value=cn_number(slot.group(1))
                if value:
                    if waiting.get('kind')=='selection':
                        return VoiceDecision('agent','select',parameters={'index':value},
                                             confidence=1,source='context_rule',reason='pending_selection')
                    params={'episode':value}
                    if state.selected_content:params['candidate_id']=state.selected_content['id']
                    return VoiceDecision('agent','play',parameters=params,
                                         confidence=1,source='context_rule',reason='pending_episode')
        if clean in ('确认','确定','是的','对','好','就这个','可以','行','没问题','确认播放','确定播放','好的'):
            return VoiceDecision('agent','confirm',confidence=1,source='context_rule')
        if (re.search(r'(?:正在|在)?(?:播放|放|播)(?:的)?(?:是)?(?:什么|啥)[？?。！!]*$',clean)
                and not re.search(r'资源|电影|剧|搜索|找|推荐',clean)):
            return VoiceDecision('agent','query_media',confidence=1,source='context_rule')
        if re.fullmatch(r'(?:请|帮我)?(?:取消|撤销|算了|不用了|别播了)(?:吧|了)?'
                        r'(?:刚才|这个|上一个)?(?:的)?(?:操作|任务|播放)?(?:吧|了)?',clean):
            return VoiceDecision('agent','deny',confidence=1,source='context_rule')
        if clean in ('不要这个','不是这个','换一个','换一部'):
            return VoiceDecision('agent','reject',confidence=1,source='context_rule')
        correction=re.search(r'第([0-9一二三四五六七八九十百]+)(?:部|个|项)',clean)
        if correction and re.search(r'不是第[0-9一二三四五六七八九十百]+集|不是集|不是一集',clean):
            return VoiceDecision('agent','select',parameters={'index':cn_number(correction.group(1))},
                                 confidence=1,source='context_rule',reason='user_correction')
        describe=re.fullmatch(r'第([0-9一二三四五六七八九十百]+)(?:部|个|项)(?:是|叫)?(?:什么|啥|哪一部|哪个)(?:呢|啊|呀)?',clean)
        if describe:
            return VoiceDecision('agent','describe',parameters={'index':cn_number(describe.group(1))},
                                 confidence=1,source='context_rule')
        if re.search(r'上次看到(?:了)?(?:哪里|哪儿|哪了|哪一集|第几集)|看到哪了|上次看哪|上回看到',clean):
            return VoiceDecision('agent','recall',confidence=1,source='context_rule')
        ordinal=re.fullmatch(r'(?:(?:先|我要看|我要|我就要|就要|就看|请|选|要看|播放|放|看|要|来|就)\s*)*'
                             r'第([0-9一二三四五六七八九十百]+)(?:部|个|项)(?:吧|的|啊)?',clean)
        if ordinal:
            return VoiceDecision('agent','select',parameters={'index':cn_number(ordinal.group(1))},confidence=1,source='context_rule')
        episode=re.fullmatch(r'(?:请|帮我)?(?:播放|放|看|从)?第([0-9一二三四五六七八九十百]+)集'
                             r'(?:开始|播放|播起)?(?:吧|的)?',clean)
        if episode:
            params={'episode':cn_number(episode.group(1))}
            if state.selected_content:params['candidate_id']=state.selected_content['id']
            return VoiceDecision('agent','play',parameters=params,confidence=1,source='context_rule')
        region='国产' if re.fullmatch(r'(?:要)?(?:国产|中国|大陆|内地)(?:的)?',clean) else None
        if region:
            return VoiceDecision('agent','refine',parameters={'region':region},confidence=1,source='context_rule')
        state_now=self.sessions.get(tv)
        history=state_now.played_history
        if re.search(r'类似|相似|差不多|一样的',clean) and (history or state_now.selected_content):
            base=(state_now.selected_content or history[0])
            return VoiceDecision('agent','refine',parameters={'like':base['id']},confidence=1,
                                 source='context_rule')
        # Reference + action with a real playback history (never invented).
        reference=re.search(r'(?:刚才|刚刚|上次|之前|前面|上回|刚才看)(?:那|看的|看过的|看的那个)?'
                            r'(?:部|集|个)?',clean)
        referent=state_now.selected_content or (history[0] if history else None)
        if reference and referent and re.search(r'放|播|看|继续|再来|接着|重复',clean):
            last=referent
            if re.search(r'那集|这一集|刚才那集',clean) and (state_now.current_playback or {}).get('episode'):
                return VoiceDecision('agent','resume_episode',confidence=1,source='context_rule')
            return VoiceDecision('agent','play',parameters={'candidate_id':last['id']},
                                 confidence=1,source='context_rule')
        if re.search(r'上一部|上一个|还是看之前|回到刚才那部',clean) and history:
            return VoiceDecision('agent','play',parameters={'candidate_id':history[0]['id']},
                                 confidence=1,source='context_rule')
        if re.search(r'刚才看到的那一集|接着看|继续刚才那集',clean):
            return VoiceDecision('agent','resume_episode',confidence=1,source='context_rule')
        swap=re.fullmatch(r'(?:不是这个|不要这个|换一个)[，,]?\s*(?:换|要|看)?第([0-9一二三四五六七八九十]+)(?:个|部|项)?',clean)
        if swap:
            return VoiceDecision('agent','swap',parameters={'index':cn_number(swap.group(1))},
                                 confidence=1,source='context_rule')
        combined=self._split_combination(clean)
        if combined is not None:
            return combined
        genre=next((g for g in GENRES if g in clean),None)
        kind_hit=re.search(r'电影|短剧|电视剧|纪录片|动画',clean)
        if kind_hit and re.search(r'播放|播|放|看|找|搜|推荐|查',clean) and not genre and not re.search(r'第[0-9一二三四五六七八九十百]+集',clean):
            if re.search(r'推荐|随便|挑一|好看的',clean) and self.assistant is not None:
                return VoiceDecision('agent','assistant',confidence=1,source='context_rule',
                                     reason='recommendation')
            kind='movie' if '电影' in kind_hit.group(0) else 'series'
            return VoiceDecision('agent','search',parameters={'kind':kind},confidence=1,source='context_rule')
        # "找<片名>" / "搜索<片名>": a titled lookup is a real library search,
        # not a genre guess and not a model invention.
        lookup=re.fullmatch(r'(?:帮我|给我)?(?:搜索|找一下|搜一下|找|搜|查)\s*(?:一下)?\s*(.+)',clean)
        if lookup and not genre:
            payload=lookup.group(1).strip(' 的了')
            payload=re.sub(r'^(?:个|部|点|些)', '', payload).strip()
            if len(payload)>=2 and not re.fullmatch(r'(?:电影|电视剧|短剧|动画|纪录片)',payload):
                return VoiceDecision('agent','search',parameters={'query':payload},
                                     confidence=1,source='context_rule')
        if re.search(r'找|搜|推荐',clean) and (genre or re.search(r'电影|电视剧|短剧',clean)):
            params={}
            if genre:params['genre']=genre
            if re.search(r'国产|大陆|内地',clean):params['region']='国产'
            params['kind']='movie' if '电影' in clean else 'series'
            return VoiceDecision('agent','search',parameters=params,confidence=1,source='slot_fallback')
        return None

    def _list(self,tv,rows,reason):
        try:rows=self.sessions.set_results(tv,rows,source='library')
        except ValueError:return self._ask(tv,'媒体库候选资料不完整，请提供具体片名。')
        if not rows:return self._ask(tv,'没有查到符合条件的真实媒体库结果；缺少类型或地区资料时我不会猜测。请换个条件或提供片名。')
        labels='；'.join(f"{i+1}.《{r['title']}》" for i,r in enumerate(rows[:8]))
        return self._ask(tv,'找到这些实际入库内容：'+labels+'。请选择第几部。','selection',rows)

    def _browse_library(self,tv,d):
        """Read-only catalog inspection, without LLM-authored facts or ADB."""
        p=d.parameters
        scope=p.get('scope','local')
        kind=p.get('kind','any')
        if scope=='online':
            reply='在线片源扩展目前没有接入 AI 管家的资源清单查询工具，所以我不能核实它有哪些影片。你可以查询 NAS 本地影视库。'
            return {'route':'agent','status':'UNSUPPORTED','reply':reply,
                    'candidates':[],'answer_source':'unverified', 'speech':self.responder.respond('failure',reply,status='UNSUPPORTED')}
        if hasattr(self.catalog,'overview'):
            info=self.catalog.overview(kind=kind,limit=8)
        else:
            # Compatible with legacy/test adapters; don't misreport search's
            # possibly capped result count as the complete library size.
            info={'available':True,'total':None,'movies':None,'series':None,
                  'rows':self.catalog.search(kind=kind)[:8]}
        if not info.get('available'):
            reply='目前 AI 管家未启用或无法访问 NAS 本地影视库索引。请检查 media.enabled、media.roots 和 Docker 只读目录映射。'
            return {'route':'agent','status':'UNAVAILABLE','reply':reply,
                    'candidates':[],'answer_source':'unverified', 'speech':self.responder.respond('failure',reply,status='UNAVAILABLE')}
        rows=info['rows']
        count=info.get('total')
        label={'movie':'电影','series':'剧集','any':'影视作品'}[kind]
        if count==0 or (not rows and count is None):
            summary=self.tools.summary() if self.tools is not None else {}
            if kind=='movie' and summary.get('movies')==0 and summary.get('series'):
                reply=(f'本地库里目前没有电影，有 {summary["series"]} 部剧集'
                       f'（共 {summary["episodes"]} 集）。要我列几部剧集，或者按片名找吗？')
            elif kind=='series' and summary.get('series')==0 and summary.get('movies'):
                reply=f'本地库里目前没有剧集，有 {summary["movies"]} 部电影。要看电影吗？'
            elif summary.get('ambiguous_folders'):
                reply=(f'当前本地媒体索引中没有查到{label}；另有 '
                       f'{summary["ambiguous_folders"]} 个文件夹无法确定集数结构，未计入统计。')
            else:
                reply=f'当前本地媒体索引中没有查到{label}。如果 NAS 实际有文件，请检查媒体目录是否已扫描。'
        else:
            quantity=f'共 {count} 部{label}' if count is not None else f'查到以下{label}'
            breakdown=('，其中电影 '+str(info['movies'])+' 部、剧集 '+str(info['series'])+' 部'
                       if kind=='any' and info.get('movies') is not None else '')
            titles='；'.join(f"{i+1}.《{item['title']}》" for i,item in enumerate(rows))
            reply='已查询 NAS 本地影视库：'+quantity+breakdown+'。例如：'+titles+'。'
            if count is None or count>len(rows):
                reply+='以上只是部分条目，可以继续按片名或类型搜索。'
            else:
                reply+='可以说“第几部”来选择。'
            # Keep only items actually displayed, so "第二部" has a factual id.
            self.sessions.set_results(tv,rows,source='library')
        if scope=='all':
            reply+=' 在线片源扩展尚未接入资源查询工具，在线作品数量无法核实。'
        return {'route':'agent','status':'SUCCESS','reply':reply,'candidates':rows,
                'answer_source':'local_catalog',
                'speech':self.responder.respond('success',reply,status='SUCCESS')}

    # ---------- V4.2 assistant loop ----------
    def _preferences_for(self,tv):
        if not self.persist_preferences or self.preferences_path is None:return {}
        try:
            if self.preferences_path.is_file():
                data=json.loads(self.preferences_path.read_text(encoding='utf-8'))
                if isinstance(data,dict):self._preferences=data
        except (OSError,ValueError):
            pass
        value=self._preferences.get(tv)
        return dict(value) if isinstance(value,dict) else {}

    def _remember_preferences(self,tv,preferences):
        """Persist only explicit preference fields, never utterances (opt-in)."""
        if not self.persist_preferences or self.preferences_path is None or not preferences:
            return
        safe={}
        genres=[g for g in (preferences.get('genres') or []) if isinstance(g,str) and 0<len(g)<=20][:8]
        if genres:safe['genres']=genres
        if preferences.get('kind') in ('any','movie','series'):safe['kind']=preferences['kind']
        if type(preferences.get('max_episodes')) is int:safe['max_episodes']=preferences['max_episodes']
        if not safe:return
        self._preferences[tv]=safe
        try:
            self.preferences_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            temp=self.preferences_path.with_suffix('.tmp')
            temp.write_text(json.dumps(self._preferences,ensure_ascii=False),encoding='utf-8')
            temp.chmod(0o600)
            temp.replace(self.preferences_path)
        except OSError:
            pass

    @staticmethod
    def _query_from(outcome):
        for call in reversed(outcome.get('tool_trace') or []):
            if call.get('tool') in ('library_search','library_browse','library_recommend'):
                arguments=call.get('arguments') or {}
                return {key:value for key,value in arguments.items()
                        if key in ('query','genre','kind','year','max_episodes')}
        return {}

    def _assistant_proposal(self,tv,proposal,diagnostics,trace):
        item=proposal.get('item') or {}
        title=item.get('title')
        if not title:return None
        season=proposal.get('season');episode=proposal.get('episode')
        resolved=self.catalog.resolve(title,season,episode)
        if not resolved:
            reply=_clean_reply(f'本地库里找不到《{title}》对应的季集，我没有执行播放。')
            return {'route':'agent','status':'UNSUPPORTED','reply':reply,'candidates':[],
                    'speech':self.responder.respond('failure',reply,status='UNSUPPORTED'),
                    'diagnostics':diagnostics,'tool_trace':trace}
        season=resolved.get('season') or season or 1
        episode=resolved.get('episode') or episode or 1
        try:
            self.sessions.set_results(tv,[item],source='library')
            self.sessions.select(tv,candidate_id=item['id'])
        except ValueError:
            return None
        command=('播放'+_quoted(title)+f'第{season}季'+
                 ('第一集' if episode==1 else f'第{episode}集'))
        self.sessions.set_pending(tv,nonce=secrets.token_hex(16),command=command,
                                  intent='play',candidate_id=item['id'])
        self.sessions.update_assistant(tv,last_item={'id':item['id'],'title':title})
        reply=_clean_reply(f'确认播放《{title}》第{season}季第{episode}集吗？请说“确认”或“取消”。')
        return {'route':'agent','status':'NEEDS_CONFIRMATION','reply':reply,
                'candidates':[item],
                'speech':self.responder.respond('confirm',reply,status='NEEDS_CONFIRMATION'),
                'diagnostics':diagnostics,'tool_trace':trace}

    def _assistant_turn(self,tv,text,extra=None,budget=None):
        """Run the read-only tool loop and fold its result into the session."""
        if self.assistant is None:return None
        session=self.sessions.get(tv)
        state=dict(self.sessions.assistant_state(tv))
        preferences=self._preferences_for(tv)
        if preferences:state['preferences']=preferences
        state['tv']=dict(session.current_playback or {})
        state['candidates']=[{'id':row['id'],'title':row['title']}
                             for row in session.recent_results]
        state['played_history']=[{'id':row.get('id'),'title':row.get('title'),
                                  'season':row.get('season'),'episode':row.get('episode')}
                                 for row in session.played_history[:3]]
        try:
            outcome=self.assistant.run(text,state=state,budget=budget)
        except Exception:
            return None
        trace=list(outcome.get('tool_trace') or [])
        diagnostics=dict(outcome.get('diagnostics') or {})
        query=self._query_from(outcome)
        self.sessions.update_assistant(
            tv,library_scope='local',last_intent='assistant',
            last_tools=[call['tool'] for call in trace if call.get('tool')],
            last_status=str(outcome.get('status') or 'unknown').lower(),
            last_query=query or None)
        if extra is not None and query:
            extra['criteria'].update({k:v for k,v in query.items() if k in ('genre','kind','region')})
        proposal=outcome.get('proposal')
        if proposal:
            return self._assistant_proposal(tv,proposal,diagnostics,trace)
        rows=outcome.get('candidates') or []
        if rows:
            try:self.sessions.set_results(tv,rows,source='library')
            except ValueError:rows=[]
        status=outcome.get('status') or 'SUCCESS'
        reply=_clean_reply(outcome.get('reply') or '')
        stage='success' if status=='SUCCESS' else 'failure'
        return {'route':'agent','status':status,'reply':reply,'candidates':rows,
                'speech':self.responder.respond(stage,reply,status=status),
                'diagnostics':diagnostics,'tool_trace':trace}

    @staticmethod
    def _library_question(text):
        clean=text or ''
        if not re.search(r'资源|影视库|媒体库|片库|电影|电视剧|剧集|短剧|推荐|好看|新加|最近|多少部|几部|有什么|有哪些|类似|换一部|太长|短一点|集数',clean):
            return False
        # Pure playback/control wording keeps the existing deterministic path.
        if re.fullmatch(r'(?:请|帮我)?(?:播放|打开|暂停|继续|音量|返回|回到桌面).*',clean.strip()):
            return False
        return True

    _PRONOUN=re.compile(r'(?:这|那)(?:一)?部(?:作品|剧|电影|剧集)?|这个|那个|它|这部|那部')

    def _play_selection(self,tv,decision):
        p=decision.parameters
        selected=None
        if p.get('candidate_id'):
            selected=self.sessions.select(tv,candidate_id=p['candidate_id'])
            if not selected:raise ValueError('unknown candidate')
        else:
            title=p.get('title') or decision.target
            if isinstance(title,str):
                # Strip a leading pronoun ("这一部/那个/它") and a trailing
                # season/episode phrase so only a real title remains.
                stripped=re.sub(r'^(?:这|那)(?:一)?部(?:作品|剧|电影|剧集)?(?:的)?','',title.strip())
                stripped=re.sub(r'第[0-9一二三四五六七八九十百]+(?:季|集).*$','',stripped).strip()
                stripped=stripped.strip('的')
                if not stripped or self._PRONOUN.fullmatch(stripped):
                    title=None
                else:
                    title=stripped
            if title:
                rows=self.catalog.search(query=title)
                exact=[r for r in rows if normalize(r['title'])==normalize(title)]
                if not exact and hasattr(self.catalog,'exact_title'):
                    exact=[r for r in rows if self.catalog.exact_title(title,r['title'])]
                if len(exact)!=1:raise ValueError('unconfirmed title')
                self.sessions.set_results(tv,exact,source='library')
                selected=self.sessions.select(tv,index=1)
            else:
                selected=self.sessions.get(tv).selected_content
        if not selected:raise ValueError('missing content reference')
        season=p.get('season') or selected.get('season')
        episode=p.get('episode') or selected.get('episode') or 1
        resolved=self.catalog.resolve(selected['title'],season,episode)
        if not resolved:raise ValueError('requested episode not observed')
        season=resolved.get('season') or season or 1
        episode=resolved.get('episode') or episode
        # An explicit episode-one request is represented literally for the existing parser.
        ep='第一集' if episode==1 else f'第{episode}集'
        return '播放'+_quoted(selected['title'])+f'第{season}季'+ep,selected

    def _command(self,tv,d):
        p=d.parameters;intent=d.intent
        if intent=='open':
            goal=parse_request('打开'+d.target,self.cfg)
            configured={v.get('package') for v in self.cfg.get('apps',{}).values()}
            if d.target in configured:return '打开'+next(k for k,v in self.cfg['apps'].items() if v.get('package')==d.target),None
            if goal.action!='open' or not goal.package:raise ValueError('unconfigured application')
            return '打开'+d.target,None
        if intent=='play':return self._play_selection(tv,d)
        commands={'pause':'暂停播放','resume':'继续播放','home':'回到桌面','back':'返回',
                  'query_app':'当前应用是什么','query_media':'现在播放的是什么'}
        if intent in commands:return commands[intent],None
        if intent=='volume_set':return f"音量调到{p['level']}",None
        if intent in ('volume_up','volume_down'):
            return '音量'+('加' if intent=='volume_up' else '减')+str(p.get('delta',1)),None
        if intent=='sequence':
            commands=[]
            for step in p['steps']:
                child=validate_decision(dict(route='agent',confidence=d.confidence,risk=d.risk,reply='',**step))
                commands.append(self._command(tv,child)[0])
            return '然后'.join(commands),None
        raise ValueError('no finite command template')

    def _coordinate(self,d):
        if self.device is None:return None
        try:
            if d.intent=='open':
                if self.device.current_package()==d.target:return '目标应用已经在前台。'
            if d.intent in ('pause','resume'):
                state=self.device.playback_status(include_ui=False)
                if d.intent=='pause' and state.get('state')=='PAUSED':return '当前已经暂停。'
                if d.intent=='resume' and state.get('playing') is True:return '当前已经在播放。'
            if d.intent=='volume_set':
                volume=self.device.volume()
                if volume.get('known') and volume.get('index')==d.parameters['level']:return '音量已经达到目标。'
        except Exception:return None
        return None

    def _execute(self,tv,d,command,selected,source):
        if source=='xiaomi_logcat' and self.settle:time.sleep(self.settle)
        coordinated=self._coordinate(d) if source=='xiaomi_logcat' else None
        start=time.monotonic()
        outcome={'ok':True,'status':'SUCCESS','summary':coordinated} if coordinated else self.agent.run(command)
        status=outcome.get('status','UNKNOWN')
        success=status=='SUCCESS'
        if success:
            state=self.sessions.get(tv)
            app=state.current_app
            if d.intent=='open':app=parse_request(command,self.cfg).package or app
            playback=dict(state.current_playback or {})
            if selected:playback.update(title=selected['title'],season=d.parameters.get('season') or selected.get('season') or 1,episode=d.parameters.get('episode') or selected.get('episode') or 1)
            if d.intent in ('pause','resume','play'):playback['state']='paused' if d.intent=='pause' else 'playing'
            if selected and d.intent=='play':
                self.sessions.remember_play(tv,{k:selected.get(k) for k in
                    ('id','title','year','genres','region','season','episode')})
            self.sessions.update_observation(tv,app=app,playback=playback)
        reply='已完成。' if success else '还没有确认操作成功，请查看电视当前状态或补充具体要求。'
        if success and d.intent in ('query_media','query_app'):
            # Query intents exist to report a fact. The per-step summary carries
            # it ("正在播放：X"); the top-level summary is only a completion note.
            steps=outcome.get('steps') or []
            step_summary=(steps[-1].get('summary') if steps and isinstance(steps[-1],dict) else '') or ''
            summary=step_summary.strip() or (outcome.get('summary') or '').strip()
            if summary and summary not in ('全部步骤已完成','已完成。'):
                reply=summary
            elif d.intent=='query_media':
                playback=self.sessions.get(tv).current_playback or {}
                if playback.get('title'):
                    reply=f'正在播放《{playback["title"]}》'
                    if playback.get('season'):reply+=f' 第{playback["season"]}季'
                    if playback.get('episode'):reply+=f' 第{playback["episode"]}集'
                elif playback.get('state'):
                    reply=f'电视当前状态：{playback["state"]}'
                else:
                    reply='暂时读不到电视当前播放状态，请确认电视已连接。'
        if status in ('UNSUPPORTED','UNKNOWN'):reply='目前无法可靠完成这个操作，我没有猜测执行。请换一种明确的指令。'
        reply=_clean_reply(reply)
        stage='success' if success else 'failure'
        try:
            playback_now=(self.device.playback_status(include_ui=False).get('state','')
                          if self.device is not None else '')
        except Exception:
            playback_now=''
        spoken=self.responder.respond(stage,reply,status=status,playback_state=playback_now)
        return {'route':'agent','status':status,'reply':reply,'coordinated':bool(coordinated),
                'execution':{'status':status,'ok':bool(outcome.get('ok'))},
                'answer_source':('device_status' if d.intent in ('query_media','query_app')
                                 else 'local_catalog'),
                'speech':spoken,'adb_ms':(time.monotonic()-start)*1000}

    def _observe_context(self,tv):
        if self.device is None:return 0.0
        # Consecutive turns in one conversation usually change nothing; avoid
        # re-dumping the TV (0.6-0.9 s each) within a short window.
        last=self._last_observe.get(tv,0.0)
        if self.observe_ttl and (time.monotonic()-last)<self.observe_ttl:
            return 0.0
        started=time.monotonic()
        try:
            app=self.device.current_package()
            playback=self.device.playback_status(include_ui=False)
            factual={'state':playback.get('state','unknown')}
            metadata=playback.get('metadata') or {}
            if metadata.get('title'):factual['title']=metadata['title']
            elif playback.get('title'):factual['title']=playback['title']
            for key in ('season','episode'):
                if type(playback.get(key)) is int:factual[key]=playback[key]
            self.sessions.update_observation(tv,app=app,playback=factual)
        except Exception:
            # Failure to observe is unknown, never fabricated context.
            self.sessions.update_observation(tv)
        self._last_observe[tv]=time.monotonic()
        return (time.monotonic()-started)*1000

    def _revalidate(self,tv,pending):
        """Re-check target, candidate and device state before executing a proposal."""
        command=pending.get('command') or ''
        goal=parse_request(command,self.cfg)
        if goal.action=='unknown':
            raise ValueError('目标指令已无法解析')
        configured={v.get('package') for v in self.cfg.get('apps',{}).values()}
        if goal.action=='open' and goal.package not in configured:
            raise ValueError('目标应用未配置或已变化')
        selected=None
        candidate_id=pending.get('candidate_id')
        if candidate_id:
            rows=self.sessions.get(tv).recent_results
            selected=next((r for r in rows if r['id']==candidate_id),None)
            if selected is None:
                raise ValueError('候选内容已不在当前会话结果中')
            resolved=self.catalog.resolve(selected['title'],
                                          goal.season or selected.get('season'),
                                          goal.episode or selected.get('episode') or 1)
            if not resolved:
                raise ValueError('媒体库中已找不到该内容')
        elif goal.title and goal.action=='play':
            rows=self.catalog.search(query=goal.title)
            exact=[r for r in rows if normalize(r['title'])==normalize(goal.title)]
            if len(exact)!=1:
                raise ValueError('片名不再唯一匹配')
            selected=exact[0]
        # Device-state sanity check for relative/state operations.
        decision=VoiceDecision('agent',goal.action,goal.title or '',
                               {'level':goal.level} if goal.action=='volume_set' else {},
                               1,'','low','revalidated',reason='pending_revalidated')
        if self.device is not None and goal.action in ('pause','resume','volume_set'):
            try:
                state=self.device.playback_status(include_ui=False)
                if goal.action=='pause' and state.get('state')=='PAUSED':
                    raise ValueError('电视当前已暂停，无需重复执行')
                if goal.action=='resume' and state.get('playing') is True:
                    raise ValueError('电视当前已在播放')
            except ValueError:
                raise
            except Exception:
                pass  # observation failure alone must not block an explicit confirm
        return decision,command,selected

    def handle(self,text,*,tv_id,source='xiaomi_logcat'):
        if source not in ('xiaomi_logcat','web_microphone','text'):raise ValueError('unsupported voice source')
        with self._lock:
            start=time.monotonic();extra=self._extra(tv_id)
            context=self.sessions.snapshot(tv_id)
            # A pending question (selection / episode) must survive this turn's
            # device observation: update_observation replaces app/playback and
            # clears waiting_question, but a one-word slot answer ("3", "第二集")
            # is only meaningful against the question asked last turn. Capture it
            # before observing and feed it back into routing + slot resolution.
            pending_question=context.get('waiting_question')
            fast=self.router.rule_decision(text,context)
            observe_ms=0.0 if fast and (fast.route in ('native','ignore') or fast.intent=='browse_library') else self._observe_context(tv_id)
            model_context=self.sessions.snapshot(tv_id)
            model_context['search_constraints']=copy.deepcopy(extra['criteria'])
            if pending_question:
                model_context['waiting_question']=pending_question
            decision=self.router.decide(text,model_context)
            # Clear, finite slot answers are safe even if the model is offline.
            contextual=self._context_decision(self.router.policy_text(text)[0],tv_id,
                                              pending=pending_question)
            if contextual and decision.route not in ('native','ignore') and not decision.requires_confirmation:
                try:
                    checked=validate_decision(contextual.structured())
                    decision=replace(checked,source=contextual.source,
                                     route_ms=decision.route_ms,llm_ms=decision.llm_ms)
                except ValueError:
                    decision=VoiceDecision('clarify',reply='这个序号或集数超出范围，请重新说明。',reason='invalid_context_slot')
            if (self.assistant is not None and decision.intent=='unknown'
                    and decision.route in ('clarify','agent') and self._library_question(text)):
                # A natural library/chat question must not fall back to the old
                # generic template: let the read-only tool loop answer it.
                decision=VoiceDecision('agent','assistant',confidence=1,
                                       source='assistant_fallback',reason='library_question')
            decision=replace(decision,conflict_risk=source=='xiaomi_logcat' and decision.intent not in ('search','refine','select','query_app','query_media','unknown'))
            if decision.intent not in ('confirm','deny'):
                self.sessions.clear_pending(tv_id)
            # One absolute deadline for the assistant stage of this turn; the
            # loop spends whatever is left, and every model call is bounded by it.
            if self.assistant is not None:
                self._remaining_budget=max(0.5,(start+float(self.assistant.budget))-time.monotonic())
            else:
                self._remaining_budget=None
            try:
                result=self._handle(tv_id,decision,extra,source,text)
            except (KeyError,ValueError,TypeError) as exc:
                message=str(exc)
                if message in ('missing content reference','unknown candidate','unconfirmed title'):
                    rows=self.sessions.get(tv_id).recent_results
                    if rows:
                        labels='；'.join(f'{i+1}.《{r["title"]}》' for i,r in enumerate(rows[:8]))
                        result=self._ask(tv_id,'你是指哪一部？当前候选：'+labels+'。可以说“第几部”。',
                                         'selection',rows)
                    else:
                        result=self._ask(tv_id,'请先让我列出本地候选，并说“第几部”选择后再播放。')
                elif message in ('requested episode not observed',):
                    result=self._ask(tv_id,'这部作品没有你指定的那一集，我没有执行播放。')
                else:
                    result=self._ask(tv_id,'我还无法确认你指的内容，请提供具体片名或从实际候选中明确选择。')
            except Exception:
                result={'route':'agent','status':'UNKNOWN','reply':'暂时无法读取或控制电视，请检查连接后重试。'}
            self.sessions.update_result(tv_id,{'status':result['status']})
            self.sessions.remember_turn(tv_id,result.get('route','clarify'),decision.intent,{'status':result['status']})
            audit=decision.audit()
            audit['route']=result.get('route','clarify')
            audit['requires_confirmation']=result['status']=='NEEDS_CONFIRMATION'
            if audit['requires_confirmation'] and not decision.requires_confirmation:
                audit['reason']='native_conflict_confirmation'
            elif audit['route']=='clarify' and decision.route=='agent':
                audit['reason']='unresolved_context_or_target'
            diagnostics=result.pop('diagnostics',None)
            result.pop('tool_trace',None)
            answer_source=result.pop('answer_source',None)
            metrics={'route_ms':decision.route_ms,'llm_ms':decision.llm_ms,
                     'total_ms':(time.monotonic()-start)*1000,'adb_ms':result.pop('adb_ms',0),
                     'observe_ms':observe_ms,'tts_ms':(result.get('speech') or {}).get('tts_ms')}
            if answer_source and not diagnostics:
                metrics['answer_source']=answer_source
            if diagnostics:
                metrics.update({key:diagnostics.get(key) for key in
                                ('model_name','decision_source','tool_name','tool_status',
                                 'llm_latency_ms','tool_latency_ms','total_latency_ms',
                                 'fallback_reason','answer_source','failed_stage',
                                 'budget_seconds')})
            result.update(session_id=self.sessions.get(tv_id).session_id,decision=audit,
                          metrics=metrics)
            return result

    def _handle(self,tv,d,extra,source,text=''):
        if d.route in ('native','ignore'):
            return {'route':d.route,'status':'NATIVE' if d.route=='native' else 'IGNORED',
                    'reply':'交由电视原生助手处理。' if d.route=='native' else ''}
        if d.intent=='confirm':
            if d.source not in ('rule','context_rule'):
                return self._ask(tv,'请明确说“确认”或“不确认”，我不会把模型推测当作你的授权。',
                                 'confirmation',status='NEEDS_CONFIRMATION')
            pending=self.sessions.take_pending(tv)          # one-shot, expiry enforced
            if not pending:
                return self._ask(tv,'没有可确认的操作（可能已过期或在另一入口执行过），请重新说明要求。')
            try:
                decision,command,selected=self._revalidate(tv,pending)
            except ValueError as exc:
                return self._ask(tv,f'确认前的重新校验未通过：{exc}，未执行。')
            return self._execute(tv,decision,command,selected,source)
        if d.intent=='deny':
            had_pending=bool(self.sessions.get(tv).pending_action)
            extra['pending']=None
            self.sessions.clear_pending(tv)
            reply=('已取消刚才的待确认操作，没有执行任何电视动作。' if had_pending
                   else '现在没有待确认的操作，我没有执行任何电视动作。')
            return self._ask(tv,reply)
        if d.route=='clarify' and not d.requires_confirmation:
            return self._ask(tv,d.reply or '请补充具体的应用、片名或操作。')
        if d.intent=='assistant':
            outcome=self._assistant_turn(tv,text,extra,budget=getattr(self,'_remaining_budget',None))
            if outcome is None:
                return self._ask(tv,'自然语言助理暂不可用。'+_CAPABILITY)
            return outcome
        if d.intent=='describe':
            rows=self.sessions.get(tv).recent_results
            index=d.parameters.get('index')
            if not rows or type(index) is not int or not 1<=index<=len(rows):
                return self._ask(tv,'现在没有可说明的候选，请先让我列一下本地影视库。')
            item=rows[index-1]
            bits=[f'第{index}个是《{item["title"]}》']
            if item.get('year'):bits.append(f'{item["year"]} 年')
            if item.get('genres'):bits.append('类型：'+'、'.join(item['genres']))
            else:bits.append('没有类型元数据')
            if item.get('region'):bits.append('地区：'+item['region'])
            reply='，'.join(bits)+'。'
            return {'route':'agent','status':'SUCCESS','reply':reply,'candidates':rows,
                    'answer_source':'local_catalog',
                    'speech':self.responder.respond('success',reply,status='SUCCESS')}
        if d.intent=='recall':
            state=self.sessions.get(tv)
            playback=dict(state.current_playback or {})
            history=state.played_history
            title=playback.get('title') or (history[0]['title'] if history else None)
            if not title:
                return self._ask(tv,'我还没有记录到你上次播放到哪一集。可以说片名让我查，或说“随便推荐一部”。')
            season=playback.get('season') or (history[0].get('season') if history else None)
            episode=playback.get('episode') or (history[0].get('episode') if history else None)
            parts=[f'上次记录到的是《{title}》']
            if season:parts.append(f'第{season}季')
            if episode:parts.append(f'第{episode}集')
            reply='，'.join(parts)+'。可以说“接着看”继续。'
            return {'route':'agent','status':'SUCCESS','reply':reply,
                    'candidates':history,'speech':self.responder.respond('success',reply,status='SUCCESS')}
        if d.intent=='browse_library':
            # "最近新加了什么" is a recency question, not a full inventory scan.
            if self.assistant is not None and re.search(r'最近|新加|新增|刚加|更新了',text or ''):
                outcome=self._assistant_turn(tv,text,extra)
                if outcome is not None:return outcome
            return self._browse_library(tv,d)
        if d.intent=='search':
            params=copy.deepcopy(d.parameters)
            if d.target and not params.get('query'):
                params['query']=d.target
            if not any(params.get(key) for key in ('query','genre','region','kind')):
                return self._ask(tv,'你想找什么类型、地区或片名？请补充一个搜索条件。')
            extra['criteria']=params
            rows=self.catalog.search(**params)
            if not rows and self.assistant is not None:
                outcome=self._assistant_turn(tv,text,extra)
                if outcome is not None:return outcome
            return self._list(tv,rows,'search')
        if d.intent=='refine' and d.parameters.get('like'):
            base=next((r for r in (self.sessions.get(tv).recent_results +
                                   self.sessions.get(tv).played_history)
                       if r['id']==d.parameters['like']),None)
            if not base:return self._ask(tv,'我不确定你指的是哪一部，请说出片名。')
            if not base.get('genres') and not base.get('region'):
                return self._ask(tv,'这部作品缺少类型/地区元数据，我无法可靠找相似内容；请给片名。')
            rows=[r for r in self.catalog.search(genre=(base.get('genres') or [None])[0],
                                                 region=base.get('region'), kind=None)
                  if r['id']!=base['id']]
            return self._list(tv,rows,'like')
        if d.intent=='refine':
            if not self.sessions.get(tv).recent_results:return self._ask(tv,'当前没有可筛选的真实结果，请先搜索具体内容。')
            rows=self.sessions.refine(tv,genre=d.parameters.get('genre'),region=d.parameters.get('region'))
            extra['criteria'].update(d.parameters)
            return self._list(tv,rows,'refine')
        if d.intent=='select':
            selected=self.sessions.select(tv,**d.parameters)
            if not selected:return self._ask(tv,'这个序号或候选项不存在，请从刚才的实际列表中选择。')
            return self._ask(tv,'已选择《'+selected['title']+'》。你想从第几集开始？','episode')
        if d.intent=='swap':
            state=self.sessions.get(tv)
            if not state.selected_content:
                return self._ask(tv,'当前没有已选内容可替换，请先选择一部。')
            self.sessions.reject_selected(tv)
            target=self.sessions.select(tv,index=d.parameters.get('index'))
            if not target:return self._ask(tv,'这个序号超出当前候选范围，请重新选择。')
            command='播放'+_quoted(target['title'])+'第一集'
            self.sessions.set_pending(tv,nonce=secrets.token_hex(16),command=command,
                                      intent='play',candidate_id=target['id'])
            return self._ask(tv,'确认播放 '+command+' 吗？','confirmation',status='NEEDS_CONFIRMATION')
        if d.intent=='resume_episode':
            state=self.sessions.get(tv)
            playback=state.current_playback or {}
            title=playback.get('title')
            candidate=next((r for r in state.played_history if normalize(r['title'])==normalize(title or '')),None)
            if not candidate:
                return self._ask(tv,'我不知道你说的“刚才那集”是哪一部，请说出片名。')
            season=playback.get('season') or candidate.get('season') or 1
            episode=playback.get('episode') or candidate.get('episode') or 1
            command='播放'+_quoted(candidate['title'])+f'第{season}季第{episode}集'
            self.sessions.set_pending(tv,nonce=secrets.token_hex(16),command=command,
                                      intent='play',candidate_id=candidate['id'])
            return self._ask(tv,'确认继续第'+str(episode)+'集吗？','confirmation',status='NEEDS_CONFIRMATION')
        if d.intent=='reject':
            state=self.sessions.get(tv)
            if not state.selected_content:return self._ask(tv,'你指的是哪一部？请说具体片名或序号，我不会自动替你更换播放。')
            rows=self.sessions.reject_selected(tv)
            return self._list(tv,rows,'reject')
        command,selected=self._command(tv,d)
        # Reparse the generated finite command. No model-provided shell or free-form plan.
        parsed=parse_request(command,self.cfg)
        if parsed.action=='unknown':raise ValueError('unparseable canonical command')
        # Playing always changes what the viewer sees, so both entry points
        # confirm it; Xiaomi also confirms relative actions it may duplicate.
        conflict_confirmation = d.intent in ('play','sequence') or (
            source=='xiaomi_logcat' and d.intent in ('volume_up','volume_down','back','sequence'))
        if d.requires_confirmation or conflict_confirmation:
            nonce=secrets.token_hex(16)
            self.sessions.set_pending(tv,nonce=nonce,command=command,intent=d.intent,
                                      candidate_id=(selected or {}).get('id'))
            return self._ask(tv,'确认执行 '+command+' 吗？小爱可能也响应同一句话；请明确说“确认”或“不确认”。',
                             'confirmation',status='NEEDS_CONFIRMATION')
        return self._execute(tv,d,command,selected,source)
