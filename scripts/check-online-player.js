// 临时校验脚本：提取 avdanyu-viewer.html 内所有 <script> 用 Node 做语法解析，
// 并检查在线播放（javday 源）改造后的标识符引用是否配对、有无旧 123av 逻辑遗留。
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'avdanyu-viewer.html'), 'utf8');

const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
console.log('script blocks:', scripts.length);
let bad = false;
scripts.forEach((s, i) => {
  try { new Function(s); console.log('block', i, 'OK,', s.length, 'chars'); }
  catch (e) { console.log('block', i, 'SYNTAX ERROR:', e.message); bad = true; }
});

['ONLINE_PLAY_SITES', 'toggleOnlineMenu', 'closeOnlineMenu', 'initOnlineMenu', '_onlineMenu', 'online-menu', 'missav', 'netflav', 'supjav'].forEach(k => {
  if (html.includes(k)) { console.log('LEFTOVER FOUND:', k); bad = true; }
});

['online-overlay', 'online-box', 'online-head', 'online-title', 'online-close', 'online-video',
 'online-srcs', 'online-foot', 'online-status', 'online-search', 'online-open',
 'openOnlinePlayer', 'closeOnlinePlayer', 'bindOnlinePlayButton', 'onlinePlayCode',
 'onlineSearchUrl', 'resolveOnlinePlayer', 'renderOnlineSources', 'playOnlineSource',
 'destroyOnlineHls', '_onlineSources', '_onlineReqSeq', '_onlineHls',
 '/__online-player', '/__online-m3u8', 'searchUrl',
 'hls.min.js', 'volume = 0.3'].forEach(k => {
  const n = html.split(k).length - 1;
  console.log(k + ': ' + n + ' refs');
  if (!n) bad = true;
});

// 旧 123av iframe 整页/纯播放器内嵌的痕迹必须清除：浮层不得再有 iframe，也不得引用 123av
if (html.includes('online-frame')) { console.log('LEFTOVER: online-frame（应已改为 online-video）'); bad = true; }
if (html.includes('online-eps')) { console.log('LEFTOVER: online-eps（应已改为 online-srcs）'); bad = true; }
if (html.includes('online-cn')) { console.log('LEFTOVER: online-cn 中字徽章（在线字幕功能已替代，应移除）'); bad = true; }
if (/data\.cnSub/.test(html)) { console.log('LEFTOVER: 在线播放 cnSub 依赖'); bad = true; }
if (html.includes('123av.com')) { console.log('LEFTOVER: 123av.com 引用'); bad = true; }
if (/在线播放[^"'\n]*123av/.test(html)) { console.log('LEFTOVER: 123av 文案'); bad = true; }
if (html.includes('正在加载 123av 播放页')) { console.log('LEFTOVER: 旧状态文案'); bad = true; }
if (html.includes('已内嵌 123av 纯播放器')) { console.log('LEFTOVER: 解析成功提示文案（应清空状态栏）'); bad = true; }

// closeDrawer 必须调用 closeOnlinePlayer（关抽屉停止播放）
if (!/function closeDrawer\(\)\s*\{\s*closeOnlinePlayer\(\)/.test(html)) { console.log('MISSING: closeDrawer -> closeOnlinePlayer'); bad = true; }

console.log(bad ? 'RESULT: FAIL' : 'RESULT: PASS');
process.exit(bad ? 1 : 0);
