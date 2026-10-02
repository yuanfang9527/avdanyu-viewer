# `scripts/` 文件说明

查看器通过项目根目录的 `start-viewer.bat` 启动。这里既有运行时文件，也有只在更新数据时才使用的工具，以及自动生成的记录文件。

作品详情中的磁力链接由 `avdanyu-server.py` 的 `GET /__magnets` 接口实时抓取外站，仅在打开作品详情时按番号查询，结果内存缓存 30 分钟；不再读取本地的 `avdanyu-data/magnets.json`。源顺序：sukebei（.si → .net 镜像）→ JavBus（备源）→ btdig（.com → .co）；直连被墙时自动经本机代理（`AVDANYU_PROXY` 可指定）重试。

作品详情中的 JavDB 評論區由 `GET /__comments` 接口抓取：番号搜索定位视频页 → 评论片段端点（`/v/{id}/reviews/lastest`，视频页解析兜底）→ 解析作者/评分/日期/正文，内存缓存 30 分钟。JavDB 屏蔽日本/韩国出口而预告片（FANZA）又需要日本出口，两者按目标站点分出口：JavDB 请求独立挑选出口（`AVDANYU_JAVDB_PROXY` 显式指定 > 直连与本机常见代理端口逐个试探，地区封锁/CF 验证页视为出口不可用，可用出口记忆 10 分钟），磁力/预告片/在线播放仍走原有全局代理逻辑；域名可用环境变量 `AVDANYU_JAVDB_HOSTS` 覆盖。

| 文件 | 用途 | 什么时候需要 |
| --- | --- | --- |
| `avdanyu-server.py` | 提供本地网页、数据库和译文同步接口，中继标题翻译请求，中继外站磁力搜索（`/__magnets`）与 JavDB 評論區抓取（`/__comments`），解析 FANZA 预告片直链（`/__trailer`），搜索迅雷字幕库（`/__subtitles`）并中继下载转 WebVTT（`/__subtitle-file`，域名白名单限迅雷系 CDN） | 每次通过 `start-viewer.bat` 启动时 |
| `test_javdb_comments.py` | JavDB 評論區解析器单元测试（离线，喂合成/真实片段 HTML，含根目录 `javdb_geo_block.html` 地区封锁页 fixture）；`python scripts/test_javdb_comments.py` | 修改评论解析逻辑后 |
| `sql-wasm.js`、`sql-wasm.wasm` | 浏览器读取 `avdanyu.db` 所需的 SQLite 引擎，两个文件须配套保留 | 打开数据库时 |
| `avdanyu-exporter.user.js` | 浏览器油猴脚本，从来源网站导出月度作品文件；不由查看器自动执行 | 抓取或更新作品数据时 |
| `avdanyu-merge.py` | 把月度 `.txt` 文件整合进 `avdanyu-data/avdanyu.db`，并保留已有封面 | 导入新月度数据时 |
| `dedupe_works.py` | 按同月、同番号、同标题合并重复作品；`avdanyu-merge.py` 也会导入其中的去重函数 | 合并数据时必须保留；也可手动运行全库去重 |
| `backfill-covers.py` | 验证外部图床返回有效图片后，为数据库中的空封面回填地址 | 继续补封面时，可选 |
| `retranslate-en.py` | 用智谱 GLM 重译译文中英文词汇偏多的条目（谷歌引擎时代遗留，人名被翻成罗马音等）；筛选、批量重译、增量写回，支持中断续跑 | 觉得旧译文英文太多时，可选 |
| `cover-backfill-state.json` | 回填脚本生成的最近一次批次及累计成功数；任务本身以数据库中仍为空的封面为准 | 仅用于查看回填统计，可重新生成 |
| `retranslate-progress.json` | 重译脚本记录的已处理条目（GLM 译文也可能合法含 "NO.1 STYLE" 等英文词，仅靠筛选无法识别"已修好"，故用此文件防止重复重译） | 任务全部完成后可删除以重新全量筛选 |
| `cover-missing.txt` | 回填脚本生成的未解决作品清单和失败原因 | 排查剩余空封面时，可重新生成 |
| `avdanyu-server.log` | 本地服务生成的诊断日志 | 排查服务问题时，可重新生成 |

Python 运行产生的 `__pycache__/` 是缓存，不属于项目源文件。

## 常用维护命令

在项目根目录运行：

```powershell
python scripts/avdanyu-merge.py
python scripts/dedupe_works.py
python scripts/dedupe_works.py --apply
python scripts/backfill-covers.py --workers 8 --timeout 12
python scripts/retranslate-en.py --dry-run
python scripts/retranslate-en.py --limit 50
```

- 去重脚本不带 `--apply` 时只统计；执行 `--apply` 会修改数据库和月度文件，并在 `avdanyu-data/` 生成恢复用 ZIP。
- 封面回填只写入原本为空、且经 HTTP 和图片内容检查的地址。完整运行会验证当前所有安全候选；可先加 `--limit 100` 小批量试跑。再次运行会重新扫描数据库中仍为空的记录，因此失败的候选也会再次尝试。`cover-backfill-state.json` 记录统计，不是自动跳过失败候选的任务队列。
- 重译脚本需要先在 `avdanyu-data/zhipu-config.json` 配好智谱 Key；`--dry-run` 只统计数量，`--limit 50` 可先小批量试看质量。被智谱内容过滤拒绝的条目保留原译文并记为已处理，不再重试。重译完成后打开或刷新查看器页面，浏览器会自动以磁盘译文为准更新本地缓存。
- `avdanyu-merge.py -del` 会在整合后备份数据库并删除已成功读取的月度源文件。需要保留月度文件时使用不带 `-del` 的命令。
