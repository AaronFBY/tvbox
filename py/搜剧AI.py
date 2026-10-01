"""
导航:   https://www.qiushui.vip   
           https://www.qiushuitv.cn
           https://www.qiushuiying.cn
由「影视py源生成器」自动生成 2026-09-18 20:39
站点: https://souju.ai
"""
import re
import sys
import os
import json
import time
import hmac
import hashlib
import requests
from urllib.parse import quote

sys.path.append('..')
from base.spider import Spider


DEFAULT_SITES = ['https://souju.ai']
PLAY = 'https://souju.ai'
SECRET = 'f39d73aa7a6426203cdee1ef17b31d3b7ea8c23f4c59c62a3a8aa0f39ee5e79d'
COOKIE = '__guid=195299758.4051754031411531300.1789731057113.5867; ai_movie_home_address_visited_v1=1; ai_movie_browser=brw_5OVOVHdcpmXm_yWLaI3sA0QrHW8HaO3vdYHrkZCzwOA; ai_movie_session=ums_I6vEzK0KN-RVptBjW45BXRcn8AwVvQO6JeZoojYdGWo'
_KINDS = [('latest', '最新更新'), ('movie', '电影'), ('series', '剧集'), ('short_drama', '短剧'), ('anime', '动漫'), ('variety', '综艺'), ('documentary', '纪录片'), ('sports', '体育')]


def _nonce():
    return ''.join('%02x' % b for b in os.urandom(16))


