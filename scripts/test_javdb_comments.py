#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JavDB 評論區解析器单元测试：不联网，直接喂合成 HTML 给解析函数。

运行：python scripts/test_javdb_comments.py
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent / 'avdanyu-server.py'
spec = importlib.util.spec_from_file_location('avdanyu_server', SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

FAILS = []


def check(name, cond, detail=''):
    mark = 'PASS' if cond else 'FAIL'
    print(f'[{mark}] {name}' + (f'  -> {detail}' if detail and not cond else ''))
    if not cond:
        FAILS.append(name)


# ---------- 真实捕获的 JavDB 地区封锁页 ----------
GEO_BLOCK = Path(__file__).resolve().parent.parent / 'javdb_geo_block.html'
geo_html = GEO_BLOCK.read_text(encoding='utf-8') if GEO_BLOCK.exists() else (
    'Due to copyright restrictions, access to this site is prohibited in the country '
    'where your internet is located. 由於版權限制，本站禁止了你的網路所在國家的訪問。')


# ---------- _javdb_get：多出口试探（monkeypatch _http_once，不联网）----------
SEARCH_OK = '<html><body><a class="box" href="/v/good01"><div class="video-title">IPX-177 结果</div></a></body></html>'


def fake_http_once_factory(route):
    """route: 出口(空串=直连) -> (text|None, err)。返回带 calls 记录的 fake。"""
    calls = []
    def fake(url, timeout=15, headers=None, proxy=''):
        calls.append((url, proxy))
        return route.get(proxy if proxy else 'direct', (None, 'connection refused'))
    fake.calls = calls
    return fake


def reset_exits():
    srv._javdb_exit_state = {'ok': None, 'bad': {}}


_real_http_once = srv._http_once

# 1) 全部出口地区封锁 -> 返回分流指引
srv._http_once = fake_http_once_factory({'direct': (geo_html, ''), 'http://127.0.0.1:7897': (geo_html, ''),
                                         'http://127.0.0.1:7890': (geo_html, '')})
reset_exits()
host, text, err = srv._javdb_get('/search?q=ABP-769&f=all')
check('全部出口封锁：识别为错误', text is None and '屏蔽' in err, f'err={err[:80]}')
check('全部出口封锁：提示含分流/专用代理指引',
      'AVDANYU_JAVDB_PROXY' in err and '分流' in err, err[:200])
check('全部出口封锁：提示其他功能不受影响', '预告片' in err, err[:200])

# 2) 直连封锁但某代理出口可用 -> 自动找到并成功，且记住该出口
reset_exits()
fake = fake_http_once_factory({
    'direct': (geo_html, ''),
    'http://127.0.0.1:7890': (SEARCH_OK, ''),
})
srv._http_once = fake
host, text, err = srv._javdb_get('/search?q=ABP-769&f=all')
check('多出口试探：绕开封锁出口成功', text is not None and host == 'javdb.com', f'host={host} err={err[:80]}')
check('多出口试探：可用出口被记住', srv._javdb_exit_state['ok'] == 'http://127.0.0.1:7890',
      str(srv._javdb_exit_state))
fake.calls.clear()
host2, text2, err2 = srv._javdb_get('/v/good01')
proxies_used = {p for _, p in fake.calls}
check('多出口试探：后续请求直接用记住的出口', text2 is not None and proxies_used == {'http://127.0.0.1:7890'},
      str(proxies_used))

# 3) AVDANYU_JAVDB_PROXY 显式指定 -> 最优先使用
reset_exits()
os.environ['AVDANYU_JAVDB_PROXY'] = 'http://127.0.0.1:3344'
try:
    srv._http_once = fake_http_once_factory({
        'http://127.0.0.1:3344': (SEARCH_OK, ''),
        'direct': (geo_html, ''),
    })
    host3, text3, err3 = srv._javdb_get('/search?q=x')
    check('专用代理：优先使用 AVDANYU_JAVDB_PROXY', text3 is not None, f'err={err3[:80]}')
    check('专用代理：成功后被记住', srv._javdb_exit_state['ok'] == 'http://127.0.0.1:3344',
          str(srv._javdb_exit_state))
finally:
    del os.environ['AVDANYU_JAVDB_PROXY']

# 4) 记住的出口后来失效（封锁）-> 换到下一个可用出口
reset_exits()
srv._http_once = fake_http_once_factory({
    'direct': (None, 'TLS reset'),
    'http://127.0.0.1:7897': (geo_html, ''),
    'http://127.0.0.1:7890': (geo_html, ''),
    'http://127.0.0.1:7891': (SEARCH_OK, ''),
})
srv._javdb_exit_state['ok'] = 'http://127.0.0.1:7890'
host4, text4, err4 = srv._javdb_get('/search?q=x')
check('出口失效降级：换到下一个可用出口', text4 is not None and srv._javdb_exit_state['ok'] == 'http://127.0.0.1:7891',
      f'err={err4[:80]} ok={srv._javdb_exit_state["ok"]}')

# 5) 全部出口网络不可达 -> 汇总错误
reset_exits()
srv._http_once = fake_http_once_factory({})
host5, text5, err5 = srv._javdb_get('/search?q=x')
check('全部出口不可达：返回网络错误', text5 is None and '不可达' in err5, err5[:100])

# 6) 出口全部处于坏出口记忆时必须真实重试（清空记忆再探一轮），不能空手而归
reset_exits()
now = time.time()
srv._javdb_exit_state = {'ok': None, 'bad': {e: now for e in srv._javdb_exit_candidates()}}
srv._http_once = fake_http_once_factory({'direct': (SEARCH_OK, '')})
host6, text6, err6 = srv._javdb_get('/search?q=x')
check('坏记忆不吞请求：清空后重试成功', text6 is not None and host6 == 'javdb.com', f'err={err6[:80]}')

reset_exits()
now = time.time()
srv._javdb_exit_state = {'ok': None, 'bad': {e: now for e in srv._javdb_exit_candidates()}}
srv._http_once = fake_http_once_factory({'direct': (geo_html, '')})
host7, text7, err7 = srv._javdb_get('/search?q=x')
check('坏记忆不吞请求：重试后错误信息仍完整', text7 is None and '屏蔽' in err7 and 'AVDANYU_JAVDB_PROXY' in err7,
      err7[:100])

srv._http_once = _real_http_once
reset_exits()

# ---------- 搜索页解析：两套卡片模板 + 精确匹配 ----------
SEARCH_PAGE = '''<html><body>
<div class="item"><a class="box" href="/v/wqzKbd" title="IPX-176 其他作品">
  <div class="cover"><img src="https://c0.jdbstatic.com/covers/0e/xxx.jpg"></div>
  <div class="video-title is-vertical-subtitle">IPX-176 何か別の作品</div>
  <div class="meta"><span class="score">3.12 分, 87 人評價</span><span class="time">08/19/2018</span></div>
</a></div>
<div class="item"><a class="box" href="/v/aB3xYz" title="IPX-177 桃太郎">
  <div class="cover"><img src="https://c0.jdbstatic.com/covers/ab/yyy.jpg"></div>
  <div class="video-title is-vertical-subtitle">IPX-177 いもうと桃太郎</div>
  <div class="meta"><span class="score">4.34 分, 601 人評價</span><span class="time">01/11/2019</span></div>
</a></div>
<div class="item"><a class="box" href="/v/zZ9qWe" title="IPX-1770 邻号误配">
  <div class="video-title">IPX-1770 邻号</div>
</a></div>
</body></html>'''
href, title = srv._javdb_find_video(SEARCH_PAGE, 'IPX-177')
check('搜索命中精确番号的视频页', href == '/v/aB3xYz', f'href={href}')
check('搜索标题提取', 'IPX-177' in title, f'title={title}')

href2, _ = srv._javdb_find_video(SEARCH_PAGE, 'ipx177')   # 无横杠写法
check('无横杠番号同样命中', href2 == '/v/aB3xYz', f'href={href2}')

href3, _ = srv._javdb_find_video(SEARCH_PAGE, 'ZZZ-999')
check('未收录番号返回空', href3 == '', f'href={href3}')

# ---------- 视频页评论解析：平铺 + 楼中楼 + 按钮噪音 ----------
VIDEO_PAGE = '''<html><body>
<div class="panel"><div class="panel-block"><strong>ID:</strong> IPX-177</div></div>
<div class="block-comments">
  <div class="panel panel-simple">
    <div class="panel-heading"><strong>評論區</strong></div>
    <div class="tabs-wrapper"><ul class="tabs"><li class="is-active"><a>磁鏈</a></li>
      <li><a>評論 (3)</a></li></ul></div>
    <div class="panel-body">
      <article class="review-item" id="comment-100">
        <figure class="media-left"><p class="image is-48x48"><img src="/avatars/u1.png"></p></figure>
        <div class="media-content">
          <div class="content">
            <p>
              <a class="user-name" href="/users/abc123">影迷甲</a>
              <span class="score">5 分</span>
              <br>
              画面和剧情都在线，桃太郎演得非常好，强烈推荐！
              <br>
              <small><time>2019-02-14</time> · <a href="javascript:void(0)">回覆</a> ·
              <a href="javascript:void(0)">檢舉</a></small>
            </p>
          </div>
        </div>
      </article>
      <article class="review-item" id="comment-101">
        <a class="user-name" href="/users/def456">路人乙</a>
        <div class="comment">字幕哪里有下载？求好心人分享。&lt;br>谢谢</div>
        <time datetime="2020-05-01">2020-05-01</time>
        <a href="javascript:void(0)">讚</a>
      </article>
      <article class="review-item" id="comment-102">
        <a class="user-name" href="/users/ghi789">回复者丙</a>
        <div class="comment">回复 楼上：官方版本自带字幕。</div>
        <span class="stars">★★★★</span>
        <time>2021-08-09</time>
      </article>
    </div>
  </div>
</div>
<footer class="footer"><span>© 2026 javdb580.com</span></footer>
</body></html>'''
total, items = srv._javdb_parse_comments(VIDEO_PAGE)
check('评论总数从标签提取', total == 3, f'total={total}')
check('解析出 3 条评论', len(items) == 3, f'len={len(items)}')
if len(items) == 3:
    a, b, c = items
    check('作者提取（user-name 链接）', a['author'] == '影迷甲', a['author'])
    check('评分提取（N 分）', a['score'] == '5', a['score'])
    check('日期提取（time 标签）', a['date'] == '2019-02-14', a['date'])
    check('正文干净（无按钮词/头像）', '画面和剧情都在线' in a['text'] and '回覆' not in a['text'] and '檢舉' not in a['text'], a['text'][:80])
    check('第二条：datetime 属性日期', b['date'] == '2020-05-01' and '讚' not in b['text'] and '字幕哪里有下载' in b['text'], f"date={b['date']} text={b['text'][:60]}")
    check('星级字形兜底评分', c['score'] == '4', c['score'])
    check('HTML 实体转义', '&lt;' not in b['text'], b['text'][:60])

# ---------- 真实结构：/reviews/lastest 片段（2026-10 从 javdb.com 实抓的骨架） ----------
REVIEWS_FRAGMENT = '''<article class="message video-panel"><div class="message-body"><dl class="review-items">
<dt class="review-item" id="review-item-245074835">
 <div class="review-title">
  <div class="report is-pulled-right">
   <form class="button_to" method="post" action="/v/nK7Jne/reviews/245074835/report"><button class="button is-small is-danger" type="submit"><span class="label">檢舉</span></button><input type="hidden" name="authenticity_token" value="x"></form>
  </div>
  <div class="likes is-pulled-right">
   <form class="button_to" method="post" action="/v/nK7Jne/reviews/245074835/like"><button class="button is-small is-info" type="submit"><span class="label">贊</span><span class="likes-count">4</span></button><input type="hidden" name="authenticity_token" value="y"></form>
  </div>
  15***5&nbsp; &nbsp;<span class="score-stars"><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star gray"></i><i class="icon-star gray"></i></span>&nbsp; <span class="time">2026-08-13</span>
 </div>
 <div class="content"><p>把瑜伽裤全脱了干是最大的败笔！</p></div>
</dt>
<dt class="review-item" id="review-item-249327191">
 <div class="review-title">
  <div class="report is-pulled-right"><form class="button_to" method="post"><button type="submit"><span class="label">檢舉</span></button></form></div>
  <div class="likes is-pulled-right"><form class="button_to" method="post"><button type="submit"><span class="label">贊</span><span class="likes-count">1</span></button></form></div>
  sn***n&nbsp; &nbsp;<span class="score-stars"><i class="icon-star"></i><i class="icon-star gray"></i><i class="icon-star gray"></i><i class="icon-star gray"></i><i class="icon-star gray"></i></span>&nbsp; <span class="time">2026-09-05</span>
 </div>
 <div class="content"><p>这都干不喷，看着确实闹心。。。</p></div>
</dt>
<dt class="review-item" id="review-item-244729304">
 <div class="review-title">
  <div class="report is-pulled-right"><form class="button_to" method="post"><button type="submit"><span class="label">檢舉</span></button></form></div>
  <div class="likes is-pulled-right"><form class="button_to" method="post"><button type="submit"><span class="label">贊</span><span class="likes-count">0</span></button></form></div>
  44***4&nbsp; &nbsp;<span class="score-stars"><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i></span>&nbsp; <span class="time">2026-08-11</span>
 </div>
 <div class="content"><p>DSOD-067被下媚药的那段太顶了</p></div>
</dt>
<div class="message">Already commented on this video. Want to see more reviews? Please log in first...</div>
</dl></div></article>'''
frag_items = srv._javdb_parse_review_items(REVIEWS_FRAGMENT)
check('真实结构：解析出 3 条评论', len(frag_items) == 3, f'len={len(frag_items)}')
if len(frag_items) == 3:
    x, y, z = frag_items
    check('真实结构：作者（review-title 尾部直接文本，含&nbsp;）', x['author'] == '15***5', repr(x['author']))
    check('真实结构：亮星计分排除灰星（3/1/5 颗）', (x['score'], y['score'], z['score']) == ('3', '1', '5'),
          f"{x['score']}/{y['score']}/{z['score']}")
    check('真实结构：日期（span.time）', x['date'] == '2026-08-13', x['date'])
    check('真实结构：正文（div.content p）', x['text'] == '把瑜伽裤全脱了干是最大的败笔！', repr(x['text']))
    check('真实结构：点赞数/檢舉不混入正文与作者', all('贊' not in i['text'] and '檢舉' not in i['author'] for i in (x, y, z)),
          str([i['author'] for i in (x, y, z)]))
    check('真实结构：登录提示文案不入列', all('log in' not in it['text'] for it in frag_items), str(frag_items))

# 楼中楼：嵌套 article 的切分
NESTED = '''<div class="block-comments">
<article id="p1"><a class="user-name" href="/users/p1">楼主</a><div class="comment">主楼内容：求种子</div><time>2022-01-01</time>
  <article id="c1"><a class="user-name" href="/users/c1">楼下</a><div class="comment">楼中楼回复：评论区自取</div><time>2022-01-02</time></article>
</article>
<article id="p2"><a class="user-name" href="/users/p2">另一楼</a><div class="comment">第二条主楼</div><time>2022-02-02</time></article>
</div>'''
_, nested = srv._javdb_parse_comments(NESTED)
texts = [it['text'] for it in nested]
check('楼中楼：两条主楼都被解析', any('求种子' in t for t in texts) and any('另一楼' in t or '第二条主楼' in t for t in texts), str(texts))
check('楼中楼：子回复独立成条', any('楼中楼回复' in t for t in texts), str(texts))

# 空评论页（Reviews(0)）
EMPTY = '<div class="tabs"><a>評論 (0)</a></div><div class="panel-body"></div>'
t0, i0 = srv._javdb_parse_comments(EMPTY)
check('零评论页：总数 0 列表空', t0 == 0 and i0 == [], f'total={t0} items={len(i0)}')

# 英文界面（locale 未生效时）
EN = '<li><a>Reviews (12)</a></li>'
t1, _ = srv._javdb_parse_comments(EN)
check('英文 Reviews 标签总数', t1 == 12, f'total={t1}')

# fetch_javdb_comments：未收录路径 + 缓存（monkeypatch 网络）
def fake_javdb_get_factory(pages):
    def fake(path, referer=None):
        return ('javdb.com', pages.get(path.split('?')[0], ''), '')
    return fake


_real_get = srv._javdb_get
srv._javdb_get = fake_javdb_get_factory({'/search': '<a class="box" href="/v/abc12"><div class="video-title">IPX-999 不相关</div></a>'})
r = srv.fetch_javdb_comments('IPX-777')
srv._javdb_get = _real_get
check('未收录番号：ok 且 notFound', r.get('ok') and r.get('notFound') and r['items'] == [], str(r)[:120])

srv._javdb_get = fake_javdb_get_factory({
    '/search': SEARCH_PAGE,
    '/v/aB3xYz': VIDEO_PAGE,
    '/v/aB3xYz/reviews/lastest': REVIEWS_FRAGMENT,
})
srv._javdb_cache.clear()
r2 = srv.fetch_javdb_comments('IPX-177')
r3 = srv.fetch_javdb_comments('IPX-177')     # 第二次应命中缓存
srv._javdb_get = _real_get
check('完整链路：搜索→评论端点→条目', r2.get('ok') and len(r2.get('items', [])) == 3, str(r2)[:150])
check('完整链路：总数来自视频页标签', r2.get('total') == 3, f"total={r2.get('total')}")
check('完整链路：视频页 URL 拼接', r2.get('videoUrl') == 'https://javdb.com/v/aB3xYz', r2.get('videoUrl', ''))
check('第二次请求命中缓存', r3.get('cached') is True, str(r3.get('cached')))
check('缓存条目与首次一致', r3['items'] == r2['items'] and r3['videoUrl'] == r2['videoUrl'])

# 评论端点失败 → 回退视频页解析
srv._javdb_get = fake_javdb_get_factory({
    '/search': SEARCH_PAGE,
    '/v/aB3xYz': VIDEO_PAGE,
    '/v/aB3xYz/reviews/lastest': None,   # 模拟不可达由空串触发？空串等于无条目，故再单独测 None
})
srv._javdb_cache.clear()
r4 = srv.fetch_javdb_comments('IPX-177')
srv._javdb_get = _real_get
check('回退：无评论端点时用视频页条目', r4.get('ok') and len(r4.get('items', [])) == 3, str(r4)[:150])

print()
if FAILS:
    print(f'共 {len(FAILS)} 项失败: {FAILS}')
    sys.exit(1)
print('全部通过 ✓')
