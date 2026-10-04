# Agent 工作流与项目约定

本文件是后续 Agent 在本项目工作的操作指南。先读本文件和 `README.md`；
历史记录（`MEMORY.md`、`.workbuddy/memory/`、旧版变更说明）只作为背景，
可能包含过期路径、依赖状态或数据数量，执行前必须以当前文件和命令输出为准。

## 1. 项目结构与数据流

- `jobanal/`：**共享规则与数据层的唯一来源**，只用标准库，可脱离 pandas 测试。
  - `parsing.py`：薪资解析（含量纲校验与异常隔离）、城市/学历/经验/技能归一、
    公司名口径。改动解析规则只改这里。
  - `taxonomy.py`：工种族词表、体量权重、匹配顺序。**词表与族定义必须一致**，
    `validate_taxonomy()` 与 `tests/test_smart_crawl.py` 会强制检查——历史上
    词表里有 `mobile` 而族定义里没有，长出了「幽灵族」，导致虚增已铺族数、
    提前跳过真正该抓的组合。
  - `classify.py`：岗位标题 -> 工种族。规则是「长词优先 + 英文词边界 + 泛化词让位」，
    顺序问题（如把 `java` 匹配到 `javascript`）会直接影响「哪些组合被跳过」。
  - `store.py`：SQLite 数据层（`data/jobs.db`）。主键用抓取器的 job_id，
    另有 `postings` 快照表保存 `scraped_at`，支撑趋势与挂岗时长分析。
  - `config.py`：路径、批次文件名模式、列定义。
- `cli.py`：统一命令行入口（`import` / `import-csv` / `verify` / `trends` / `coverage` / `csv`）。
- `app.py`：Flask 后端、CSV 清洗、筛选、统计及导出；可选读取 SQLite 提供 `/api/trends`。
- `templates/`、`static/`：页面和前端资源；`static/vendor/echarts.min.js` 为本地 ECharts，
  页面会优先用它、失败再回退 CDN（不要改回只用 CDN，断网演示会全白）。
- `scraper/scripts/boss_cdp_raw.py`：CDP 抓取器；抓取结果落到 `抓取结果/`。
- `crawl_and_import.py`：一次关键词/城市抓取并导入。
- `smart_crawl.py`：覆盖度调度、抓取归档和周期性导入；批量采集优先使用它。
- `boss_import.py`：将 `抓取结果/` 中的 JSON 转为系统标准 CSV，默认写 `data/boss_jobs.csv`。
- `merge_jobs.py`：按岗位 ID 和信息完整度合并全部归档，默认写 `data/boss_jobs.csv`。
- `tools/`：校准与诊断脚本（如 `calibrate_family_classifier.py`），不属于主链路。
- `data/boss_jobs.csv`：应用默认数据源。上传文件只在当前 Python 进程内临时生效，不等于更新这个 CSV。
- `data/jobs.db`：SQLite 库，由 `python cli.py import --archive 抓取结果` 生成，可反复执行（幂等）。
- `data/crawl_ledger.json`：智能调度状态；重置或改写它会影响后续计划。

数据主链路：

```text
BOSS CDP 抓取 -> 抓取结果/<批次>/boss_jobs_*.json
              -> boss_import.py   -> data/boss_jobs.csv   -> app.py 看板 / API
              -> cli.py import    -> data/jobs.db         -> app.py /api/trends
```

`boss_import.py` 和 `merge_jobs.py` 都会重写输出 CSV，不是仅向文件末尾追加。
要运行它们前先确认输入归档完整、目标路径正确，并保护现有数据。

### 时间戳口径（改动前务必读）

`store.parse_batch()` 取 `scraped_at` 的顺序是：批次 JSON 的 `scraped_at` →
派生汇总文件的 `generated_at`（仅在 `--include-derived` 时）→ 批次目录名里的日期 →
**留空**。**不要加回「回退到文件 mtime」**：把仓库克隆到新机器后 mtime 会变成
克隆时间，趋势图最后一根柱子会被顶到导入当天。

`抓取结果/boss_jobs_all.json` 是 `consolidate_archive.py` 的一次性汇总快照，
与各批次内容重叠，因此三个导入器默认都跳过它；只有当历次批次已被清理、
只剩这个文件时才用 `--include-derived` 找回岗位。

## 2. 开始任何任务前

1. 确认当前工作目录是项目根目录；优先用项目内相对路径，不要假设机器上的绝对路径。
2. 读取与任务有关的 README、变更说明和代码；不要把旧工作日志里的环境路径、数据行数或登录状态当作当前状态。
3. 检查 `python3 --version`、依赖和目标文件当前状态。此机器历史上使用过 Python 3.9、3.13 等不同版本。
4. 检查待修改文件是否已有用户改动；只做任务所需的改动，不覆盖或清理无关文件。
5. 爬取、覆盖/合并主 CSV、重置抓取账本等会产生外部请求或影响持久数据的操作，只有用户明确要求时才执行。

## 3. Python 环境与入口

### macOS