class Spider(Spider):

    def init(self, extend=''):
        hosts = []
        cookie = COOKIE
        self._fallback_direct = False
        if extend:
            try:
                ext = json.loads(extend)
                site = (ext.get('site') or '').strip()
                if site:
                    hosts = [u.strip() if u.startswith('http') else 'https://' + u.strip()
                             for u in site.split(',') if u.strip()]
                cookie = (ext.get('cookie') or cookie or '').strip()
                if str(ext.get('fallback') or '').strip().lower() == 'direct':
                    self._fallback_direct = True
            except Exception:
                pass
        self._hosts = hosts or DEFAULT_SITES[:]
        self._host = self._hosts[0]
        self._secrets = {}
        self._refresh_at = {}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if cookie:
            self.headers['Cookie'] = cookie
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(self.headers)
        self._vod_name = ''
        self._vod_id = ''
        self._ep_idx = {}
        return self

    def getName(self):
        return '搜剧AI'

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return True

    def _secret_of(self, host):
        return self._secrets.get(host) or SECRET

    def _sign(self, method, path, host=None):
        secret = self._secret_of(host or self._host)
        ts = str(int(time.time() * 1000))
        nonce = _nonce()
        msg = '%s\n%s\n%s\n%s' % (method, path, ts, nonce)
        sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            'x-ai-movie-timestamp': ts,
            'x-ai-movie-nonce': nonce,
            'x-ai-movie-signature': sig,
        }

    def _host_cycle(self):
        return [self._host] + [h for h in self._hosts if h != self._host]

    def _refresh_secret(self, host=None):
        host = host or self._host
        now = int(time.time())
        if now - self._refresh_at.get(host, 0) < 300:
            return False
        self._refresh_at[host] = now
        try:
            hd = dict(self.headers)
            hd['Referer'] = host + '/'
            html = self.session.get(host + '/', headers=hd, timeout=15, verify=False)
            m = re.search(r'(?:src|href)="([^"]*movie-card[^"]*\.js[^"]*)"', html.text)
            if m:
                js = self.session.get(host + m.group(1), headers=hd, timeout=15, verify=False)
                m2 = re.search(r'x-ai-movie-signature"[^"]*"([0-9a-f]{64})"', js.text)
                if m2:
                    self._secrets[host] = m2.group(1)
                    return True
        except Exception:
            pass
        return False

    def _get_json(self, path):
        for host in self._host_cycle():
            for attempt in range(2):
                hd = dict(self.headers)
                hd['Referer'] = host + '/'
                hd.update(self._sign('GET', path, host))
                try:
                    r = self.session.get(host + path, headers=hd, timeout=15, verify=False)
                except Exception:
                    break
                if r.status_code == 200:
                    try:
                        self._host = host
                        return r.json()
                    except Exception:
                        return {}
                txt = r.text or ''
                if r.status_code == 401 and 'signature' in txt and attempt == 0:
                    if self._refresh_secret(host):
                        self._host = host
                        continue
                    break
                break
        return {}

    def _post_json(self, path, payload):
        for host in self._host_cycle():
            for attempt in range(2):
                hd = dict(self.headers)
                hd['Referer'] = host + '/'
                hd['Content-Type'] = 'application/json'
                hd.update(self._sign('POST', path, host))
                try:
                    r = requests.post(host + path, json=payload, headers=hd, timeout=15, verify=False)
                except Exception:
                    break
                if r.status_code in [200, 201]:
                    try:
                        self._host = host
                        return r.json()
                    except Exception:
                        return {}
                txt = r.text or ''
                if r.status_code == 401 and 'signature' in txt and attempt == 0:
                    if self._refresh_secret(host):
                        self._host = host
                        continue
                    break
                break
        return {}

    def _to_vod(self, card, tid=''):
        year = card.get('year', '')
        return {
            'vod_id': card.get('id', ''),
            'vod_name': card.get('title', ''),
            'vod_pic': (card.get('poster_url') or '').replace('&amp;', '&'),
            'vod_remarks': card.get('remarks', ''),
            'vod_year': str(year) if year else '',
            'type_id': tid or card.get('content_kind', ''),
        }

    def homeContent(self, filter):
        classes = [{'type_id': t, 'type_name': n} for t, n in _KINDS]
        videos = []
        try:
            j = self._get_json('/v1/feed/home')
            for sec in j.get('sections', []):
                for card in sec.get('cards', [])[:20]:
                    v = self._to_vod(card)
                    if v['vod_id']:
                        videos.append(v)
        except Exception:
            pass
        filters = {}
        for tid, tname in _KINDS:
            filters[tid] = [
                {'key': 'sort', 'name': '排序', 'value': [{'n': '最新', 'v': 'latest'}, {'n': '最热', 'v': 'heat_desc'}]},
                {'key': 'year', 'name': '年份', 'value': [{'n': '全部', 'v': ''}] + [{'n': str(y), 'v': str(y)} for y in range(2026, 2009, -1)]},
            ]
        return {'class': classes, 'list': videos[:24], 'filters': filters}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg) if str(pg).isdigit() and int(pg) > 0 else 1
        except Exception:
            pg = 1
        try:
            if tid == 'latest':
                params = 'intent=latest_catalog&page=%d&limit=30' % pg
            else:
                params = 'kind=%s&intent=latest_catalog&page=%d&limit=30' % (tid, pg)
            ext = extend or {}
            sort = ext.get('sort', '') if isinstance(ext, dict) else ''
            year = ext.get('year', '') if isinstance(ext, dict) else ''
            if sort and sort != 'latest':
                params += '&sort=' + sort
            if year:
                params += '&year=' + year
            j = self._get_json('/v1/browse/catalog?' + params)
            cards = j.get('cards', [])
            pageinfo = j.get('pagination', {})
            return {
                'list': [self._to_vod(c, tid) for c in cards],
                'page': pg,
                'pagecount': int(pageinfo.get('next_page', 1) or 1),
                'limit': 30,
                'total': int(pageinfo.get('total', 0) or 0),
            }
        except Exception:
            return {'list': [], 'page': pg, 'pagecount': 1, 'limit': 30, 'total': 0}

    def _fetch_episodes(self, cid):
        eps = []
        offset = 0
        try:
            while offset < 2400:
                path = '/v1/catalog/%s/episodes?offset=%d&limit=48' % (quote(cid), offset)
                j = self._get_json(path)
                if not j:
                    break
                current_eps = j.get('episodes', [])
                if not current_eps:
                    break
                eps.extend(current_eps)
                pg = j.get('episode_pagination', {})
                if not pg.get('has_more'):
                    break
                if pg.get('next_offset'):
                    offset = int(pg['next_offset'])
                else:
                    offset += pg.get('returned_count', len(current_eps))
        except Exception:
            pass
        return eps

    def _resolve(self, token, line=None):
        try:
            path = '/v1/playback/resolve/' + quote(token)
            if line:
                path += '?ps=%s&provider_id=%s&pf=%s' % (
                    quote(line.get('playback_source_id', '')),
                    quote(line.get('provider_id', '')),
                    quote(line.get('play_from', '')),
                )
            return self._get_json(path)
        except Exception:
            return {}

    def _fetch_lines(self, eps, cid):
        lines = []
        try:
            token = eps[0].get('token') if eps else cid
            j = self._resolve(token)
            for ln in j.get('line_options', []):
                uk = ln.get('url_kind')
                rm = ln.get('resolve_mode')
                if uk == 'resolve_ticket':
                    lines.append(ln)
                    continue
                if rm == 'direct' and uk in ('m3u8', 'mp4', 'flv') \
                        and str(ln.get('url') or '').startswith('http'):
                    lines.append(ln)
                    continue
        except Exception:
            pass
        return lines

    def detailContent(self, ids):
        aid = ids[0] if isinstance(ids, (list, tuple)) and ids else str(ids)
        cid = aid.split('/')[-1] if '/' in aid else aid
        j = self._get_json('/v1/catalog/' + quote(cid))
        vod = {}
        try:
            year = j.get('year', '')
            vod['vod_id'] = j.get('id', cid)
            vod['vod_name'] = j.get('title', '')
            vod['vod_pic'] = (j.get('poster_url') or '').replace('&amp;', '&')
            vod['vod_remarks'] = j.get('remarks', '')
            vod['vod_year'] = str(year) if year else ''
            vod['vod_content'] = j.get('description', '')
            vod['vod_actor'] = '/'.join(j.get('actors', []) or [])
            vod['vod_director'] = '/'.join(j.get('directors', []) or [])
            vod['vod_area'] = j.get('area', '')
            self._vod_name = vod.get('vod_name', '')
        except Exception:
            pass
        self._vod_id = vod.get('vod_id') or cid
        eps = self._fetch_episodes(cid)
        if not eps:
            eps = j.get('episodes', []) or []
        self._ep_idx = {}
        items = []
        seen = set()
        for ep in eps:
            title = ep.get('title') or ep.get('display_name') or ''
            token = ep.get('token') or ep.get('path', '').lstrip('/yj/')
            if not title or not token or token in seen:
                continue
            seen.add(token)
            m = re.search(r'(\d+)', title)
            idx = m.group(1) if m else '0'
            self._ep_idx[token] = idx
            items.append((title, token))
        lines = self._fetch_lines(eps, cid)
        play_from = []
        play_url = []
        if lines:
            for ln in lines:
                name = ln.get('display_label') or ln.get('provider_name') or '线路'
                ps = ln.get('playback_source_id', '')
                pid = ln.get('provider_id', '')
                pf = ln.get('play_from', '')
                entries = []
                for title, token in items:
                    entries.append('%s$%s@@%s@@%s@@%s' % (title, token, ps, pid, pf))
                play_from.append(name)
                play_url.append('#'.join(entries))
        if play_from:
            vod['vod_play_from'] = '$$$'.join(play_from)
            vod['vod_play_url'] = '$$$'.join(play_url)
        else:
            vod['vod_play_from'] = '暂无指定线路'
            vod['vod_play_url'] = '空播放地址$' + cid
        return {'list': [vod]}

    def searchContent(self, key, quick, pg='1'):
        try:
            pg = int(pg) if str(pg).isdigit() and int(pg) > 0 else 1
        except Exception:
            pg = 1
        params = 'q=%s&intent=catalog_search&page=%s&limit=30' % (quote(key), pg)
        j = self._get_json('/v1/browse/catalog?' + params)
        cards = j.get('cards', [])
        return {
            'list': [self._to_vod(c) for c in cards],
            'page': pg,
            'pagecount': 1,
            'limit': len(cards),
            'total': len(cards),
        }

    def _match_line(self, line_options, line, name=None):
        if not line:
            line = {}
        pid = (line.get('provider_id') or '').strip()
        pf = (line.get('play_from') or '').strip()
        ps = (line.get('playback_source_id') or '').strip()
        best, best_score = None, 0
        if pid or pf or ps:
            for lo in line_options:
                sc = 0
                if pid and lo.get('provider_id') == pid:
                    sc += 2
                if pf and lo.get('play_from') == pf:
                    sc += 1
                if ps and lo.get('playback_source_id') == ps:
                    sc += 4
                if sc > best_score:
                    best, best_score = lo, sc
            if best is not None:
                return best
        nm = (name or '').strip()
        if nm:
            labels = []
            for lo in line_options:
                labels.append((lo, [str(lo.get(k) or '').strip()
                                    for k in ('display_label', 'provider_name', 'label')]))
            for lo, ls in labels:
                if nm in ls:
                    return lo
            for lo, ls in labels:
                for x in ls:
                    if x and (x in nm or nm in x):
                        return lo
        return None

    def _play_base(self):
        return (PLAY or '').rstrip('/') or (self._host or '').rstrip('/')

    def _site_player_url(self, token, line=None):
        host = (self._host or '').rstrip('/')
        vid = getattr(self, '_vod_id', '') or ''
        if not host or not token:
            return ''
        q = ['episode=' + quote(str(token), safe='')]
        lid = ''
        if line:
            lid = (line.get('playback_source_id') or line.get('play_from')
                   or line.get('provider_id') or '')
        if lid:
            q.append('line=' + quote(str(lid), safe=''))
        q.append('resume=1')
        return '%s/player/%s?%s' % (host, quote(str(vid), safe=''), '&'.join(q))

    def playerContent(self, flag, id, vipFlags=None):
        raw = (id or '').strip()
        parts = raw.split('@@')
        head = parts[0]
        token = head.split('$', 1)[1] if '$' in head else head
        line = None
        if len(parts) == 4:
            line = {
                'playback_source_id': parts[1],
                'provider_id': parts[2],
                'play_from': parts[3],
            }
        try:
            j = self._resolve(token)
            los = j.get('line_options', []) or []
            direct = [lo for lo in los
                      if lo.get('resolve_mode') == 'direct'
                      and lo.get('url_kind') in ('m3u8', 'mp4', 'flv')
                      and str(lo.get('url') or '').startswith('http')]
            selected = self._match_line(los, line, flag)
            if selected is not None:
                su = str(selected.get('url') or '')
                uk = selected.get('url_kind') or ''
                if uk in ('m3u8', 'mp4', 'flv', 'ts', 'mkv') and su.startswith('http'):
                    return self._play_result(token, su)
                if uk == 'resolve_ticket' or su.startswith('resolve://'):
                    rd = self._post_json('/v1/playback/resolve-line',
                                         {'ticket': su.replace('resolve://', '')})
                    if rd and 'line' in rd:
                        lu = rd['line'].get('url', '')
                        lk = rd['line'].get('url_kind', '')
                        if lu.startswith('http') and self._is_playable(lu, lk):
                            return self._play_result(token, lu)
                    if getattr(self, '_fallback_direct', False):
                        alt = self._pick_direct(direct)
                        if alt:
                            return self._play_result(token, alt)
                    return self._site_result(token, line)
            if direct:
                url = self._pick_direct(direct)
                if url:
                    return self._play_result(token, url)
            for lo in los:
                if lo.get('url_kind') == 'resolve_ticket':
                    rd = self._post_json('/v1/playback/resolve-line',
                                         {'ticket': str(lo.get('url') or '').replace('resolve://', '')})
                    if rd and 'line' in rd:
                        lu = rd['line'].get('url', '')
                        lk = rd['line'].get('url_kind', '')
                        if lu.startswith('http') and self._is_playable(lu, lk):
                            return self._play_result(token, lu)
        except Exception:
            pass
        return self._site_result(token, line, fallback_id=id)

    def _site_result(self, token, line=None, fallback_id=None):
        u = self._site_player_url(token, line)
        if not u:
            pb = self._play_base()
            u = (pb + '/player/' + quote(str(token or ''), safe='')) if (pb and token) else (fallback_id or '')
        return {
            'parse': 1,
            'url': u,
            'header': {},
            'danmaku': self._build_danmaku(token),
        }

    def _pick_direct(self, direct):
        order = ["豪华", "红牛", "速播", "暴风", "新浪", "ikun", "量子", "牛牛",
                 "非凡", "极速", "最大", "无水印", "豆瓣", "360", "猫眼", "金鹰"]
        cands = []
        for kw in order:
            for lo in direct:
                name = lo.get('display_label') or lo.get('provider_name') or ''
                if kw in name:
                    u = lo.get('url')
                    if u and u not in cands:
                        cands.append(u)
        for lo in direct:
            u = lo.get('url')
            if u and u not in cands:
                cands.append(u)
        for u in cands:
            if u and u.startswith('http') and self._probe_ok(u):
                return u
        return cands[0] if cands else ''

    def _probe_ok(self, url, timeout=4):
        try:
            r = requests.get(url, headers={
                'User-Agent': self.headers['User-Agent'],
                'Range': 'bytes=0-4096',
            }, timeout=timeout, verify=False, allow_redirects=True, stream=True)
            code = r.status_code
            if code not in (200, 206):
                r.close()
                return False
            head = r.raw.read(4096) or b''
            r.close()
            low = head.lstrip().lower()
            if low.startswith(b'<!doctype') or low.startswith(b'<html'):
                return False
            return True
        except Exception:
            return False

    def _is_playable(self, url, kind):
        if not url or not url.startswith('http'):
            return False
        kind = (kind or '').lower()
        if kind in ('m3u8', 'mp4', 'flv', 'ts', 'webm', 'mkv', 'mp3', 'aac', 'm4a', 'resolve_ticket', 'unknown'):
            return True
        if url.lower().split('?')[0].endswith(('.m3u8', '.mp4', '.flv', '.ts', '.webm', '.mkv', '.mp3', '.aac', '.m4a')):
            return True
        return False

    def _play_result(self, token, url):
        return {
            'parse': 0,
            'url': url,
            'header': {'User-Agent': self.headers['User-Agent'],
                       'Referer': self._play_base() + '/'},
            'danmaku': self._build_danmaku(token),
        }

    def _build_danmaku(self, token):
        try:
            vod_name = self._vod_name or ''
            vod_name = re.sub(r'[\u300a\u300b\u3008\u3009\u300c\u300d\u300e\u300f\uff08\uff09\(\)\[\]\{\}]', '', vod_name)
            vod_name = re.sub(r'(在线观看|在线播放|免费播放|免费观看|高清播放|高清在线|完整版|全集|电视剧|电影|免费|高清|播放|观看|全集免费|在线|影院)$', '', vod_name)
            vod_name = re.sub(r'[-\s]+$', '', vod_name).strip()
            idx = self._ep_idx.get(token, '1')
            m = re.search(r'(\d+)', idx)
            vod_index = m.group(1) if m else '1'
            return 'http://127.0.0.1:9978/proxy?do=appdanmu&vodName=%s&vodIndex=%s' % (quote(vod_name or ''), vod_index)
        except Exception:
            return ''

    def localProxy(self, param):
        return [200, {}, '']

    def destroy(self):
        try:
            self.session.close()
        except Exception:
            pass


