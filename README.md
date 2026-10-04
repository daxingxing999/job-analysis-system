# 招聘网站岗位需求分析与可视化系统

基于 **Flask + Pandas + ECharts** 的岗位数据分析系统，可直接从 BOSS 直聘抓取真实岗位数据并完成清洗、统计、检索与可视化。

一句话流程：**抓取真实数据 → 转成标准 CSV → 启动系统看看板**。

---

## 目录结构

```
job-analysis-system/
├── cli.py                  # 统一命令行入口：import / verify / trends / coverage / csv
├── app.py                  # Flask 后端：清洗、统计、筛选、导出、趋势接口
├── jobanal/                # 核心包（只用标准库，可独立测试）
│   ├── parsing.py          #   字段解析与薪资量纲校验的唯一入口
│   ├── taxonomy.py         #   工种族词表与匹配顺序（唯一来源）
│   ├── classify.py         #   岗位标题 -> 工种族 分类器
│   ├── store.py            #   SQLite 数据层：落库、快照、趋势聚合
│   └── config.py           #   路径与常量集中定义
├── boss_import.py          # 抓取结果 JSON -> 系统标准 CSV
├── merge_jobs.py           # 按 job_id 合并全部归档，取信息最全的快照
├── crawl_and_import.py     # 一键：单次抓取 + 导入（推荐入口）
├── smart_crawl.py          # 覆盖度驱动的批量调度器（城市 × 工种族）
├── batch_crawl.py          # 早先的多关键词×多城市批量抓取
├── requirements.txt
├── data/
│   ├── boss_jobs.csv       # 真实抓取数据（默认数据源，当前 8274 条）
│   ├── jobs.db             # SQLite 库（由 cli.py import 生成，可选但推荐）
│   └── crawl_ledger.json   # 智能调度账本
├── templates/index.html    # 前端页面
├── static/                 # css / js；vendor/ 内置 ECharts（离线可用）
├── scraper/                # BOSS 直聘 CDP 抓取脚本（第三方，见 docs/THIRD_PARTY_LICENSE）
│   ├── scripts/boss_cdp_raw.py
│   └── data/city_codes.json
├── 抓取结果/                # 历次抓取归档，导入时自动累加
├── tools/                  # 校准与诊断脚本（非主链路）
└── docs/                   # 毕业设计论文、代码评审报告等文档
```

> 各脚本之间共享的规则（薪资解析、经验分箱、工种词表、路径常量）都收敛在
> `jobanal/` 包里，不再各写一份。`tests/` 有对应的回归用例。

## 一、跑起来看效果

### macOS

双击项目目录中的 `启动系统.command` 即可启动；也可以在终端运行：

```bash
cd "/你的路径/job-analysis-system"
chmod +x "启动系统.command"  # 首次需要执行一次
./启动系统.command
```

脚本会优先使用项目内的 `.venv`，并自动打开系统页面。默认端口为 5000；
如果被 macOS 系统服务占用，会自动选择下一个空闲端口。
如果提示缺少依赖，在项目目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

服务运行期间请保持终端窗口打开；关闭窗口即可停止服务。

### Windows

```bash
cd C:\Users\Administrator\Desktop\作业\job-analysis-system
py -m pip install -r requirements.txt
py app.py
```

浏览器打开 <http://127.0.0.1:5000/>。

默认读取 `data/boss_jobs.csv`（当前 8274 条真实岗位数据）。项目不提供示例数据；
如果默认文件缺失，启动时会明确报错，提示先导入抓取归档或指定有效 CSV。
如需使用其它已有的标准岗位 CSV，可设置 `ZOUYE_DATA_FILE`：

```bat
set ZOUYE_DATA_FILE=C:\...\job-analysis-system\data\other_jobs.csv
py app.py
```

macOS/Linux 可这样指定：

```bash
ZOUYE_DATA_FILE=/path/to/other_jobs.csv python3 app.py
```

> 环境若没装依赖，先建虚拟环境：
> `py -m venv .venv` 然后 `.venv\Scripts\pip install -r requirements.txt`

