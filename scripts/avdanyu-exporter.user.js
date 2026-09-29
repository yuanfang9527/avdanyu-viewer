// ==UserScript==
// @name         avdanyuwiki 作品导出器
// @name:zh-CN   avdanyuwiki 作品导出器
// @namespace    https://github.com/local/avdanyu-exporter
// @version      2.0.7
// @description  抓取 avdanyuwiki.com 当月/当年作品，导出为 JS 数据文件，供 avdanyu-viewer.html 读取展示（含 Cloudflare 挑战识别与退避 / 非规范地址提示 / CSP 安全样式注入；不改默认并发与请求间隔）
// @match        https://avdanyuwiki.com/*
// @match        https://www.avdanyuwiki.com/*
// @match        http://avdanyuwiki.com/*
// @match        http://www.avdanyuwiki.com/*
// @match        http://127.0.0.1/*
// @match        http://localhost/*
// @grant        GM_xmlhttpRequest
// @connect      *
// @grant        GM_addStyle
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // 本地查看器页面（file:// 或 localhost）：提供带 Blob 的 GM 桥。
  // 图床无 CORS 头，查看器靠此桥抓取封面图存入 IndexedDB 做持久缓存。
  if (location.hostname === '127.0.0.1' || location.hostname === 'localhost') {
    try {
      const pw = (typeof unsafeWindow !== 'undefined') ? unsafeWindow : window;
      pw.__avdGMFetch = (url, opts = {}) => new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method: opts.method || 'GET',
          url,
          data: opts.body || undefined,        // 转发请求体（POST JSON 等）
          headers: opts.headers || {},         // 转发请求头（Content-Type / 令牌等）
          responseType: 'blob',
          timeout: opts.timeout || 20000,
          onload: r => resolve({
            ok: r.status >= 200 && r.status < 300,
            status: r.status,
            blob: () => Promise.resolve(r.response),
            text: () => Promise.resolve(r.response.text()),
            json: () => r.response.text().then(t => JSON.parse(t)),
          }),
          onerror: () => reject(new Error('network error')),
          ontimeout: () => reject(new Error('timeout')),
        });
      });
    } catch (e) { /* GM 不可用时查看器降级为远程加载 */ }
  }

  // 归档页 URL 识别：
  //   月度  /2026/08/   /2026/08/page/2/   （月份须两位：站点只认 /2018/09/，不认 /2018/9/）
  //   年度  /2026/      /2026/page/3/
  //   文章  /2026/08/13/slug/
  // 返回 { kind, y, m, d, slug, page, canonical }；非归档/文章页返回 null。
  // canonical=false 表示月份/日未零填充——这类地址站点可能不返回内容，入口处会提示跳转。
  function parseArchivePath(p) {
    let m = p.match(/^\/(\d{4})\/(\d{1,2})\/(?:page\/(\d+)\/)?$/);
    if (m) return { kind: 'month', y: m[1], m: m[2].padStart(2, '0'),
      page: m[3] ? parseInt(m[3], 10) : 1, canonical: m[2].length === 2 };
    m = p.match(/^\/(\d{4})\/(?:page\/(\d+)\/)?$/);
    if (m) return { kind: 'year', y: m[1], page: m[2] ? parseInt(m[2], 10) : 1 };
    m = p.match(/^\/(\d{4})\/(\d{1,2})\/(\d{1,2})\/([^/]+)\/?$/);
    if (m) return { kind: 'post', y: m[1], m: m[2].padStart(2, '0'), d: m[3].padStart(2, '0'),
      slug: m[4], canonical: m[2].length === 2 && m[3].length === 2 };
    return null;
  }
  const route = parseArchivePath(location.pathname);

  const ORIGIN = location.origin;
  const FETCH_RETRY = 3;          // 单次请求失败重试次数
  const WORKER_DELAY = 200;       // 每个并发 worker 的请求间隔(毫秒)
  const CONC_DEFAULT = 16;        // 默认并发数（用户手调值，勿动）
  const CONC_MIN = 1, CONC_MAX = 32;

  // Cloudflare 挑战：连续被拦到 CF_MAX_STRIKES 次就收手——越试越容易被加深风控
  const CF_MAX_STRIKES = 3;
  const CF_BACKOFF = [15000, 45000, 90000];
  let cfStrikes = 0;
  const cfHalted = () => cfStrikes >= CF_MAX_STRIKES;
  class ChallengeError extends Error { }
  class ChallengeHaltError extends Error { }

  // 需要退避而不是"立刻重试"的状态码。
  // 403/429 = Cloudflare 挑战或限流；503/520-524 = 边缘或源站被压垮
  // （实测该站点每次 HTML 请求都回源、CF 不缓存，硬冲会直接把源站压出 5xx）
  const THROTTLE_STATUS = [403, 429, 503, 520, 521, 522, 523, 524];

  // 挑战页指纹。注意：只认挑战页独有的标记，不能认 "challenge-platform"
  // （正文页若嵌 Turnstile 也会出现该字符串，会误判）
  const CF_MARKERS = ['_cf_chl_opt', 'cf_chl_', 'challenge-form', 'challenge-stage',
    'cf-browser-verification', 'cf-please-wait'];
  function looksLikeChallenge(html) {
    if (!html) return false;
    const head = html.slice(0, 6000);
    return CF_MARKERS.some(m => head.includes(m));
  }
  // 挑战页 DOM 特征。新旧两版都要覆盖，且已用真实挑战页样本校准：
  // 老版挑战页有 #challenge-form / #challenge-stage，
  // 现行 "managed" 版只有 <span id="challenge-error-text"> 与 window._cf_chl_opt。
  const CF_DOM_SEL = '#challenge-form, #challenge-stage, #challenge-running,'
    + ' #challenge-error-text, #cf-please-wait, .cf-browser-verification,'
    + ' [id^="cf-chl"], [name^="cf_chl"]';
  function isChallengePage(doc) {
    try {
      if (doc.querySelector(CF_DOM_SEL)) return true;
      const t = (doc.title || '').trim();
      // 挑战页标题就是这几个字，必须整串匹配——
      // 否则 "Just a moment of summer" 这类正常标题会被误判成拦截
      if (/^(just a moment|请稍候|稍等片刻)[.…]*$/i.test(t)) return true;
      return /(^|\|)\s*cloudflare\s*$/i.test(t);   // 拦截页/错误页的标题后缀
    } catch (e) { return false; }
  }

  // 熔断时抛出的错误：把"为什么停"和"接下来怎么办"一起带上，而不是只丢一句报错
  function haltErr() {
    return new ChallengeHaltError(
      `连续 ${cfStrikes} 次被 Cloudflare 拦截，已熔断（继续硬冲只会被加深风控）。`);
  }

  // ---------- 样式注入（CSP 安全梯度） ----------
  // 站点部分响应带 nonce-CSP：没有 nonce 的 <style> 会被拒（"Refused to apply inline style"）。
  // 三轮降级，任一轮成功即返回：
  //   ① 构造式样式表 CSSStyleSheet + adoptedStyleSheets —— 不参与 style-src 校验，最稳
  //   ② GM_addStyle —— 走扩展层，多数情况不受页面 CSP 约束
  //   ③ <style nonce=…> —— 从页面已有带 nonce 的标签上"借"一个 nonce
  function harvestNonce() {
    try {
      const el = document.querySelector('script[nonce], style[nonce], link[nonce]');
      return (el && (el.nonce || el.getAttribute('nonce'))) || '';
    } catch (e) { return ''; }
  }
  function injectCSS(css, id) {
    try {
      if (window.CSSStyleSheet && 'replaceSync' in CSSStyleSheet.prototype
        && 'adoptedStyleSheets' in Document.prototype) {
        const sheet = new CSSStyleSheet();
        sheet.replaceSync(css);
        document.adoptedStyleSheets = document.adoptedStyleSheets.concat(sheet);
        return 'adopted';
      }
    } catch (e) { /* 降级 */ }
    try {
      if (typeof GM_addStyle === 'function') { GM_addStyle(css); return 'gm'; }
    } catch (e) { /* 降级 */ }
    const nonce = harvestNonce();
    const s = document.createElement('style');
    if (id) s.id = id;
    if (nonce) s.nonce = nonce;      // 必须在插入前设置，插入后再设不生效
    s.textContent = css;
    document.head.appendChild(s);
    return nonce ? 'nonce' : 'plain';
  }

  injectCSS(`
    #avd-panel { position: fixed; right: 16px; bottom: 16px; z-index: 999999;
      width: 320px; background: #14181c; color: #dde5ee; border: 1px solid #445566;
      border-radius: 8px; font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      box-shadow: 0 6px 24px rgba(0,0,0,.5); }
    #avd-panel * { box-sizing: border-box; }
    .avd-head { padding: 8px 12px; background: #1c242c; border-radius: 8px 8px 0 0;
      cursor: pointer; font-weight: 700; display:flex; justify-content:space-between; }
    .avd-body { padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
    .avd-body.avd-hidden { display: none; }
    #avd-panel button { background: #00b020; color: #fff; border: 0; border-radius: 4px;
      padding: 6px 8px; cursor: pointer; font-size: 12px; }
    #avd-panel button.avd-sec { background: #2c3a47; }
    #avd-panel button.avd-all { background: #40bcf4; color: #14181c; font-weight: 700; }
    #avd-panel button:disabled { background: #556; cursor: default; opacity:.6; }
    #avd-panel .avd-row { display: flex; gap: 6px; flex-wrap: wrap; }
    #avd-panel .avd-status { font-size: 12px; color: #9ab; white-space: pre-wrap; max-height: 130px; overflow-y: auto; }
    #avd-panel .avd-prog { display: block; width: 100%; height: 4px; border: 0; padding: 0;
      background: #2c3a47; border-radius: 2px; -webkit-appearance: none; appearance: none; }
    #avd-panel .avd-prog::-webkit-progress-bar { background: #2c3a47; border-radius: 2px; }
    #avd-panel .avd-prog::-webkit-progress-value { background: #00b020; border-radius: 2px; }
    #avd-panel .avd-prog::-moz-progress-bar { background: #00b020; border-radius: 2px; }
    #avd-panel .avd-tip { font-size: 11px; color: #6b7a89; }
    /* 标签与输入框全部走类，杜绝 style="…" 内联属性（CSP 下会被整条丢掉） */
    #avd-panel .avd-lab { font-size: 12px; color: #9ab; display: inline-flex; align-items: center; gap: 5px; }
    #avd-panel .avd-num { background: #2c3a47; color: #dde5ee; border: 1px solid #445566;
      border-radius: 4px; padding: 3px 5px; font-size: 12px; }
    #avd-panel .avd-num-sm { width: 52px; }
    #avd-panel .avd-num-xs { width: 46px; }
  `, 'avd-style');

  // ---------- 工具 ----------
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  async function fetchDoc(url) {
    let lastErr = null;
    for (let attempt = 1; attempt <= FETCH_RETRY; attempt++) {
      try {
        const resp = await fetch(url, {
          credentials: 'same-origin',   // 必须带 cookie：cf_clearance 靠它才能过验证
          headers: { 'Accept': 'text/html,application/xhtml+xml' },
        });
        const html = await resp.text();
        // 403 / 429 / 503 / 52x / 正文是 challenge 页：拿到的不是作品列表。
        // 这类"拦截"靠立刻重试解决不了（只会被加深风控），交给退避逻辑；
        // 也绝不能当数据解析，否则静默导出 0 部
        if (THROTTLE_STATUS.includes(resp.status) || looksLikeChallenge(html)) {
          const isChallenge = resp.status === 403 || looksLikeChallenge(html);
          throw new ChallengeError(isChallenge
            ? 'Cloudflare 挑战页（HTTP ' + resp.status + '）'
            : '边缘/源站过载（HTTP ' + resp.status + '）');
        }
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return new DOMParser().parseFromString(html, 'text/html');
      } catch (e) {
        lastErr = e;
        if (e instanceof ChallengeError) {
          cfStrikes++;
          if (attempt < FETCH_RETRY && !cfHalted()) {
            const wait = CF_BACKOFF[Math.min(cfStrikes, CF_BACKOFF.length) - 1];
            log(`🛑 ${e.message}，退避 ${Math.round(wait / 1000)} 秒（${cfStrikes}/${CF_MAX_STRIKES}）`);
            await sleep(wait);
          }
        } else if (attempt < FETCH_RETRY) {
          log(`⚠️ 抓取失败（${e.message}），${3 * attempt} 秒后重试…`);
          await sleep(3000 * attempt);
        }
      }
    }
    throw lastErr;
  }

  // ---------- 字段解析 ----------
  // 日文标签 → 数据字段。normalize 后（去空白、小写）查表。
  const LABEL_MAP = {
    '出演者': 'actresses', '出演': 'actresses',
    '出演男優': 'maleActors', '出演av男優': 'maleActors',
    '監督': 'director',
    '配信開始日': 'deliveryDate',
    '商品発売日': 'releaseDate',
    '収録時間': 'duration',
    'シリーズ': 'series',
    'メーカー': 'maker',
    'レーベル': 'label',
    'ジャンル': 'genres',
    '配信品番': 'deliveryCode',
    'メーカー品番': 'makerCode',
    '品番': 'code',
  };
  const normLabel = s => s.replace(/[\s　]+/g, '').toLowerCase();

  // 把一段 HTML 值解析为 { text, links:[{name,url}] }
  function parseVal(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    const text = div.textContent.replace(/[ \t　]+/g, ' ').trim();
    const links = [...div.querySelectorAll('a')]
      .map(a => ({ name: a.textContent.trim(), url: a.href }))
      .filter(x => x.name);
    return { text, links };
  }

  // ジャンル：无链接时按空白切分为字符串数组；有链接时混合为 [{name,url}] 与字符串
  function parseGenres(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    const hasLinks = !!div.querySelector('a');
    const out = [];
    for (const node of div.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        for (const t of node.textContent.split(/[\s　]+/)) {
          if (t) out.push(hasLinks ? t : t);
        }
      } else if (node.nodeName === 'A') {
        const name = node.textContent.trim();
        if (name) out.push(hasLinks ? { name, url: node.href } : name);
      }
    }
    return out;
  }

  function applyField(work, key, valueHtml) {
    if (key === 'genres') {
      const g = parseGenres(valueHtml);
      if (g.length) work.genres = g;
      return;
    }
    const v = parseVal(valueHtml);
    if (key === 'actresses' || key === 'maleActors') {
      // 人物字段保留原文与链接，展示端优先用链接名
      if (v.text || v.links.length) work[key] = v;
      return;
    }
    if (key === 'director' || key === 'series' || key === 'maker' || key === 'label') {
      if (v.text || v.links.length) work[key] = v;
      return;
    }
    // 日期 / 时长 / 品番：纯文本，空值忽略
    if (v.text) work[key] = v.text;
  }

  // 只把文章详情页当作作品 URL；归档页 URL 不能作为作品身份键。
  function postUrl(href) {
    if (!href) return '';
    try {
      const u = new URL(href, location.href);
      return u.origin === location.origin && /^\/\d{4}\/\d{2}\/\d{2}\/[^/]+\/?$/.test(u.pathname)
        ? u.origin + u.pathname : '';
    } catch (e) { return ''; }
  }

  function postLinksOnPage(doc) {
    const links = new Map();
    // 部分老文章没有删除报告链接，但归档页顶部的文章目录仍有详情链接。
    for (const a of doc.querySelectorAll('h4 a.tooltip-link')) {
      const title = (a.dataset.title || a.textContent || '').trim();
      const url = postUrl(a.href);
      if (title && url && !links.has(title)) links.set(title, url);
    }
    return links;
  }

  // ---------- 单篇文章解析 ----------
  function parseArticle(art, postLinks) {
    const work = {};
    const hEl = art.querySelector('h2') || art.querySelector('h1.entry-title') || art.querySelector('h1');
    work.title = hEl ? hEl.textContent.trim() : '';
    if (/^post-\d+$/.test(art.id || '')) work.postId = art.id;

    // 优先删除报告链接，再用归档页文章目录；文章页自身可直接用当前地址。
    const rep = art.querySelector('a.report-list-link');
    work.url = (rep && postUrl(rep.href)) || (postLinks && postLinks.get(work.title))
      || postUrl(location.href) || '';
    const dm = (work.url || '').match(/\/(\d{4})\/(\d{2})\/(\d{2})\//);
    if (dm) work.date = `${dm[1]}/${dm[2]}/${dm[3]}`;

    // 封面：直接 <img>（DMM 图床等）
    const img = art.querySelector('img');
    if (img && img.src && !/spacer|blank|1x1/i.test(img.src)) work.cover = img.src;

    // 封面兜底：老卡片无 <img>，但 DMM 推广链接（al.dmm.co.jp）的 lurl 里带 cid，
    // 直接构造官方封面图地址（pics.dmm.co.jp 图床，实测可外链）
    if (!work.cover) {
      const aff = art.querySelector('a[href*="al.dmm.co.jp"]');
      if (aff) {
        const cid = (aff.href.match(/[?&]lurl=[^&]*?cid(?:%3D|=)([a-z0-9_]+)/i) || [])[1];
        if (cid) work.cover = `https://pics.dmm.co.jp/digital/video/${cid}/${cid}pl.jpg`;
      }
    }

    // mgstage  affiliate 挂件：记录品番，封面后续尝试解析
    const w = art.querySelector('script#mgs_Widget_affiliate');
    if (w && w.src) {
      try {
        const wp = new URL(w.src);
        work.widgetCode = wp.searchParams.get('p') || '';
        work.widgetClass = wp.searchParams.get('class') || '';
        work.widgetUrl = w.src;
        if (!work.cover && work.widgetCode) work.cover = guessMgsCover(work.widgetCode);
      } catch (e) { /* ignore */ }
    }

    // 文章头部「出演AV男優」标签链接（带 URL，优先于正文纯文本）
    const tagLinks = [...art.querySelectorAll('span.post-tags a')]
      .map(a => ({ name: a.textContent.trim(), url: a.href }))
      .filter(x => x.name);

    // 正文「标签：值」行（以 <br> 分隔，冒号全半角、周围空格不定）
    // 新文章用 <p>，2014-2019 及更早的老文章用 <div style="margin:0..."> —— 取含标签行的最小容器
    let infoEl = null;
    for (const el of art.querySelectorAll('p, div')) {
      const html = el.innerHTML;
      if (!/<br\s*\/?>/i.test(html) || !/[:：]/.test(html)) continue;
      if (!/(発売日|品番|メーカー|出演|配信|収録時間)/.test(html)) continue;
      if (!infoEl || html.length < infoEl.innerHTML.length) infoEl = el;
    }
    if (infoEl) {
      // 老文章信息块常缺闭合标签，文末描述会嵌进 infoEl；按 <br> 与块级标签双重切分，描述自成段后自然被忽略
      const segs = infoEl.innerHTML.split(/<br\s*\/?>|<\/p>|<p[^>]*>|<\/div>|<div[^>]*>/i);
      for (const seg of segs) {
        const m = seg.match(/^\s*([^<>:：]{1,20}?)\s*[:：]\s*([\s\S]*)$/);
        if (!m) continue;
        const rawLabel = m[1].trim();
        const key = LABEL_MAP[normLabel(rawLabel)];
        if (key) { applyField(work, key, m[2]); continue; }
        // 站点字段名不固定：未识别的标签也保留进 extra，不丢数据
        const v = parseVal(m[2]);
        if (!v.text && !v.links.length) continue;
        (work.extra = work.extra || []).push({ label: rawLabel, text: v.text, links: v.links });
      }
    }

    if (tagLinks.length) {
      const cur = work.maleActors;
      if (!cur || !cur.links || !cur.links.length) {
        work.maleActors = { text: tagLinks.map(x => x.name).join('　'), links: tagLinks };
      }
    }
    // 老文章常只有品番而没有封面图片；按 DMM cid 规则补出可验证的候选地址。
    if (!work.cover && work.code) {
      const cid = String(work.code).toLowerCase();
      if (/^[a-z0-9_]+$/.test(cid))
        work.cover = `https://pics.dmm.co.jp/digital/video/${cid}/${cid}pl.jpg`;
    }
    return work;
  }

  function parseListPage(doc) {
    const postLinks = postLinksOnPage(doc);
    return [...doc.querySelectorAll('#list article.entry-card')]
      .map(art => parseArticle(art, postLinks))
      .filter(w => w.title);
  }

  function getLastPage(doc) {
    let max = 1;
    doc.querySelectorAll('.page-numbers').forEach(a => {
      const n = parseInt(a.textContent.trim(), 10);
      if (Number.isFinite(n) && n > max) max = n;
    });
    return max;
  }

  // ---------- mgstage 封面：按图床 URL 规则直连拼接 ----------
  // 挂件脚本（mgs_Widget_affiliate）依赖的 JSONP 接口对境外网络不可达，
  // 但其渲染出的图片走 image.mgstage.com 图床，URL 规则为：
  //   images/{厂商slug}/{品番前缀小写}/{数字}/pb_e_{品番小写}.jpg
  // 厂商 slug 与品番前缀的对应表由真实数据验证得出；未知前缀留空。
  // 品番前缀 → 厂商 slug。本表由「真实数据反推 + 图床实测」得出，不是猜的：
  //   反推：从数据里已存在的 image.mgstage.com 封面 URL 回收 前缀→slug 对应关系；
  //   实测：对没有先例的前缀逐个打图床，只认 HTTP 200 + image/jpeg
  //         （403 + application/xml = 猜错，这是很干净的判据）。
  // 新增前缀不必瞎猜：跑 `node --experimental-sqlite tests/audit-data.js`，
  // 报告第三节会列出「待补前缀 + 可补条数 + 样本品番」，照单补即可。
  const MGS_MAKER_SLUG = {
    '200GANA': 'nanpatv', '259LUXU': 'luxutv', '261ARA': 'ara', '277DCV': 'documentv',
    '278GGEN': 'gets', '296CPDE': 'zokusei', '300MAAN': 'prestigepremium', '300MIUM': 'prestigepremium',
    '300NTK': 'prestigepremium', '326AID': 'kurofune', '326CAN': 'kurofune', '326DEN': 'kurofune',
    '326EVA': 'kurofune', '326GCP': 'kurofune', '326HGP': 'kurofune', '326IED': 'kurofune',
    '326INK': 'kurofune', '326INS': 'kurofune', '326JKK': 'kurofune', '326KJN': 'kurofune',
    '326KJO': 'kurofune', '326KNTR': 'kurofune', '326KURO': 'kurofune', '326MASS': 'kurofune',
    '326MCC': 'kurofune', '326MTP': 'kurofune', '326NKR': 'kurofune', '326ONS': 'kurofune',
    '326OPA': 'kurofune', '326PAPA': 'kurofune', '326PIZ': 'kurofune', '326PSZ': 'kurofune',
    '326SCP': 'kurofune', '326SPB': 'kurofune', '326SPOR': 'kurofune', '326URA': 'kurofune',
    '326URF': 'kurofune', '326ZAK': 'kurofune', '332NAMA': 'namanamanet', '336DTT': 'kanbi',
    '336KBI': 'kanbi', '345SIMM': 'shiroutomanman', '348NTR': 'ntrnet', '353HEN': 'hentaisamurai',
    '383REIW': 'reiwashirouto', '390JAC': 'jackson', '390JNT': 'jackson', '407KAG': 'kurokage',
    '428SUKE': 'sukekiyo', '430MMH': 'shiroutolovetube', '435MFC': 'moonforce', '436HLM': 'haremtv',
    '451HHH': 'uratalknouratalk', '459TEN': 'diego', '476MLA': 'manmanland', '483SGK': 'hamechan',
    '485GCB': 'goodbyecherryboy', '499NDH': 'nanpadehamehame', '502SEI': 'seikyouiku',
    '812MMC': 'momoco', '892OERO': 'kyantamaseisouin', '908JDH': 'jdhamehame',
    ABF: 'prestige', ABP: 'prestige', ABS: 'prestige', ABW: 'prestige',
    AKA: 'prestige', AOI: 'prestige', BGN: 'prestige', CHN: 'prestige', DIC: 'prestige',
    DLV: 'prestige', DOCP: 'doc', EVO: 'prestige', FIG: 'prestige', KDS: 'doc',
    MAS: 'prestige', PXH: 'prestige', SAD: 'prestige', SGA: 'prestige', SIRO: 'shirouto',
    SRS: 'prestige', TUS: 'prestige', WPS: 'prestige', YRH: 'prestige', YRZ: 'prestige',
  };
  function guessMgsCover(code) {
    if (!code) return '';
    const m = String(code).match(/^([A-Za-z0-9]+)-(\d+)$/);
    if (!m) return '';
    const slug = MGS_MAKER_SLUG[m[1].toUpperCase()];
    if (!slug) return '';
    const pre = m[1].toLowerCase(), num = m[2];
    return `https://image.mgstage.com/images/${slug}/${pre}/${num}/pb_e_${pre}-${num}.jpg`;
  }

  // 挂件系作品封面即时解析（同步完成，无需等待脚本渲染）；未知前缀留空
  async function fillWidgetCovers(works) {
    for (const w of works) {
      if (!w.cover && w.widgetCode) w.cover = guessMgsCover(w.widgetCode);
      delete w.widgetUrl;   // 导出前去掉中间字段
      delete w.widgetClass;
    }
  }

  // ---------- 存储（IndexedDB；GM 存储超 64MiB 会炸掉脚本初始化，不能再用） ----------
  let idb = null;
  function openStore() {
    if (idb) return Promise.resolve(idb);
    return new Promise((res, rej) => {
      const req = indexedDB.open('avd-exporter', 1);
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains('months')) d.createObjectStore('months', { keyPath: 'key' });
      };
      req.onsuccess = () => { idb = req.result; res(idb); };
      req.onerror = () => rej(req.error);
    });
  }
  async function loadMonth(key) {
    try {
      const db = await openStore();
      return await new Promise((res, rej) => {
        const rq = db.transaction('months', 'readonly').objectStore('months').get(key);
        rq.onsuccess = () => res(rq.result || null);
        rq.onerror = () => rej(rq.error);
      });
    } catch (e) { console.warn('[AVD] load failed', e); }
    return null;
  }
  async function saveMonth(key, st) {
    try {
      const db = await openStore();
      await new Promise((res, rej) => {
        const tx = db.transaction('months', 'readwrite');
        tx.objectStore('months').put(st);
        tx.oncomplete = res;
        tx.onerror = () => rej(tx.error);
      });
      return true;
    } catch (e) {
      log('⚠️ 保存失败，请尽快导出 JS 文件！');
      return false;
    }
  }
  async function allMonthKeys() {
    try {
      const db = await openStore();
      return await new Promise((res, rej) => {
        const rq = db.transaction('months', 'readonly').objectStore('months').getAllKeys();
        rq.onsuccess = () => res(rq.result || []);
        rq.onerror = () => rej(rq.error);
      });
    } catch (e) { return []; }
  }
  async function deleteMonth(key) {
    try {
      const db = await openStore();
      await new Promise((res) => {
        const tx = db.transaction('months', 'readwrite');
        tx.objectStore('months').delete(key);
        tx.oncomplete = res; tx.onerror = res;
      });
    } catch (e) {}
  }

  function workIdentity(w) {
    const code = w.deliveryCode || w.code || w.makerCode || w.widgetCode;
    if (w.title && typeof code === 'string' && /^[A-Za-z0-9_-]{4,40}$/.test(code.trim())) {
      return `code:${code.trim().toLowerCase()}|${w.title.trim()}`;
    }
    return w.url || (w.postId ? `post:${w.postId}` : w.title);
  }

  function mergeWorkFields(old, fresh) {
    // 日期路径指向作品详情；归档页链接不能取代它。
    if (/\/\d{4}\/\d{2}\/\d{2}\//.test(fresh.url || '') &&
        !/\/\d{4}\/\d{2}\/\d{2}\//.test(old.url || '')) old.url = fresh.url;
    for (const k of Object.keys(fresh)) {
      if (k === 'widgetUrl' || k === 'widgetClass') continue;
      if (old[k] === undefined || old[k] === '' || old[k] === null) old[k] = fresh[k];
    }
  }

  function mergeWorks(st, works) {
    // 旧版按 URL 去重，曾把同一作品的多个归档页链接保存成多条。
    const unified = {};
    for (const old of Object.values(st.works)) {
      const id = workIdentity(old);
      if (!unified[id]) unified[id] = old;
      else mergeWorkFields(unified[id], old);
    }
    st.works = unified;
    let added = 0;
    for (const w of works) {
      const id = workIdentity(w);
      if (!st.works[id]) { st.works[id] = w; added++; }
      else mergeWorkFields(st.works[id], w);
    }
    st.count = Object.keys(st.works).length;
    return added;
  }

  async function monthState(key, title, url) {
    let st = await loadMonth(key);
    if (!st) st = { key, meta: { key, title, url }, works: {} };
    if (title) st.meta.title = title;
    if (url) st.meta.url = url;
    return st;
  }

  // ---------- 导出 JS 数据文件 ----------
  function monthFileContent(st) {
    const data = {
      meta: Object.assign({}, st.meta, {
        exportedAt: new Date().toISOString(),
        count: Object.keys(st.works).length,
      }),
      works: Object.values(st.works).sort((a, b) => (b.date || '').localeCompare(a.date || '')),
    };
    return '// avdanyuwiki.com 月度作品数据 · 由 avdanyu-exporter.user.js 生成\n'
      + '// 放入 avdanyu-viewer.html 旁边的 avdanyu-data/ 目录即可被读取\n'
      + 'window.AVDANYU_DATA = window.AVDANYU_DATA || {};\n'
      + `window.AVDANYU_DATA[${JSON.stringify(st.key)}] = `
      + JSON.stringify(data, null, 2) + ';\n';
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], { type: 'application/javascript;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  function exportMonth(st, auto, skipIndex) {
    const content = monthFileContent(st);
    try { window.__avd_lastExport = content; } catch (e) { /* 测试钩子 */ }
    downloadText(st.key + '.txt', content);
    log(`💾 已${auto ? '自动' : ''}导出 ${st.key}.txt（${Object.keys(st.works).length} 部）`);
  }

  // ---------- 抓取 ----------
  function pageUrl(base, page) {
    return page === 1 ? base : base.replace(/\/+$/, '') + '/page/' + page + '/';
  }

  // 抓取一个归档（月或年）的全部页，返回 { key: works[] }（年抓取按月份分组）
  // 第 1 页确定总页数后，由并发 worker 池抓取剩余页面，逐页解析即弃，不占内存
  async function crawlArchive(base, groupByMonth, isStopped) {
    cfStrikes = 0;   // 每次归档抓取重置熔断计数
    const buckets = {};   // monthKey -> works[]
    const collect = works => {
      for (const w of works) {
        const key = groupByMonth
          ? ((w.date || '').slice(0, 7).replace('/', '-'))
          : 'single';
        if (!key || key === '-') continue;
        (buckets[key] = buckets[key] || []).push(w);
      }
    };
    const stopped = () => isStopped && isStopped();

    log('抓取第 1 页…');
    const first = await fetchDoc(pageUrl(base, 1));
    if (stopped()) return buckets;
    // 列表容器缺失 = 页面结构变了或被拦截：明确报警，别静默导出 0 部
    if (!first.querySelector('#list')) {
      log('⚠️ 第 1 页没有作品列表容器 #list（页面结构可能已变，或响应被拦截）');
    }
    const last = getLastPage(first);
    collect(parseListPage(first));
    const conc = getConcurrency();
    log(`共 ${last} 页，并发 ${conc} 抓取…`);

    let nextPage = 2, done = 1, failed = 0;
    async function worker(wid) {
      await sleep(wid * 120);   // 错开启动，避免同一瞬间 burst
      while (!stopped() && !cfHalted()) {
        const p = nextPage++;
        if (p > last) return;
        try {
          const doc = await fetchDoc(pageUrl(base, p));
          collect(parseListPage(doc));
        } catch (e) {
          if (cfHalted()) return;   // 熔断：立即停手，不再往下冲
          failed++;
          log(`⚠️ 第 ${p} 页抓取失败：${e.message}`);
        }
        done++;
        setProgress(done, last);
        if (done % 20 === 0) log(`页面进度 ${done}/${last}（失败 ${failed}）`);
        await sleep(WORKER_DELAY);
      }
    }
    await Promise.all(Array.from({ length: conc }, (_, i) => worker(i)));
    if (cfHalted()) throw haltErr();
    if (failed) log(`⚠️ 共 ${failed} 页抓取失败（缺页可重新抓取补齐）`);
    return buckets;
  }

  // ---------- 面板 UI ----------
  const panel = document.createElement('div');
  panel.id = 'avd-panel';
  panel.innerHTML = `
    <div class="avd-head"><span>🎞 avdanyu 导出器</span><span class="avd-toggle">─</span></div>
    <div class="avd-body avd-hidden">
      <div class="avd-status">初始化…</div>
      <progress class="avd-prog" max="100" value="0"></progress>
      <div class="avd-row">
        <label class="avd-lab">
          并发 <input type="number" id="avd-conc" class="avd-num avd-num-sm" min="${CONC_MIN}" max="${CONC_MAX}" value="${CONC_DEFAULT}"> （${CONC_MIN}-${CONC_MAX}）
        </label>
      </div>
      <div class="avd-row" id="avd-btns"></div>
      <div class="avd-tip">导出的 .txt 放入数据目录后，在查看器里点「🔄 同步」即可读取</div>
    </div>`;
  document.body.appendChild(panel);
  const body = panel.querySelector('.avd-body');
  const statusEl = panel.querySelector('.avd-status');
  const progEl = panel.querySelector('.avd-prog');
  const btnsEl = panel.querySelector('#avd-btns');
  panel.querySelector('.avd-head').addEventListener('click', () => {
    body.classList.toggle('avd-hidden');
  });

  let logs = [];
  function log(msg) {
    logs.push(msg);
    if (logs.length > 6) logs.shift();
    statusEl.textContent = logs.join('\n');
  }
  function setProgress(done, total) {
    progEl.max = total || 1;
    progEl.value = total ? done : 0;
  }
  function addBtn(text, fn, cls) {
    const b = document.createElement('button');
    b.textContent = text;
    if (cls) b.className = cls;
    b.addEventListener('click', fn);
    btnsEl.appendChild(b);
    return b;
  }
  function setBusy(busy) {
    btnsEl.querySelectorAll('button').forEach(b => { if (!b.dataset.stop) b.disabled = busy; });
  }
  function getConcurrency() {
    const el = document.getElementById('avd-conc');
    let n = parseInt(el && el.value, 10);
    if (!Number.isFinite(n)) n = CONC_DEFAULT;
    n = Math.min(CONC_MAX, Math.max(CONC_MIN, n));
    if (el) el.value = n;
    return n;
  }

  // ---------- 月份范围 UI 与抓取（月页 / 年页共用） ----------
  function addMonthRangeUI(defFrom, defTo) {
    const mrow = document.createElement('div');
    mrow.className = 'avd-row';
    mrow.innerHTML = `
      <label class="avd-lab">
        起始月 <input type="number" id="avd-mfrom" class="avd-num avd-num-xs" min="1" max="12" value="${defFrom}">
      </label>
      <label class="avd-lab">
        结束月 <input type="number" id="avd-mto" class="avd-num avd-num-xs" min="1" max="12" value="${defTo}">
      </label>`;
    body.insertBefore(mrow, btnsEl);
  }

  function getMonthRange() {
    const read = (id, dflt) => {
      let n = parseInt(document.getElementById(id).value, 10);
      if (!Number.isFinite(n)) n = dflt;
      return Math.min(12, Math.max(1, n));
    };
    let from = read('avd-mfrom', 1), to = read('avd-mto', 12);
    if (from > to) [from, to] = [to, from];
    document.getElementById('avd-mfrom').value = from;
    document.getElementById('avd-mto').value = to;
    return [from, to];
  }

  // 流水线：本月抓页的同时，上一月的挂件封面在后台并行补全；补完立即导出上一月
  function makePendingFinisher() {
    let pending = null;
    return {
      set(key, st, works, task) { pending = { key, st, works, task }; },
      async flush() {
        if (!pending) return 0;
        const p = pending;
        pending = null;
        await p.task;
        const added = mergeWorks(p.st, p.works);
        await saveMonth(p.key, p.st);
        log(`${p.key}：新增 ${added}，共 ${p.st.count} 部`);
        exportMonth(p.st, true, true);
        return added;
      },
    };
  }

  // 逐月抓取指定范围：每月独立合并、独立导出，已完成月份随时中断不丢
  async function crawlYearRange(y, from, to, btn) {
    setBusy(true);
    btn.dataset.stop = '';
    btn.textContent = '⏹ 停止';
    let stopped = false;
    btn.addEventListener('click', () => { stopped = true; }, { once: true });
    try {
      log(`${y}年 ${from}月-${to}月，共 ${to - from + 1} 个月`);
      const finisher = makePendingFinisher();
      for (let m = from; m <= to; m++) {
        if (stopped) { log(`已手动停止（${y}-${String(m).padStart(2, '0')} 及之后未抓）`); break; }
        const mm = String(m).padStart(2, '0');
        const key = `${y}-${mm}`;
        const base = `${ORIGIN}/${y}/${mm}/`;
        const st = await monthState(key, `${y}年${m}月`, base);
        log(`${key}：开始抓取…`);
        const buckets = await crawlArchive(base, false, () => stopped);
        if (stopped) { log(`已手动停止（${key} 未导出，已抓进度已保存可续抓）`); break; }
        const works = buckets.single || [];
        log(`${key}：${works.length} 部，封面已解析`);
        const task = fillWidgetCovers(works);   // 不等待，与下一月抓取并行
        await finisher.flush();                  // 导出上一月（封面已并行补完）
        finisher.set(key, st, works, task);
      }
      await finisher.flush();
      setProgress(1, 1);
      log('✅ 月份范围抓取结束');
    } catch (err) {
      if (err instanceof ChallengeHaltError) {
        log('🛑 ' + err.message);
        log('已抓月份已安全导出；请在浏览器里完成人机验证后，从断点月份重新开始。');
      } else {
        log('❌ ' + err.message);
      }
    } finally {
      delete btn.dataset.stop;
      setBusy(false);
      btn.textContent = '🚀 抓取指定范围并导出';
    }
  }

  // 全部提取：从 (y, m) 起按年月倒序逐月回溯至 1998-06（站点最早月份）
  const ALL_END_Y = 1998, ALL_END_M = 6;
  async function crawlAllDown(y0, m0, btn) {
    setBusy(true);
    btn.dataset.stop = '';
    btn.textContent = '⏹ 停止（已抓月份已导出）';
    let stopped = false;
    btn.addEventListener('click', () => { stopped = true; }, { once: true });
    let y = y0, m = m0, doneMonths = 0, totalAdded = 0;
    try {
      log(`🔥 全部提取：从 ${y0}-${String(m0).padStart(2, '0')} 倒序回溯至 ${ALL_END_Y}-${String(ALL_END_M).padStart(2, '0')}`);
      const finisher = makePendingFinisher();
      while (true) {
        if (stopped) { log('已手动停止'); break; }
        const mm = String(m).padStart(2, '0');
        const key = `${y}-${mm}`;
        const base = `${ORIGIN}/${y}/${mm}/`;
        log(`${key}：开始抓取…`);
        let buckets = null;
        try {
          buckets = await crawlArchive(base, false, () => stopped);
        } catch (e) {
          if (e instanceof ChallengeHaltError) { log('🛑 ' + e.message); break; }
          log(`⚠️ ${key} 抓取失败（${e.message}），跳过继续`);
        }
        if (stopped) { log(`已手动停止（${key} 未导出，已抓进度已保存可续抓）`); break; }
        if (buckets) {
          const works = buckets.single || [];
          if (works.length) {
            const st = await monthState(key, `${y}年${m}月`, base);
                log(`${key}：${works.length} 部，封面已解析`);
            const task = fillWidgetCovers(works);   // 不等待，与下一月抓取并行
            totalAdded += await finisher.flush();    // 导出上一月（封面已并行补完）
            finisher.set(key, st, works, task);
          } else {
            log(`${key}：无作品，跳过`);
          }
          doneMonths++;
        }
        if (y === ALL_END_Y && m === ALL_END_M) { log('✅ 已到 1998-06，全部提取完成'); break; }
        m--;
        if (m < 1) { m = 12; y--; }
      }
      totalAdded += await finisher.flush();
      setProgress(1, 1);
      log(`✅ 结束：处理 ${doneMonths} 个月份，新增 ${totalAdded} 部`);
    } catch (err) {
      if (err instanceof ChallengeHaltError) {
        log('🛑 ' + err.message);
        log('已抓月份已安全导出；请在浏览器里完成人机验证后，从断点月份重新开始。');
      } else {
        log('❌ ' + err.message);
      }
    } finally {
      delete btn.dataset.stop;
      setBusy(false);
      btn.textContent = '🔥 全部提取（倒序至 1998-06）';
    }
  }

  // ---------- 入口逻辑 ----------
  if (!route) return;   // 非归档/文章页：静默退出（无面板）

  // ① 挑战页：明确告知，不再静默什么都不做
  if (isChallengePage(document)) {
    body.classList.remove('avd-hidden');
    log('🛑 当前是 Cloudflare 人机验证页，不是目标页面。');
    log('请先在浏览器里手动完成验证（通常会自动跳回），再重开本面板。');
    return;
  }

  // ② 非规范地址（月份/日未零填充）：本站可能不返回内容——旧版在这里会静默失效
  const canonicalUrl = route.kind === 'month' ? `${ORIGIN}/${route.y}/${route.m}/`
    : route.kind === 'post' ? `${ORIGIN}/${route.y}/${route.m}/${route.d}/${route.slug}/`
    : null;
  if (canonicalUrl && !route.canonical) {
    body.classList.remove('avd-hidden');
    log('🔗 当前地址不是规范形式（月份/日须零填充）');
    log(`本站只认 /${route.y}/${route.m}/ 这种写法；请先跳到规范地址再抓。`);
    addBtn('🔗 跳到规范地址', () => location.assign(canonicalUrl), 'avd-all');
    return;
  }

  if (route.kind === 'post') {
    const y = route.y, m = route.m;
    const key = `${y}-${m}`;
    body.classList.remove('avd-hidden');
    log(`文章页：本篇可抓取并归入 ${key}`);
    addBtn('📥 抓取本篇并入月份库', async () => {
      setBusy(true);
      try {
        const art = document.querySelector('article') || document;
        const work = parseArticle(art);
        work.url = work.url || location.href.split('#')[0];
        if (!work.title) { log('⚠️ 未找到作品标题，抓取取消'); return; }
        await fillWidgetCovers([work]);
        const st = await monthState(key, `${y}年${parseInt(m, 10)}月`, `${ORIGIN}/${y}/${m}/`);
        const added = mergeWorks(st, [work]);
        await saveMonth(key, st);
        log(`✅ 已并入 ${key}：${added ? '新增 1 部' : '已存在，补全字段'}，该月共 ${st.count} 部`);
        exportMonth(st, false);
      } finally { setBusy(false); }
    });
    addBtn('📅 打开所属月份', () => location.assign(`${ORIGIN}/${y}/${m}/`), 'avd-sec');
  } else if (route.kind === 'month') {
    (async () => {
    const y = route.y, m = route.m;
    const key = `${y}-${m}`;
    const base = `${ORIGIN}/${y}/${m}/`;
    const st = await monthState(key, `${y}年${parseInt(m, 10)}月`, base);
    await saveMonth(key, st);
    body.classList.remove('avd-hidden');
    log(`月度归档：${st.meta.title}`);
    log(`本地已存 ${Object.keys(st.works).length} 部`);

    addMonthRangeUI(parseInt(m, 10), parseInt(m, 10));
    addBtn('🚀 抓取指定范围并导出', (e) => {
      const [from, to] = getMonthRange();
      crawlYearRange(y, from, to, e.target);
    }, 'avd-all');

    addBtn('🔥 全部提取（倒序至 1998-06）', (e) => {
      crawlAllDown(parseInt(y, 10), parseInt(m, 10), e.target);
    });

    addBtn('🚀 抓取整月并导出', async (e) => {
      const btn = e.target;
      setBusy(true);
      btn.dataset.stop = '';
      btn.textContent = '⏹ 停止';
      let stopped = false;
      btn.addEventListener('click', () => { stopped = true; }, { once: true });
      try {
        const buckets = await crawlArchive(base, false, () => stopped);
        const works = buckets.single || [];
        if (stopped) { log('已手动停止'); return; }
        log(`解析 ${works.length} 部…`);
        await fillWidgetCovers(works);
        const added = mergeWorks(st, works);
        await saveMonth(key, st);
        setProgress(1, 1);
        log(`✅ 完成：新增 ${added}，共 ${st.count} 部`);
        exportMonth(st, true);
      } catch (err) {
        if (err instanceof ChallengeHaltError) {
          log('🛑 ' + err.message);
          log('已抓页面进度已保存；完成人机验证后重试即可补齐。');
        } else {
          log('❌ ' + err.message);
        }
      } finally {
        delete btn.dataset.stop;
        setBusy(false);
        btn.textContent = '🚀 抓取整月并导出';
      }
    }, 'avd-all');

    addBtn('📥 抓取本页并导出', async () => {
      setBusy(true);
      try {
        const works = parseListPage(document);
        await fillWidgetCovers(works);
        const added = mergeWorks(st, works);
        await saveMonth(key, st);
        log(`本页 ${works.length} 部，新增 ${added}，共 ${st.count} 部`);
        exportMonth(st, false);
      } finally { setBusy(false); }
    });

    addBtn('💾 仅导出', () => exportMonth(st, false));

    addBtn('🗑 清空本月', async () => {
      if (confirm(`确定清空 ${st.meta.title} 的已存数据？`)) {
        await deleteMonth(key);
        st.works = {}; st.count = 0;
        log('已清空');
      }
    }, 'avd-sec');
    })();
  } else if (route.kind === 'year') {
    const y = route.y;
    body.classList.remove('avd-hidden');
    log(`年度归档：${y}年`);

    addMonthRangeUI(1, 12);
    addBtn('🚀 抓取指定范围并导出', (e) => {
      const [from, to] = getMonthRange();
      crawlYearRange(y, from, to, e.target);
    }, 'avd-all');

    addBtn('🔥 全部提取（倒序至 1998-06）', (e) => {
      crawlAllDown(parseInt(y, 10), 12, e.target);
    });

    addBtn('📥 抓取本页', async () => {
      setBusy(true);
      try {
        const works = parseListPage(document);
        await fillWidgetCovers(works);
        let total = 0;
        for (const w of works) {
          const key = (w.date || '').slice(0, 7).replace('/', '-');
          if (!key) continue;
          const st = await monthState(key, key.replace('-', '年') + '月', `${ORIGIN}/${key.replace('-', '/')}/`);
          mergeWorks(st, [w]);
          await saveMonth(key, st);
            total++;
        }
        log(`本页 ${works.length} 部已归入各月份（有效 ${total}）`);
      } finally { setBusy(false); }
    });

    addBtn('🗑 清空全年', async () => {
      if (confirm(`确定清空 ${y} 年所有月份的已存数据？`)) {
        const keys = (await allMonthKeys()).filter(k => k.startsWith(y + '-'));
        for (const k of keys) await deleteMonth(k);
        log(`已清空 ${keys.length} 个月份`);
      }
    }, 'avd-sec');
  }

})();