import requests as _rq
from urllib.parse import urlparse as _up


def _pow_params(text):
    if not text:
        return None
    if not ("正在验证您的浏览器" in text or "__cdn_pow" in text):
        return None
    import re as _re
    m = _re.search(r'var\s+TS\s*=\s*"([^"]+)"\s*,\s*SIG\s*=\s*"([^"]+)"\s*,'
                   r'\s*DIFF\s*=\s*"([^"]+)"\s*(?:,\s*MODE\s*=\s*"([^"]*)")?',
                   text)
    if not m:
        return None
    mc = _re.search(r'var\s+POW\s*=\s*"([^"]+)"', text)
    return {"ts": m.group(1), "sig": m.group(2),
            "diff": m.group(3) or "0000", "mode": m.group(4) or "auto",
            "cookie": mc.group(1) if mc else "__cdn_pow"}


def _pow_solve(sig, diff, mx=4000000):
    import hashlib as _hl
    import time as _t
    t0 = _t.time()
    n = 0
    while n < mx:
        if _hl.sha256((sig + str(n)).encode()).hexdigest().startswith(diff):
            return n
        n += 1
        if n % 50000 == 0 and _t.time() - t0 > 8:
            return None
    return None


def _cdndefend_params(text):
    if not text or "cdndefend" not in text:
        return None
    import re as _re
    mh = _re.search(r"['\"]([0-9a-fA-F]{40})['\"]", text)
    mk = _re.search(r"['\"]([A-Za-z_][A-Za-z0-9_]*=)['\"]", text)
    if not mh or not mk:
        return None
    mt = _re.search(r"s\[n1\]\s*===\s*0x([0-9a-fA-F]{1,2})\s*&&\s*"
                    r"s\[n1\+0x1\]\s*===\s*0x([0-9a-fA-F]{1,2})", text)
    hexv = mh.group(1)
    try:
        n1 = int(hexv[0], 16)
    except Exception:
        n1 = 0
    return {"hex": hexv, "cookie": mk.group(1),
            "n1": n1,
            "b0": int(mt.group(1), 16) if mt else 0xb0,
            "b1": int(mt.group(2), 16) if mt else 0x0b}


