#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""avdanyuwiki 本地作品资料查看器 · 启动引导脚本
- 显示运行环境与数据库信息（Python 版本、数据库统计、译文缓存、curl_cffi 状态）
- 检查/启动本地服务（端口 8971，后台常驻）
- 自动打开默认浏览器
- 输出清晰的操作说明与访问地址
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

# 确保在 Windows 控制台下输出 UTF-8，防止 GBK 编码崩溃
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE = Path(__file__).resolve().parent.parent
PORT = 8971
URL = f"http://127.0.0.1:{PORT}/avdanyu-viewer.html"
HEALTH_URL = f"http://127.0.0.1:{PORT}/__health"

if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("avdanyuwiki 本地作品资料查看器 · 启动器")
    except Exception:
        pass



def check_health(timeout=1.5):
    try:
        req = urllib.request.Request(HEALTH_URL, headers={'User-Agent': 'avdanyu-launcher'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get('app') == 'avdanyu-server' and data.get('ok'):
                    return True
    except Exception:
        pass
    return False


def get_db_info():
    db_path = BASE / 'avdanyu-data' / 'avdanyu.db'
    if not db_path.exists():
        return False, "未找到数据库文件 (avdanyu-data/avdanyu.db)"
    size_mb = db_path.stat().st_size / (1024 * 1024)
    works_count = 0
    months_count = 0
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        if 'meta' in tables:
            cur.execute("SELECT v FROM meta WHERE k='count'")
            row = cur.fetchone()
            if row:
                works_count = int(row[0])
        if 'months' in tables:
            cur.execute("SELECT count(*) FROM months")
            row = cur.fetchone()
            if row:
                months_count = row[0]
        conn.close()
    except Exception:
        pass
    info = f"{works_count:,} 部作品 · {months_count} 个月份 · {size_mb:.1f} MB"
    return True, info


def get_translations_info():
    tr_path = BASE / 'avdanyu-data' / 'translations.json'
    if not tr_path.exists():
        return "0 条 (未生成)"
    try:
        with open(tr_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return f"{len(data):,} 条译文"
    except Exception:
        return "已存在"


def start_server_process():
    server_script = BASE / 'scripts' / 'avdanyu-server.py'
    pythonw = 'pythonw'
    from shutil import which
    if not which(pythonw):
        pythonw = sys.executable

    kwargs = {}
    if os.name == 'nt':
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs['creationflags'] = CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs['close_fds'] = True

    subprocess.Popen([pythonw, str(server_script)], cwd=str(BASE), **kwargs)



def main():
    print("======================================================================")
    print("   avdanyuwiki 本地作品资料查看器")
    print("======================================================================")

    # 1. 环境与资源检测
    py_ver = f"Python {sys.version.split()[0]}"
    print(f"  [√] 运行环境  : {py_ver}")

    db_ok, db_desc = get_db_info()
    if db_ok:
        print(f"  [√] 作品数据  : avdanyu-data/avdanyu.db ({db_desc})")
    else:
        print(f"  [!] 作品数据  : {db_desc}")
        print("                 提示: 请先运行 python scripts/avdanyu-merge.py 整合数据")

    tr_desc = get_translations_info()
    print(f"  [√] 译文缓存  : avdanyu-data/translations.json ({tr_desc})")

    has_curl = False
    try:
        import curl_cffi  # noqa
        has_curl = True
    except ImportError:
        pass
    if has_curl:
        print("  [√] 谷歌备用  : curl_cffi 已安装 (备用引擎支持浏览器级指纹请求)")
    else:
        print("  [-] 谷歌备用  : 未安装 curl_cffi (备用引擎可用但稳定性稍差，可运行: pip install curl_cffi)")

    # 智谱 GLM 翻译引擎检测（已配置时优先于谷歌引擎）
    zc = {}
    try:
        with open(BASE / 'avdanyu-data' / 'zhipu-config.json', 'r', encoding='utf-8') as f:
            zc = json.load(f)
        if not isinstance(zc, dict):
            zc = {}
    except Exception:
        pass
    zp_key = os.environ.get('ZHIPU_API_KEY', '').strip() or str(zc.get('api_key') or '').strip()
    zp_model = os.environ.get('ZHIPU_MODEL', '').strip() or str(zc.get('model') or '').strip()
    if zp_key:
        print(f"  [√] 翻译引擎  : 智谱 GLM 已配置 (模型 {zp_model or 'glm-4.6 默认'}，谷歌收底)")
    else:
        print("  [-] 翻译引擎  : 未配置智谱 API Key (当前使用免费谷歌引擎)")
        print("                   提示: 将 Key 填入 avdanyu-data/zhipu-config.json 即可启用 GLM 模型翻译")

    print("----------------------------------------------------------------------")

    # 2. 检查服务是否已在运行
    server_running = check_health(timeout=1.5)
    if server_running:
        print(f"  [*] 本地服务  : 已在运行中 (端口 {PORT})")
    else:
        print(f"  [*] 正在启动本地后台服务 (端口 {PORT}) ...")
        start_server_process()
        for _ in range(12):
            time.sleep(0.5)
            if check_health(timeout=0.8):
                server_running = True
                break

        if server_running:
            print(f"  [√] 本地服务  : 启动成功！(端口 {PORT})")
        else:
            print(f"  [!] 本地服务  : 正在后台初始化，稍后将自动响应")

    # 3. 打开浏览器
    print(f"  [*] 正在打开默认浏览器访问查看器...")
    try:
        webbrowser.open(URL)
        print(f"  [√] 浏览器已自动打开！")
    except Exception as e:
        print(f"  [!] 自动打开浏览器失败，请手动在浏览器访问网址: {e}")

    # 4. 显示使用说明
    print("----------------------------------------------------------------------")
    print(f"  * 访问地址 : {URL}")
    print(f"  * 本地端口 : {PORT}")
    print("----------------------------------------------------------------------")
    print("  [操作提示]")
    print("  1. 浏览器已自动打开，您可以在网页中自由检索、浏览与整理作品。")
    print("  2. 本窗口可以安全关闭，后台服务仍会持续运行，绝不影响网页使用。")
    print("  3. 若需彻底关闭本地后台服务，请双击运行同目录下的 stop-viewer.bat。")
    print("======================================================================\n")

    # 5. 等待用户按键退出（交互式终端中运行，非交互式直接退出）
    if sys.stdin.isatty():
        try:
            print("  按任意键关闭本窗口 (后台服务保持运行)... ", end='', flush=True)
            import msvcrt
            msvcrt.getch()
            print()
        except Exception:
            pass


if __name__ == '__main__':
    main()
