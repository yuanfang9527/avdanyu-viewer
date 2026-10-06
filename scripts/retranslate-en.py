#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用智谱 GLM 重新翻译译文中质量不佳的条目（谷歌引擎时代遗留）。

默认模式：重译含 ≥2 个英文/罗马音词、或完全没有汉字的译文。
--names 模式：重译人名不规范的译文（含假名人名如 白花にあ，或结尾挂罗马音人名如 Fuua Kaede），
配合服务端「日本人名转换为通行简体汉字」的提示词规则。

用法（项目根目录运行）：
  python scripts/retranslate-en.py --dry-run            # 只统计数量和样例，不修改
  python scripts/retranslate-en.py                      # 默认模式全量重译
  python scripts/retranslate-en.py --names --dry-run    # 人名模式统计
  python scripts/retranslate-en.py --names              # 人名模式全量重译
  python scripts/retranslate-en.py --names --limit 50   # 小批量试跑
  python scripts/retranslate-en.py --names --reset      # 清空人名模式进度重新开始

说明：
- 复用 avdanyu-server.py 的智谱翻译链路（端点探测、内容过滤自动拆批）。
- 每完成 5 批即把结果原子写回 translations.json，随时可 Ctrl+C 中断，重跑自动续。
- 重译失败/被内容过滤的条目保留原值；默认模式记入 retranslate-progress.json，
  人名模式记入 retranslate-names-progress.json（防止重复重译）。
- 浏览器端下次打开页面时自动以磁盘译文为准更新本地缓存。
"""
import argparse
import importlib.util
import json
import re
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TR_FILE = BASE / 'avdanyu-data' / 'translations.json'
PROGRESS_FILE = BASE / 'avdanyu-data' / 'retranslate-progress.json'
PROGRESS_NAMES_FILE = BASE / 'avdanyu-data' / 'retranslate-names-progress.json'
BATCH = 15

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# 加载服务器模块，复用 zhipu_translate_texts（含端点探测与内容过滤拆批）
_spec = importlib.util.spec_from_file_location('avdanyu_server', BASE / 'scripts' / 'avdanyu-server.py')
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)


def cjk_count(v):
    return sum(1 for ch in v if '\u4e00' <= ch <= '\u9fff')


def needs_retry(zh):
    if not zh:
        return False
    lw = re.findall(r'[A-Za-z]{2,}', zh)
    return len(lw) >= 2 or (cjk_count(zh) == 0 and len(lw) >= 1)


def needs_name_fix(zh):
    """人名问题条目：含连续假名（如 白花にあ），或结尾挂罗马音人名（如 Fuua Kaede）。"""
    if not zh:
        return False
    if re.search(r'[\u3040-\u309f\u30a0-\u30ff]{2,}', zh):
        return True
    if re.search(r'[A-Z][a-z]+ [A-Z][a-z]+$', zh.strip()):
        return True
    return False


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, data):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(description='用智谱 GLM 重译英文词汇偏多或人名不规范的译文')
    ap.add_argument('--dry-run', action='store_true', help='只统计，不翻译不修改')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少条（0=不限）')
    ap.add_argument('--workers', type=int, default=2, help='并发数（默认 2）')
    ap.add_argument('--reset', action='store_true', help='清空进度记录重新开始')
    ap.add_argument('--names', action='store_true',
                    help='人名修复模式：重译含假名人名或结尾罗马音人名的条目（独立进度）')
    args = ap.parse_args()

    needs = needs_name_fix if args.names else needs_retry
    progress_file = PROGRESS_NAMES_FILE if args.names else PROGRESS_FILE

    if args.reset and progress_file.exists():
        progress_file.unlink()
        print('已清空进度记录。')

    data = load_json(TR_FILE, {})
    if not data:
        print('未找到译文文件或内容为空。')
        return
    done = set(load_json(progress_file, []))

    targets = [k for k, v in data.items() if needs(v) and k not in done]
    total_all = sum(1 for v in data.values() if needs(v))
    print(f'译文总数 {len(data)}，命中筛选 {total_all} 条，待处理 {len(targets)} 条'
          f'（已完成 {total_all - len(targets)}）。')

    if args.dry_run:
        for k in targets[:10]:
            print(f'  [{len(re.findall(r"[A-Za-z]{2,}", data[k]))}词] {data[k][:60]}')
        return
    if not targets:
        print('没有需要重译的条目。')
        return
    if args.limit > 0:
        targets = targets[:args.limit]
        print(f'按 --limit 只处理前 {len(targets)} 条。')

    lock = threading.Lock()
    batches = [targets[i:i + BATCH] for i in range(0, len(targets), BATCH)]
    state = {'bi': 0, 'ok': 0, 'skip': 0, 'since_save': 0, 't0': time.time()}
    updates = {}     # 本脚本改过的条目；写回前重读文件再合并，避免覆盖运行期间浏览器新增的译文

    def save_progress():
        current = load_json(TR_FILE, {})
        current.update(updates)
        save_json_atomic(TR_FILE, current)
        save_json_atomic(progress_file, sorted(done))

    def worker():
        while True:
            with lock:
                if state['bi'] >= len(batches):
                    return
                bi = state['bi']
                state['bi'] += 1
            batch = batches[bi]
            try:
                results, model, err = srv.zhipu_translate_texts(batch, 'ja', 'zh-CN')
            except Exception as e:
                results, err = None, f'{type(e).__name__}: {e}'
            with lock:
                if results:
                    for jp, zh in zip(batch, results):
                        done.add(jp)
                        if zh and zh != data.get(jp):
                            data[jp] = zh
                            updates[jp] = zh
                            state['ok'] += 1
                        else:
                            state['skip'] += 1
                else:
                    state['skip'] += len(batch)
                state['since_save'] += 1
                n = state['bi']
                if state['since_save'] >= 5 or n >= len(batches):
                    state['since_save'] = 0
                    try:
                        save_progress()
                    except Exception as e:
                        print(f'!! 写回失败：{e}')
                el = time.time() - state['t0']
                print(f'批次 {n}/{len(batches)}：累计重译 {state["ok"]}，保留 {state["skip"]}'
                      f'（{el:.0f}s，{err[:60] if not results else model}）')

    threads = [threading.Thread(target=worker) for _ in range(max(1, args.workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    save_progress()
    print(f'完成：重译成功 {state["ok"]} 条，保留原值 {state["skip"]} 条，'
          f'用时 {time.time() - state["t0"]:.0f}s。')
    print('打开查看器页面（或按 F5）即可看到更新后的译名。')


if __name__ == '__main__':
    main()
