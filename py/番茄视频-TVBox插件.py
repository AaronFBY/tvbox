# coding=utf-8
"""
番茄视频（wieuc 站群 · site_id=107）· TVBox Python 插件 · 纯标准库自实现
（手写 AES-CBC + HMAC，手机壳里没 pycryptodome / requests 也能跑，有则自动走库）
=====================================================================================
入口： https://mjfdtn.459002.coupons:8283/?channel=Onerun5-045-fq
挂法： {"name":"🔞番茄视频┃免登入直取","type":3,"api":"番茄视频-TVBox插件.py"}
      extend 可传 {"api":"换API域","site":107}

【站情 · 2026-09-27 实地逆向】
  入口是 APISIX 网关后面的加密壳 SPA：首页 HTML 里只有一句 window.CONFIG='gAAAAAB…'
  （Fernet 令牌），真配置解密后才出来。实测解出：
    site_id=107  site_name=番茄视频  channel_id=5466  channel=Onerun5-045-fq
    api_url=sm-api.wieuc.com        public_url=sh-static.wieuc.com
    video_img_url=hm-img.hdstfb.com
    线路表：线路1/线路2 = hm-vip.hdstfb.com + hm-img.hdstfb.com；海外专线 = hm-img.aa66cc.live
    （VIP线路 sort=1000 主机与线路1 完全相同，同源，不重复给）

【VIP / DRM / 金币 —— 实测结论：接口层没有门，全量直取】
  ① 免登入：不注册、不登录、不买会员，分类 / 列表 / 详情 / 搜索 / 播放 / 预览全部直出。
     游客身份打 /api/vod/video?site_id=107 就是完整片库（实测 total=106019）。
  ② 付费片（is_paid=1，price 3.00 之类）**照样带 play_url + down_url** ——
     站方收费只走 App 内购弹窗，接口层没鉴权门，播放地址明文给。
  ③ 金币/会员只是**展示数据**：CONFIG 里 vip_combo=[]、perfect_vip_combo=[]（本站没上会员套餐），
     只有 point_combo（充 56/106/206… 换点数）和 events（完美VIP折扣 188），跟取片无关。
  ④ 播放链不是 DRM：HLS 标准 AES-128（#EXT-X-KEY:METHOD=AES-128,URI="enc.key"），
     enc.key 明文 16 字节、分片名伪装成 abc000.png；清单/密钥/分片全都不校验 Referer、
     不要签名、不要 cookie（三条线路逐一实测 200）。

【信封钥匙】
  Fernet（AES-128-CBC + HMAC-SHA256，标准结构 0x80|8B ts|16B iv|密文|32B hmac），
  密钥 key[0:16]=签名键、key[16:32]=加密键。本插件里 AES 是手写的（查表实现），
  不依赖任何三方库。

【封面】pic 是 Fernet 二次封装：裸取 /source/…/source_jpg.image 拿到的是 gAAAAAB… 信封，
  明文形如 data:image/jpeg;base64,… 再 @@ 后接 base64 续写。壳的图片加载器解不了这层，
  所以统一走本机回环中继：本地取 → 当场解封装 → 按真实 MIME 回给壳（订阅端就有图）。
"""
import base64
import gzip
import hashlib
import hmac
import io
import json
import threading
import time
import urllib.parse
import urllib.request

try:
    import requests
except Exception:
    requests = None

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except Exception:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

try:
    from base.spider import Spider as _Spider
except Exception:
    class _Spider(object):
        pass

# ---------------------------------------------------------------- 站点常量
SITE_NAME = '番茄视频'
SITE_ID = 107
API_HOST = 'sm-api.wieuc.com'
IMG_HOST = 'hm-img.hdstfb.com'
FERNET_KEY = 'NyGRG56A8i5J2JMqh7da83r2MMfgbM7Ppw1aCF8YnAY='
LINE_NAME = ['线路1', '线路2', '海外专线']
LINE_HOST = ['hm-vip.hdstfb.com', 'hm-img.hdstfb.com', 'hm-img.aa66cc.live']
PER = 24
UA = ('Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/131.0.0.0 Mobile Safari/537.36')

# ============================ AES 查表（标准 S 盒，工具生成，勿手改） ============================
_SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
)

