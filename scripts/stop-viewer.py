#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""avdanyuwiki 本地作品资料查看器 · 服务停止脚本"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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

PORT = 8971
HEALTH_URL = f"http://127.0.0.1:{PORT}/__health"

if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("avdanyuwiki 本地作品资料查看器 · 停止服务")
    except Exception:
        pass



def is_avdanyu_server():
    try:
        req = urllib.request.Request(HEALTH_URL, headers={'User-Agent': 'avdanyu-stopper'})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get('app') == 'avdanyu-server' and data.get('ok')
    except Exception:
        pass
    return False


def get_listening_pids():
    pids = []
    if os.name == 'nt':
        try:
            out = subprocess.check_output(f'netstat -ano | findstr ":{PORT} " | findstr "LISTENING"', shell=True, text=True)
            for line in out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[-1].isdigit():
                    pid = int(parts[-1])
                    if pid not in pids:
                        pids.append(pid)
        except Exception:
            pass
    return pids


def main():
    print("======================================================================")
    print("   avdanyuwiki 本地作品资料查看器 · 停止服务")
    print("======================================================================")

    pids = get_listening_pids()
    if not pids and not is_avdanyu_server():
        print(f"  [-] 本地服务当前未在运行 (端口 {PORT} 未被占用)。")
        print("======================================================================")
        return

    is_our_server = is_avdanyu_server()
    if not is_our_server and pids:
        print(f"  [!] 警告: 端口 {PORT} 正被进程 (PID: {pids}) 占用，但不是 avdanyu-server 服务。")
        print("      为保障安全，已取消自动终止该进程。")
        print("======================================================================")
        return

    stopped = []
    for pid in pids:
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, check=False)
            else:
                os.kill(pid, 9)
            stopped.append(pid)
        except Exception as e:
            print(f"  [!] 停止 PID {pid} 失败: {e}")

    # 验证是否已停止
    time.sleep(0.5)
    still_alive = is_avdanyu_server()
    if not still_alive:
        pid_str = ', '.join(str(p) for p in stopped) if stopped else str(PORT)
        print(f"  [√] 本地后台服务已成功停止！(PID: {pid_str})")
        print(f"  [√] 端口 {PORT} 已释放。")
    else:
        print(f"  [!] 服务未能完全停止，请稍后重试或在任务管理器中结束 python 进程。")

    print("======================================================================")
    if sys.stdin.isatty():
        try:
            print("  按任意键关闭本窗口... ", end='', flush=True)
            import msvcrt
            msvcrt.getch()
            print()
        except Exception:
            pass


if __name__ == '__main__':
    main()