- Web 应用启动：优先双击 `启动系统.command`；终端启动可用 `python3 app.py`。
- 初始化项目环境：

  ```bash
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  ```

- 测试：`.venv/bin/python -m unittest discover -s tests -v`
- `启动系统.command` 优先使用 `.venv`，并在默认端口被占用时选后续空闲端口。

### Windows

- 使用 `py` 或项目虚拟环境的解释器；项目现有 `启动系统.bat` 是 Windows 入口。
- 初始化依赖：

  ```bat
  py -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  ```

### 解释器版本注意事项

- `smart_crawl.py` 和应用源码应优先通过 `.py` 文件运行；`__pycache__/*.pyc` 是解释器版本相关的缓存，不是源码，不要尝试用另一个 Python 版本打开或提交。
- 第三方 `scraper/scripts/boss_cdp_raw.py` 使用 Python 3.10+ 的类型语法；Python 3.9 运行抓取器可能在启动时失败。真正抓取前先确认被 `--python` 指定的解释器为 3.10+，并用该解释器运行抓取脚本 `--check`。应用本身可以单独使用满足依赖要求的 Python 运行。
- 抓取器还需要 `requests` 和 `websocket-client`；它们已列在 `requirements.txt`。
- 安装包缺失时才安装/修复依赖；不要为了单个检查随意升级全套依赖。

## 4. 测试和验证

每次代码改动：

1. 运行覆盖本次行为的定向测试；项目测试框架为标准库 `unittest`，不需另装 pytest。
2. 运行完整回归测试：

   ```bash
   python3 -m unittest discover -s tests -v
   ```

3. 若改动 Flask 数据或 API，使用 `app.app.test_client()` 检查状态码和关键返回值，不要只以 HTTP 200 判定正确。
4. 若改动归档导入/合并，在临时目录测试异常文件，确认失败会指出文件路径，并且失败时不会覆盖目标 CSV。
5. 不以真实 BOSS 登录或真实网络抓取作为普通代码测试。优先使用临时 CSV/JSON 和 mock；调度器端到端测试前备份并恢复 ledger。
6. 测试后清理自己创建的临时文件和测试进程，不要删除用户数据、抓取归档或备份。

## 5. 数据导入、合并和保护

1. 先确认来源和输出：

   ```bash
   python3 boss_import.py --list
   python3 boss_import.py --dry-run
   python3 merge_jobs.py --dry-run
   python3 cli.py import --archive 抓取结果        # SQLite 落库（幂等）
   python3 cli.py verify                            # 落库后核对数据质量
   ```

   `boss_import.py --dry-run` 检查普通导入流程；`merge_jobs.py --dry-run` 检查所有归档并统计智能合并结果。
   `cli.py import` 是增量的、可反复执行：同一份归档重复导入时 `new`/`updated` 都是 0。
2. 写入 `data/boss_jobs.csv` 前先确认有效记录数、错误归档数、输出路径，并制作独立备份。
3. `boss_import.py` 会根据所选来源重新生成输出文件；除非确认它涵盖所需的全部归档，不要把它当作增量 append 命令。
4. `merge_jobs.py` 面向全部抓取归档；若任何归档损坏或结构不正确，它应报错并中止，不要绕过报错强行写出不完整 CSV。
5. 写入后用 CSV 解析器验证：行数、列名、去重结果、薪资空值比例；再用应用 API 验证仪表盘总量。
6. 用户只要求查看或计划时，不要实际写入主 CSV；使用 `--dry-run`。
7. 隔离文件 `data/salary_quarantine.csv` 记录被剔除的薪资异常记录（含原因），
   它已被 `.gitignore` 忽略；发现异常数量突变时应先看这个文件再决定是否调整解析规则。

## 6. 抓取工作流

只有用户明确要求实际抓取时才运行：

1. 阅读 `smart_crawl.py --help` 和 README 的当前限制；检查登录态、Edge/CDP 和 Python 3.10+ 抓取解释器。
2. 先生成计划并向用户汇报范围、组合数、页数、可能请求量和输出路径：

   ```bash
   python3 smart_crawl.py --plan --budget <组合数> --pages <页数>
   ```

3. 计划确认后再运行 `--run`。遵守 README 的低频和单次不超过 300 条约定；请求之间保留项目默认等待，不并发发起抓取。
4. 不运行 `--reset`，不清理/改写 ledger 或历史归档，除非用户明确要求。
5. 如果用户要求更新系统数据，确保导入输出指向 `data/boss_jobs.csv`，不能悄悄输出到临时 CSV 后就报告系统已更新。
6. 抓取完成后核对原始批次数、有效记录数、新增唯一岗位数和主 CSV 行数；明确说明过滤、重复、无 JD 等情况。

## 7. 完成任务前

- 更新与改动直接相关的 README 或变更说明；重要操作约定放在本文件。
- 报告改了什么、运行了哪些测试、是否修改了主数据/ledger/归档、还有哪些未验证。
- 不声称“数据已更新”除非验证 `data/boss_jobs.csv` 和应用 API 确实读到新数据。