_RSBOX = (
    0x52, 0x09, 0x6a, 0xd5, 0x30, 0x36, 0xa5, 0x38, 0xbf, 0x40, 0xa3, 0x9e, 0x81, 0xf3, 0xd7, 0xfb,
    0x7c, 0xe3, 0x39, 0x82, 0x9b, 0x2f, 0xff, 0x87, 0x34, 0x8e, 0x43, 0x44, 0xc4, 0xde, 0xe9, 0xcb,
    0x54, 0x7b, 0x94, 0x32, 0xa6, 0xc2, 0x23, 0x3d, 0xee, 0x4c, 0x95, 0x0b, 0x42, 0xfa, 0xc3, 0x4e,
    0x08, 0x2e, 0xa1, 0x66, 0x28, 0xd9, 0x24, 0xb2, 0x76, 0x5b, 0xa2, 0x49, 0x6d, 0x8b, 0xd1, 0x25,
    0x72, 0xf8, 0xf6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xd4, 0xa4, 0x5c, 0xcc, 0x5d, 0x65, 0xb6, 0x92,
    0x6c, 0x70, 0x48, 0x50, 0xfd, 0xed, 0xb9, 0xda, 0x5e, 0x15, 0x46, 0x57, 0xa7, 0x8d, 0x9d, 0x84,
    0x90, 0xd8, 0xab, 0x00, 0x8c, 0xbc, 0xd3, 0x0a, 0xf7, 0xe4, 0x58, 0x05, 0xb8, 0xb3, 0x45, 0x06,
    0xd0, 0x2c, 0x1e, 0x8f, 0xca, 0x3f, 0x0f, 0x02, 0xc1, 0xaf, 0xbd, 0x03, 0x01, 0x13, 0x8a, 0x6b,
    0x3a, 0x91, 0x11, 0x41, 0x4f, 0x67, 0xdc, 0xea, 0x97, 0xf2, 0xcf, 0xce, 0xf0, 0xb4, 0xe6, 0x73,
    0x96, 0xac, 0x74, 0x22, 0xe7, 0xad, 0x35, 0x85, 0xe2, 0xf9, 0x37, 0xe8, 0x1c, 0x75, 0xdf, 0x6e,
    0x47, 0xf1, 0x1a, 0x71, 0x1d, 0x29, 0xc5, 0x89, 0x6f, 0xb7, 0x62, 0x0e, 0xaa, 0x18, 0xbe, 0x1b,
    0xfc, 0x56, 0x3e, 0x4b, 0xc6, 0xd2, 0x79, 0x20, 0x9a, 0xdb, 0xc0, 0xfe, 0x78, 0xcd, 0x5a, 0xf4,
    0x1f, 0xdd, 0xa8, 0x33, 0x88, 0x07, 0xc7, 0x31, 0xb1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xec, 0x5f,
    0x60, 0x51, 0x7f, 0xa9, 0x19, 0xb5, 0x4a, 0x0d, 0x2d, 0xe5, 0x7a, 0x9f, 0x93, 0xc9, 0x9c, 0xef,
    0xa0, 0xe0, 0x3b, 0x4d, 0xae, 0x2a, 0xf5, 0xb0, 0xc8, 0xeb, 0xbb, 0x3c, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2b, 0x04, 0x7e, 0xba, 0x77, 0xd6, 0x26, 0xe1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0c, 0x7d,
)
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _key_expand(key):
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w, nr


def _add_round_key(st, w, rnd):
    for c in range(4):
        for r in range(4):
            st[r][c] ^= w[rnd * 4 + c][r]


def _inv_shift_rows(st):
    for r in range(1, 4):
        st[r] = st[r][-r:] + st[r][:-r]


def _inv_sub_bytes(st):
    for r in range(4):
        for c in range(4):
            st[r][c] = _RSBOX[st[r][c]]


def _inv_mix_columns(st):
    for c in range(4):
        a = [st[r][c] for r in range(4)]
        st[0][c] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
        st[1][c] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
        st[2][c] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
        st[3][c] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)


def _dec_block(block, w, nr):
    st = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(st, w, nr)
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(st)
        _inv_sub_bytes(st)
        _add_round_key(st, w, rnd)
        _inv_mix_columns(st)
    _inv_shift_rows(st)
    _inv_sub_bytes(st)
    _add_round_key(st, w, 0)
    return bytes(st[r][c] for c in range(4) for r in range(4))


