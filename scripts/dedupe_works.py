#!/usr/bin/env python3
"""Remove unambiguous same-month duplicate works from the DB and month files.

Only rows with the same nonempty title and exact normalized product code merge.
The most complete row wins; fields missing from it are filled from the others.
Run without --apply to inspect counts first.
"""
import argparse
import json
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / 'avdanyu-data'
DB = DATA / 'avdanyu.db'
ASSIGN = re.compile(r'window\.AVDANYU_DATA\["([^"]+)"\]\s*=\s*')
CODE = re.compile(r'[A-Za-z0-9_-]{4,40}\Z')


def identity(work):
    title = work.get('title')
    code = next((work.get(k) for k in ('deliveryCode', 'code', 'makerCode', 'widgetCode') if work.get(k)), None)
    if isinstance(title, str) and title.strip() and isinstance(code, str) and CODE.fullmatch(code.strip()):
        return (code.strip().lower(), title.strip())
    return None


def score(work):
    url = work.get('url') or ''
    article = bool(re.search(r'/\d{4}/\d{2}/\d{2}/', url))
    return (article, bool(work.get('postId')), bool(work.get('cover')),
            sum(v not in (None, '', [], {}) for v in work.values()))


def dedupe_works(works):
    groups = {}
    for i, work in enumerate(works):
        key = identity(work)
        if key: groups.setdefault(key, []).append(i)
    replacements = {}
    skipped = set()
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        best = max(indexes, key=lambda i: score(works[i]))
        merged = dict(works[best])
        for i in indexes:
            if i == best:
                continue
            for key, value in works[i].items():
                if merged.get(key) in (None, '', [], {}) and value not in (None, '', [], {}):
                    merged[key] = value
        replacements[indexes[0]] = merged
        skipped.update(indexes[1:])
    output = [replacements.get(i, work) for i, work in enumerate(works) if i not in skipped]
    return output, len(skipped)


def rewrite_month_text(text):
    decoder = json.JSONDecoder()
    edits = []
    removed = 0
    for match in ASSIGN.finditer(text):
        start = match.end()
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or not isinstance(value.get('works'), list):
            continue
        works, n = dedupe_works(value['works'])
        if n:
            value['works'] = works
            if isinstance(value.get('meta'), dict): value['meta']['count'] = len(works)
            edits.append((start, start + consumed, json.dumps(value, ensure_ascii=False, indent=2)))
            removed += n
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text, removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Write deduplicated data')
    args = parser.parse_args()
    conn = sqlite3.connect(DB)
    rows = conn.execute('SELECT month, data FROM months').fetchall()
    db_changes = {}
    db_removed = 0
    for month, raw in rows:
        value = json.loads(raw)
        works, n = dedupe_works(value['works'])
        if n:
            value['works'] = works
            if isinstance(value.get('meta'), dict): value['meta']['count'] = len(works)
            db_changes[month] = (raw, json.dumps(value, ensure_ascii=False))
            db_removed += n
    txt_changes = {}
    txt_removed = 0
    for path in DATA.rglob('*'):
        if path.suffix.lower() not in ('.txt', '.js') or not path.is_file():
            continue
        original = path.read_text(encoding='utf-8')
        updated, n = rewrite_month_text(original)
        if n:
            txt_changes[path] = (original, updated)
            txt_removed += n
    print(f'DB: {len(db_changes)} months, {db_removed} duplicate rows; month files: {len(txt_changes)} files, {txt_removed} duplicate rows')
    if not args.apply:
        conn.close()
        return
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = DATA / f'dedupe-backup-{stamp}.zip'
    with zipfile.ZipFile(backup, 'w', zipfile.ZIP_DEFLATED) as archive:
        for month, (original, _) in db_changes.items():
            archive.writestr(f'db-months/{month}.json', original)
        for path, (original, _) in txt_changes.items():
            archive.writestr(str(path.relative_to(DATA)).replace('\\', '/'), original)
    for path, (_, updated) in txt_changes.items():
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(updated, encoding='utf-8')
        temp.replace(path)
    with conn:
        for month, (_, updated) in db_changes.items():
            conn.execute('UPDATE months SET data = ? WHERE month = ?', (updated, month))
        count = conn.execute("SELECT COALESCE(SUM(json_array_length(data, '$.works')), 0) FROM months").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('count', ?)", (str(count),))
    conn.close()
    print(f'Applied. Backup: {backup}; current DB works: {count}')


if __name__ == '__main__':
    main()
