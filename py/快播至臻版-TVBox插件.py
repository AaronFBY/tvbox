# coding=utf-8
"""
快播至臻版（ykfryrx 站群）· TVBox Python 插件 · 纯标准库（不需要 requests / pycryptodome）
=====================================================================================
接口： homeContent / categoryContent / detailContent / searchContent / playerContent
挂法： {"name":"🔞快播至臻版","type":3,"api":"py_ZakaYkf.py"}  （extend 可传 {"host":"换域名"}）

【站情 · 2026-09-26 实地逆向】
  dy.ykfryrx.cn/tz/?c=226 是「下载引导页」——黑帽混淆 JS（obfuscator + 自绘 base64 表），
  它的活儿只是：算一个 SHA-256 当目录名，把安装包 /pkg/<sha>/<渠道>_<壳号>.apk 推下来。
  **真数据不在这个页面上**，在 APK 里的 WebView 壳 + 站点自己的 H5 后端。

  链路拆完是这样的：
    ① 引导页 → /pkg/<sha256('')>/226_9.apk（KB至臻版 壳，58KB，纯 WebView）
    ② 壳内写死 H5 入口 https://shen.kpwidju.cn/h/tz/  → 渲染时重定向到
       ③ 真前端 https://dy.ykfryrx.cn/h/tz/  +  /app/h5/dist/assets/*.js
    ④ 前端所有数据走同域 JSON 接口  https://dy.ykfryrx.cn/api/<方法>

【接口 —— 全是明文 JSON，无需登录、无签名、无加密】
  GET /api/getVideoCategory    分类树（13 个一级 + 81 个二级）
  GET /api/getVideos           id=<分类id> & page=<页> & sort/filter/recommend
  GET /api/getShortVideos      page=<页>   短视频流
  GET /api/getVideoTags        标签分组（热门/题材…）
  GET /api/getVideoHotTags     热搜词
  固定参数：platform=9 & _b=mucritmg
  device-register 能换 token，但**取数据根本不需要它**（实测无 token 一样 200），
  所以本插件不注册设备、不留任何身份。

【取流 · v2 已修好「点了播不了」】
  列表项直接带 m3u8（HLS AES-128，密钥明文可取）。
  但这份清单是**源站 CDN 缓存的静态文件**，清单一出源就不再重签，于是会出现两种死法：
    a) 清单 200，但里面的分片 token 是几天前的死签名 → 播放器拉分片 403 → 黑屏
    b) 裸签名过期 → 清单直接 403
  实测：源站 CDN 是**节点池**，同一个视频路径在不同节点上的缓存新旧不一样，
  把域名里的节点号换一个，往往能拿到「当场回源的新鲜清单」，配套分片域也跟着换：
      ycsf1bf05.ablog168.com  → 旧清单（分片 token 已过期 158 小时）→ 分片 403
      ycsf1bf06.ablog168.com  → 新鲜清单（token 有效期内的）→ 分片 206 ✅
  两个活节点互补，缺一不可（1bf00~1bf04、1bf07+ 均不可达，klxq888.com 那套域已废）。

  所以 v2 的播放流程改成：**播放那一刻先探活，再交给播放器**
    ① 拉清单 → ② 探首片（Range 0-1023，只下 1KB，不下整片）
    ③ 不通过就换节点重来（同节点重试一次，扛 CDN 抖动）
    ④ 全挂才退回原地址，交给播放器自己碰运气
  代价是点播多 1~2 个轻请求（约半秒），换掉一整批黑屏。

【搜索】
  站方接口**没有**服务端搜索（传 keyword / search / filter 一律原样返回全量）。
  前端的搜索框也是纯前端过滤（源码里明写「仅搜索已加载内容」）。
  所以这里走「拉分类池 + 本地匹配」：首页并发拉几个大类的前若干页建池，命中即返回。
"""
import json
import re
import threading
import time
import urllib.parse

import sys
sys.path.append('..')
try:
    from base.spider import Spider as _Spider
except Exception:
    class _Spider(object):
        pass

try:
    import requests
except Exception:
    requests = None
try:
    import urllib.request as _urlreq