def _cdndefend_solve(p, mx=3000000):
    import hashlib as _hl
    import time as _t
    t0 = _t.time()
    n = 0
    while n < mx:
        d = _hl.sha1((p["hex"] + str(n)).encode()).digest()
        if d[p["n1"]] == p["b0"] and d[p["n1"] + 1] == p["b1"]:
            return n
        n += 1
        if n % 50000 == 0 and _t.time() - t0 > 8:
            return None
    return None


try:
    class _PowSession(_rq.Session):

        def request(self, method, url, *a, **kw):
            r = _rq.Session.request(self, method, url, *a, **kw)
            try:
                txt = r.text or ""
            except Exception:
                return r
            for _i in range(2):
                p = _pow_params(txt)
                if p:
                    n = _pow_solve(p["sig"], p["diff"])
                    if n is None:
                        break
                    self.cookies.set(p["cookie"],
                                     "%s_%s_%s_%s" % (p["ts"], p["mode"], n, p["sig"]),
                                     domain=_up(url).netloc, path="/")
                else:
                    q = _cdndefend_params(txt)
                    if not q:
                        break
                    n = _cdndefend_solve(q)
                    if n is None:
                        break
                    self.cookies.set(q["cookie"].rstrip("="),
                                     q["hex"] + str(n),
                                     domain=_up(url).netloc, path="/")
                r = _rq.Session.request(self, method, url, *a, **kw)
                try:
                    txt = r.text or ""
                except Exception:
                    break
            return r
