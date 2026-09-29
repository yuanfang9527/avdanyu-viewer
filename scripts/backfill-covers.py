#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可续跑的封面回填器：只写入空 cover，候选必须是 HTTP 200 有效图片。"""
import argparse, json, re, sqlite3, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE=Path(__file__).resolve().parent.parent; DB=BASE/'avdanyu-data'/'avdanyu.db'
STATE=Path(__file__).with_name('cover-backfill-state.json'); MISS=Path(__file__).with_name('cover-missing.txt')
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
FIELDS=('deliveryCode','code','makerCode'); IMAGE_TYPES=('image/jpeg','image/jpg','image/png','image/webp','image/gif')

def dmm_url(code):
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_]{4,32}',code.strip()): return None
    c=code.strip().lower(); return f'https://pics.dmm.co.jp/digital/video/{c}/{c}pl.jpg'
def extract_code(value):
    if not isinstance(value,str): return []
    s=value.strip()
    if re.fullmatch(r'[A-Za-z0-9_]{4,32}',s): return [s]
    return list(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9])([A-Za-z]{1,12}[_-]?\d{3,8}[A-Za-z0-9_]*)\b',s)))
def load_mgs_map():
    text=(BASE/'avdanyu-viewer.html').read_text(encoding='utf-8'); start=text.find('const MGS_MAKER_SLUG'); end=text.find('};',start)
    block=text[start:end]
    out={}
    for a,b,slug in re.findall(r"(?:'([^']+)'|\b([A-Za-z][A-Za-z0-9]*)\b)\s*:\s*'([^']+)'",block): out[(a or b).upper()]=slug
    # Same maker families already established by multiple working covers.
    # Keep this list deliberately narrow; unknown prefixes stay unresolved.
    out.update({'MAAN':'prestigepremium','SUKE':'sukekiyo','SIMM':'shiroutomanman',
                'MFC':'moonforce','MFCS':'moonforce','MFCW':'moonforce','435MFCS':'moonforce','435MFCW':'moonforce',
                '336FFT':'kanbi','336KBR':'kanbi','336TNB':'kanbi','336KNB':'kanbi'})
    return out
def mgs_url(code,makers):
    if not isinstance(code,str): return None
    m=re.fullmatch(r'([A-Za-z0-9]+)-(\d+)',code.strip())
    if not m or m.group(1).upper() not in makers: return None
    pre,num=m.group(1).lower(),m.group(2); return f'https://image.mgstage.com/images/{makers[m.group(1).upper()]}/{pre}/{num}/pb_e_{pre}-{num}.jpg'
def valid_image(url,timeout):
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=timeout) as r:
            if getattr(r,'status',0)!=200: return False
            ct=(r.headers.get('Content-Type') or '').split(';')[0].lower(); head=r.read(16)
            magic=head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n') or head.startswith((b'GIF87a',b'GIF89a')) or head.startswith(b'RIFF')
            return ct in IMAGE_TYPES or (ct.startswith('image/') and magic) or magic
    except Exception: return False
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--limit',type=int,default=0); ap.add_argument('--offset',type=int,default=0); ap.add_argument('--workers',type=int,default=8); ap.add_argument('--timeout',type=float,default=12); args=ap.parse_args()
    makers=load_mgs_map(); conn=sqlite3.connect(DB); rows=conn.execute('SELECT month,data FROM months').fetchall(); conn.close(); tasks=[]
    for month,data in rows:
        for i,w in enumerate(json.loads(data).get('works',[])):
            if w.get('cover'): continue
            c=[]
            if w.get('widgetCode') and mgs_url(w['widgetCode'],makers): c.append(('mgstage',mgs_url(w['widgetCode'],makers)))
            for f in FIELDS:
                for code in extract_code(w.get(f)):
                    u=dmm_url(code)
                    if u: c.append(('dmm',u))
            if c: tasks.append((month,i,list(dict.fromkeys(c)),w))
    tasks=tasks[args.offset:(args.offset+args.limit if args.limit else None)]; print(f'待验证 {len(tasks)} 条，workers={args.workers}, timeout={args.timeout}s',flush=True)
    results={}
    def probe(t):
        m,i,c,_=t
        for src,u in c:
            if valid_image(u,args.timeout): return m,i,src,u
        return m,i,None,None
    with ThreadPoolExecutor(max_workers=max(1,args.workers)) as pool:
        fs=[pool.submit(probe,t) for t in tasks]
        for n,f in enumerate(as_completed(fs),1):
            m,i,s,u=f.result(); results[(m,i)]=(s,u)
            if n%100==0: print(f'  进度 {n}/{len(tasks)}',flush=True)
    ok=0; bysrc={}; updates={}; still=[]
    for month,data in rows:
        d=json.loads(data); changed=False
        for i,w in enumerate(d.get('works',[])):
            got=results.get((month,i))
            if got and got[1] and not w.get('cover'): w['cover']=got[1]; ok+=1; bysrc[got[0]]=bysrc.get(got[0],0)+1; changed=True
            if not w.get('cover'):
                c=[]
                if w.get('widgetCode') and mgs_url(w['widgetCode'],makers): c.append(w['widgetCode'])
                for f in FIELDS:
                    for code in extract_code(w.get(f)):
                        if dmm_url(code): c.append(code)
                if not c: still.append((month,i,w,'no-safe-candidate'))
                elif (month,i) in results: still.append((month,i,w,'candidates-failed'))
                else: still.append((month,i,w,'pending'))
        if changed: updates[month]=json.dumps(d,ensure_ascii=False)
    conn=sqlite3.connect(DB)
    with conn:
        for month,payload in updates.items(): conn.execute('UPDATE months SET data=? WHERE month=?',(payload,month))
    conn.close(); MISS.write_text(''.join(f'{m} | {"".join(ch if ord(ch) >= 32 and ord(ch) not in (0x85, 0x2028, 0x2029) else " " for ch in str(w.get("title", "")))[:80]} | {r} | code={w.get("code")} widget={w.get("widgetCode")}\n' for m,i,w,r in still),encoding='utf-8')
    try: prev=json.loads(STATE.read_text(encoding='utf-8'))
    except Exception: prev={}
    total_success=int(prev.get('totalSuccess',0))+ok; total_by=dict(prev.get('totalBySource',{}))
    for k,v in bysrc.items(): total_by[k]=int(total_by.get(k,0))+v
    STATE.write_text(json.dumps({'lastOffset':args.offset+len(tasks),'lastRunCount':len(tasks),'success':ok,'bySource':bysrc,'totalSuccess':total_success,'totalBySource':total_by,'remainingReported':len(still)},ensure_ascii=False,indent=2),encoding='utf-8'); print(f'完成：{ok} 条（DMM {bysrc.get("dmm",0)}，MGStage {bysrc.get("mgstage",0)}），未解决清单 {len(still)} 条',flush=True)
if __name__=='__main__': sys.exit(main())
