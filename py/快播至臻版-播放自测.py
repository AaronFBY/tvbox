# coding=utf-8
"""
快播至臻版 · 播放自测脚本（纯标准库，不装任何第三方库就能跑）
================================================================
用法：把本脚本和 快播至臻版-TVBox插件.py 放同一个目录，然后
      python 快播至臻版-播放自测.py
      想测特定分类： python 快播至臻版-播放自测.py 短视频 日韩AV
它干的事：按盒端的真实调法把插件跑一遍（分类 → 列表 → 详情 → 取流），
          每条都**真的去拉清单、拉密钥、拉分片**，并把分片 AES 解密后
          看第 2 个 TS 包的首字节是不是 0x47（有 openssl 才做这步）。
          最后打印 可播/不可播 的逐条明细 + 汇总。
"""
import binascii
import importlib.util
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, '快播至臻版-TVBox插件.py')
UA = ('Mozilla/5.0 (Linux; Android 12; KBZhizhen/1.0) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
REFERER = 'https://dy.ykfryrx.cn/h/tz/'
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def load_plugin():
    spec = importlib.util.spec_from_file_location('kbspider', PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODE = [None]


def _pick_mode():
    """先定一次通道：urllib 能直连就用 urllib，否则全程走 curl（避免每次白等一次超时）"""
    if _MODE[0] is None:
        try:
            urllib.request.urlopen(urllib.request.Request(
                'https://dy.ykfryrx.cn/api/getVideoCategory?platform=9&_b=mucritmg',
                headers={'User-Agent': UA}), timeout=6, context=CTX)
            _MODE[0] = 'urllib'
        except Exception:
            _MODE[0] = 'curl'
    return _MODE[0]


def get(url, timeout=20, rng=None):
    """返回 (status, bytes)。"""
    hdrs = {'User-Agent': UA, 'Referer': REFERER}
    if rng:
        hdrs['Range'] = rng
    if _pick_mode() == 'urllib':
        req = urllib.request.Request(url, headers=hdrs)
        try:
            r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
            data = r.read(4096 if rng else 400000)
            return (getattr(r, 'status', 200) or 200, data)
        except Exception as e:
            code = getattr(e, 'code', 0) or 0
            if code in (403, 404, 410):
                return (code, b'')
    # curl
    try:
        cmd = ['curl', '-s', '-L', '--noproxy', '*', '--max-time', str(int(timeout)),
               '-A', UA, '-H', 'Referer: ' + REFERER, '-w', '\n__C__%{http_code}']
        if rng:
            cmd += ['-r', rng.replace('bytes=', '')]
        cmd.append(url)
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 5).stdout or b''
        m = re.search(rb'__C__(\d+)$', out)
        if m:
            return (int(m.group(1)), out[:m.start()])
    except Exception:
        pass
    return (0, b'')


def play_check(url, retries=3):
    """模拟播放器：清单 → 密钥 → 首片 →（有 openssl 就）解密验 TS 同步字"""
    tag = 'init'
    for _ in range(retries):
        c, b = get(url)
        if c != 200 or b'#EXTM3U' not in b:
            tag = '清单HTTP_%s' % c
            time.sleep(0.3)
            continue
        txt = b.decode('utf-8', 'replace')
        segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith('#')]
        if not segs:
            tag = '清单里没分片'
            time.sleep(0.3)
            continue
        base = url[:url.find('/', url.find('//') + 2)]
        keylines = [l for l in txt.splitlines() if l.startswith('#EXT-X-KEY')]
        kh = ''
        if keylines:
            m = re.search(r'URI="([^"]+)"', keylines[0])
            if m:
                ku = m.group(1)
                if ku.startswith('/'):
                    ku = base + ku
                kc, kb = get(ku)
                if kc == 200 and len(kb) >= 16:
                    kh = binascii.hexlify(kb[:16]).decode()
                else:
                    tag = '密钥HTTP_%s' % kc
                    time.sleep(0.3)
                    continue
        sc, sb = get(segs[0], rng='bytes=0-4095')
        if sc not in (200, 206) or len(sb) < 400:
            tag = '分片HTTP_%s/%dB' % (sc, len(sb))
            time.sleep(0.3)
            continue
        tag = '分片HTTP_%s' % sc
        if kh and _has_openssl():
            iv = '00' * 16
            m = re.search(r'IV=0x([0-9a-fA-F]+)', keylines[0])
            if m:
                iv = m.group(1)
            try:
                p = subprocess.run(
                    ['openssl', 'enc', '-d', '-aes-128-cbc', '-K', kh, '-iv', iv, '-nopad'],
                    input=sb, capture_output=True, timeout=10)
                out = p.stdout or b''
            except Exception:
                out = sb
            if len(out) > 188 and out[188] == 0x47:
                return (True, 'OK · TS同步字正确')
            tag = '解密后不是TS'
        else:
            if len(sb) >= 400:
                return (True, 'OK · 分片可拉 %dB' % len(sb))
        time.sleep(0.3)
    return (False, tag)


_OS = [None]


def _has_openssl():
    if _OS[0] is None:
        try:
            subprocess.run(['openssl', 'version'], capture_output=True, timeout=5)
            _OS[0] = True
        except Exception:
            _OS[0] = False
    return _OS[0]


def main():
    want = [a for a in sys.argv[1:] if not a.startswith('-')]
    mod = load_plugin()
    sp = mod.Spider()
    sp.init('')
    home = sp.homeContent(False)
    cats = home['class']
    if want:
        cats = [c for c in cats if c['type_name'] in want] or cats
    print('分类 %d 个；每条真实拉清单/密钥/分片' % len(cats))
    print('openssl：%s' % ('有（会验 TS 同步字）' if _has_openssl() else '没有（只验分片可拉）'))
    print('-' * 72)

    total = ok = 0
    bad = []
    for c in cats:
        r = sp.categoryContent(c['type_id'], 1, False, {})
        items = r.get('list') or []
        if not items:
            print('  %-8s 列表为空' % c['type_name'])
            continue
        picks = items[:2] if len(items) > 1 else items[:1]
        for it in picks:
            detail = sp.detailContent([it['vod_id']])['list'][0]
            pu = detail.get('vod_play_url', '')
            raw = pu.split('$', 1)[1] if '$' in pu else pu
            if not raw:
                print('  %-8s %-16s 详情里没有播放地址' % (c['type_name'], it['vod_name'][:16]))
                continue
            pl = sp.playerContent('快播至臻版', raw, '')
            url = pl.get('url', '')
            node = re.search(r'1bf\d\d', url)
            passed, tag = play_check(url)
            total += 1
            ok += 1 if passed else 0
            if not passed:
                bad.append((c['type_name'], it['vod_name'][:16], tag))
            print('  %-8s %-16s %s  %s' % (
                c['type_name'], it['vod_name'][:16],
                '✅' if passed else '❌', (tag + ' @' + node.group(0)) if node else tag))
    print('-' * 72)
    print('可播 %d/%d' % (ok, total))
    if bad:
        print('失败明细：')
        for b in bad:
            print('   %s | %s | %s' % b)


if __name__ == '__main__':
    main()