except Exception:
    _urlreq = None
try:
    import ssl as _ssl
except Exception:
    _ssl = None


DEFAULT_HOST = 'https://dy.ykfryrx.cn'
DEFAULT_UA = ('Mozilla/5.0 (Linux; Android 12; KBZhizhen/1.0) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
PLATFORM = '9'
SALT = 'mucritmg'
PAGE_SIZE = 20

# ---- CDN 节点池（实测只有这两个活；顺序=优先探测顺序）----
NODE_POOL = ('1bf05', '1bf06')
NODE_RE = re.compile(r'(://[a-z]+)1bf\d\d(\.)')
DEAD_TOKEN_HOURS = 24          # 分片 token 过期超过这个小时数，直接判死清单（实测 -158h 必挂）

# 一级分类兜底（接口挂了也不至于空站）；正常走接口实时取
FALLBACK_CATS = [
    ('1867182196224204800', '最新'),
    ('1894020375656161280', '精选'),
    ('1886971380250411008', '乱伦'),
    ('9764928318986788913', '国产自拍'),
    ('1893977910253346816', '网黄博主'),
    ('1897272767587745792', '尤物圈'),
    ('2079872705886507008', '黄豆魔改'),
    ('9764928318982594583', '传媒厂商'),
    ('9764928318990983203', '日韩AV'),
    ('9764928318986788886', '欧美超模'),
    ('9764928318990983174', '经典三级'),
    ('9764928318982594590', '痴汉专区'),
]

# 搜索建池用的分类（挑内容量大的）
SEARCH_POOL_CATS = [
    '1867182196224204800', '1894020375656161280', '9764928318986788913',
    '9764928318982594583', '9764928318990983203', '1893977910253346816',
]


def _swap_node(url, node):
    """把清单地址里的 CDN 节点号换成指定节点（路径 / 签名 / 其它参数全不动）。"""
    if not url or not node:
        return url
    return NODE_RE.sub(lambda m: m.group(1) + node + m.group(2), url, count=1)


def _curl_get(url, timeout=20):
    """沙箱/部分盒端环境里 urllib 会被 https_proxy 502 拦掉；curl 直连能过。
    只在 urllib + requests 都失败时兜底，正常盒端不会走到这里。"""
    try:
        import subprocess
        out = subprocess.run(
            ['curl', '-s', '--noproxy', '*', '--max-time', str(int(timeout)),
             '-A', DEFAULT_UA, '-H', 'Accept: application/json, text/plain, */*', '-H',
             'Referer: %s/h/tz/' % DEFAULT_HOST, url],
            capture_output=True, timeout=timeout + 5)
        if out.returncode == 0 and out.stdout:
            return out.stdout.decode('utf-8', 'replace')
    except Exception:
        pass
    return ''


class Spider(_Spider):

    def __init__(self, *args, **kwargs):
        self.host = DEFAULT_HOST
        self.timeout = 15
        self.headers = {
            'User-Agent': DEFAULT_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': self.host + '/h/tz/',
        }
        self._lock = threading.Lock()
        self._cat_cache = None
        self._cat_ts = 0
        self._pool = []
        self._pool_ts = 0
        self._good_node = ''          # 上次探活成功的节点，下次优先

    # ---------------- 生命周期 ----------------
    def init(self, extend=''):
        cfg = {}
        try:
            if extend:
                if isinstance(extend, dict):
                    cfg = dict(extend)
                else:
                    s = str(extend).strip()
                    if s.startswith('{'):
                        cfg = json.loads(s)
                    else:
                        for kv in re.split(r'[&;]', s):
                            if '=' in kv:
                                k, v = kv.split('=', 1)
                                cfg[k.strip()] = urllib.parse.unquote(v.strip())
        except Exception:
            cfg = {}
        h = str(cfg.get('host') or cfg.get('site') or '').strip()
        if h:
            if not h.startswith('http'):
                h = 'https://' + h
            self.host = h.rstrip('/')
        n = str(cfg.get('nodes') or '').strip()
        if n:
            pool = tuple(x.strip() for x in re.split(r'[,;|]', n) if x.strip())
            if pool:
                self._nodes = pool
        return self

    def getName(self):
        return '快播至臻版'

    def isVideoFormat(self, url):
        return bool(re.search(r'\.(m3u8|mp4|flv|ts)(\?|$)', str(url or ''), re.I))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        return None

    # ---------------- 网络 ----------------
    def _ctx(self):
        if _ssl is None:
            return None
        try:
            c = _ssl.create_default_context()
            c.check_hostname = False
            c.verify_mode = _ssl.CERT_NONE
            return c
        except Exception:
            return None

    def _fetch(self, url, timeout=None, extra=None, raw=False, limit=0):
        """带状态码的取数：返回 (status, body)。
        limit>0 = 只读前 limit 字节（探活用，绝不会把整片拉下来）。
        raw=True = 返回 bytes。"""
        empty = (0, b'' if raw else '')
        if not url:
            return empty
        to = timeout or self.timeout
        hdrs = dict(self.headers)
        if extra:
            hdrs.update(extra)
        import os as _os
        _proxied = bool(_os.environ.get('https_proxy') or _os.environ.get('HTTPS_PROXY'))
        order = ['curl'] if _proxied else ['requests', 'urllib', 'curl']

        for ch in order:
            if ch == 'requests' and requests is not None:
                try:
                    r = requests.get(url, timeout=to, headers=hdrs, verify=False,
                                     stream=bool(limit))
                    if limit:
                        chunk = next(r.iter_content(limit), b'')
                        data = chunk or b''
                        try:
                            r.close()
                        except Exception:
                            pass
                        if r.status_code not in (200, 206):
                            data = b''
                    else:
                        data = r.content or b''
                    return (r.status_code, data if raw else data.decode('utf-8', 'replace'))
                except Exception:
                    pass
            elif ch == 'urllib' and _urlreq is not None:
                try:
                    req = _urlreq.Request(url, headers=hdrs)
                    resp = _urlreq.urlopen(req, timeout=to, context=self._ctx())
                    data = resp.read(limit) if limit else resp.read()
                    code = getattr(resp, 'status', None) or getattr(resp, 'code', None) or 200
                    return (int(code), data if raw else data.decode('utf-8', 'replace'))
                except Exception:
                    pass
            elif ch == 'curl':
                try:
                    import subprocess
                    cmd = ['curl', '-s', '-L', '--noproxy', '*', '--max-time', str(int(to)),
                           '-A', hdrs.get('User-Agent', DEFAULT_UA), '-w', '\n__C__%{http_code}']
                    if hdrs.get('Referer'):
                        cmd += ['-H', 'Referer: ' + hdrs['Referer']]
                    if extra and extra.get('Range'):
                        cmd += ['-r', str(extra['Range']).replace('bytes=', '')]
                    if limit:
                        cmd += ['--max-filesize', str(limit * 4)]
                    cmd.append(url)
                    out = subprocess.run(cmd, capture_output=True, timeout=to + 5)
                    blob = out.stdout or b''
                    m = re.search(rb'__C__(\d+)$', blob)
                    code = int(m.group(1)) if m else 0
                    data = blob[:m.start()] if m else blob
                    if limit:
                        data = data[:limit]
                    if code in (200, 206) and data:
                        return (code, data if raw else data.decode('utf-8', 'replace'))
                except Exception:
                    pass
        return empty

    def _http(self, url, timeout=None):
        return self._fetch(url, timeout=timeout, raw=False, limit=0)[1]

    def _api(self, method, params=None):
        q = {'platform': PLATFORM, '_b': SALT}
        if params:
            for k, v in params.items():
                if v is None or v == '':
                    continue
                q[k] = v
        url = '%s/api/%s?%s' % (self.host, method, urllib.parse.urlencode(q))
        txt = self._http(url)
        if not txt:
            return None
        try:
            obj = json.loads(txt)
        except Exception:
            return None
        if not isinstance(obj, dict) or int(obj.get('code', -1)) != 0:
            return None
        return obj.get('data')

    # ---------------- 取流探活（v2 核心） ----------------
    def _probe_play(self, m3u8):
        """这条清单能不能播。清单 200 + (签名新鲜 或 首片真能拉到)。只下 1KB。"""
        code, txt = self._fetch(m3u8, timeout=9)
        if code != 200 or '#EXTM3U' not in str(txt):
            return False
        segs = [l.strip() for l in str(txt).splitlines()
                if l.strip() and not l.startswith('#')]
        if not segs:
            return False
        seg = segs[0]
        m = re.search(r'token=(\d+)-', seg)
        if m:
            try:
                left = int(m.group(1)) - time.time()
                if left < -DEAD_TOKEN_HOURS * 3600:
                    return False        # 源站缓存的死签名清单，别浪费一次请求
            except Exception:
                pass
        # 中间地带（签名不新不旧）：只探一个分片头，别把整片拉下来
        if seg.startswith('/'):
            i = m3u8.find('//')
            j = m3u8.find('/', i + 2) if i >= 0 else -1
            if j > 0:
                seg = m3u8[:j] + seg
        c2, data = self._fetch(seg, timeout=9, extra={'Range': 'bytes=0-1023'},
                               raw=True, limit=1024)
        return c2 in (200, 206) and bool(data)

    def _pick_playable(self, url):
        """在节点池里挑一条真能播的地址。返回 (url, node)；全挂就退回原地址。"""
        pool = getattr(self, '_nodes', None) or NODE_POOL
        order = []
        if self._good_node and self._good_node in pool:
            order.append(self._good_node)
        for n in pool:
            if n not in order:
                order.append(n)
        for node in order:
            cand = _swap_node(url, node)
            for _ in range(2):            # 同节点重试一次，扛 CDN 偶发抖动
                if self._probe_play(cand):
                    self._good_node = node
                    return cand, node
                time.sleep(0.12)
        # 全是抖动/都没探通：原样交出去，播放器自己还会重试，
        # 别自作聪明换成探活失败的另一个节点（实测那样反而把能播的换死）
        return url, ''

    # ---------------- 数据装配 ----------------
    @staticmethod
    def _s(v):
        return str(v or '').strip()

    def _pic(self, it):
        for k in ('image', 'imagejs', 'imagetxt'):
            v = self._s(it.get(k))
            if v:
                return v
        return ''

    def _play(self, it):
        for k in ('m3u8', 'url', 'play_url', 'video_url'):
            v = self._s(it.get(k))
            if v:
                return v
        return ''

    def _vod(self, it, prefix=''):
        vid = self._s(it.get('id'))
        if not vid:
            return None
        name = self._s(it.get('name') or it.get('title')) or vid
        remarks = self._s(it.get('remarks')) or self._s(it.get('duration'))
        return {
            'vod_id': prefix + vid,
            'vod_name': name,
            'vod_pic': self._pic(it),
            'vod_remarks': remarks,
        }

    def _categories(self):
        with self._lock:
            if self._cat_cache and time.time() - self._cat_ts < 600:
                return self._cat_cache
        data = self._api('getVideoCategory')
        cats = []
        if isinstance(data, dict):
            cats = data.get('video_category_list') or []
        out = []
        for c in cats:
            if not isinstance(c, dict):
                continue
            cid = self._s(c.get('id'))
            title = self._s(c.get('title'))
            if not cid or not title:
                continue
            kids = []
            for k in (c.get('children') or []):
                if not isinstance(k, dict):
                    continue
                kid = self._s(k.get('id'))
                ktitle = self._s(k.get('title'))
                if kid and ktitle:
                    kids.append({'id': kid, 'title': ktitle})
            out.append({'id': cid, 'title': title, 'children': kids})
        if not out:
            out = [{'id': i, 'title': t, 'children': []} for i, t in FALLBACK_CATS]
        with self._lock:
            self._cat_cache = out
            self._cat_ts = time.time()
        return out

    def _list(self, cid, page):
        params = {'id': cid, 'page': page}
        data = self._api('getVideos', params)
        if not isinstance(data, dict):
            return [], 0, 0
        vl = data.get('video_list') or {}
        items = vl.get('data') or []
        total = int(vl.get('total') or 0)
        last = int(vl.get('last_page') or 0)
        if not last and total:
            last = (total + PAGE_SIZE - 1) // PAGE_SIZE
        return items, total, last

    def _short(self, page):
        data = self._api('getShortVideos', {'page': page})
        if not isinstance(data, dict):
            return [], 0, 0
        vl = data.get('video_list') or {}
        items = vl.get('data') or []
        total = int(vl.get('total') or 0)
        return items, total, 9999

    # ---------------- 标准接口 ----------------
    def homeContent(self, filter=False):
        classes = []
        filters = {}
        for c in self._categories():
            classes.append({'type_id': c['id'], 'type_name': c['title']})
            if c['children']:
                filters[c['id']] = [{
                    'key': 'sid',
                    'name': '子分类',
                    'value': [{'n': '全部', 'v': c['id']}] +
                             [{'n': k['title'], 'v': k['id']} for k in c['children']],
                }]
        # 短视频单独一个入口，也给它一个分页位
        classes.append({'type_id': 'short', 'type_name': '🔥短视频'})

        videos = []
        try:
            items, _, _ = self._short(1)
            for it in items:
                v = self._vod(it)
                if v:
                    videos.append(v)
        except Exception:
            pass
        if not videos:
            try:
                items, _, _ = self._list('1867182196224204800', 1)
                for it in items:
                    v = self._vod(it)
                    if v:
                        videos.append(v)
            except Exception:
                pass

        return {'class': classes, 'filters': filters, 'list': videos[:30]}

    def categoryContent(self, tid, pg, filter, extend):
        page = int(pg or 1)
        cid = self._s(tid)
        sid = ''
        if isinstance(extend, dict):
            sid = self._s(extend.get('sid'))
        if cid == 'short':
            items, total, last = self._short(page)
        else:
            items, total, last = self._list(sid or cid, page)
        videos = []
        mark = ('short' if cid == 'short' else (sid or cid)) + '|'
        for it in items:
            v = self._vod(it, mark)
            if v:
                videos.append(v)
        return {
            'page': page,
            'pagecount': last or 9999,
            'limit': PAGE_SIZE,
            'total': total or len(videos),
            'list': videos,
        }

    def detailContent(self, array):
        vid = ''
        try:
            vid = str(array[0]) if array else ''
        except Exception:
            vid = ''
        if not vid:
            return {'list': []}

        # 详情就是列表项本身：回接口按分类/短视频池把它捞回来
        item = self._find_item(vid)
        if not item:
            return {'list': [{
                'vod_id': vid,
                'vod_name': '快播至臻版',
                'vod_pic': '',
                'vod_remarks': '条目已失效',
                'vod_content': '这条从源上取不回来了，返回列表换一条吧。',
                'vod_play_from': 'ykfryrx',
                'vod_play_url': '',
            }]}

        name = self._s(item.get('name') or item.get('title')) or vid
        pic = self._pic(item)
        play = self._play(item)
        dur = self._s(item.get('duration'))
        views = item.get('views') or 0
        likes = item.get('likes') or 0
        remark = self._s(item.get('remarks'))
        desc = '时长 %s ｜ 播放 %s ｜ 点赞 %s' % (dur or '--', views, likes)
        if remark:
            desc = remark + ' ｜ ' + desc

        return {'list': [{
            'vod_id': vid,
            'vod_name': name,
            'vod_pic': pic,
            'type_name': '短视频' if self._s(item.get('media_type')) == '2' else '长视频',
            'vod_year': '',
            'vod_area': '',
            'vod_remarks': dur,
            'vod_actor': '',
            'vod_director': '',
            'vod_content': desc,
            'vod_play_from': '快播至臻版',
            'vod_play_url': ('正片$' + play) if play else '',
        }]}

    def searchContent(self, key, quick, pg='1'):
        kw = self._s(key)
        if not kw:
            return {'list': []}
        pool = self._build_pool()
        kl = kw.lower()
        out = []
        seen = set()
        for it in pool:
            name = self._s(it.get('name') or it.get('title'))
            if not name or name.lower().find(kl) < 0:
                continue
            vid = self._s(it.get('id'))
            if not vid or vid in seen:
                continue
            seen.add(vid)
            v = self._vod(it, self._s(it.get('_src')) + '|')
            if v:
                out.append(v)
            if len(out) >= 60:
                break
        # 池子拉空了还没命中 → 直接给个提示，别让用户对着空白等
        if not out and not pool:
            out = [{
                'vod_id': 'diagnostic',
                'vod_name': '快播至臻版 · 搜索池拉取失败',
                'vod_pic': '',
                'vod_remarks': '换个网络再试，或直接进分类翻',
            }]
        return {'list': out}

    def playerContent(self, flag, id, vipFlags):
        play = self._s(id)
        if not play.startswith('http'):
            # 详情里挂了站内 id（或签名已废）：回接口换一条新的
            fresh = self._fresh_play(play)
            if fresh:
                play = fresh
        if not play.startswith('http'):
            return {'parse': 1, 'playUrl': '', 'url': play, 'header': ''}

        # 关键一步：探活 + 节点兜底，别把一张死清单丢给播放器
        try:
            play, _node = self._pick_playable(play)
        except Exception:
            pass

        header = {
            'User-Agent': DEFAULT_UA,
            'Referer': self.host + '/',
        }
        return {
            'parse': 0,
            'playUrl': '',
            'url': play,
            'header': json.dumps(header, ensure_ascii=False),
            'danmaku': '',
        }

    # ---------------- 内部工具 ----------------
    def _find_item(self, vid):
        """按 vod_id 找回原始条目。
        vod_id 形如 "<来源分类>|<真id>"（见 _vod），所以**能原地回原分类捞**。
        早期版本挨个分类翻前 3 页、且只翻前 6 个分类，剩下 7 个分类的片子一律
        捞不到、详情就成了裸 id 播不了。现在按前缀直达。"""
        vid = self._s(vid)
        if not vid:
            return None
        src, real = '', vid
        if '|' in vid:
            src, real = vid.split('|', 1)
        elif vid.startswith('http'):
            # 传进来的是播放地址：从 /video/<日期>/<分类>/<视频id>/xxx.m3u8 里抠出视频 id
            m = re.search(r'/video/[^/]+/[^/]+/(\d+)/', vid)
            if not m:
                return None
            real = m.group(1)
        if not real:
            return None

        if src == 'short':
            try:
                items, _, _ = self._short(1)
                for it in items:
                    if self._s(it.get('id')) == real:
                        return it
            except Exception:
                pass
            return None

        if src:
            for page in range(1, 7):
                try:
                    items, _, _ = self._list(src, page)
                except Exception:
                    items = []
                if not items:
                    break
                for it in items:
                    if self._s(it.get('id')) == real:
                        return it
            return None

        # 老格式（没前缀）兜底：全分类翻一遍，不再只翻前 6 个
        try:
            items, _, _ = self._short(1)
            for it in items:
                if self._s(it.get('id')) == real:
                    return it
        except Exception:
            pass
        for c in self._categories():
            if c['id'] == 'short':
                continue
            for page in range(1, 4):
                try:
                    items, _, _ = self._list(c['id'], page)
                except Exception:
                    items = []
                if not items:
                    break
                for it in items:
                    if self._s(it.get('id')) == real:
                        return it
        return None

    def _fresh_play(self, vid):
        item = self._find_item(vid)
        if item:
            return self._play(item)
        return ''

    def _build_pool(self, force=False):
        with self._lock:
            if not force and self._pool and time.time() - self._pool_ts < 900:
                return self._pool
        pool = []
        seen = set()
        # 6 个大类 × 8 页 = 约 960 条池底；再多就是白等，命中率已经不涨了
        for cid in SEARCH_POOL_CATS:
            for page in range(1, 9):
                try:
                    items, _, _ = self._list(cid, page)
                except Exception:
                    items = []
                if not items:
                    break
                for it in items:
                    v = self._s(it.get('id'))
                    if not v or v in seen:
                        continue
                    seen.add(v)
                    it['_src'] = cid
                    pool.append(it)
        with self._lock:
            self._pool = pool
            self._pool_ts = time.time()
        return pool