## 二、抓取真实数据

macOS/Linux 使用 `python3`（或虚拟环境中的 `.venv/bin/python`）；Windows 使用 `py`。

```bash
python3 crawl_and_import.py --keyword "Python" --city 杭州 --pages 3
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--keyword` | Python | 搜索关键词 |
| `--city` | 杭州 | 城市名（中文） |
| `--pages` | 3 | 页数，上限 10，每页 15 条 |
| `--details` | 0 | 抓多少条职位描述（JD）。**默认 0 = 不抓** |
| `--browser` | edge | `edge` 或 `chrome` |

**为什么默认不抓 JD**：系统需要的 10 个字段里，`job_name / company / city / salary_low / salary_high / education / work_years / category / skills` 在列表页就已齐全，只有 `description` 需要进详情页。而详情页每条要加载页面、模拟滚动、间隔等待，单条 10-25 秒。不抓详情时 3 页约 **1 分钟**；抓 20 条详情要 8-10 分钟。做词云需要 JD 时再抓少量：

```bash
python3 crawl_and_import.py --keyword "Python" --city 杭州 --pages 3 --details 5
```

抓取结果会自动归档到 `抓取结果/<时间戳>/` 并**与历史批次累加**导入，不会覆盖已有数据。

### 首次使用需要登录一次

```bash
python3 crawl_and_import.py --setup-only --browser edge   # 没装 Chrome 就用 edge
```

会打开一个**独立 profile 的专用浏览器**（`~/.boss-zhipin-scraper/chrome-profile`），不读取你日常浏览器的 Cookie 和密码。在其中登录 BOSS 直聘后，登录态持久保存，之后抓取无需重复登录。

其他命令：

```bash
python3 crawl_and_import.py --check      # 环境自检（依赖 / CDP / 登录态）
```

## 三、只转换已有抓取结果

如果只想把已有的 JSON 转成 CSV（不联网）：

```bash
python3 boss_import.py                                   # 合并 抓取结果/ 下全部批次
python3 boss_import.py --list                            # 查看有哪些批次
python3 boss_import.py --city 杭州 --output data/x.csv    # 按城市筛选
python3 boss_import.py --dry-run                         # 只看统计不写文件
```

转换规则：

| 系统字段 | 来源 | 处理 |
| --- | --- | --- |
| `job_name` | title | — |
| `company` | 详情 company，回退 boss_name | — |
| `city` | location | 取 `·` 前第一段 |
| `salary_low` / `salary_high` | salary | 支持 `15-30K`、`30-60K·15薪`、`1.5-2万`、`300-500元/天`（×21.75 折算月薪）、`50元/时`；`面议` 留空并过滤，不伪造 |
| `education` | tags | 匹配学历标签 |
| `work_years` | tags | 归一到 `应届/无经验`、`1-3年`、`3-5年`、`5年以上`；实习岗标签缺经验时按标题回退 |
| `category` | company_industry | — |
| `skills` | skills | `\|` 分隔转逗号分隔；福利/条件描述（「不接受居家办公」「五险一金」）会被剔除，不当作技能 |
| `description` | 详情 jd | 按 `job_id` 关联，换行压平 |
| `job_key` | job_id | 抓取器给的稳定标识（md5 前 16 位）。**重新导入 CSV 不会变**，详情页用它定位 |
| `job_link` | job_link | 原始岗位链接，详情弹窗里可直接跳回 BOSS |

> `job_key` / `job_link` 是后加的两列。老的 CSV（只有前 10 列）依然能用 ——
> `clean_data()` 会把缺失的列补成空字符串，只是详情页没有原始链接可跳。

> **薪资异常会被隔离而不是静默入库**：月薪低于 1000 或高于 10 万的、上下限跨度
> 超过 50 倍的、量纲冲突的（同时出现「元/天」和「元/时」）都会带原因写进
> `data/salary_quarantine.csv`，不进主表。此前「95700-104400」这类疑似年包
> 与「250000-260000」这类极端值会直接把看板平均薪资抬高。

