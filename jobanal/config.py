"""项目路径与运行参数的集中定义。

以前路径和常量散落在 9 个脚本里（每个脚本各写一遍 ``BASE_DIR``、
``DEFAULT_OUTPUT``、批次文件名模式），改一处要同步多处。这里统一。

本模块**只用标准库**，且不导入任何项目内其它模块，避免循环导入。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

BASE_DIR = Path(__file__).resolve().parent.parent
SCRAPER_SCRIPT = BASE_DIR / "scraper" / "scripts" / "boss_cdp_raw.py"
CITY_CODES_FILE = BASE_DIR / "scraper" / "data" / "city_codes.json"

DATA_DIR = BASE_DIR / "data"
DEFAULT_DATA_FILE = DATA_DIR / "boss_jobs.csv"
DEFAULT_DB_FILE = DATA_DIR / "jobs.db"
ARCHIVE_DIR = BASE_DIR / "抓取结果"
REPORT_FILE = DATA_DIR / "crawl_report.md"
LEDGER_FILE = DATA_DIR / "crawl_ledger.json"
PLAN_FILE = DATA_DIR / "crawl_plan_last.json"
PROGRESS_FILE = DATA_DIR / ".crawl_progress.json"
LOG_DIR = DATA_DIR / "logs"

# 抓取器固定输出目录（在用户家目录下，由第三方脚本决定）
JOB_RESULT_DIR = Path.home() / ".boss-zhipin-scraper" / "job-result"

# ---------------------------------------------------------------- 数据源

def resolve_data_file() -> Path:
    """默认数据源，可用环境变量 ZOUYE_DATA_FILE 覆盖。"""
    return Path(os.environ.get("ZOUYE_DATA_FILE") or DEFAULT_DATA_FILE).expanduser()


def resolve_db_file() -> Path:
    """SQLite 库路径，可用环境变量 ZOUYE_DB_FILE 覆盖。"""
    return Path(os.environ.get("ZOUYE_DB_FILE") or DEFAULT_DB_FILE).expanduser()


# ---------------------------------------------------------------- 抓取批次命名

JOB_BATCH_GLOB = "boss_jobs_*.json"
DETAIL_BATCH_GLOB = "boss_details_*.json"
# 派生汇总文件：consolidate_archive.py 把全部批次整合成这些文件。
# 它们本身没有 scraped_at，导入时必须跳过 —— 否则会被按文件 mtime 落库，
# 把时间序列的最后一根柱子顶到「导入当天」，趋势图直接失真。
DERIVED_ARCHIVE_FILES = {"boss_jobs_all.json", "boss_jobs_all.csv"}
# 批次文件里的时间戳，如 boss_jobs_杭州_backend_20260928_202537.json
BATCH_STAMP_PATTERN = r"(\d{8}_\d{4,6})"


def is_derived_archive(path) -> bool:
    return getattr(path, "name", "") in DERIVED_ARCHIVE_FILES

# 输出 CSV 的列顺序（导入链路的事实标准）
# job_key / job_link 放在末尾：前者是抓取器给的稳定标识（md5 前 16 位，跨批次不变），
# 后者用于跳回原岗位。旧代码把这两个字段在导入时就丢掉了，导致详情只能靠行号定位。
OUTPUT_COLUMNS = [
    "job_name",
    "company",
    "city",
    "salary_low",
    "salary_high",
    "education",
    "work_years",
    "category",
    "skills",
    "description",
    "job_key",
    "job_link",
]

# 看板/详情使用的列
JOB_COLUMNS = [
    "job_id",
    "job_name",
    "company",
    "city",
    "salary_low",
    "salary_high",
    "avg_salary",
    "education",
    "work_years",
    "experience",
    "category",
    "skills",
    "description",
    # 稳定标识与原始链接（旧 CSV 没有这两列时由 clean_data 补空）
    "job_key",
    "job_link",
]

# 采集时间维度（SQLite 落库后由这些列支撑趋势分析）
SOURCE_COLUMNS = ["job_id", "scraped_at", "source_batch", "keyword", "city"]
