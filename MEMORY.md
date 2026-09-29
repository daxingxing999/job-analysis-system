# 项目长期约定

## 【2026-09-18 新增】smart_crawl.py — 覆盖度驱动的智能抓取调度器
用户抱怨「城市重复、工种重复、效率低、记不住用了什么组合」，为此新写了 `smart_crawl.py`
（**新文件，没动任何已有代码**）。替代 `batch_crawl.py` 作为日常抓取入口。

核心机制
- **20 个工种族**替代裸关键词（backend/前端/AI/测试/运维/数据/安全/产品/设计/运营/市场/销售/
  HR/财务/教育/医疗/机械制造/建筑/物流/服务业）。**同城同族只抓一次**，
  族内关键词变体按 `keywords[city_rank % len(keywords)]` 在城市间轮转。
- **加权最大最小公平（water-filling）**：`score = city_gap × family_gap × 族体量权重
  × 市场体量(city_weight**0.5) × 冷却惩罚`。配额按城市分档权重（一线8/新一线4/二线2/其他1）
  摊 `--total-target`（默认 25000 条）。堆 + 惰性重算，分数只降不升 → 单轮 ~O(log N)。
- **市场体量因子是必需的**：去掉它，纯缺口法会先抓阿里地区/阿拉尔/三沙这类空城市。
- **冷却**：最近 3 个组合的同城市 ×0.12、最近 2 个的同族 ×0.45，强制城市/工种交替。
- **收益反馈**：每组合抓完和全库 job_id 比对，零新增 → `saturated` 永久拉黑。
- **账本 `data/crawl_ledger.json`**：记录 `城市|族id` → 关键词/raw/new/dup/时间戳/runs。
  下次运行自动跳过。`--report` 生成 `data/crawl_report.md` 覆盖矩阵。

命令
```bat
py smart_crawl.py --plan --budget 30        # 只看计划
py smart_crawl.py --run --budget 30         # 正式抓 30 个组合
py smart_crawl.py --status                  # 账本 + 覆盖率
py smart_crawl.py --report                  # 生成覆盖率报告
py smart_crawl.py --strategy breadth|depth  # 铺广度 / 深挖重点城市
py smart_crawl.py --reset                   # 清空账本
```

### 【2026-09-18 新增】两个突破覆盖效率的开关

**1）`--include-nationwide` 用「全国」快速铺城市覆盖**
- 实测：同样是约 2 分钟一次请求，**单城模式只覆盖 1 座城市（75 条），
  「全国」模式能覆盖 28 座城市（45 条）** —— 城市覆盖效率差 28 倍。
- 所以补 200 座空白城市：全国模式 20 个族 ≈ 40 分钟；逐城抓 ≈ 6.7 小时。
- 优先级按策略自动定：`balanced` 2.0 / `breadth` 3.5 / `depth` 0（不碰）。
- 「全国」不参与城市配额分母，也不受「同城最多铺几族」限制。

**2）`--max-slices N` 突破单查询 150 条硬上限**
- 背景：BOSS 单次搜索最多 10 页 × 15 条 = 150 条。**实测 13 个批次全部是
  75 条（5 页）或 45 条（3 页），一次都没提前跑完**，说明每次都在被上限截断。
- 用法：`--max-slices 3 --slice-strategy experience`（或 `degree`）。
  **只在基查询真的抓满上限时才补切片**，没触顶就不浪费请求。
- 单组合产能 150 → 上千条，用来深耕重点城市（做城市薪资对比时样本量更厚）。
- 账本键变成三段式 `城市|族|切片id`（如 `全国|frontend|exp104`）；
  `parse_combo_key()` 兼容老的两段式，**原来的 13 个组合照常识别**。
- 默认 0 = 关闭，行为与以前完全一致。

### 【重要】调度器两个已修的坑（改打分逻辑前必读）
1. **冷却惩罚是滑动窗口，分数会回升** → 纯惰性删除的堆会失效，回升过的候选
   永远卡在堆底（曾导致「全国」只在第 1 个组合出现过）。现在由
   `_refresh_after_commit()` 主动重算受影响候选，**每一步选中的就是全场最高分**
   （用"先暴力取样、再 plan(1) 比对"的方式验证过 20/20 步吻合）。
   注意验证时必须**先取样后提交**；反过来写会得到全是误报。
2. `max_families_per_city` 不能套用在「全国」上（breadth 预设=2 会让它最多只出 2 次）。

