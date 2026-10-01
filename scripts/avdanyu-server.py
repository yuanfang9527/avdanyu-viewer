#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""avdanyu 本地服务器：静态文件 + 标题翻译中继 + 磁力外站抓取中继 + FANZA 预告片解析 + 在线播放解析
- 静态服务：同 http.server（查看器、数据库文件）
- POST /__translate：{texts: [...], from: "ja", to: "zh-CN"} → 优先智谱 GLM 模型翻译（需配置 API Key），
  失败时降级 curl_cffi / Google 免费端点，最后兜底 MyMemory
- GET /__magnets?q=番号[&refresh=1]：按番号搜索外站磁力（sukebei 主源，btdig 备源），内存缓存 30 分钟
- GET /__comments?q=番号[&refresh=1]：JavDB 評論區抓取（搜索定位视频页 → 解析评论），内存缓存 30 分钟
- GET /__trailer?cid=番号[&refresh=1]：解析 FANZA 试看片直链（多画质，带签名 token），内存缓存 1 小时
- GET /__online-player?code=番号：解析 javday 播放源（搜索番号 → 视频页 → m3u8 直链，多线路），
  内存缓存 30 分钟
- GET /__online-m3u8?u=javday播放列表地址：播放列表代理，逐片子域重写为主域（分段 CORS 全开，
  浏览器直连拉流不经服务器中转）
