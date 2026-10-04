"""归档仓库的单遍分析：收益率、重复度、字段差异、薪资缺口。

取代原先三个各扫一遍归档的一次性脚本：

* ``analyze_yield.py``          —— 同一个目录**完整扫了 3 遍**（28、61、78 行各一遍
  ``rglob``），而且第一遍的结果被后面的循环覆盖成了死代码；
* ``analyze_duplicates.py``     —— 又扫 2 遍，重复实现关键词/城市统计；
* ``consolidate_archive.py`` 的统计部分。

本模块一次遍历同时产出这三者需要的全部数字。**只用标准库**（无 pandas）。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from jobanal import parsing
from jobanal.config import JOB_BATCH_GLOB, is_derived_archive

# 判断「同一岗位的多份快照之间有没有实质差异」时比较的字段
DIFF_FIELDS = ["title", "salary", "location", "company_industry", "skills", "tags", "boss_name"]


@dataclass
class ArchiveScan:
    """一次归档遍历的全部结果。"""

    batches: int = 0
    raw_records: int = 0
    records_without_job_id: int = 0
    unreadable: list[str] = field(default_factory=list)
    # job_id -> 该岗位的全部快照（含来源批次名）
    by_job: dict[str, list[tuple[dict, str]]] = field(default_factory=lambda: defaultdict(list))
    # job_id -> 命中它的 (城市, 关键词) 组合
    job_dimensions: dict[str, set[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(set)
    )
    # (城市, 关键词) -> 该组合下出现过的 job_id（含重复）
    combo_jobs: dict[tuple[str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # 业务主键（无 job_id 时的兜底口径）-> 出现次数
    business_keys: Counter = field(default_factory=Counter)
    salary_missing: int = 0

    @property
    def unique_jobs(self) -> int:
        return len(self.by_job)

    @property
    def dedupe_rate(self) -> float:
        if not self.raw_records:
            return 0.0
        return self.unique_jobs / self.raw_records * 100

    @property
    def cities(self) -> set[str]:
        """归档里出现过的城市标签（来自批次 payload 的 city 字段）。"""
        return {city for city, _ in self.combo_jobs}

    @property
    def keywords(self) -> set[str]:
        return {keyword for _, keyword in self.combo_jobs}


def scan_archive(archive: Path, *, include_derived: bool = False) -> ArchiveScan:
    """单遍扫描归档，收集收益率与重复度分析所需的全部信息。"""
    scan = ArchiveScan()
    if archive.is_file():
        paths = [archive]
    else:
        paths = sorted(archive.rglob(JOB_BATCH_GLOB))
    if not include_derived:
        paths = [p for p in paths if not is_derived_archive(p)]

    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            scan.unreadable.append(f"{path}: {exc}")
            continue
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            scan.unreadable.append(f"{path}: jobs 字段不是列表")
            continue
        city_tag = str(payload.get("city") or "?") if isinstance(payload, dict) else "?"
        keyword_tag = str(payload.get("keyword") or "?") if isinstance(payload, dict) else "?"
        scan.batches += 1

        for job in jobs:
            if not isinstance(job, dict):
                continue
            scan.raw_records += 1
            jid = str(job.get("job_id") or "").strip()
            key = (
                str(job.get("title") or "").strip(),
                str(job.get("boss_name") or "").strip(),
                str(job.get("location") or "").split("·")[0].strip(),
                str(job.get("salary") or "").strip(),
            )
            scan.business_keys[key] += 1
            salary = str(job.get("salary") or "").strip()
            if not salary or salary in parsing.NO_SALARY_WORDS or "面议" in salary:
                scan.salary_missing += 1
            if not jid:
                scan.records_without_job_id += 1
                continue
            scan.by_job[jid].append((job, path.name))
            scan.job_dimensions[jid].add((city_tag, keyword_tag))
            scan.combo_jobs[(city_tag, keyword_tag)].append(jid)
    return scan


# ---------------------------------------------------------------- 维度收益率


@dataclass
class YieldRow:
    name: str
    records: int
    unique_jobs: int

    @property
    def rate(self) -> float:
        return 100.0 * self.unique_jobs / self.records if self.records else 0.0


def yield_by_city(scan: ArchiveScan) -> list[YieldRow]:
    """按城市：抓了多少条、拿到多少不重复岗位、收益率。"""
    records: Counter = Counter()
    unique: dict[str, set[str]] = defaultdict(set)
    for jid, dims in scan.job_dimensions.items():
        for city, _ in dims:
            unique[city].add(jid)
    for (city, _), job_ids in scan.combo_jobs.items():
        records[city] += len(job_ids)
    rows = [
        YieldRow(name=city, records=records[city], unique_jobs=len(ids))
        for city, ids in unique.items()
    ]
    return sorted(rows, key=lambda row: (-row.unique_jobs, row.name))


def yield_by_keyword(scan: ArchiveScan) -> list[YieldRow]:
    """按关键词：换关键词到底有没有用。"""
    records: Counter = Counter()
    unique: dict[str, set[str]] = defaultdict(set)
    for jid, dims in scan.job_dimensions.items():
        for _, keyword in dims:
            unique[keyword].add(jid)
    for (_, keyword), job_ids in scan.combo_jobs.items():
        records[keyword] += len(job_ids)
    rows = [
        YieldRow(name=keyword, records=records[keyword], unique_jobs=len(ids))
        for keyword, ids in unique.items()
    ]
    return sorted(rows, key=lambda row: (-row.unique_jobs, row.name))


def cross_tagging(scan: ArchiveScan) -> dict:
    """一个岗位被几个关键词/城市维度命中 —— 越高说明换词/换城越白抓。"""
    per_job = [len(dims) for dims in scan.job_dimensions.values()]
    if not per_job:
        return {"avg_dimensions": 0.0, "multi_keyword_jobs": 0, "total_jobs": 0, "multi_ratio": 0.0}
    multi = sum(1 for n in per_job if n > 1)
    return {
        "avg_dimensions": sum(per_job) / len(per_job),
        "multi_keyword_jobs": multi,
        "total_jobs": len(per_job),
        "multi_ratio": 100.0 * multi / len(per_job),
    }


# ---------------------------------------------------------------- 重复度


def duplicate_distribution(scan: ArchiveScan, top: int = 12) -> list[tuple[int, int]]:
    """同一 job_id 出现 1 次、2 次、3 次……的岗位数分布。"""
    counter = Counter(len(group) for group in scan.by_job.values())
    return sorted(counter.items())[:top]


def field_differences(scan: ArchiveScan) -> tuple[Counter, list[tuple[str, dict]]]:
    """重复快照之间哪些字段出现过差异 —— 决定「合并」值不值得。

    返回 (差异计数, 样本列表)。样本取前 3 组重复快照，列出每张表的多个取值。
    """
    diffs: Counter = Counter()
    samples: list[tuple[str, dict]] = []
    multi = [(jid, group) for jid, group in scan.by_job.items() if len(group) > 1]
    for jid, group in multi:
        first = group[0][0]
        for job, _ in group[1:]:
            for name in DIFF_FIELDS:
                if str(first.get(name) or "").strip() != str(job.get(name) or "").strip():
                    diffs[name] += 1
        if len(samples) < 3:
            varying: dict[str, list[str]] = {}
            for name in DIFF_FIELDS:
                values: list[str] = []
                for job, _ in group[:4]:
                    value = str(job.get(name) or "").strip()
                    if value not in values:
                        values.append(value)
                if len(values) > 1:
                    varying[name] = values[:3]
            if varying:
                samples.append((jid, varying))
    return diffs, samples


# ---------------------------------------------------------------- 报告


def build_report(scan: ArchiveScan, *, top: int = 15) -> str:
    """生成 Markdown 分析报告（收益率 + 重复度 + 数据缺口）。"""
    lines: list[str] = []
    lines.append("# 归档数据质量与采集收益分析")
    lines.append("")
    lines.append("> 由 `tools/archive_report.py` 单遍扫描生成；不修改任何数据。")
    lines.append("")
    lines.append("## 一、总览")
    lines.append("")
    lines.append(f"- 批次文件：{scan.batches} 个")
    lines.append(f"- 原始记录：{scan.raw_records} 条")
    lines.append(f"- 唯一岗位（job_id）：{scan.unique_jobs} 个")
    lines.append(f"- 整体收益率：{scan.dedupe_rate:.1f}%（唯一岗位 / 原始记录）")
    lines.append(f"- 没有 job_id 的记录：{scan.records_without_job_id} 条")
    lines.append(f"- 唯一业务主键数：{len(scan.business_keys)} 个")
    lines.append(f"- 无薪资/面议（导入时会被过滤）：{scan.salary_missing} 条")
    if scan.unreadable:
        lines.append(f"- ⚠ 无法读取的批次：{len(scan.unreadable)} 个")
        for item in scan.unreadable[:5]:
            lines.append(f"  - `{item}`")
    lines.append("")

    cross = cross_tagging(scan)
    lines.append("## 二、换关键词/换城市到底有没有用")
    lines.append("")
    lines.append(f"- 平均每个岗位被 {cross['avg_dimensions']:.2f} 个「城市×关键词」维度命中")
    lines.append(f"- 被 2 个以上维度同时命中的岗位：{cross['multi_keyword_jobs']} / "
                 f"{cross['total_jobs']}（{cross['multi_ratio']:.0f}%）")
    lines.append("")
    lines.append("比例越高，说明在同一批城市里换关键词基本是在重复命中同一批岗位。")
    lines.append("")

    lines.append("## 三、按城市：值不值得继续抓")
    lines.append("")
    lines.append("| 城市 | 记录数 | 独立岗位 | 收益率 |")
    lines.append("| --- | --- | --- | --- |")
    for row in yield_by_city(scan)[:top]:
        lines.append(f"| {row.name} | {row.records} | {row.unique_jobs} | {row.rate:.1f}% |")
    lines.append("")

    lines.append("## 四、按关键词：换词收益")
    lines.append("")
    lines.append("| 关键词 | 记录数 | 独立岗位 | 收益率 |")
    lines.append("| --- | --- | --- | --- |")
    for row in yield_by_keyword(scan)[:top]:
        lines.append(f"| {row.name} | {row.records} | {row.unique_jobs} | {row.rate:.1f}% |")
    lines.append("")

    lines.append("## 五、重复分布与字段差异")
    lines.append("")
    lines.append("同一 job_id 被反复抓到的次数分布：")
    lines.append("")
    lines.append("| 出现次数 | 岗位数 |")
    lines.append("| --- | --- |")
    for times, count in duplicate_distribution(scan):
        lines.append(f"| {times} | {count} |")
    lines.append("")

    diffs, samples = field_differences(scan)
    lines.append("重复快照之间出现字段差异的次数（差异越多，「合并取最全」越有价值）：")
    lines.append("")
    if diffs:
        lines.append("| 字段 | 差异次数 |")
        lines.append("| --- | --- |")
        for name, count in diffs.most_common():
            lines.append(f"| `{name}` | {count} |")
    else:
        lines.append("（重复快照之间没有任何字段差异）")
    lines.append("")
    for jid, varying in samples:
        lines.append(f"<details><summary>样本 {jid}</summary>")
        lines.append("")
        for name, values in varying.items():
            lines.append(f"- `{name}`：{values}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return "\n".join(lines)