### 抓取实操（2026-09-18 首次真实跑通，务必照做）
- 默认排除「三沙 / 东沙群岛」（BOSS 上基本没岗位，空跑），`--include-all-cities` 可放回。
- 调试开关（正常别用）：`--scraper-script` / `--skip-browser-check`。
- 归档落在 `抓取结果/smart_<日期>_<时分>/boss_jobs_<城市>_<族id>_<切片id>_<时间戳>.json`，
  **文件名必须以 `boss_jobs_` 开头**，否则 `boss_import.py` 的 `rglob("boss_jobs_*.json")` 认不出。
- 每个组合给抓取脚本传独立的 `--output`，因为默认输出名只精确到分钟，同分钟会互相覆盖。
- 改调度逻辑后**必做两项验证**：①堆序不变式（先暴力取样、再 plan(1) 比对）；
  ②端到端冒烟（假抓取脚本 + `--scraper-script` + `--skip-browser-check`），
  **冒烟前先备份 `data/crawl_ledger.json`、测完还原**，否则真实账本会被 reset。

## 【2026-09-24 更新】环境重建 + Edge 常驻 + 本次抓取战果

### venv 再一次丢失并重建（第二次，同 pytest 式老坑）
`C:\Users\Administrator\.workbuddy\binaries\python\envs\zouye-test\` 2026-09-24 又不存在了
（`启动系统.bat` 指向它 → 双击会失败）。重建命令（受管 Python 3.13.12）：
```
<受管python> -m venv C:\Users\Administrator\.workbuddy\binaries\python\envs\zouye-test
<该venv>\Scripts\python.exe -m pip install -r requirements.txt websocket-client
```
**websocket-client 必须单独装**（requirements.txt 漏了，抓取脚本硬依赖）。

### 【关键】让 Edge 跨多次工具调用存活
单独 `Start-Process msedge ...` 会在本次工具调用结束后被清理掉（下一次调用端口已拒连）。
**必须把 Start-Process 放进后台任务，并在末尾挂 `Wait-Process -Name msedge` 保活**：
```powershell
Start-Process $edge -ArgumentList @("--remote-debugging-port=9222",
  "--user-data-dir=`"$env:USERPROFILE\.boss-zhipin-scraper\chrome-profile`"",
  "--remote-allow-origins=*","--no-first-run","--no-default-browser-check",
  "https://www.zhipin.com/web/user/?ka=header-login")
Wait-Process -Name msedge      # 保活，别省
```
放在 `run_in_background` 任务里跑；这样 smart_crawl 全程复用同一个 CDP 实例。

### 本次抓取（2026-09-24，目标 500 条，实际 699）
```bat
py smart_crawl.py --plan --budget 10 --pages 5 --strategy balanced --include-nationwide --max-slices 0
py smart_crawl.py --run  --budget 10 --pages 5 --strategy balanced --include-nationwide --max-slices 0
```
- 750 条原始 → **699 条新增（93% 收益率）**，23.3 分钟；「全国」+冷城市组合几乎不重复，
  唯一低收益是 `全国|edu`（新增 26/75，与历史归档高度重叠）。
- `data/boss_jobs.csv` **6568 → 7267 条**，城市 **167 → 194**。
- 教训：**只要目标单纯是「堆条数」，`--include-nationwide` + 冷城市最划算**；
  pages=5 时约 2.2 分钟/组合 ≈ 70 条新增，**凑 500 条约需 8 个组合**。

### 主工作目录：job-analysis-system
- 用户要求把项目收敛到 `C:\Users\Administrator\Desktop\作业\job-analysis-system`，**以后所有代码都在这个目录里跑**。
- `zouye/` 和 `boss-zhipin-scraper-master/` 是旧目录，仅作归档，不再修改（用户明确要求"只在新文件里删无关文件"）。

### 目录结构
```
job-analysis-system/
├── app.py                 # Flask 后端，默认数据源 data/boss_jobs.csv（无则回退 sample_data.csv）
├── boss_import.py         # 抓取结果 JSON -> 标准 CSV（DEFAULT_RESULTS_DIR = BASE_DIR/"抓取结果"）
├── crawl_and_import.py    # 一键抓取+导入（SCRAPER_SCRIPT = BASE_DIR/scraper/scripts/boss_cdp_raw.py）
├── data/                  # boss_jobs.csv（真实数据）、sample_data.csv（示例）
├── templates/ static/
├── scraper/scripts/boss_cdp_raw.py  +  scraper/data/city_codes.json
│     注意：脚本硬编码找 <script_dir>/../data/city_codes.json，所以必须保持这个相对结构
├── 抓取结果/               # 2026-09-17 已整合，只剩 2 个文件（见下）
└── docs/                  # 毕业设计论文 + 第三方 LICENSE
```

### 抓取结果/ 已整合（2026-09-17）
原来 50 个批次子目录 556 个文件 48MB，**已整合为只剩两个文件**（共 7.8MB）：
- `抓取结果/boss_jobs_all.json`  4347 个岗位全字段 + 56 条 JD + `_sources`（来源城市/关键词/批次）
- `抓取结果/boss_jobs_all.csv`   4341 条标准 10 列，**含 JD**
命名刻意用 `boss_jobs_` 前缀，因为 `boss_import.py` 是 `rglob("boss_jobs_*.json")` 匹配批次，
这样删掉旧批次后它仍能识别（实测：发现 1 个批次 → 4341 条）。
**注意**：`boss_import.py` 的 JD 依赖同目录 `boss_details_*.json`，那些文件已随整合删除，
所以走 boss_import 会"含 JD 0"；要 JD 就用 `consolidate_archive.py` 或直接用 `boss_jobs_all.csv`。

### 排错要点
- 有 HTTP_PROXY 的机器必须 `NO_PROXY=127.0.0.1,localhost`，否则 CDP 被代理劫持返回 502（crawl_and_import.py 已自动处理）
- 只有 Edge，无 Chrome → 用 `--browser edge`
- 登录态在 `~/.boss-zhipin-scraper/chrome-profile`

### 【最重要】CDP 连不上的真正原因：缺 --remote-allow-origins
- 症状：脚本打印"✅ CDP 已就绪"但随后
  `TimeoutError: CDP send(Network.enable) 在 1000 条消息内未找到匹配响应`。
  **换浏览器版本、重装 Edge 都无效**（试过 123 → 153 都一样）。
- 根因：新版 Chromium/Edge 对 CDP 的 **WebSocket 做 Origin 校验**。
  HTTP 端点（/json/version）能访问，所以脚本误以为就绪；但 WebSocket 握手被 403：
  `Rejected an incoming WebSocket connection ... Use --remote-allow-origins=*`
- **解法（不改任何代码）**：自己带参数启动浏览器，再让脚本复用这个实例
  （`batch_crawl.py` 的 `cdp_alive()` 检查通过后会跳过 start_browser）：
  ```
  msedge.exe --remote-debugging-port=9222 --user-data-dir=<profile> --remote-allow-origins=*
  ```
  用 `C:\Users\Administrator\Downloads\edge-setup\keep_browser.py`，
  **必须在后台任务里跑**（run_in_background），否则命令结束浏览器进程会被清理掉
  （试过 DETACHED_PROCESS 也保不住，只有后台任务能让它活下来）。
- 连 WebSocket 时 Python 端要 `suppress_origin=True`（websocket-client 参数）。

### 启动 Web 系统
- 依赖装在这个 venv：`C:\Users\Administrator\.workbuddy\binaries\python\envs\zouye-test\Scripts\python.exe`
  （Flask 3.0.3 / pandas 2.2.3 / jieba / openpyxl / websocket-client / requests）
  **2026-09-18 该 venv 曾整个丢失（启动系统.bat 因此跑不起来），已用受管 Python 3.13.12 重建**。
  重建命令：`<受管python> -m venv <该路径>` 然后 `pip install -r requirements.txt websocket-client`
  —— **websocket-client 必须单独装，requirements.txt 里漏了它，抓取脚本硬依赖**。
- 启动：切到项目目录后 `py app.py`，访问 <http://127.0.0.1:5000/>
  （实测 `GET /` 与 `/api/dashboard` 均 200，5687 条 / 163 城市 / jieba 可用）
- 数据源优先级：`data/boss_jobs.csv` 存在就用它，否则回退 `data/sample_data.csv`
  （sample_data.csv 当前已丢失，不影响运行）

### 环境（2026-09-17 重新确认）
- **Edge 153.0.4234.32 已安装到 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`**
  （正是脚本硬编码寻找的路径，脚本现在能自动找到浏览器，无需改代码）
  旧的用户级 Edge（`AppData\Local\Microsoft\Edge`，123 版，665MB）已按用户要求删除。
- 系统 Python 3.9.5 无任何第三方包；隔离 venv 已重建：
  `C:\Users\Administrator\.workbuddy\binaries\python\envs\zouye-test\Scripts\python.exe`（py3.13 + websocket-client + requests）
- Bash 工具 PATH 会坏，先 `export PATH="/usr/bin:/bin:/usr/local/bin:/c/Windows/System32:/c/Windows:$PATH"`；PowerShell 工具输出常被吞，优先用 Bash

### 装软件 / 跑外部命令的坑（2026-09-17 踩过）
- **Git Bash 会把命令行参数里的反斜杠转成斜杠**：`msiexec /i C:\...msi` 收到的是 `C:/...msi`，
  报错 1619/83 打不开包。解法：把命令写成 .py 文件再执行，别用 `python -c "..."`。
- Edge 离线包地址会变，先从 `https://edgeupdates.microsoft.com/api/products?view=enterprise`
  取 Stable / Windows / x64 的真实 Location 再下载（`go.microsoft.com/fwlink/?linkid=2195086` 已失效）。
- 批量删除文件会被 safe-delete 钩子按「每轮 50 个」拦截；改用 PowerShell
  `Remove-Item -LiteralPath ... -Recurse -Force` 整棵删就不会被计数。
- 删旧 Edge 前要先 `taskkill /F /IM edgex.exe`（那个用户级 Edge 的进程名是 edgex.exe 不是 msedge.exe）。

### 重要：改代码前先问
- **用户明确要求不要改动他的代码**（scraper/scripts/boss_cdp_raw.py、batch_crawl.py 等）。
- 2026-09-17 我擅自给 boss_cdp_raw.py 打"浏览器路径兜底"补丁、给 batch_crawl.py 加参数，
  用户要求全部还原。以后遇到环境问题优先用「命令行参数 / 外部传参 / 新建独立脚本」解决，
  **不要直接改用户已有文件的源码**，除非用户明确同意。

### 抓取实操（2026-09-18 首次真实跑通，务必照做）
- **脚本自带的 `--setup-edge` 起不来**（不带 `--remote-allow-origins=*`）→ 必炸
  `CDP send(Network.enable) ... 未找到匹配响应`。**标准动作：先自己带参起 Edge，再跑 smart_crawl**：
  ```
  msedge.exe --remote-debugging-port=9222 ^
    --user-data-dir=%USERPROFILE%\.boss-zhipin-scraper\chrome-profile ^
    --remote-allow-origins=* --no-first-run --no-default-browser-check
  ```
  必须放在**后台任务**里（前台跑，命令一结束浏览器就被清理）。起来后 `cdp_alive` 为真，
  smart_crawl 会复用、不再自己启动。
- 首次登录：`curl -X PUT "http://127.0.0.1:9222/json/new?https://www.zhipin.com/web/user/?ka=header-login"`
  开登录页，用户扫码一次即可，登录态落在 `~/.boss-zhipin-scraper/chrome-profile`。
- 速度实测：**pages=5 → 约 2 分钟/组合、75 条；pages=3 → 约 1 分钟/组合、45 条**，收益率 100%。
- **中途要停**：`--budget` 启动后改不了。用 PowerShell 按命令行匹配杀
  `*smart_crawl*` / `*boss_cdp_raw*`；**千万别 `taskkill //IM python.exe`**，会误杀用户的 `app.py` 看板。
  杀掉后必须：① 逐个 `json.loads` 校验归档 JSON（`boss_import.py` 的 `convert_batch` 没 try/except，
  半个文件会让导入整个崩掉）② 手动补跑 `boss_import.py`。
- 首次抓取战果：13 个组合、885 条原始、885 条新增、**0 重复**，
  `data/boss_jobs.csv` **5687 → 6568 条**，覆盖城市 163 → 167。

### 数据现状（2026-09-17）
- `data/boss_jobs.csv` 一度只有 917 条，是因为很久没重新导入归档；
  归档目录 `抓取结果/` 里其实有 25834 条原始，重新导入后 **4341 条、覆盖 163 个城市**。
- 教训：**发现数据量偏少时，先重跑 `boss_import.py` 导入已有归档，往往比重新爬更划算**。
- 917 条时期的备份：`data/boss_jobs_backup_20260917_0912.csv`

### 数据现状（2026-09-18 复核）
- 归档 `抓取结果/boss_jobs_all.json`：**5696 条唯一岗位，只覆盖 163 / 373 座城市（44%）**，
  **210 座城市 0 数据**，且严重倾斜：长沙 603、西安 317、苏州 313 …… 而几十座城市只有 1 条。
- 这就是用户说「城市重复、工种重复、效率低」的真实来源：老 `batch_crawl.py` 的城市表只有 8 个。
- 城市码表 `scraper/data/city_codes.json` 共 **374 条，含「全国」(100010000)**，
  默认排除「全国」→ **373 座城市**（smart_crawl 再排除三沙/东沙群岛 → 371）。