启动：python scripts/avdanyu-server.py （start-viewer.bat 启动本脚本）
"""
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PORT = 8971
MAX_BODY = 4 * 1024 * 1024        # 请求体上限 4MB
# 每次启动生成随机令牌：查看器启动时从 /__health 同源读取，写接口必须携带。
# 跨站页面因同源策略拿不到令牌，无法读写本地接口。
TOKEN = secrets.token_hex(16)


# ==================== LLM 模型翻译（智谱 GLM 引擎） ====================
# Key 来源（环境变量优先）：avdanyu-data/zhipu-config.json 的 api_key 字段，或环境变量 ZHIPU_API_KEY。
# coding plan 订阅 Key 与普通按量 Key 的接口域名相同但路径不同，这里自动探测并缓存可用组合。
ZHIPU_CFG_FILE = BASE / 'avdanyu-data' / 'zhipu-config.json'
ZHIPU_TIMEOUT = 25
ZHIPU_DEFAULT_MODEL = 'glm-4.6'
ZHIPU_MODEL_FALLBACKS = ['glm-4.7', 'glm-4.5-air']
ZHIPU_ENDPOINTS = [
    'https://open.bigmodel.cn/api/coding/paas/v4',   # GLM Coding Plan 订阅 Key 专用
    'https://open.bigmodel.cn/api/paas/v4',          # 普通按量计费 Key
]

# 探测成功的 {'endpoint':..., 'model':...}，后续请求直接复用
_llm_ok = {'zhipu': {}}

_LANG_NAMES = {'ja': '日语', 'zh-CN': '简体中文', 'zh-TW': '繁体中文', 'en': '英语', 'ko': '韩语'}


def _tr_load_config():
    cfg = {'api_key': '', 'model': '', 'base_url': ''}
    try:
        with open(ZHIPU_CFG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in cfg:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    cfg[k] = v.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f'翻译配置文件读取失败: {e}')
    # 环境变量覆盖文件配置，改配置文件无需重启服务即可生效
    for k in ('api_key', 'model', 'base_url'):
        cfg[k] = (os.environ.get('ZHIPU_' + k.upper()) or '').strip() or cfg[k]
    # 兼容只填路径的写法（如 /api/coding/paas/v4）：自动补全官方域名
    bu = cfg['base_url']
    if bu and not bu.lower().startswith('http'):
        cfg['base_url'] = 'https://open.bigmodel.cn' + (bu if bu.startswith('/') else '/' + bu)
    return cfg


def _llm_vendor_conf(cfg):
    """提取智谱引擎的连接配置（Key、模型链、端点、超时）。"""
    return {
        'api_key': cfg['api_key'],
        'model': cfg['model'],
        'base_url': cfg['base_url'],
        'default_model': ZHIPU_DEFAULT_MODEL,
        'fallbacks': ZHIPU_MODEL_FALLBACKS,
        'default_endpoints': ZHIPU_ENDPOINTS,
        'timeout': ZHIPU_TIMEOUT,
        'unconfigured': '未配置智谱 API Key',
    }


def _zhipu_parse_translations(content, expect):
    """从模型输出解析出与输入等长的译文数组；失败返回 None。"""
    s = (content or '').strip()
    if not s:
        return None
    if s.startswith('```'):            # 去掉可能出现的 markdown 代码块围栏
        s = re.sub(r'^```[\w-]*\s*', '', s)
        s = re.sub(r'\s*```\s*$', '', s).strip()
    try:
        data = json.loads(s)
    except Exception:
        a, b = s.find('{'), s.rfind('}')
        if a < 0 or b <= a:
            return None
        try:
            data = json.loads(s[a:b + 1])
        except Exception:
            return None
    arr = None
    if isinstance(data, dict):
        for k in ('t', 'translations', 'data'):
            if isinstance(data.get(k), list):
                arr = data[k]
                break
    elif isinstance(data, list):
        arr = data
    if not isinstance(arr, list) or len(arr) != expect:
        return None
    out = []
    for v in arr:
        if isinstance(v, str):
            out.append(v.strip())
        elif isinstance(v, dict):
            out.append(str(v.get('t') or v.get('translation') or '').strip())
        else:
            return None
    return out


def _llm_chat_once(api_key, endpoint, model, prompt, expect, with_extras, timeout):
    """单次智谱请求（OpenAI 兼容 chat/completions）。返回 (译文列表|None, 错误类别|None, 错误信息)。
    错误类别：param=400 参数不兼容（可去参重试）；auth=401/403 Key 无权限（可换端点）；
    giveup=429 限流（放弃本引擎）；filtered=内容过滤（上层拆批）；retry=网络/服务端错误（换端点）；
    parse=输出无法解析（换模型）。"""
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': 4000,
    }
    if with_extras:
        body['response_format'] = {'type': 'json_object'}
        body['thinking'] = {'type': 'disabled'}          # 关闭深度思考，翻译提速明显
    try:
        req = urllib.request.Request(
            endpoint.rstrip('/') + '/chat/completions',
            data=json.dumps(body).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8', 'replace')[:180]
        except Exception:
            detail = ''
        if e.code == 400:
            # 内容过滤（1301）与端点、模型无关，由上层拆批处理
            if '1301' in detail or 'contentFilter' in detail or '敏感' in detail:
                return None, 'filtered', f'HTTP 400 ({model}): {detail}'
            return None, 'param', f'HTTP 400 ({model}): {detail}'
        if e.code in (401, 403):
            return None, 'auth', f'HTTP {e.code} ({model}): {detail}'
        if e.code == 429:
            return None, 'giveup', f'HTTP 429 限流: {detail}'
        return None, 'retry', f'HTTP {e.code} ({model}): {detail}'
    except Exception as e:
        return None, 'retry', f'{type(e).__name__}: {str(e)[:180]}'
    try:
        content = str((((data.get('choices') or [{}])[0].get('message') or {}).get('content')) or '')
    except Exception:
        content = ''
    out = _zhipu_parse_translations(content, expect)
    if out is None:
        return None, 'parse', f'模型输出无法解析为 {expect} 条译文: {content[:120]}'
    return out, None, None


def zhipu_translate_texts(texts, source_lang, target_lang):
    """智谱 GLM 翻译。
    返回 (结果列表, 模型名, 错误信息)：结果列表与 texts 等长，被内容过滤或失败的条目为 None；
    至少一条成功时错误为 None；未配置 Key 时结果列表为 None。
    批次触发内容过滤时自动二分拆批重试，尽量保住未触发过滤的条目。"""
    cfg = _tr_load_config()
    vc = _llm_vendor_conf(cfg)
    if not vc['api_key']:
        return None, None, vc['unconfigured']
    src = _LANG_NAMES.get(source_lang, source_lang)
    dst = _LANG_NAMES.get(target_lang, target_lang)

    def build_prompt(chunk):
        return (f'你是标题翻译引擎。将下列 JSON 数组中的每条{src}文本翻译成{dst}，'
                f'番号（如 ABP-769）、英文人名、品牌等保留原文不译。'
                f'仅输出一个 JSON 对象 {{"t": ["译文1", "译文2", ...]}}，'
                f'数组长度必须等于输入的 {len(chunk)} 条，禁止输出任何其他内容。\n'
                + json.dumps(chunk, ensure_ascii=False))

    endpoints = [vc['base_url']] if vc['base_url'] else list(vc['default_endpoints'])
    models = []
    for m in [vc['model'] or vc['default_model']] + vc['fallbacks']:
        if m not in models:
            models.append(m)
    state = {'last_err': '无可用模型组合'}
    cache = _llm_ok.setdefault('zhipu', {})

    def call_chunk(chunk):
        """端点×模型链尝试翻译一个文本块；返回 (译文列表|None, 是否被内容过滤)。"""
        ok = dict(cache)
        tries = [(ok['endpoint'], [ok['model']])] if ok else []
        tries += [(ep, models) for ep in endpoints]
        for ep, mlist in tries:
            for model in mlist:
                out, kind, err = _llm_chat_once(vc['api_key'], ep, model,
                                                build_prompt(chunk), len(chunk), True,
                                                vc['timeout'])
                if out is not None:
                    cache.update({'endpoint': ep, 'model': model})
                    return out, False
                state['last_err'] = err
                if kind == 'filtered':
                    return None, True          # 内容过滤与端点/模型无关，换模型重试无意义
                if kind == 'giveup':
                    return None, False         # 限流：放弃本引擎，由下一级引擎接手
                if kind == 'param':
                    # 个别模型不支持 response_format/thinking 参数，去掉后原地重试一次
                    out, kind, err = _llm_chat_once(vc['api_key'], ep, model,
                                                    build_prompt(chunk), len(chunk), False,
                                                    vc['timeout'])
                    if out is not None:
                        cache.update({'endpoint': ep, 'model': model})
                        return out, False
                    state['last_err'] = err
                    if kind == 'filtered':
                        return None, True
                if kind in ('auth', 'retry'):
                    break                      # auth：换端点；retry：网络/服务端错误，换端点更有效
            if ok:
                cache.clear()                  # 缓存组合已失效，进入完整探测
                ok = {}
        return None, False

    def translate_chunk(chunk):
        out, filtered = call_chunk(chunk)
        if out is not None:
            return out
        if filtered and len(chunk) > 1:
            mid = (len(chunk) + 1) // 2
            return translate_chunk(chunk[:mid]) + translate_chunk(chunk[mid:])
        return [None] * len(chunk)

    results = translate_chunk(list(texts))
    if any(v is not None for v in results):
        return results, (cache.get('model') or vc['model'] or vc['default_model']), None
    return None, None, state['last_err']


# ==================== 谷歌免费端点翻译（备用引擎） ====================
_google_no_curl = False     # 本机 curl_cffi 证书链损坏时置 True，本进程内改用系统 urllib


def _google_fetch(url, q):
    """谷歌端点请求：优先 curl_cffi（浏览器级指纹），证书异常自动降级 urllib。返回 (json|None, 错误)。"""
    global _google_no_curl
    if not _google_no_curl:
        try:
            from curl_cffi import requests as creq
            r = creq.post(url, data={'q': q}, impersonate='chrome', timeout=15)
            if r.status_code == 200:
                return r.json(), None
            return None, f'HTTP {r.status_code} (curl_cffi)'
        except ImportError:
            _google_no_curl = True
        except Exception as e:
            s = str(e)
            if 'trust anchors' in s or 'CAfile' in s or 'certificate' in s.lower() or 'SSL' in s:
                _google_no_curl = True   # 证书链损坏（如 certifi 安装异常），永久降级 urllib
            return None, s
    try:
        data = urllib.parse.urlencode({'q': q}).encode('utf-8')
        req = urllib.request.Request(url, data=data,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8')), None
    except Exception as e:
        return None, str(e)


def google_translate_texts(texts, source_lang, target_lang):
    """谷歌免费端点批量翻译（换行聚合）。返回 (等长译文列表|None, 错误信息)。"""
    last_err = None
    for client in ['dict-chrome-ex', 'it', 'atf']:
        url = f'https://translate.googleapis.com/translate_a/single?client={client}&sl={source_lang}&tl={target_lang}&dt=t'
        data, err = _google_fetch(url, '\n'.join(texts))
        if data is None:
            last_err = err or 'google 无响应'
            continue
        try:
            chunks = [item[0] for item in (data[0] or []) if item and item[0]]
            lines = ''.join(chunks).split('\n')
            if len(lines) == len(texts):
                return [l.strip() for l in lines], None
            last_err = f'译文行数与输入不一致（{len(lines)}/{len(texts)}）'
        except Exception as e:
            last_err = str(e)
    return None, last_err


def free_translate_relay(payload):
    texts = payload.get('texts')
    if texts is None and 'text' in payload:
        texts = [payload['text']]
    if not texts:
        return 200, json.dumps({'ok': False, 'error': '未提供待翻译文本'})

    target_lang = payload.get('to') or 'zh-CN'
    source_lang = payload.get('from') or 'ja'
    cleaned = [str(t).replace('\r', ' ').replace('\n', ' ') for t in texts]

    # 1) 智谱 GLM 模型（主引擎）；成人标题可能触发平台内容过滤（1301），智谱内部已自动拆批，
    #    最终被过滤的条目由谷歌收底补齐
    results = [None] * len(cleaned)
    engine = None
    try:
        zt, zmodel, zerr = zhipu_translate_texts(cleaned, source_lang, target_lang)
    except Exception as e:
        zt, zmodel, zerr = None, None, f'智谱调用异常: {type(e).__name__}: {str(e)[:150]}'
    if zt:
        results = zt
        if zmodel:
            engine = f'zhipu-{zmodel}'
    elif zerr and not zerr.startswith('未配置'):
        log(f'智谱翻译失败，降级谷歌引擎：{zerr[:200]}')

    # 2) 谷歌免费端点（收底引擎）补齐剩余条目（智谱整体失败时为全部，部分被内容过滤时仅为被过滤条目）
    pending = [i for i, v in enumerate(results) if not v]
    if pending:
        g_out, g_err = google_translate_texts([cleaned[i] for i in pending], source_lang, target_lang)
        if g_out:
            hit = 0
            for idx, i in enumerate(pending):
                if g_out[idx]:
                    results[i] = g_out[idx]
                    hit += 1
            if hit:
                engine = f'{engine}+google' if engine else 'google'
        elif g_err and not engine:
            log(f'谷歌引擎翻译失败：{g_err[:150]}')

    if any(results):
        return 200, json.dumps({'ok': True, 'engine': engine or 'mixed',
                                'translations': [v or '' for v in results]}, ensure_ascii=False)

    # 3) MyMemory 兜底（仅单条）
    if len(cleaned) == 1:
        try:
            enc = urllib.parse.quote(cleaned[0])
            url = f'https://api.mymemory.translated.net/get?q={enc}&langpair={source_lang}|{target_lang}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            zh = (data.get('responseData') or {}).get('translatedText')
            if zh and zh.strip():
                return 200, json.dumps({'ok': True, 'engine': 'mymemory', 'translations': [zh.strip()]})
        except Exception:
            pass

    return 200, json.dumps({'ok': False, 'error': '智谱与谷歌引擎翻译均失败（详见服务器日志）'})


# ==================== 外站磁力抓取（详情抽屉「磁力资源」数据源） ====================
# 浏览器直连外站有 CORS 限制，且需浏览器级 UA/指纹，因此由本服务器中继抓取。
# 源顺序：javbus（主源：直连快、带官方字幕/高清徽标；需完整浏览器头过年龄墙，AJAX 磁力表）
#         → sukebei（.si 主域 → .net 镜像；带做种数，javbus 无此番号时启用）
#         → btdig（.com → .co，元信息较少，末位兜底）。
# 直连失败（典型：TLS 握手被重置）时自动经本机代理（Clash 等）重试；
# 代理可用环境变量 AVDANYU_PROXY 显式指定，否则依次探测 7897/7890/7891/10809。

MAGNET_CACHE_TTL = 1800            # 内存缓存 30 分钟；「重新抓取」带 refresh=1 绕过
_magnet_cache = {}                 # 番号大写 -> (写入时间戳, items, sources)
_magnet_cache_lock = threading.Lock()

_BTIH_RE = re.compile(r'urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})')
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

_PROXY_CANDIDATES = ['http://127.0.0.1:%d' % p for p in (7897, 7890, 7891, 10809)]
if os.environ.get('AVDANYU_PROXY'):
    _PROXY_CANDIDATES = [os.environ['AVDANYU_PROXY'].strip()]
_proxy_state = {'ok': '', 'dead_until': 0.0}   # 探测结果缓存：成功记住地址，失败 10 分钟内不再探测
_proxy_state_lock = threading.Lock()


def _pick_local_proxy():
    """探测可用的本机代理；返回代理 URL 或 ''。结果缓存，避免每个请求都探测。"""
    with _proxy_state_lock:
        if _proxy_state['ok']:
            return _proxy_state['ok']
        if time.time() < _proxy_state['dead_until']:
            return ''
    found = ''
    for p in _PROXY_CANDIDATES:
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({'http': p, 'https': p}))
            req = urllib.request.Request('https://www.gstatic.com/generate_204',
                                         headers={'User-Agent': _UA})
            with opener.open(req, timeout=4) as resp:
                if resp.status in (200, 204):
                    found = p
                    break
        except Exception:
            continue
    with _proxy_state_lock:
        _proxy_state['ok'] = found
        if not found:
            _proxy_state['dead_until'] = time.time() + 600
    return found


def _http_once(url, timeout, headers, proxy):
    """单次 GET 尝试：curl_cffi（浏览器指纹）优先，urllib 兜底；proxy 为 '' 时直连。"""
    curl_err = ''
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    try:
        from curl_cffi import requests as creq
        try:
            r = creq.get(url, impersonate='chrome', timeout=timeout, headers=headers, proxies=proxies)
            if r.status_code == 200 and r.text:
                return r.text, ''
            curl_err = f'HTTP {r.status_code}'
        except Exception as e:
            curl_err = str(e)
            # 部分环境 certifi CA 路径异常（如中文用户名目录）会导致所有 https 证书校验失败：
            # 降级为不校验证书重试（均为公开抓取目标，可接受）
            if 'trust anchor' in curl_err or '(77)' in curl_err:
                try:
                    r = creq.get(url, impersonate='chrome', timeout=timeout, headers=headers,
                                 proxies=proxies, verify=False)
                    if r.status_code == 200 and r.text:
                        return r.text, ''
                    curl_err = f'HTTP {r.status_code}'   # 拿到了真实状态码（如 404）：如实上报，避免残留 TLS 错误误导
                except Exception as e2:
                    curl_err = f'{curl_err} / {e2}'
    except ImportError:
        pass
    try:
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', 'replace'), ''
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', 'replace'), ''
    except Exception as e:
        return None, curl_err or str(e)


def _ext_http_get(url, timeout=12, extra_headers=None, allow_proxy=True):
    """外站 GET：直连优先，失败（如被墙 TLS 重置）时经本机代理重试。返回 (text|None, error)"""
    headers = {'User-Agent': _UA, 'Accept-Language': 'en-US,en;q=0.8,zh-CN;q=0.6'}
    if extra_headers:
        headers.update(extra_headers)
    text, err = _http_once(url, timeout, headers, '')
    if text is not None:
        return text, ''
    if not allow_proxy:
        return None, err
    proxy = _pick_local_proxy()
    if not proxy:
        return None, err
    text, err2 = _http_once(url, timeout, headers, proxy)
    if text is not None:
        return text, ''
    return None, f'{err} | via {proxy}: {err2}'


def _magnet_item(title, uri, size, date, seeders, leechers, source, source_url, cn_sub=False, hd=False):
    return {
        'title': (title or '').strip()[:300],
        'uri': uri,
        'size': (size or '').strip(),
        'date': (date or '').strip(),
        'seeders': (seeders or '').strip(),
        'leechers': (leechers or '').strip(),
        'source': source,
        'sourceUrl': source_url,
        'cnSub': bool(cn_sub),   # 站点官方标注"包含字幕"（目前仅 JavBus 有）
        'hd': bool(hd),          # 站点官方标注"高清"（目前仅 JavBus 有）
    }


def _search_sukebei(code):
    """sukebei 搜索（.si 主域 → .net 镜像）：结果行含标题 / magnet / 大小 / 日期 / 做种数"""
    last_err = ''
    for host in ('sukebei.nyaa.si', 'sukebei.nyaa.net'):
        url = f'https://{host}/?q=' + urllib.parse.quote(code)
        page, err = _ext_http_get(url)
        if page is None:
            last_err = f'{host}: {err}'
            continue
        items = []
        for row in re.findall(r'<tr[^>]*>.*?</tr>', page, re.S):
            m = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', row)
            if not m or not _BTIH_RE.search(m.group(1)):
                continue
            uri = html.unescape(m.group(1))
            # 标题：.si 旧模板 href 在前；.net 新模板 class 在前
            tm = re.search(r'<a[^>]+href="/view/\d+"[^>]*\stitle="([^"]*)"', row)
            # 大小/日期/做种数：两套模板列类名不同，逐个兼容
            size = (re.search(r'<td class="text-center">([\d.]+\s*[KMGT]iB)\s*</td>', row)
                    or re.search(r'<td class="col-size">\s*([\d.]+\s*[KMGT]iB)\s*</td>', row))
            date = (re.search(r'data-timestamp="\d+"[^>]*>([^<]+)<', row)
                    or re.search(r'<td class="col-date"[^>]*>([^<]+)<', row))
            nums = re.findall(r'<td class="text-center">\s*(\d+)\s*</td>', row)
            if not nums:
                seed = re.search(r'<td class="col-num num-s">\s*(\d+)\s*</td>', row)
                leech = re.search(r'<td class="col-num num-l">\s*(\d+)\s*</td>', row)
                nums = [seed.group(1) if seed else '', leech.group(1) if leech else '']
            items.append(_magnet_item(
                html.unescape(tm.group(1)) if tm else '',
                uri,
                size.group(1) if size else '',
                date.group(1).strip() if date else '',
                nums[0] if nums else '',
                nums[1] if len(nums) > 1 else '',
                'sukebei', url))
        return items, ''
    return None, last_err


def _search_btdig(code):
    """btdig 搜索（.com 主域 → .co 镜像）：提取 magnet 锚点，标题取锚文本或 dn 参数（无大小/做种数）"""
    last_err = ''
    for host in ('btdig.com', 'btdig.co'):
        url = f'https://{host}/search?q=' + urllib.parse.quote(code)
        page, err = _ext_http_get(url)
        if page is None:
            last_err = f'{host}: {err}'
            continue
        items = []
        for m in re.finditer(r'<a[^>]+href="(magnet:\?xt=urn:btih:[^"]+)"[^>]*>(.*?)</a>', page, re.S):
            if not _BTIH_RE.search(m.group(1)):
                continue
            uri = html.unescape(m.group(1))
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not title:
                dn = urllib.parse.parse_qs(uri.split('?', 1)[1]).get('dn')
                title = dn[0] if dn else ''
            items.append(_magnet_item(title, uri, '', '', '', '', 'btdig', url))
        if items:
            return items, ''
    return None, last_err


# JavBus 磁力表由 AJAX 填充；作品页需完整浏览器请求头（sec-fetch/sec-ch-ua）才能通过年龄验证墙，
# 仅靠 UA/Cookie 会被识别为非浏览器而返回验证页。
_JAVBUS_DOC_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7',
    'Cache-Control': 'no-cache', 'Pragma': 'no-cache',
    'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1', 'Upgrade-Insecure-Requests': '1',
    'sec-ch-ua': '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
    'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"',
    'Cookie': 'existmag=all',
}
_JAVBUS_XHR_HEADERS = {
    'Accept': '*/*', 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7',
    'X-Requested-With': 'XMLHttpRequest',
    'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
    'sec-ch-ua': '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
    'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"',
    'Cookie': 'existmag=all',
}


def _search_javbus(code):
    """JavBus 备源：作品页取内联变量 gid/uc/img/lang → AJAX uncledatoolsbyajax 拉磁力表。
    返回行含 标题/大小/日期；existmag=all 展示全部磁力。"""
    url = 'https://www.javbus.com/' + urllib.parse.quote(code)
    page, err = _ext_http_get(url, extra_headers=_JAVBUS_DOC_HEADERS)
    if page is None:
        return None, err
    if 'Age Verification' in page:
        return None, 'javbus: 被年龄验证页拦截（指纹校验）'
    gm = re.search(r'var gid = (\d+);', page)
    if not gm:
        # 页面正常但没有 gid：多半是番号不存在的 404 作品页，视为无结果
        if 'magnet-table' in page or 'container' in page:
            return [], ''
        return None, 'javbus: 作品页结构异常'
    uc = (re.search(r'var uc = (\d+);', page) or [None, '0'])[1]
    img = (re.search(r"var img = '([^']*)';", page) or [None, ''])[1]
    lang = (re.search(r"var lang = '([^']*)';", page) or [None, 'zh'])[1]
    ajax = ('https://www.javbus.com/ajax/uncledatoolsbyajax.php?gid=' + gm.group(1)
            + '&lang=' + urllib.parse.quote(lang)
            + '&img=' + urllib.parse.quote(img)
            + '&uc=' + uc
            + '&floor=' + str(int(time.time() * 10) % 1000))
    frag, err2 = _ext_http_get(ajax, extra_headers={**_JAVBUS_XHR_HEADERS, 'Referer': url})
    if frag is None:
        return None, err2
    items = []
    for row in re.findall(r'<tr[^>]*>.*?</tr>', frag, re.S):
        hrefs = re.findall(r'href="(magnet:\?xt=urn:btih:[^"]+)"', row)
        if not hrefs:
            continue
        uri = html.unescape(hrefs[0])
        if not _BTIH_RE.search(uri):
            continue
        # 官方徽标：btn-warning=包含字幕（中字）、btn-primary=高清。先识别再去掉，
        # 否则徽标文本"字幕/高清"会混进标题/大小的提取
        cn_sub = bool(re.search(r'<a class="btn[^"]*btn-warning[^"]*"[^>]*title="包含字幕', row))
        hd = bool(re.search(r'<a class="btn[^"]*btn-primary[^"]*"[^>]*title="包含高清', row))
        clean = re.sub(r'<a class="btn[^"]*"[^>]*>.*?</a>', '', row, flags=re.S)
        texts = [x.strip() for x in re.findall(r'>([^<>]*)</a>', clean) if x.strip()]
        title = texts[0] if texts else ''
        if not title:
            dn = urllib.parse.parse_qs(uri.split('?', 1)[1]).get('dn')
            title = dn[0] if dn else ''
        items.append(_magnet_item(
            title, uri,
            texts[1] if len(texts) > 1 else '',
            texts[2] if len(texts) > 2 else '',
            '', '',
            'javbus', url, cn_sub=cn_sub, hd=hd))
    return items, ''


MAGNET_SOURCES = (('javbus', _search_javbus), ('sukebei', _search_sukebei), ('btdig', _search_btdig))


def fetch_remote_magnets(q, refresh=False):
    q = (q or '').strip()
    if not q or len(q) > 64:
        return {'ok': False, 'error': '缺少或非法的番号参数'}
    cache_key = q.upper()
    now = time.time()
    if not refresh:
        hit = _magnet_cache.get(cache_key)
        if hit and now - hit[0] < MAGNET_CACHE_TTL:
            return {'ok': True, 'q': q, 'cached': True, 'items': hit[1], 'sources': hit[2]}
    items, sources, errors, seen = [], [], [], set()
    for name, fn in MAGNET_SOURCES:
        found, err = fn(q)
        if found is None:
            errors.append(f'{name}: {err[:120]}')
            continue
        sources.append(name)
        for it in found:
            h = _BTIH_RE.search(it['uri'])
            key = h.group(1).lower() if h else it['uri'].lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
        if items:   # 主源有结果就不再访问备源
            break
    # 做种数多的排前面，无做种数的排最后
    items.sort(key=lambda it: -(int(it['seeders']) if (it.get('seeders') or '').isdigit() else -1))
    result = {'ok': True, 'q': q, 'cached': False, 'items': items,
              'sources': sources, 'errors': errors}
    if sources:    # 至少一个源成功响应（含 0 结果）才写入缓存，全部失败则下次重试
        with _magnet_cache_lock:
            _magnet_cache[cache_key] = (time.time(), items, sources)
    return result


# ==================== JavDB 評論區抓取（详情抽屉「JavDB 評論區」数据源） ====================
# 流程：番号 -> JavDB 搜索页(/search?q=番号&f=all)取第一个标题以番号开头的 /v/ 短链
#       -> 视频页解析評論區（Reviews 标签面板，服务端渲染在页面 HTML 里）。
# 注意：JavDB 屏蔽日本/韩国等地区出口 IP（返回「版權限制」页），而预告片（FANZA）又需要日本出口：
#       两者分流解决 —— JavDB 请求独立挑选出口（详见下方「JavDB 专用出口选择」），其余功能不受影响；
#       域名轮换频繁，默认依次尝试 javdb.com 与页脚公示的当前官方域名，可用环境变量
#       AVDANYU_JAVDB_HOSTS 覆盖（逗号分隔）。
JAVDB_CACHE_TTL = 1800            # 内存缓存 30 分钟；「重新抓取」带 refresh=1 绕过
_javdb_cache = {}                 # 番号大写 -> (写入时间戳, result)
_javdb_cache_lock = threading.Lock()

_JAVDB_DOC_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Cache-Control': 'no-cache', 'Pragma': 'no-cache',
    'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1', 'Upgrade-Insecure-Requests': '1',
    'sec-ch-ua': '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
    'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"',
    'Cookie': 'over18=1; locale=zh_CN',   # 年龄墙为弹层，带 over18 即可；zh_CN 输出简体中文界面
}


def _javdb_hosts():
    raw = os.environ.get('AVDANYU_JAVDB_HOSTS', 'javdb.com,javdb580.com')
    return [h.strip() for h in raw.split(',') if h.strip()] or ['javdb.com']


# ---- JavDB 专用出口选择 ----
# JavDB 屏蔽日本/韩国等地区出口，而预告片（FANZA）恰恰需要日本出口：单一全局节点无法两全。
# 因此 JavDB 请求独立挑选出口，其余功能（磁力/预告片/在线播放）仍走原有全局代理逻辑，互不影响：
# 1) 环境变量 AVDANYU_JAVDB_PROXY 显式指定的代理（如另一个走港/台/美节点的本机端口）最优先；
# 2) 否则依次尝试 直连 + 本机常见代理端口。地区封锁页 / Cloudflare 验证页同样视为「该出口不可用」
#    并换下一个出口 —— 普通代理逻辑拿到 HTML 即算成功，感知不到这种封锁；
# 3) 可用出口与被封锁出口均记忆 10 分钟，避免每次请求全量试探。
JAVDB_EXIT_RETRY = 600
_javdb_exit_lock = threading.Lock()
_javdb_exit_state = {'ok': None, 'bad': {}}   # bad: 出口 -> 标记时间


def _javdb_exit_candidates():
    out = []
    env = os.environ.get('AVDANYU_JAVDB_PROXY', '').strip()
    if env:
        out.append(env)
    out.append('')                    # 直连（TUN/全局代理模式下即全局出口）
    seen = set(out)
    extra = []
    if os.environ.get('AVDANYU_PROXY', '').strip():
        extra.append(os.environ['AVDANYU_PROXY'].strip())
    extra += ['http://127.0.0.1:%d' % p for p in (7897, 7890, 7891, 10809)]
    for p in extra:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _javdb_exit_label(exit_proxy):
    if not exit_proxy:
        return '直连'
    env = os.environ.get('AVDANYU_JAVDB_PROXY', '').strip()
    return 'AVDANYU_JAVDB_PROXY' if exit_proxy == env else exit_proxy


def _javdb_fetch_once(url, headers, exit_proxy):
    """单出口抓取。返回 (text|None, kind, err)：kind = ok / blocked / challenge / error。"""
    text, err = _http_once(url, 15, headers, exit_proxy)
    if text is None:
        return None, 'error', (err or '无响应')
    if '版權限制' in text or 'copyright restrictions' in text[:800].lower():
        return None, 'blocked', '地区封锁'
    if 'Just a moment' in text[:600] or 'challenge-platform' in text[:2000]:
        return None, 'challenge', 'Cloudflare 人机验证'
    return text, 'ok', ''


def _javdb_get(path, referer=None):
    """JavDB 专用 GET：跨「出口 × 域名」尝试。地区封锁按出口 IP 生效，换域名无用，只换出口；
    返回 (host, text|None, error)。"""
    headers = dict(_JAVDB_DOC_HEADERS)
    if referer:
        headers['Referer'] = referer

    def probe():
        """一轮完整试探。返回 (host|None, text|None, notes, any_gated)。"""
        now = time.time()
        with _javdb_exit_lock:
            ok_exit = _javdb_exit_state['ok']
            bad = {e for e, ts in _javdb_exit_state['bad'].items() if now - ts < JAVDB_EXIT_RETRY}
        exits = ([ok_exit] if ok_exit is not None else []) + \
                [e for e in _javdb_exit_candidates() if e != ok_exit]
        notes, gated = [], False
        for exit_proxy in exits:
            if exit_proxy in bad:
                continue
            for host in _javdb_hosts():
                text, kind, err = _javdb_fetch_once('https://' + host + path, headers, exit_proxy)
                if kind == 'ok':
                    with _javdb_exit_lock:
                        _javdb_exit_state['ok'] = exit_proxy
                        _javdb_exit_state['bad'].pop(exit_proxy, None)
                    return host, text, notes, gated
                label = _javdb_exit_label(exit_proxy) + '/' + host
                if kind in ('blocked', 'challenge'):
                    gated = True
                    notes.append(f'{label}: {err}')
                    break   # 封锁按出口 IP 生效：同出口换域名无用，换下一个出口
                notes.append(f'{label}: {err[:80]}')   # 网络错误可能是域名级（SNI 阻断）：同出口换下一个域名
            with _javdb_exit_lock:
                _javdb_exit_state['bad'][exit_proxy] = time.time()
                if _javdb_exit_state['ok'] == exit_proxy:
                    _javdb_exit_state['ok'] = None
        return None, None, notes, gated

    host, text, notes, any_gated = probe()
    if text is not None:
        return host, text, ''
    if not notes:
        # 全部出口被近期的坏出口记忆跳过：「重新抓取」应当真的重试 —— 清空记忆再完整试一轮
        with _javdb_exit_lock:
            _javdb_exit_state['bad'] = {}
        host, text, notes, any_gated = probe()
        if text is not None:
            return host, text, ''
    if any_gated:
        tip = ('JavDB 屏蔽了全部已尝试出口（' + '、'.join(notes[:4]) + '）。'
               'JavDB 拒绝日本/韩国出口，而预告片又需要日本节点，两者需分流，任选其一：'
               '① 在 Clash 等工具中为 javdb.com 与 javdb580.com 添加分流规则，指向香港/台湾/新加坡/美国等节点'
               '（磁力、预告片等其他功能不受影响）；'
               '② 设置环境变量 AVDANYU_JAVDB_PROXY 指向一个非日韩出口的本机代理端口；'
               '③ 本机若有多个代理端口，服务器会自动逐个试探，无需配置。')
        return None, None, tip
    return None, None, ('javdb: 所有出口均不可达：' + '；'.join(notes[:4]))[:300]


def _javdb_code_compact(code):
    return re.sub(r'[^0-9a-z]', '', (code or '').lower())


def _javdb_find_video(search_page, code):
    """搜索结果页 -> 第一个标题以番号开头的 /v/ 短链与标题（相关度排序下精确匹配排最前）。
    只认标题前缀匹配，避免 ABP-769 误配 ABP-7690 之类的邻号。"""
    want = _javdb_code_compact(code)
    if not want:
        return '', ''
    for href, inner in re.findall(r'<a[^>]+href="(/v/[A-Za-z0-9]+)"[^>]*>(.*?)</a>', search_page, re.S):
        card = html.unescape(re.sub(r'<[^>]+>', ' ', inner))
        # 卡片文本以番号开头（番号总在标题最前）；标题以外的日期/评分文本不影响前缀判断
        if not _javdb_code_compact(card)[:64].startswith(want):
            continue
        tm = re.search(r'class="[^"]*(?:video-title|current-title)[^"]*"[^>]*>([^<]*)', inner)
        return href, (html.unescape(tm.group(1)).strip() if tm else card.strip()[:150])
    return '', ''


def _javdb_clean_text(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()


def _javdb_item_fields(chunk):
    """单条评论块 -> (author, score, date)。真实结构：作者名/星级/日期都在 review-title 的直接文本里，
    检举与点赞按钮藏在嵌套 form 中，星级用 icon-star 图标表示（gray 类为灰星占位，不计入评分）。"""
    author = ''
    am = re.search(r'class="review-title"[^>]*>(.*?)<div[^>]*class="content"', chunk, re.S)
    if am:
        head = re.sub(r'<form[^>]*>.*?</form>', ' ', am.group(1), flags=re.S)
        head = re.sub(r'<span[^>]*class="score-stars"[^>]*>.*?</span>', ' ', head, flags=re.S)
        head = re.sub(r'<span[^>]*class="time"[^>]*>.*?</span>', ' ', head, flags=re.S)
        head = re.sub(r'<[^>]+>', ' ', head)
        author = re.sub(r'\s+', ' ', html.unescape(head).replace('\xa0', ' ')).strip()
    if not author:
        fm = re.search(r'<a[^>]*href="/users/[^"]*"[^>]*>(.*?)</a>', chunk, re.S)
        if fm:
            author = _javdb_clean_text(fm.group(1))
    tm = (re.search(r'<span[^>]*class="time"[^>]*>([^<]+)</span>', chunk)
          or re.search(r'<time[^>]*>([^<]+)</time>', chunk)
          or re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
          or re.search(r'\b(\d{4}-\d{2}-\d{2})\b', chunk))
    date = _javdb_clean_text(tm.group(1)) if tm else ''
    sm = (re.search(r'data-score="([\d.]+)"', chunk)
          or re.search(r'(\d+(?:\.\d+)?)\s*分', chunk))
    score = sm.group(1) if sm else ''
    if not score:
        # 亮星数（class 含 gray 的 icon-star 是灰星占位，必须排除）；个别模板用 ★ 字形
        lit = len(re.findall(r'<i[^>]*class="icon-star(?![^"]*gray)[^"]*"', chunk))
        if 0 < lit <= 10:
            score = str(lit)
        else:
            stars = len(re.findall('[★⭐]', re.sub(r'<[^>]+>', '', chunk)))
            if 0 < stars <= 10:
                score = str(stars)
    return author[:60], score, date[:20]


def _javdb_item_text(chunk):
    """评论正文：JavDB 正文在 div.content 的 <p> 段落里；兜底做全块去噪取文本。"""
    ps = re.findall(r'<div[^>]*class="content"[^>]*>(.*?)</div>', chunk, re.S)
    if ps:
        paras = []
        for p in re.findall(r'<p[^>]*>(.*?)</p>', ps[0], re.S) or [ps[0]]:
            t = _javdb_clean_text(_javdb_strip_noise(p))
            if t:
                paras.append(t)
        if paras:
            return '\n\n'.join(paras)[:4000]
    return _javdb_clean_text(_javdb_strip_noise(chunk))[:4000]


def _javdb_strip_noise(c):
    """去掉头像/作者/时间/星级/按钮等非正文标记（对任意片段复用）。"""
    c = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', c, flags=re.S | re.I)
    c = re.sub(r'<img[^>]*>', ' ', c)
    c = re.sub(r'<i[^>]*class="[^"]*icon-star[^"]*"[^>]*>.*?</i>', ' ', c, flags=re.S)
    c = re.sub(r'<a[^>]*href="/users/[^"]*"[^>]*>.*?</a>', ' ', c, flags=re.S)
    c = re.sub(r'<span[^>]*class="(?:time|likes-count|score)[^"]*"[^>]*>.*?</span>', ' ', c, flags=re.S)
    c = re.sub(r'<time[^>]*>.*?</time>', ' ', c, flags=re.S)
    # 按钮（回覆/檢舉/讚等）多为无实义 href 的锚点或 button；正文里的真链接极少，整体去掉
    c = re.sub(r'<a[^>]*href="(?:#|javascript:)[^"]*"[^>]*>.*?</a>', ' ', c, flags=re.S | re.I)
    c = re.sub(r'<(button|form)[^>]*>.*?</\1>', ' ', c, flags=re.S | re.I)
    return c


def _javdb_parse_review_items(frag):
    """评论列表片段（/reviews/lastest 或视频页内嵌）-> 评论列表。
    每条评论为 dt.review-item（视频页整体结构里也可能以 article 出现，一并兼容）。"""
    items = []
    parts = re.split(r'(?=<dt[^>]*class="[^"]*review-item[^"]*"|<article\b)', frag)[1:]
    for chunk in parts:
        chunk = chunk[:30000]
        if 'panel-heading' in chunk[:500]:
            continue                      # 区块容器，非评论
        # 切分按下一个评论起点边界：截到本条自身的闭合标签，避免吞进后续区块
        end = min([p for p in (chunk.find('</dt>'), chunk.find('</article>'), chunk.find('<footer'),
                               chunk.find('related'), chunk.find('block-comments')) if p > 0] or [len(chunk)])
        own = chunk[:end] if end > 0 else chunk
        author, score, date = _javdb_item_fields(own)
        text = _javdb_item_text(own)
        if not text or not (author or date):
            continue                      # 无正文或缺少作者与日期的块不是评论
        items.append({'author': author[:60], 'score': score, 'date': date[:20], 'text': text})
    return items


def _javdb_parse_comments(page):
    """视频页 -> (评论总数, 评论列表)。总数取自 Reviews 标签（評論 (N)），列表复用评论项解析。"""
    total = None
    cm = re.search(r'(?:評論|评论|Reviews?)\s*[(（]\s*(\d+)\s*[)）]', page)
    if cm:
        total = int(cm.group(1))
    return total, _javdb_parse_review_items(page)


def fetch_javdb_comments(q, refresh=False):
    q = (q or '').strip()
    if not q or len(q) > 64:
        return {'ok': False, 'error': '缺少或非法的番号参数'}
    cache_key = q.upper()
    if not refresh:
        hit = _javdb_cache.get(cache_key)
        if hit and time.time() - hit[0] < JAVDB_CACHE_TTL:
            return {'ok': True, 'cached': True, **hit[1]}
    host, page, err = _javdb_get('/search?q=' + urllib.parse.quote(q) + '&f=all')
    if page is None:
        return {'ok': False, 'error': err[:600]}
    href, title = _javdb_find_video(page, q)
    if not href:
        if '/v/' not in page:
            return {'ok': False, 'error': 'javdb: 搜索页无任何结果卡片，疑似页面结构变化或被拦截（详见服务器日志）'}
        # 搜索正常但没有番号匹配：JavDB 未收录，同样写缓存避免反复请求
        result = {'q': q, 'videoUrl': '', 'videoTitle': '', 'total': 0, 'items': [], 'notFound': True}
        with _javdb_cache_lock:
            _javdb_cache[cache_key] = (time.time(), result)
        return {'ok': True, 'cached': False, **result}
    video_url = 'https://' + (host or 'javdb.com') + href

    # 评论走专用片段端点（视频页 Reviews 面板 AJAX 同源；未登录可读，结尾带登录提示文案）
    frag_host, frag, err2 = _javdb_get(href + '/reviews/lastest', referer=video_url)
    items = _javdb_parse_review_items(frag) if frag else []
    total = None
    # 评论端点失败或为空时回退解析视频页；有评论时也取一次视频页拿「評論 (N)」总数
    host2, vpage, err3 = _javdb_get(href, referer=video_url)
    if vpage is not None:
        page_total, page_items = _javdb_parse_comments(vpage)
        total = page_total
        if not items:
            items = page_items
    elif frag is None:
        log(f'javdb 视频页与评论端点均不可达（{q}）：{err2[:80]} / {err3[:80]}')
    result = {
        'q': q,
        'videoUrl': video_url,
        'videoTitle': title,
        'total': total if total is not None else len(items),
        'items': items,
    }
    if total and not items:
        log(f'javdb 评论总数 {total} 但未解析到条目（{q}），页面结构可能已变化')
    with _javdb_cache_lock:
        _javdb_cache[cache_key] = (time.time(), result)
    return {'ok': True, 'cached': False, **result}


# ==================== FANZA 预告片解析（详情抽屉「预告片」本地播放） ====================
# 流程：cid -> FANZA html5_player 页面（需年龄验证 Cookie）-> 解析内嵌 JSON 的 bitrates 数组
#       -> 得到带签名 token 的 cc3001.dmm.co.jp/pv/ 直链（可直接热链播放，无需 Referer）。
TRAILER_CACHE_TTL = 3600            # 直链 token 有效期未知，保守缓存 1 小时
_trailer_cache = {}                 # cid -> (写入时间戳, result)
_trailer_cache_lock = threading.Lock()


def fetch_fanza_trailer(cid, refresh=False):
    cid = (cid or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{4,40}', cid):
        return {'ok': False, 'error': '缺少或非法的番号 cid'}
    cache_key = cid
    now = time.time()
    if not refresh:
        hit = _trailer_cache.get(cache_key)
        if hit and now - hit[0] < TRAILER_CACHE_TTL:
            return {'ok': True, 'cached': True, **hit[1]}
    url = ('https://www.dmm.co.jp/service/digitalapi/-/html5_player/=/cid=' + cid +
           '/mtype=AhRVShI_/service=litevideo/mode=/width=560/height=360/')
    page, err = _ext_http_get(url, extra_headers={'Cookie': 'age_check_done=1',
                                                  'Referer': 'https://www.dmm.co.jp/'})
    if page is None:
        return {'ok': False, 'error': f'请求 FANZA 失败: {err[:120]}'}
    items = []
    m = re.search(r'"bitrates":(\[.*?\])', page, re.S)
    if m:
        try:
            for it in json.loads(m.group(1)):
                src = str(it.get('src') or '').replace('\\/', '/')
                label = str(it.get('bitrate') or '').strip()
                if src.startswith('//'):
                    src = 'https:' + src
                if label and src.startswith('https://'):
                    items.append({'label': label, 'url': src})
        except Exception:
            items = []
    result = {'cid': cid, 'items': items}
    with _trailer_cache_lock:
        _trailer_cache[cid] = (time.time(), result)
    return {'ok': True, 'cached': False, **result}


# ==================== javday 在线播放解析（详情抽屉「在线播放」本地 hls 播放） ====================
# 流程：番号 -> javday 搜索页取视频页链接 -> 视频页 HTML 直接内嵌 m3u8 直链（javday.homes）
#       -> 前端 hls.js 本地 <video> 播放，画面仅视频本身、无站点元素、无第三方播放器弹窗广告。
# 播放列表里的分段指向逐片子域（如 8bnuuk.javday.homes，CORS 不开放），但主域 javday.homes
# 的同路径分段免 Referer 且 CORS 全开（已实测），故 /__online-m3u8 把列表里的子域重写为主域
# 后原样返回，分段由浏览器直连主域拉取、不经本地服务器中转。
ONLINE_CACHE_TTL = 1800            # m3u8 直链与视频页绑定，保守缓存 30 分钟
_online_cache = {}                 # code -> (写入时间戳, result)
_online_cache_lock = threading.Lock()

_ONLINE_CODE_RE = re.compile(r'[a-z0-9]{2,12}-[a-z0-9]{2,8}')
_ONLINE_M3U8_HOST_RE = re.compile(r'^https://[a-z0-9.-]*javday\.homes/[a-z0-9./_-]+\.m3u8$', re.I)
_ONLINE_SUB_HOST_RE = re.compile(r'https://[a-z0-9-]+\.javday\.homes/', re.I)
# 中文字幕标记：搜索卡片/视频页标题里出现即视为中字版本，多结果时优先选择
_ONLINE_CN_RE = re.compile(r'中文字幕|中字|简体|繁体|简中|繁中')


def fetch_online_player(code, refresh=False):
    code = (code or '').strip().lower()
    if not _ONLINE_CODE_RE.fullmatch(code):
        return {'ok': False, 'error': '缺少或非法的番号参数'}
    now = time.time()
    if not refresh:
        hit = _online_cache.get(code)
        if hit and now - hit[0] < ONLINE_CACHE_TTL:
            return {'ok': True, 'cached': True, **hit[1]}
    hdrs = {'Referer': 'https://javday.app/'}
    search, err = _ext_http_get(f'https://javday.app/search/wd/{urllib.parse.quote(code)}/',
                                extra_headers=hdrs)
    if search is None:
        return {'ok': False, 'error': f'请求 javday 失败: {err[:120]}'}
    # 解析搜索卡片：(链接, 标题)；同番号常有无字/中字两个版本，标题带中字标记的优先
    entries, seen = [], set()
    for href, body in re.findall(r'<a href="(/videos/[a-zA-Z0-9_-]+/)"[^>]*class="videoBox">([\s\S]*?)</a>', search):
        if href in seen:
            continue
        seen.add(href)
        tm = re.search(r'<span class="title">([^<]*)</span>', body)
        entries.append((href, html.unescape(tm.group(1)).strip() if tm else ''))
    if not entries:
        return {'ok': False, 'error': '该番号在 javday 无搜索结果，可点右下角「搜索番号」确认'}
    cn_entries = [e for e in entries if _ONLINE_CN_RE.search(e[1])]
    link, vtitle = (cn_entries or entries)[0]
    page, err = _ext_http_get('https://javday.app' + link, extra_headers=hdrs)
    if page is None:
        return {'ok': False, 'error': f'请求 javday 视频页失败: {err[:120]}'}
    m3u8s = list(dict.fromkeys(re.findall(r'https://[a-z0-9.-]*javday\.homes/[^\s"\'\\<>]+?\.m3u8', page)))
    if not m3u8s:
        return {'ok': False, 'error': '该视频页没有可用播放源（可能需要登录或已下架）'}
    poster_m = re.search(r'(https://img\.javday\.app/upload/[^"\'\s\\)]+)', page)
    pt = re.search(r'<title>([^<]*)</title>', page)
    page_title = html.unescape(pt.group(1)).strip() if pt else ''
    result = {
        'code': code,
        'pageUrl': 'https://javday.app' + link,
        'poster': poster_m.group(1) if poster_m else '',
        'title': vtitle or page_title,
        'cnSub': bool(cn_entries) or bool(_ONLINE_CN_RE.search(page_title)),
        'sources': [{'name': f'线路{i + 1}', 'url': u} for i, u in enumerate(m3u8s)],
    }
    with _online_cache_lock:
        _online_cache[code] = (time.time(), result)
    return {'ok': True, 'cached': False, **result}


def proxy_online_m3u8(url):
    """javday 播放列表代理：校验域名后抓取，把逐片子域重写为主域（主域分段 CORS 全开）。"""
    url = (url or '').strip()
    if not _ONLINE_M3U8_HOST_RE.match(url):
        return None, '仅允许 javday.homes 的 m3u8 地址'
    text, err = _ext_http_get(url, extra_headers={'Referer': 'https://javday.app/'})
    if text is None:
        return None, f'抓取播放列表失败: {err[:120]}'
    if '#EXTM3U' not in text[:64]:
        return None, '返回内容不是有效的 m3u8 播放列表'
    return _ONLINE_SUB_HOST_RE.sub('https://javday.homes/', text), ''


TR_FILE = BASE / 'avdanyu-data' / 'translations.json'
_tr_lock = threading.Lock()


def load_translations_file():
    if not TR_FILE.exists():
        return {}
    try:
        with open(TR_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f'Error reading translations.json: {e}')
        return {}


def save_translations_file(diff=None, remove=None):
    patch = diff if (diff and isinstance(diff, dict)) else None
    drop = [k for k in remove if isinstance(k, str) and k] if (remove and isinstance(remove, list)) else []
    if not patch and not drop:
        return 200, json.dumps({'ok': True, 'count': len(load_translations_file()), 'written': 0, 'removed': 0})
    with _tr_lock:
        current = load_translations_file()
        written = 0
        removed = 0
        if patch:
            for k, v in patch.items():
                if k and v and current.get(k) != v:
                    current[k] = v
                    written += 1
        if drop:
            for k in drop:
                if k in current:
                    del current[k]
                    removed += 1
        if written > 0 or removed > 0 or not TR_FILE.exists():
            TR_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = TR_FILE.with_suffix('.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(current, f, ensure_ascii=False, indent=1)
            tmp_file.replace(TR_FILE)
            log(f'Updated translations on disk: +{written} / -{removed}. Total: {len(current)}')
        return 200, json.dumps({'ok': True, 'count': len(current), 'written': written, 'removed': removed})


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(BASE), **kw)

    def end_headers(self):
        if getattr(self, 'command', '') == 'GET' and (self.path.endswith('.html') or self.path == '/' or self.path == ''):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

    def do_OPTIONS(self):
        # 本服务仅供同源查看器使用，不发放跨域许可；
        # 同源请求无需 CORS，跨站预检因缺少许可被浏览器拦截
        self.send_response(204)
        self.send_header('Allow', 'GET, POST, OPTIONS')
        self.end_headers()

    def _token_ok(self):
        return self.headers.get('X-Avd-Token') == TOKEN

    def do_GET(self):
        if self.path == '/__health':
            raw = json.dumps({'app': 'avdanyu-server', 'ok': True, 'token': TOKEN}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return
        if self.path.split('?', 1)[0] == '/__magnets':
            # 会触发对外请求，与写接口同样要求同源令牌，防止跨站滥用本地服务器
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            q = (qs.get('q') or [''])[0]
            refresh = (qs.get('refresh') or [''])[0] in ('1', 'true')
            out = json.dumps(fetch_remote_magnets(q, refresh), ensure_ascii=False)
            return self._send_json(200, out)
        if self.path.split('?', 1)[0] == '/__comments':
            # 与 /__magnets 同理：触发对外请求，需同源令牌防跨站滥用
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            q = (qs.get('q') or [''])[0]
            refresh = (qs.get('refresh') or [''])[0] in ('1', 'true')
            out = json.dumps(fetch_javdb_comments(q, refresh), ensure_ascii=False)
            return self._send_json(200, out)
        if self.path.split('?', 1)[0] == '/__trailer':
            # 与 /__magnets 同理：同源令牌防跨站滥用
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cid = (qs.get('cid') or [''])[0]
            refresh = (qs.get('refresh') or [''])[0] in ('1', 'true')
            out = json.dumps(fetch_fanza_trailer(cid, refresh), ensure_ascii=False)
            return self._send_json(200, out)
        if self.path.split('?', 1)[0] == '/__online-player':
            # 与 /__magnets 同理：触发对外请求，需同源令牌防跨站滥用
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            code = (qs.get('code') or [''])[0]
            out = json.dumps(fetch_online_player(code), ensure_ascii=False)
            return self._send_json(200, out)
        if self.path.split('?', 1)[0] == '/__online-m3u8':
            # hls.js 经 xhrSetup 携带同源令牌；域名白名单防跨站滥用作 SSRF 跳板
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            u = (qs.get('u') or [''])[0]
            text, err = proxy_online_m3u8(u)
            if text is None:
                return self._send_json(400, json.dumps({'ok': False, 'error': err}, ensure_ascii=False))
            data = text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
        if self.path == '/__translations':
            data = load_translations_file()
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return
        super().do_GET()

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            return None, '请求体过大（上限 4MB）'
        try:
            return json.loads(self.rfile.read(length).decode('utf-8') or '{}'), None
        except Exception:
            return None, '请求体不是合法 JSON'

    def _send_json(self, status, out):
        data = out.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path == '/__translate':
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            payload, err = self._read_json_body()
            if payload is None:
                return self._send_json(400, json.dumps({'ok': False, 'error': err}))
            status, out = free_translate_relay(payload)
            return self._send_json(status, out)
        elif self.path == '/__translations':
            if not self._token_ok():
                return self._send_json(403, json.dumps({'ok': False, 'error': 'forbidden: missing or invalid token'}))
            payload, err = self._read_json_body()
            if payload is None:
                return self._send_json(400, json.dumps({'ok': False, 'error': err}))
            diff = payload.get('diff') or payload.get('translations') or {}
            remove = payload.get('remove') or []
            if not isinstance(diff, dict):
                return self._send_json(400, json.dumps({'ok': False, 'error': 'diff 必须是对象'}))
            if not isinstance(remove, list):
                return self._send_json(400, json.dumps({'ok': False, 'error': 'remove 必须是列表'}))
            status, out = save_translations_file(diff, remove)
            return self._send_json(status, out)
        self.send_error(404)

    def log_message(self, fmt, *args):
        pass   # 静音


class SafeHTTPServer(ThreadingHTTPServer):
    """socketserver 的 handle_error 定义在服务器类上（不是 Handler 类），
    连接被浏览器中途重置时若不在这里吞掉，异常会打死 serve_forever 主循环。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et and issubclass(et, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError)):
            return   # 客户端断开属正常现象，静默
        super().handle_error(request, client_address)