except Exception:
    _PowSession = _rq.Session


import re as _ndre

_NETDISK_DOM = (r"pan\.quark\.cn|quark\.cn|pan\.xunlei\.com|xlshare|"
                r"pan\.baidu\.com|115\.com|anxia\.com|alipan\.com|aliyundrive\.com|"
                r"drive\.uc\.cn|fast\.uc\.cn|"
                r"123pan\.com|123865\.com|123684\.com|wopan189\.cn|"
                r"cloud\.189\.cn|caiyun\.139\.cn|pikpako|mypikpak\.com|lanzou[a-z]*\.com")

_NETDISK_RX = _ndre.compile(r"^https?://[\w.-]*(?:" + _NETDISK_DOM + r")", _ndre.I)

_NETDISK_FIND_RX = _ndre.compile(
    r"https?://[\w.-]*(?:" + _NETDISK_DOM + r")[^\s#'\"$@,;|<>\\]*", _ndre.I)

_NETDISK_LINE_RX = _ndre.compile(
    r"网盘|云盘|夸克|百度|迅雷|UC|阿里|天翼|移动云|蓝奏|123|115", _ndre.I)


def _netdisk_ua(sp):
    for _a in ("ua", "UA"):
        _v = getattr(sp, _a, None)
        if _v:
            return _v
    _h = getattr(sp, "headers", None)
    if isinstance(_h, dict):
        _v = _h.get("User-Agent") or _h.get("user-agent")
        if _v:
            return _v
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _netdisk_push_pan(url):
    try:
        import requests as _ndrq
        _ndrq.post("http://127.0.0.1:9978/action",
                   data={"do": "push", "url": url},
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   timeout=3)
    except Exception:
        pass