## 三之二、入库并查看趋势（推荐）

CSV 里没有采集时间，所以「技能需求随时间怎么变」这类分析只能靠 SQLite。
落库后看板会多出一张「采集趋势」折线图：

```bash
python3 cli.py import --archive 抓取结果        # 归档 -> data/jobs.db（幂等，可反复执行）
python3 cli.py verify                            # 核对数据质量与完整性
python3 cli.py trends --limit 6                  # 命令行直接看趋势
python3 cli.py coverage --limit 10               # 城市/行业覆盖与顶部岗位
python3 cli.py csv --output data/export.csv      # 从库里导出
```

`data/jobs.db` 是**生成物、不入库**（`.gitignore` 已忽略，约 5 MB），
第一次跑 `import` 会自动建好；没有它时看板照常工作，只是不显示趋势图。

关于口径的两点说明：

- `import` **默认不读** `抓取结果/boss_jobs_all.json`——它是
  `consolidate_archive.py` 生成的一次性汇总快照，与各批次内容重叠。如果历次批次
  已被清理、只剩这个汇总文件，加 `--include-derived` 才能把那部分岗位找回来；
  代价是它在趋势里会被算作同一个采集日。
- 时间戳优先取批次 JSON 里的 `scraped_at`（派生文件另认 `generated_at`），
  其次取批次目录名里的日期，都拿不到就留空。**不会拿文件修改时间兜底**，
  否则把仓库克隆到新机器后，趋势图的最后一根柱子会被顶到「今天」。

## 四、功能一览

- **数据清洗**：UTF-8/GB18030 自动识别；去空、去重、薪资解析（含量纲校验与异常隔离）、经验分箱
- **多维筛选**：城市、学历、经验、行业、关键词、薪资区间
- **统计看板**：岗位总量、平均薪资、薪资/学历/经验/城市/行业/企业分布、技能与关键词
- **采集趋势**：技能需求与城市岗位量随采集日的变化（需先落库，见第三之二节）
- **数据质量核对**：`cli.py verify` 报告 JD 覆盖率、匿名公司占比、重复抓到次数等
- **采集收益分析**：`tools/archive_report.py` 单遍扫描归档，给出各城市/关键词的
  收益率、重复快照的字段差异、数据缺口（替代原先三个各扫一遍的脚本）
- **岗位浏览**：表格支持排序（薪资/城市/公司/岗位名）与分页，每页 20/50/100 条
- **岗位详情**：点击列表查看完整字段与职位描述
- **数据导入**：页面上传 CSV/Excel，或命令行转换抓取结果、导入 SQLite
- **结果导出**：按当前筛选条件导出 CSV / Excel
- **文本分析**：优先 `jieba` 分词，未安装时降级为规则分词，页面会标明

> ⚠️ **关于文本分析的现状**：抓取链路（`smart_crawl.py`、`batch_crawl.py`）写死了
> `--no-detail`，归档里因此没有职位描述，当前 `data/boss_jobs.csv` 的 8274 行
> `description` 全为空。所以页面上「职位文本高频词」实际取的是技能标签，
> 词云能力要等抓取时带上 `--details N` 才有数据。`cli.py verify` 会主动提示这一点。

## 五、主要接口

| 接口 | 作用 |
| --- | --- |
| `/api/dashboard` | 看板全部统计 |
| `/api/options` | 筛选项 |
| `/api/jobs-page` | 岗位列表分页/排序（`limit` `offset` `sort` `order`），返回 `total` |
| `/api/jobs/by-key/<job_key>` | 按**稳定标识**取详情（重新导入后链接依然有效） |
| `/api/trends` | 采集趋势（技能/城市按天）；未建库时返回 `available: false` |
| `/api/jobs` | 岗位列表（固定取前 N 条，保留给旧调用方） |
| `/api/jobs/<job_id>` | 岗位详情（按行号，重新导入后会漂移，仅作兼容保留） |
| `/export/csv` | 按筛选条件导出 CSV |
| `/export/excel` | 按筛选条件导出 Excel |