def log(msg):
    try:
        import datetime
        with open(BASE / 'scripts' / 'avdanyu-server.log', 'a', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S ') + msg + chr(10))
    except Exception:
        pass


if __name__ == '__main__':
    try:
        import curl_cffi  # noqa
    except ImportError:
        log('提示：未安装 curl_cffi，谷歌备用引擎不可用（静态服务正常）。安装：pip install curl_cffi')
    tcfg = _tr_load_config()
    if tcfg['api_key']:
        log(f'智谱 GLM 翻译已启用（默认模型 {tcfg["model"] or ZHIPU_DEFAULT_MODEL}），谷歌翻译作为备用引擎')
    else:
        log('未配置智谱 API Key（avdanyu-data/zhipu-config.json 或环境变量 ZHIPU_API_KEY），翻译使用免费谷歌引擎')
    log('翻译引擎优先级：智谱 GLM → 谷歌免费端点 → MyMemory（单条兜底）')
    try:
        srv = SafeHTTPServer(('127.0.0.1', PORT), Handler)
        log(f'服务器已启动 http://127.0.0.1:{PORT}/avdanyu-viewer.html')
        while True:
            try:
                srv.serve_forever(poll_interval=0.5)
            except Exception:
                # 兜底：任何漏网异常都不允许杀死服务，记录后继续服务
                import traceback
                log('serve_forever 异常（已自动恢复）：' + chr(10) + traceback.format_exc())
                time.sleep(0.5)
    except Exception:
        import traceback
        log('服务器异常退出：' + chr(10) + traceback.format_exc())