def _netdisk_extract(text):
    _s = str(text or "")
    if not _s:
        return ""
    for _seg in _ndre.split(r"[#$]", _s):
        _seg = _seg.strip()
        if _NETDISK_RX.match(_seg):
            _m = _NETDISK_FIND_RX.match(_seg)
            if _m:
                return _m.group(0)
    return ""


def _netdisk_push_all(text):
    if not text:
        return
    _seen = set()
    for _u in _NETDISK_FIND_RX.findall(str(text)):
        if _u and _u not in _seen:
            _seen.add(_u)
            _netdisk_push_pan(_u)


def _netdisk_page_link(text):
    _t = str(text or "").replace("\\/", "/")
    _m = _NETDISK_FIND_RX.search(_t)
    return _m.group(0) if _m else ""


def _netdisk_page_of(text):
    _m = _ndre.search(r"https?://[^\s'\"<>\\]+", str(text or ""))
    if not _m:
        return ""
    return _m.group(0).rstrip("$,#").strip()


def _netdisk_http_get(sp, url):
    for _a in ("session", "sess", "_sess"):
        _s = getattr(sp, _a, None)
        if _s is not None and hasattr(_s, "get"):
            try:
                _r = _s.get(url, timeout=15, verify=False)
                _t = getattr(_r, "text", "") or ""
                if _t:
                    return _t
            except Exception:
                pass
    try:
        import requests as _ndrq
        _h = {}
        try:
            _h = dict(getattr(sp, "headers", {}) or {})
        except Exception:
            _h = {}
        return _ndrq.get(url, headers=_h, timeout=15, verify=False).text or ""
    except Exception:
        return ""