def _aes_cbc_decrypt(data, key, iv):
    """纯标准库 AES-CBC 解密 + PKCS7 去填充。key 支持 16/24/32 字节。"""
    if len(key) not in (16, 24, 32):
        raise ValueError('bad aes key len %d' % len(key))
    if len(iv) != 16:
        raise ValueError('bad iv len %d' % len(iv))
    if len(data) == 0 or len(data) % 16 != 0:
        raise ValueError('bad ciphertext len %d' % len(data))
    w, nr = _key_expand(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _dec_block(blk, w, nr)
        out.extend(bytes(dec[i] ^ prev[i] for i in range(16)))
        prev = blk
    pad = out[-1]
    if 1 <= pad <= 16:
        out = out[:-pad]
    return bytes(out)



# ---------------------------------------------------------------- Fernet 信封
def _b64d(s):
    s = str(s or '').strip()
    s += '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _fernet(tok):
    """解 Fernet 令牌：先验 HMAC-SHA256，再 AES-128-CBC。失败一律空串，不抛。"""
    try:
        key = _b64d(FERNET_KEY)
        raw = _b64d(tok)
        if not raw or (raw[0] & 0xFF) != 0x80 or len(raw) < 57 or len(key) < 32:
            return ''
        if not hmac.compare_digest(hmac.new(key[:16], raw[:-32], hashlib.sha256).digest(), raw[-32:]):
            return ''
        return _aes_cbc_decrypt(raw[25:-32], key[16:32], raw[9:25]).decode('utf-8', 'replace')
    except Exception:
        return ''


def _mime_of(b):
    if not b or len(b) < 4:
        return 'application/octet-stream'
    if b[0:2] == b'\xff\xd8':
        return 'image/jpeg'
    if b[0:4] == b'\x89PNG':
        return 'image/png'
    if b[0:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'image/webp'
    if b[0:3] == b'GIF':
        return 'image/gif'
    if b[0:2] == b'BM':
        return 'image/bmp'
    return 'application/octet-stream'


def _unbundle(b):
    """解封面封装：返回 (mime, 图片字节)；裸图按魔数原样透传；解不出返回 (None, None)"""
    if not b or len(b) < 16:
        return None, None
    if b[0:4] != b'gAAA':                      # 裸图
        m = _mime_of(b)
        return (m, b) if m.startswith('image/') else (None, None)
    sep = b.find(b'@@@', 4)
    if sep <= 0:
        return None, None
    try:
        head = _fernet(b[:sep].decode('latin-1'))
        if not head.startswith('data:'):
            return None, None
        semi = head.find(';')
        comma = head.find('base64,')
        if semi < 6 or comma < 6:
            return None, None
        mime = head[5:semi]
        allb = head[comma + 7:].encode('latin-1') + b[sep + 3:]
        img = base64.b64decode(allb)
        if len(img) < 64:
            return None, None
        return (mime or _mime_of(img)), img
    except Exception:
        return None, None


# ---------------------------------------------------------------- 封面中继（本机）
class _Relay(object):
    """封面中继：破解打码 + Fernet 解封装 + 按真 MIME 回图。起一次常驻，失败退回直链。"""

    def __init__(self, sp):
        self.sp = sp
        self.httpd = None
        self.port = 0
        self.hits = 0
        self.fails = 0

    def ensure(self):
        if self.port:
            return self.port
        try:
            relay = self

            class _H(BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'

                def log_message(self, *a):
                    pass

                def do_GET(self):
                    relay.serve(self, False)

                def do_HEAD(self):
                    relay.serve(self, True)

            srv = HTTPServer(('127.0.0.1', 0), _H)
            srv.daemon_threads = True
            th = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.5})
            th.daemon = True
            th.start()
            self.httpd = srv
            self.port = int(srv.server_address[1])
            self.sp._log('封面中继已起 127.0.0.1:%d' % self.port)
        except Exception as e:
            self.sp._log('封面中继起不来 %s' % str(e)[:110])
            return 0
        return self.port

    def img_url(self, pic, vid=''):
        p = self.ensure()
        if not p:
            return ''
        return 'http://127.0.0.1:%d/img?u=%s&v=%s' % (
            p, base64.urlsafe_b64encode(str(pic).encode('utf-8')).decode('ascii').rstrip('='),
            urllib.parse.quote(str(vid or '')))

    def serve(self, h, head=False):
        try:
            u = urllib.parse.urlparse(h.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path != '/img':
                self._send(h, 404, 'text/plain', b'bad path', head)
                return
            b64 = (qs.get('u') or [''])[0]
            b64 += '=' * (-len(b64) % 4)
            url = base64.urlsafe_b64decode(b64.encode('ascii')).decode('utf-8', 'replace')
            vid = (qs.get('v') or [''])[0]
            raw = self.sp._fetch_bytes(url) if url.startswith('http') else None
            if not raw and vid:
                fresh = self.sp._fresh_pic(vid)
                if fresh and fresh != url:
                    raw = self.sp._fetch_bytes(fresh)
            if not raw:
                self.fails += 1
                self._send(h, 502, 'text/plain', b'img fail', head)
                return
            mime, img = _unbundle(raw)
            if img is None:
                self.fails += 1
                self._send(h, 502, 'text/plain', b'decode fail', head)
                return
            self.hits += 1
            self._send(h, 200, mime, img, head)
        except Exception as e:
            self.fails += 1
            try:
                self._send(h, 500, 'text/plain', ('err %s' % str(e)[:60]).encode('utf-8'), head)
            except Exception:
                pass

    def _send(self, h, code, mime, body, head=False):
        body = body or b''
        try:
            h.send_response(code)
            h.send_header('Content-Type', mime)
            h.send_header('Content-Length', str(len(body)))
            h.send_header('Cache-Control', 'no-store')
            h.end_headers()
            if not head:
                h.wfile.write(body)
        except Exception:
            pass
        h.close_connection = True


# ---------------------------------------------------------------- 主体
class Spider(_Spider):

    def __init__(self, *args, **kwargs):
        self.api = API_HOST
        self.site = SITE_ID
        self.timeout = 20
        self.headers = {
            'User-Agent': UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Origin': 'https://' + API_HOST + '/',
            'Referer': 'https://' + API_HOST + '/',
        }
        self.img_headers = {'User-Agent': UA, 'Accept': 'image/avif,image/webp,image/*,*/*;q=0.8'}
        self._classes = None
        self._groups = {}
        self._relay = None

    def init(self, extend=''):
        ex = str(extend or '').strip()
        if ex.startswith('{'):
            try:
                o = json.loads(ex)
                h = str(o.get('api') or '').strip()
                if len(h) > 4:
                    h = h.replace('https://', '').replace('http://', '')
                    self.api = h.split('/')[0]
                sid = int(o.get('site') or 0)
                if sid > 0:
                    self.site = sid
            except Exception:
                pass
        self.headers['Origin'] = 'https://' + self.api + '/'
        self.headers['Referer'] = 'https://' + self.api + '/'
        return self

    def getName(self):
        return SITE_NAME

    def destroy(self):
        try:
            if self._relay and self._relay.httpd:
                self._relay.httpd.shutdown()
                self._relay.httpd.server_close()
        except Exception:
            pass
        self._relay = None
        self._classes = None

    def isVideoFormat(self, url):
        u = str(url or '').lower()
        return '.m3u8' in u or '.mp4' in u

    def manualVideoCheck(self):
        return False

    def _log(self, msg):
        try:
            print('[%s] %s' % (SITE_NAME, str(msg)[:200]))
        except Exception:
            pass

    # ---------------- HTTP ----------------
    def _http(self, url, want='text'):
        last = ''
        for attempt in range(2):
            if requests is not None:
                try:
                    r = requests.get(url, timeout=self.timeout, headers=self.headers, verify=False)
                    if r.status_code == 200 and (r.text or r.content):
                        return r.text if want == 'text' else r.content
                    last = 'http %s' % r.status_code
                except Exception as e:
                    last = str(e)[:100]
            try:
                req = urllib.request.Request(url, headers=self.headers)
                raw = urllib.request.urlopen(req, timeout=self.timeout).read()
                if raw[:2] == b'\x1f\x8b':
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                if raw:
                    return raw.decode('utf-8', 'replace') if want == 'text' else raw
            except Exception as e:
                last = str(e)[:100]
            if attempt == 0:
                time.sleep(0.5)
        self._log('request fail %s (%s)' % (url.split('?')[0], last))
        return '' if want == 'text' else None

    def _fetch_bytes(self, url, timeout=None):
        to = timeout or self.timeout
        if requests is not None:
            try:
                r = requests.get(url, timeout=to, headers=self.img_headers, verify=False)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:
                pass
        try:
            req = urllib.request.Request(url, headers=self.img_headers)
            raw = urllib.request.urlopen(req, timeout=to).read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw
        except Exception:
            return None

    def _api(self, path):
        """打一个接口并撕掉信封；失败返回 {}"""
        url = path if path.startswith('http') else 'https://' + self.api + path
        t = self._http(url)
        if not t or len(t) < 8:
            return {}
        try:
            o = json.loads(t)
        except Exception:
            return {}
        if not isinstance(o, dict):
            return {}
        x = o.get('x-data')
        if not x:
            return o
        p = _fernet(x)
        if not p:
            return {}
        try:
            return json.loads(p)
        except Exception:
            return {}

    @staticmethod
    def _items(r):
        d = (r or {}).get('data')
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get('items') or []
        return []

    @staticmethod
    def _data(r):
        d = (r or {}).get('data')
        return d if isinstance(d, dict) else (r or {})

    # ---------------- 封面 ----------------
    def _fresh_pic(self, vid):
        """封面地址过期/取不到时回详情接口重取（签名在服务端算，只能这么换）"""
        try:
            d = self._data(self._api('/api/vod/video/%s?site_id=%d' % (str(vid), self.site)))
            return str(d.get('pic') or '')
        except Exception:
            return ''

    def _pic(self, pic, vid=''):
        p = str(pic or '').strip()
        if not p.startswith('http'):
            return p
        if self._relay is None:
            self._relay = _Relay(self)
        u = self._relay.img_url(p, vid)
        return u or p

    # ---------------- 分类 ----------------
    def _ensure_classes(self):
        if self._classes:
            return
        out = [{'type_name': '🆕最新更新', 'type_id': 'new'},
               {'type_name': '🔥热门排行', 'type_id': 'hot'}]
        try:
            items = self._items(self._api('/api/vod/tag_group?site_id=%d' % self.site))
        except Exception:
            items = []
        seen = set()
        for o in items:
            if not isinstance(o, dict):
                continue
            if int(o.get('purpose') or 0) == 8:            # 排掉搜索组
                continue
            if int(o.get('tag_type') or 1) in (2, 3):      # 排掉小说 / 漫画
                continue
            nm = str(o.get('name') or '').strip()
            if not nm or nm in seen:
                continue
            tags = o.get('tag') or []
            ids = [str(t.get('id')) for t in tags if isinstance(t, dict) and int(t.get('id') or 0) > 0]
            if not ids:
                continue
            tid = 'g%s' % o.get('id')
            seen.add(nm)
            self._groups[tid] = {'name': nm, 'tags': tags, 'ids': ','.join(ids)}
            out.append({'type_name': nm, 'type_id': tid})
        self._classes = out

    # ---------------- 卡片 ----------------
    def _remark(self, it):
        parts = []
        dur = int(it.get('duration') or 0)
        if dur > 0:
            parts.append('%02d:%02d' % (dur // 60, dur % 60))
        for t in (it.get('tag') or [])[:2]:
            nm = str((t or {}).get('name') or '').strip()
            if nm:
                parts.append(nm)
        if int(it.get('is_paid') or 0) == 1:
            parts.append('💎会员')
        return ' · '.join(parts)

    def _cards(self, items):
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            vid = str(it.get('id') or '').strip()
            nm = str(it.get('name') or '').strip()
            if not vid or not nm:
                continue
            pic = str(it.get('pic') or '')
            pic = pic if pic.startswith('http') else ('https://' + IMG_HOST + pic)
            out.append({'vod_id': vid, 'vod_name': nm, 'vod_pic': self._pic(pic, vid),
                        'vod_remarks': self._remark(it)})
        return out

    # ---------------- 列表 ----------------
    def _list_path(self, tid, page, tag):
        if tid == 'hot':
            return '/api/vod/video/top/hits?site_id=%d' % self.site
        t = str(tag or '').strip()
        if not t:
            g = self._groups.get(tid)
            t = g['ids'] if g else ''
        q = '/api/vod/video?page=%d&per_page=%d' % (page, PER)
        if t:
            q += '&tag=' + urllib.parse.quote(t)
        return q + '&site_id=%d' % self.site

    # ---------------- 五入口 ----------------
    def homeContent(self, filter=False):
        self._ensure_classes()
        root = {'class': list(self._classes)}
        if filter:
            fl = {}
            for c in self._classes:
                tid = c['type_id']
                g = self._groups.get(tid)
                if not g:
                    continue
                vals = [{'n': '全部', 'v': ''}]
                for t in g['tags']:
                    if isinstance(t, dict):
                        vals.append({'n': str(t.get('name') or ''), 'v': 't%s' % t.get('id')})
                fl[tid] = [{'key': 'tag', 'name': '标签', 'value': vals}]
            root['filters'] = fl
        root['list'] = self._cards(self._items(self._api(self._list_path('new', 1, ''))))
        return root

    def homeVideoContent(self):
        return {'list': self._cards(self._items(self._api(self._list_path('new', 1, ''))))}

    def categoryContent(self, tid, pg, filter, extend):
        self._ensure_classes()
        try:
            page = max(1, int(str(pg or '1').strip()))
        except Exception:
            page = 1
        type_id = str(tid or 'new').strip() or 'new'
        tag = ''
        if isinstance(extend, dict):
            v = str(extend.get('tag') or '').strip()
            if v.startswith('t') and len(v) > 1:
                tag = v[1:]
        r = self._api(self._list_path(type_id, page, tag))
        lst = self._cards(self._items(r))
        d = self._data(r)
        pages = int(d.get('pages') or 0) or (page + 1 if len(lst) >= PER else page)
        total = int(d.get('total') or 0) or len(lst)
        return {'page': page, 'pagecount': pages, 'limit': len(lst), 'total': total, 'list': lst}

    def detailContent(self, ids):
        target = ''
        if isinstance(ids, (list, tuple)) and ids:
            target = str(ids[0] or '').strip()
        else:
            target = str(ids or '').strip()
        if target.startswith('folder@'):
            target = target[7:]
        vid = ''.join(c for c in target if c.isdigit())[:12]
        if not vid:
            return {'list': []}
        d = self._data(self._api('/api/vod/video/%s?site_id=%d' % (vid, self.site)))
        if not d:
            return {'list': []}
        nm = str(d.get('name') or '').strip() or (SITE_NAME + ' ' + vid)
        pic = str(d.get('pic') or d.get('pic_thumb') or '')
        pic = pic if pic.startswith('http') else ('https://' + IMG_HOST + pic)
        tags = ' '.join('#' + str((t or {}).get('name') or '') for t in (d.get('tag') or []) if (t or {}).get('name'))
        dur = int(d.get('duration') or 0)
        desc = str(d.get('description') or '').strip()
        pub = str(d.get('pubdate') or '')
        play = str(d.get('play_url') or '').strip()
        vod = {
            'vod_id': target or vid,
            'vod_name': nm,
            'vod_pic': self._pic(pic, vid),
            'vod_remarks': self._remark(d),
            'vod_year': pub[:4] if len(pub) >= 4 else '',
            'type_name': tags.strip(),
            'vod_content': ('【站源】番茄视频 · 免登录直取（站点没有登录/会员校验这一步，接口信封已在源内解）\n'
                            '【取流】HLS 标准 AES-128（密钥 enc.key 明文可取，非 DRM 阻断）\n'
                            '【线路】线路1 / 线路2 / 海外专线，三线同源，谁通走谁\n'
                            + ('【时长】%02d:%02d\n' % (dur // 60, dur % 60) if dur else '')
                            + ('【标签】' + tags.strip() + '\n' if tags.strip() else '')
                            + ('【简介】' + desc if desc else '')),
        }
        if play:
            from_list, urls = [], []
            for i, host in enumerate(LINE_HOST):
                u = play if play.startswith('http') else ('https://' + host + play)
                from_list.append(LINE_NAME[i])
                urls.append('正片$' + u)
            vod['vod_play_from'] = '$$$'.join(from_list)
            vod['vod_play_url'] = '$$$'.join(urls)
        else:
            vod['vod_play_from'] = LINE_NAME[0]
            vod['vod_play_url'] = '暂无可用线路$https://' + self.api + '/api/vod/video/' + vid
        return {'list': [vod]}

    def searchContent(self, key, quick, pg='1'):
        kw = str(key or '').strip()
        if not kw:
            return {'list': []}
        try:
            page = max(1, int(str(pg or '1').strip()))
        except Exception:
            page = 1
        q = '/search/vod/?search=%s&page=%d&per_page=%d&site_id=%d' % (
            urllib.parse.quote(kw), page, PER, self.site)
        r = self._api(q)
        lst = self._cards(self._items(r))
        d = self._data(r)
        pages = int(d.get('pages') or 0) or (page + 1 if len(lst) >= PER else page)
        total = int(d.get('total') or 0) or len(lst)
        return {'page': page, 'pagecount': pages, 'limit': len(lst), 'total': total, 'list': lst}

    def playerContent(self, flag, id, vipFlags=None):
        u = str(id or '').strip()
        if u.startswith('folder@'):
            u = u[7:]
        if '$' in u:
            u = u.split('$')[-1]
        if u.startswith('http'):
            return {'parse': 0, 'playUrl': '', 'url': u,
                    'header': json.dumps({'User-Agent': UA, 'Referer': 'https://' + IMG_HOST + '/'})}
        vid = ''.join(c for c in u if c.isdigit())[:12]
        if vid:
            d = self._data(self._api('/api/vod/video/%s?site_id=%d' % (vid, self.site)))
            p = str(d.get('play_url') or '').strip()
            if p:
                full = p if p.startswith('http') else ('https://' + LINE_HOST[0] + p)
                return {'parse': 0, 'playUrl': '', 'url': full,
                        'header': json.dumps({'User-Agent': UA, 'Referer': 'https://' + IMG_HOST + '/'})}
        return {'parse': 0, 'playUrl': '', 'url': u, 'header': json.dumps({'User-Agent': UA})}

    def localProxy(self, param):
        """部分壳会走 localProxy 取图：这里直接把裸图以 base64 回给壳（不依赖本机 http 口）"""
        try:
            u = ''
            if isinstance(param, dict):
                u = str(param.get('u') or param.get('url') or '')
            if not u and isinstance(param, str):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(param).query)
                u = (qs.get('u') or [''])[0]
            if not u.startswith('http'):
                return {'code': 404, 'content': ''}
            raw = self._fetch_bytes(u)
            mime, img = _unbundle(raw or b'')
            if img is None:
                return {'code': 502, 'content': ''}
            return {'code': 200, 'content-type': mime,
                    'content': base64.b64encode(img).decode('ascii')}
        except Exception as e:
            self._log('localProxy %s' % str(e)[:80])
            return {'code': 500, 'content': ''}

    # ---------------- 自检 ----------------
    def check(self):
        try:
            h = self.homeContent(True)
            cls = h.get('class') or []
            line = ['%s · api=%s site=%d' % (SITE_NAME, self.api, self.site)]
            line.append('home 分类 %d / 首页 %d' % (len(cls), len(h.get('list') or [])))
            tid = cls[2]['type_id'] if len(cls) > 2 else 'new'
            c = self.categoryContent('new', '1', False, {})
            line.append('最新 第1页 %d 条 total=%s' % (len(c.get('list') or []), c.get('total')))
            g = self.categoryContent(tid, '1', True, {})
            line.append('分组 %s → %d 条' % (tid, len(g.get('list') or [])))
            hot = self.categoryContent('hot', '1', False, {})
            line.append('排行 → %d 条' % len(hot.get('list') or []))
            s = self.searchContent('探花', True)
            line.append('搜索『探花』→ %d 条 total=%s' % (len(s.get('list') or []), s.get('total')))
            if c.get('list'):
                vid = c['list'][0]['vod_id']
                d = self.detailContent([vid])
                one = (d.get('list') or [{}])[0]
                play = (one.get('vod_play_url') or '').split('$$$')[0].split('$')[-1]
                line.append('详情 %s → %s' % (vid, play[:56] or '空'))
                line.append('封面 %s' % str(one.get('vod_pic') or '')[:40])
                p = self.playerContent('', play)
                txt = self._http(p.get('url') or '')
                line.append('清单 %s' % ('OK %dB' % len(txt) if '#EXTM3U' in (txt or '') else '失败'))
                # 封面中继真取一张
                if self._relay is None:
                    self._relay = _Relay(self)
                pu = (one.get('vod_pic') or '')
                src = pu
                if '/img?' in pu:
                    qq = urllib.parse.parse_qs(urllib.parse.urlparse(pu).query)
                    bb = (qq.get('u') or [''])[0]
                    bb += '=' * (-len(bb) % 4)
                    src = base64.urlsafe_b64decode(bb.encode('ascii')).decode('utf-8', 'replace')
                raw = self._fetch_bytes(src)
                mime, img = _unbundle(raw or b'')
                line.append('封面解封装 %s' % (('%s %dB' % (mime, len(img))) if img else '失败'))
            return ' | '.join(line)
        except Exception as e:
            return '%s · 自检异常 %s' % (SITE_NAME, str(e)[:120])


if __name__ == '__main__':
    sp = Spider().init()
    print(sp.check())
