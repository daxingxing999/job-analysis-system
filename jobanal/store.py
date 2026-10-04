"""SQLite 数据层：落库、查询、趋势聚合。

为什么不再只靠 CSV
------------------
原先三个导入脚本都是「读全部归档 -> 清洗 -> 全量重写 data/boss_jobs.csv」：

1. ``app.py`` 用 ``df["job_id"] = range(1, len(df) + 1)`` 生成岗位编号，
   每次重新导入同一岗位的编号都会整体漂移，``/api/jobs/<id>`` 的链接随即失效；
2. 归档目录名与抓取结果里都带 ``scraped_at``（实测 2026-09-24 ~ 09-28），
   但 CSV 里没有任何采集时间字段，于是「某技能需求随时间的走势」这类
   整个项目最有说服力的分析根本做不出来；
3. 每次都是全量重写，无法增量更新。

落库后：主键用抓取器给的 job_id（md5 前 16 位，跨批次稳定），
另存 first_seen / last_seen，并用 postings 表记录每次快照。

本模块**只用标准库**（sqlite3、json、csv），不依赖 pandas。
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jobanal import parsing
from jobanal.config import JOB_BATCH_GLOB, is_derived_archive

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 岗位主表：一个 job_id 一行，字段是该岗位「信息最全的一份快照」
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    company_anonymous INTEGER NOT NULL DEFAULT 0,
    city         TEXT NOT NULL,
    salary_low   REAL,
    salary_high  REAL,
    avg_salary   REAL,
    salary_raw   TEXT,
    salary_unit  TEXT,
    education    TEXT,
    work_years   TEXT,
    experience   TEXT,
    category     TEXT,
    skills       TEXT,
    description  TEXT,
    job_link     TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    snapshot_count INTEGER NOT NULL DEFAULT 0
);

-- 快照表：同一岗位被多次抓到就多行，用来做时间序列与挂岗时长
-- 不存原始 JSON：原始数据本就完整保存在 抓取结果/ 里，重复存一份会让库体积
-- 膨胀数倍（实测 8233 岗位时 3 MB -> 20 MB）。需要溯源时去归档目录看。
CREATE TABLE IF NOT EXISTS postings (
    job_id      TEXT NOT NULL,
    scraped_at  TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    keyword     TEXT,
    city        TEXT,
    skills      TEXT,
    salary_raw  TEXT,
    PRIMARY KEY (job_id, scraped_at, source_batch)
);

CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category);
CREATE INDEX IF NOT EXISTS idx_postings_scraped ON postings(scraped_at);
CREATE INDEX IF NOT EXISTS idx_postings_job ON postings(job_id);
"""

JOB_FIELDS = [
    "job_id", "title", "company", "company_anonymous", "city",
    "salary_low", "salary_high", "avg_salary", "salary_raw", "salary_unit",
    "education", "work_years", "experience", "category", "skills",
    "description", "job_link", "first_seen", "last_seen", "snapshot_count",
]


@dataclass
class ImportStats:
    """一次导入的结果汇总。"""

    batches: int = 0
    raw_jobs: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    quarantine: int = 0
    unparsable_files: list[str] = field(default_factory=list)
    skipped_undated: list[str] = field(default_factory=list)
    quarantine_reasons: Counter = field(default_factory=Counter)
    earliest: str = ""
    latest: str = ""

    def summary(self) -> str:
        span = f"{self.earliest[:10]} ~ {self.latest[:10]}" if self.earliest else "无"
        extra = ""
        if self.skipped_undated:
            extra += f"；跳过无时间戳 {len(self.skipped_undated)} 个"
        if self.unparsable_files:
            extra += f"；无法解析 {len(self.unparsable_files)} 个"
        return (
            f"批次 {self.batches} 个，原始记录 {self.raw_jobs} 条 -> "
            f"新增 {self.new_jobs} 个岗位，更新 {self.updated_jobs} 个；"
            f"薪资异常隔离 {self.quarantine} 条；时间跨度 {span}{extra}"
        )