def _netdisk_push(url, ua):
    _u = _netdisk_extract(url)
    if not _u:
        return None
    _netdisk_push_pan(_u)
    return {"parse": 0, "playUrl": "", "url": _u,
            "header": {"User-Agent": ua or ""}}


_nd_orig = getattr(Spider, "playerContent", None)
if _nd_orig is not None and not getattr(Spider, "_netdisk_hooked", False):
    def playerContent(self, flag, id, vipFlags=None):
        _got = _netdisk_push(id, _netdisk_ua(self))
        if _got:
            return _got
        _page = _netdisk_page_of(id)
        if _page and not _NETDISK_RX.match(_page) \
                and _NETDISK_LINE_RX.search(str(flag or "")):
            _link = _netdisk_page_link(_netdisk_http_get(self, _page))
            if _link:
                _got = _netdisk_push(_link, _netdisk_ua(self))
                if _got:
                    return _got
        return _nd_orig(self, flag, id, vipFlags)

    Spider.playerContent = playerContent
    Spider._netdisk_hooked = True


_nd_orig_detail = getattr(Spider, "detailContent", None)
if _nd_orig_detail is not None and not getattr(Spider, "_netdisk_detail_hooked", False):
    def detailContent(self, ids):
        _res = _nd_orig_detail(self, ids)
        try:
            for _v in ((_res or {}).get("list") or []):
                if isinstance(_v, dict):
                    _netdisk_push_all(_v.get("vod_play_url") or "")
        except Exception:
            pass
        return _res

    Spider.detailContent = detailContent
    Spider._netdisk_detail_hooked = True