#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""avdanyu 数据库整合器（Python 版）
把 avdanyu-data/ 目录里的月度 txt 整合为单一 SQLite 数据库 avdanyu.db。
用法：
    python scripts/avdanyu-merge.py            整合（增量更新已有 db）
    python scripts/avdanyu-merge.py -del       整合后删除目录里的月度 txt（保留 avdanyu.db）
"""
import json
import re
import sqlite3
import sys
from pathlib import Path
from dedupe_works import dedupe_works

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / 'avdanyu-data'
DB_PATH = DATA_DIR / 'avdanyu.db'


def read_months_from_txt(path: Path):
    """解析导出的数据 txt：window.AVDANYU_DATA["YYYY-MM"] = {...}; 形式，可含多个月"""
    txt = path.read_text(encoding='utf-8')
    out = {}
    for chunk in txt.split('window.AVDANYU_DATA[')[1:]:
        m = re.match(r'"([^"]+)"\]\s*=\s*', chunk)
        if not m:
            continue
        key = m.group(1)
        body = chunk[m.end():]
        end = body.rfind('};')
        if end < 0:
            continue
        try:
            val = json.loads(body[:end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict) and isinstance(val.get('works'), list):
            out[key] = val
    return out


def main():
    delete_after = '-del' in sys.argv[1:]
    if not DATA_DIR.is_dir():
        sys.exit(f'找不到数据目录：{DATA_DIR}')

    files = [p for p in DATA_DIR.rglob('*')
             if p.suffix.lower() in ('.txt', '.js') and p.name != 'avdanyu.db']
    if not files:
        sys.exit('目录里没有月度数据文件（.txt/.js）')

    db_exists = DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS months (month TEXT PRIMARY KEY, data TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)')

    seen = set()
    written_files = []   # 成功解析并写入的文件（-del 只删这些）
    for f in sorted(files):
        months = read_months_from_txt(f)
        if not months:
            print(f'  跳过（无数据）：{f.name}')
            continue
        with conn:
            for k, val in months.items():
                val['works'], _ = dedupe_works(val['works'])
                if isinstance(val.get('meta'), dict):
                    val['meta']['count'] = len(val['works'])
                # 保留回填脚本或旧导出中已有的封面，避免重新整合时被
                # 当前源文件里的空 cover 覆盖。按稳定的文章 URL/postId/标题匹配。
                old_row = conn.execute('SELECT data FROM months WHERE month = ?', (k,)).fetchone()
                if old_row:
                    try:
                        old_works = json.loads(old_row[0]).get('works', [])
                        old_by_id = {}
                        old_by_code = {}
                        for old in old_works:
                            oid = old.get('url') or (f"post:{old.get('postId')}" if old.get('postId') else old.get('title'))
                            if oid: old_by_id[oid] = old
                            for field in ('widgetCode', 'deliveryCode', 'makerCode', 'code'):
                                value = old.get(field)
                                if isinstance(value, str) and value.strip():
                                    old_by_code.setdefault((field, value.strip().lower()), old)
                        for work in val['works']:
                            wid = work.get('url') or (f"post:{work.get('postId')}" if work.get('postId') else work.get('title'))
                            previous = old_by_id.get(wid)
                            if not previous:
                                for field in ('widgetCode', 'deliveryCode', 'makerCode', 'code'):
                                    value = work.get(field)
                                    if isinstance(value, str) and value.strip():
                                        previous = old_by_code.get((field, value.strip().lower()))
                                        if previous: break
                            if previous and not work.get('cover') and previous.get('cover'):
                                work['cover'] = previous['cover']
                    except (TypeError, json.JSONDecodeError):
                        pass
                conn.execute('DELETE FROM months WHERE month = ?', (k,))
                conn.execute('INSERT INTO months (month, data) VALUES (?, ?)',
                             (k, json.dumps(val, ensure_ascii=False)))
                seen.add(k)
        n = sum(len(v['works']) for v in months.values())
        print(f'  合并 {f.name} -> {", ".join(months)}（{n} 部）')
        written_files.append(f)

    # 统计以数据库实际内容为准（避免同月多文件重复计数）
    db_months = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(json_array_length(data, '$.works')), 0) FROM months"
    ).fetchone()
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('updatedAt', ?)",
                     (__import__('datetime').datetime.now().isoformat(),))
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('count', ?)", (str(db_months[1]),))
    conn.close()

    size_mb = DB_PATH.stat().st_size / 1048576
    print(f"完成：{len(written_files)} 个文件 -> {DB_PATH.name}"
          f"（{'更新' if db_exists else '新建'}，库内共 {db_months[0]} 个月份、{db_months[1]} 部，{size_mb:.1f} MB）")

    if delete_after:
        # 先备份数据库，再只删除成功写入的源文件
        import shutil
        bak = DB_PATH.with_suffix('.db.bak')
        shutil.copy2(DB_PATH, bak)
        n = 0
        for f in written_files:
            f.unlink()
            n += 1
        print(f'已备份 {bak.name}，并删除 {n} 个成功写入的月度数据文件')


if __name__ == '__main__':
    main()