排序字段走白名单（`salary` / `salary_low` / `salary_high` / `city` / `company` /
`job_name` / `education` / `experience` / `category`），非法值安全回退，
`limit` 限制在 1~200。

## 六、排错

**CDP 连不上 / `Network.enable` 超时**
机器设置了 `HTTP_PROXY` 时，发往 `127.0.0.1:9222` 的 CDP 请求会被代理拦截并返回 502，脚本却可能误判为"CDP 已就绪"。`crawl_and_import.py` 已自动设置 `NO_PROXY=127.0.0.1,localhost`；手动运行抓取脚本时请自行加上：

```bat
set NO_PROXY=127.0.0.1,localhost
```

**浏览器窗口一闪就没了**
浏览器进程会随命令结束退出，所以"启动浏览器 / 等待登录 / 抓取 / 导入"必须串在一次执行中。`crawl_and_import.py` 已按此方式编排，直接用它即可。

**薪资为空**
通常是未登录或登录态失效。重新执行 `python3 crawl_and_import.py --setup-only` 登录即可，不要改用 DOM 兜底拿不可信的薪资。

**Excel 导出失败**
需要 `openpyxl`，执行 `pip install -r requirements.txt` 即可。

## 七、说明

抓取脚本 `scraper/scripts/boss_cdp_raw.py` 来自开源项目 [boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper)（MIT，见 `docs/THIRD_PARTY_LICENSE`）。本项目仅调用它获取本人有权查看的公开岗位信息，用于课程设计与学习研究，单次不超过 300 条，请求保持低频。

抓取环境依赖包含在 `requirements.txt` 中：其中 `websocket-client` 为 CDP 抓取脚本必需依赖。安装或更新依赖后运行：

```bash
python -m pip install -r requirements.txt
```

## 八、变更记录

- 本轮改造（分支 `feature/review-hardening`）：新增 `jobanal/` 核心包与 SQLite
  数据层、薪资量纲校验与异常隔离、采集趋势图、统一 CLI（`cli.py`）、
  分析脚本归并为单遍扫描、岗位表分页/排序，修复智能调度的零结果永久拉黑与
  切片补不满，本地化 ECharts，补齐 CI 与 LICENSE。
  详见 [`docs/代码评审报告.md`](./docs/代码评审报告.md)。
- [2026-09-29 项目变更说明](./docs/变更说明_2026-09-29.md)：移除示例数据回退并补齐抓取依赖。
- [2026-09-28 项目变更说明](./docs/变更说明_2026-09-28.md)：岗位数据合并、macOS 启动脚本及 Python 字节码说明。

## 九、运行测试

项目使用 Python 标准库 `unittest`，无需额外安装测试框架：

```bash
python3 -m unittest discover -s tests -v
```

当前共 133 个用例。本机缺少 pandas 时，`test_app.py` 整个模块会以 SkipTest 明示
（其余 112 项照常运行）；装了依赖的环境会全部执行：

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_parsing.py` | 39 | 薪资量纲、异常隔离、字段归一、公司名口径 |
| `tests/test_smart_crawl.py` | 30 | 调度硬约束、切片累计上限、账本原子写、分类器 |
| `tests/test_store.py` | 21 | SQLite 落库幂等性、时间戳策略、趋势聚合、旧库迁移 |
| `tests/test_app.py` | 21 | 看板接口、分页与排序、稳定标识详情、CSV 缓存（需 pandas） |
| `tests/test_archive.py` | 16 | 归档单遍扫描、收益率、重复分布、字段差异 |
| `tests/test_merge_jobs.py` | 6 | 归档合并与损坏文件的处理 |

## 十、后续 Agent 工作流

后续 Agent 开始操作前请先阅读 [`AGENTS.md`](./AGENTS.md)。其中记录了环境版本要求、
测试命令、抓取边界，以及导入和合并数据时保护主 CSV 与调度账本的步骤。