# ---------------------------------------------------------------- 连接与建表


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("initialized_at", datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


# 简单迁移表：列名 -> 建列语句。老库缺列时自动补，避免用户重建数据库。
_MIGRATIONS = {
    "postings": {"skills": "ALTER TABLE postings ADD COLUMN skills TEXT"},
}
# 需要移除的冗余列（曾用于存原始 JSON，体积代价过大且归档里已有原文）
_DROP_COLUMNS = {
    "postings": ["raw_json"],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue
        for column, statement in columns.items():
            if column not in existing:
                conn.execute(statement)
    for table, columns in _DROP_COLUMNS.items():
        info = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not info:
            continue
        droppable = [c for c in columns if c in info]
        if droppable:
            # SQLite 3.35+ 支持 DROP COLUMN；老版本则退化为 VACUUM 后保留
            for column in droppable:
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                except sqlite3.OperationalError:
                    pass


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


# ---------------------------------------------------------------- 归档读取


def batch_files(archive: Path, *, include_derived: bool = False) -> list[Path]:
    if archive.is_file():
        if is_derived_archive(archive) and not include_derived:
            return []
        return [archive]
    return [
        path for path in sorted(archive.rglob(JOB_BATCH_GLOB))
        if include_derived or not is_derived_archive(path)
    ]


def parse_batch(path: Path, *, include_derived: bool = False) -> tuple[list[dict], str, str, str]:
    """读取一个批次文件，返回 (jobs, scraped_at, source_batch, keyword)。

    时间戳策略（绝不猜）：
    1. 优先 payload 里的 ``scraped_at``；
    2. 派生汇总文件额外接受 ``generated_at``；
    3. 都没有就取**批次目录名**里的日期（smart_2026-09-24_1641 -> 2026-09-24T00:00:00）；
    4. 仍然没有则返回空字符串，由调用方决定是否跳过 —— 以前会回退到文件 mtime，
       结果克隆仓库时 mtime 变成「今天」，趋势图最后一根柱子被顶到导入当天。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
        scraped_at = str(payload.get("scraped_at") or "").strip()
        if not scraped_at and include_derived:
            scraped_at = str(payload.get("generated_at") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
    else:
        jobs, scraped_at, keyword = payload, "", ""
    if not isinstance(jobs, list):
        jobs = []
    if not scraped_at:
        scraped_at = _timestamp_from_dir(path.parent.name)
    if scraped_at:
        scraped_at = scraped_at.replace(" ", "T", 1) if " " in scraped_at[:11] else scraped_at
    return jobs, scraped_at, path.name, keyword


_DIR_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _timestamp_from_dir(name: str) -> str:
    """从 smart_2026-09-24_1641 这类目录名取日期（只到天，够做按天聚合）。"""
    match = _DIR_DATE.search(name)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}T00:00:00"


def _row_from_job(job: dict, detail: dict | None = None) -> tuple[dict | None, str]:
    """把一条抓取记录转成 jobs 表的一行。返回 (row, 剔除原因)。"""
    salary = parsing.parse_salary_result(job.get("salary"))
    if not salary.ok:
        return None, salary.reason
    tags = job.get("tags") or job.get("job_labels") or ""
    company = parsing.display_company(job, detail)
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return None, "missing_job_id"
    return (
        {
            "job_id": job_id,
            "title": str(job.get("title") or "").strip(),
            "company": company,
            "company_anonymous": 1 if parsing.is_placeholder_company(company) else 0,
            "city": parsing.parse_city(job.get("location")),
            "salary_low": salary.low,
            "salary_high": salary.high,
            "avg_salary": salary.avg,
            "salary_raw": salary.raw,
            "salary_unit": salary.unit,
            "education": parsing.parse_education(tags),
            "work_years": parsing.parse_experience(tags, job.get("title")),
            "experience": parsing.parse_experience(tags, job.get("title")),
            "category": str(job.get("company_industry") or "未知").strip(),
            "skills": parsing.parse_skills(job.get("skills")),
            "description": parsing.clean_description((detail or {}).get("jd")),
            "job_link": str(job.get("job_link") or "").strip(),
        },
        "",
    )


# ---------------------------------------------------------------- 导入


def import_archive(db_path: Path, archive: Path, *,
                   quarantine_path: Path | None = None,
                   include_derived: bool = False,
                   skip_undated: bool = False) -> ImportStats:
    """把归档目录下的批次导入 SQLite（增量 upsert）。

    ``include_derived``：是否把 consolidate_archive.py 生成的汇总文件
    （boss_jobs_all.json）也算进来。默认关闭，因为它的内容是各批次之和，
    与批次文件有重叠；但仓库历史上有过「批次被清理、只剩汇总文件」的情况，
    那时只能打开它才能保留那部分岗位。
    ``skip_undated``：时间戳缺失时是否跳过（默认不跳，按目录名兜底）。
    """
    stats = ImportStats()
    files = batch_files(archive, include_derived=include_derived)
    if not files:
        raise FileNotFoundError(f"在 {archive} 下没有找到 {JOB_BATCH_GLOB}")

    conn = connect(db_path)
    try:
        init_db(conn)
        quarantine_rows: list[dict] = []
        for path in files:
            try:
                jobs, scraped_at, batch_name, keyword = parse_batch(
                    path, include_derived=include_derived
                )
            except (OSError, json.JSONDecodeError) as exc:
                stats.unparsable_files.append(f"{path}: {exc}")
                continue
            if not scraped_at and skip_undated:
                stats.skipped_undated.append(path.name)
                continue
            stats.batches += 1
            stats.raw_jobs += len(jobs)
            if scraped_at:
                stats.earliest = min(stats.earliest or scraped_at, scraped_at)
                stats.latest = max(stats.latest or scraped_at, scraped_at)

            for job in jobs:
                if not isinstance(job, dict):
                    continue
                row, reason = _row_from_job(job)
                if row is None:
                    stats.quarantine += 1
                    stats.quarantine_reasons[reason] += 1
                    quarantine_rows.append({
                        "job_id": str(job.get("job_id") or ""),
                        "title": str(job.get("title") or ""),
                        "company": str(job.get("boss_name") or ""),
                        "city": parsing.parse_city(job.get("location")),
                        "salary_raw": str(job.get("salary") or ""),
                        "reason": reason,
                        "source_batch": batch_name,
                        "scraped_at": scraped_at,
                    })
                    continue
                existed = conn.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone() is not None
                # 先写快照再写主表：_upsert_job 会按 postings 的行数推导 snapshot_count
                new_snapshot = _insert_posting(
                    conn, row, job, scraped_at, batch_name, keyword
                )
                _upsert_job(conn, row, scraped_at)
                if not new_snapshot:
                    continue
                if existed:
                    stats.updated_jobs += 1
                else:
                    stats.new_jobs += 1
        conn.commit()
        if quarantine_path is not None and quarantine_rows:
            _write_quarantine(quarantine_rows, quarantine_path)
    finally:
        conn.close()
    return stats


def _upsert_job(conn: sqlite3.Connection, row: dict, scraped_at: str) -> None:
    """插入或更新岗位主表：新值更有信息时覆盖旧值，并维护 first/last_seen。

    ``snapshot_count`` 始终由 postings 表推导，而不是每调用一次 +1 ——
    这样重复导入同一份归档不会把「观察次数」刷高。
    """
    existing = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    snapshots = conn.execute(
        "SELECT COUNT(*) AS n FROM postings WHERE job_id = ?", (row["job_id"],)
    ).fetchone()["n"]

    if existing is None:
        payload = dict(row)
        payload["first_seen"] = scraped_at
        payload["last_seen"] = scraped_at
        payload["snapshot_count"] = max(1, int(snapshots))
        columns = ", ".join(JOB_FIELDS)
        placeholders = ", ".join(f":{name}" for name in JOB_FIELDS)
        conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", payload)
        return

    merged = dict(existing)
    for key, value in row.items():
        # 只在新值「更有信息」时覆盖：空值、以及「未知」这类占位值都不冲掉已有内容
        if _is_missing(value) and not _is_missing(merged.get(key)):
            continue
        if key == "company_anonymous":
            merged[key] = 1 if (value or merged.get(key)) else 0
            continue
        if key == "company":
            # 匿名名号（某…公司）不应覆盖已解析出的真实公司名
            if value and parsing.is_placeholder_company(value) \
                    and not parsing.is_placeholder_company(merged.get("company")):
                continue
        merged[key] = value
    for key in ("first_seen", "last_seen"):
        values = [v for v in (existing[key], scraped_at) if v]
        if values:
            merged[key] = min(values) if key == "first_seen" else max(values)
    merged["snapshot_count"] = max(int(existing["snapshot_count"] or 0), int(snapshots))
    assignments = ", ".join(f"{name} = :{name}" for name in JOB_FIELDS if name != "job_id")
    conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = :job_id", merged)


# 视为「没有信息」的值：写库时用来判断该不该覆盖已有内容
_MISSING_VALUES = {None, "", "未知", "nan", "none"}


def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in _MISSING_VALUES:
        return True
    return False


def _insert_posting(conn: sqlite3.Connection, row: dict, job: dict,
                    scraped_at: str, batch_name: str, keyword: str) -> bool:
    """写入一条快照。返回 True 表示这是新快照（此前不存在）。"""
    existed = conn.execute(
        "SELECT 1 FROM postings WHERE job_id = ? AND scraped_at = ? AND source_batch = ?",
        (row["job_id"], scraped_at, batch_name),
    ).fetchone() is not None
    conn.execute(
        "INSERT OR REPLACE INTO postings"
        "(job_id, scraped_at, source_batch, keyword, city, skills, salary_raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["job_id"], scraped_at, batch_name, keyword, row["city"],
            row["skills"], row["salary_raw"],
        ),
    )
    return not existed


def _write_quarantine(rows: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = ["job_id", "title", "company", "city", "salary_raw",
               "reason", "source_batch", "scraped_at"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def import_csv(db_path: Path, csv_path: Path) -> ImportStats:
    """把标准 CSV（无采集时间）导入 SQLite，用于没有归档的场景。"""
    stats = ImportStats()
    conn = connect(db_path)
    try:
        init_db(conn)
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, raw in enumerate(reader, 1):
                low, high, reason = parsing.parse_monthly_pair(
                    raw.get("salary_low"), raw.get("salary_high")
                )
                if reason:
                    stats.quarantine += 1
                    stats.quarantine_reasons[reason] += 1
                    continue
                stats.raw_jobs += 1
                job_id = str(raw.get("job_id") or "").strip() or f"csv-{index:08d}"
                row = {
                    "job_id": job_id,
                    "title": str(raw.get("job_name") or "").strip(),
                    "company": str(raw.get("company") or "未知").strip(),
                    "company_anonymous": 1 if parsing.is_placeholder_company(raw.get("company")) else 0,
                    "city": str(raw.get("city") or "未知").strip(),
                    "salary_low": low,
                    "salary_high": high,
                    "avg_salary": (low + high) / 2,
                    "salary_raw": f"{low:.0f}-{high:.0f}",
                    "salary_unit": "monthly",
                    "education": str(raw.get("education") or "未知").strip(),
                    "work_years": str(raw.get("work_years") or "未知").strip(),
                    "experience": parsing.experience_label_from_years(raw.get("work_years")),
                    "category": str(raw.get("category") or "未知").strip(),
                    "skills": str(raw.get("skills") or "").strip(),
                    "description": str(raw.get("description") or "").strip(),
                    "job_link": "",
                }
                existed = conn.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone() is not None
                if existed:
                    stats.updated_jobs += 1
                else:
                    stats.new_jobs += 1
                _upsert_job(conn, row, "")
        conn.commit()
        stats.batches = 1
    finally:
        conn.close()
    return stats


# ---------------------------------------------------------------- 查询与聚合


def fetch_jobs(conn: sqlite3.Connection, *, limit: int | None = None,
               order_by: str = "avg_salary DESC") -> list[dict]:
    """读取岗位主表。``order_by`` 只允许白名单字段，避免 SQL 拼接注入。"""
    allowed = {
        "avg_salary DESC", "avg_salary ASC", "salary_low DESC",
        "salary_high DESC", "snapshot_count DESC", "city ASC", "title ASC",
    }
    if order_by not in allowed:
        order_by = "avg_salary DESC"
    sql = f"SELECT * FROM jobs ORDER BY {order_by}"
    if limit:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def trend_by_skill(conn: sqlite3.Connection, *, limit: int = 5) -> dict:
    """技能需求随时间的变化。

    用 postings 表的 scraped_at 按天聚合：某天的批次里出现了多少条
    带该技能的岗位。这是 CSV 时代做不出来的分析。
    """
    rows = conn.execute(
        "SELECT scraped_at, skills, job_id FROM postings ORDER BY scraped_at"
    ).fetchall()
    if not rows:
        return {"dates": [], "series": []}

    dates: list[str] = []
    per_day_skill: dict[str, Counter] = {}
    per_day_total: Counter = Counter()
    seen: dict[str, set] = {}
    for row in rows:
        day = str(row["scraped_at"])[:10]
        if day not in per_day_skill:
            per_day_skill[day] = Counter()
            seen[day] = set()
            dates.append(day)
        job_id = row["job_id"]
        if job_id in seen[day]:
            continue
        seen[day].add(job_id)
        per_day_total[day] += 1
        for skill in parsing.parse_skills(row["skills"]).split(","):
            skill = skill.strip()
            if skill:
                per_day_skill[day][skill] += 1

    overall: Counter = Counter()
    for day in dates:
        overall.update(per_day_skill[day])
    top_skills = [name for name, _ in overall.most_common(limit)]

    series = []
    for skill in top_skills:
        series.append({
            "name": skill,
            "data": [int(per_day_skill[day].get(skill, 0)) for day in dates],
        })
    return {
        "dates": dates,
        "totals": [int(per_day_total[day]) for day in dates],
        "series": series,
    }


def trend_by_city(conn: sqlite3.Connection, *, limit: int = 5) -> dict:
    rows = conn.execute(
        "SELECT scraped_at, city, job_id FROM postings ORDER BY scraped_at"
    ).fetchall()
    if not rows:
        return {"dates": [], "series": []}
    dates: list[str] = []
    per_day: dict[str, Counter] = {}
    seen: dict[str, set] = {}
    for row in rows:
        day = str(row["scraped_at"])[:10]
        if day not in per_day:
            per_day[day] = Counter()
            seen[day] = set()
            dates.append(day)
        job_id = row["job_id"]
        if job_id in seen[day]:
            continue
        seen[day].add(job_id)
        per_day[day][row["city"] or "未知"] += 1
    overall: Counter = Counter()
    for day in dates:
        overall.update(per_day[day])
    cities = [name for name, _ in overall.most_common(limit)]
    return {
        "dates": dates,
        "series": [
            {"name": city, "data": [int(per_day[day].get(city, 0)) for day in dates]}
            for city in cities
        ],
    }


def coverage_stats(conn: sqlite3.Connection) -> dict:
    """数据质量概览：用于 verify 子命令与报告。"""
    total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    postings = conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]
    with_desc = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE description IS NOT NULL AND description <> ''"
    ).fetchone()["n"]
    anon = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE company_anonymous = 1"
    ).fetchone()["n"]
    multi = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE snapshot_count > 1"
    ).fetchone()["n"]
    span = conn.execute(
        "SELECT MIN(scraped_at) AS a, MAX(scraped_at) AS b FROM postings"
    ).fetchone()
    salary = conn.execute(
        "SELECT MIN(salary_low) AS lo, MAX(salary_high) AS hi,"
        " AVG(avg_salary) AS avg FROM jobs"
    ).fetchone()
    return {
        "jobs": int(total or 0),
        "postings": int(postings or 0),
        "jobs_with_description": int(with_desc or 0),
        "anonymous_company": int(anon or 0),
        "jobs_seen_twice_or_more": int(multi or 0),
        "earliest": span["a"] or "",
        "latest": span["b"] or "",
        "salary_min": salary["lo"],
        "salary_max": salary["hi"],
        "salary_avg": round(salary["avg"], 2) if salary["avg"] is not None else None,
    }
