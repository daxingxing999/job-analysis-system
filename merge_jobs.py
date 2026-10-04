"""把所有归档批次按 job_id 智能合并，输出信息最完整的一份 CSV。

与 boss_import.py 的区别：
  boss_import: 逐条转换，遇到重复 job_id 直接丢弃后者（保留先到的）
  merge_jobs : 按 job_id 分组，在同一岗位的多份快照里挑"信息最完整"的那份，
               字段缺失时互相补全（例如某次抓到 skills、另一次抓到 tags）

解析规则直接复用 boss_import 的函数，保证与系统其它部分一致（不修改其源码）。

用法
    py merge_jobs.py                       # 合并 -> data/boss_jobs.csv
    py merge_jobs.py --output data/x.csv   # 指定输出
    py merge_jobs.py --dry-run             # 只看统计，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import boss_import as bi  # 复用解析规则：parse_salary / parse_city / parse_*
from jobanal import config as app_config

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "抓取结果"
DEFAULT_OUTPUT = BASE / "data" / "boss_jobs.csv"


class ArchiveLoadError(Exception):
    """Raised when one or more archive files cannot be safely included."""


# 参与"完整性打分"的字段，越靠前越重要
COMPLETENESS_FIELDS = ["salary", "location", "company_industry", "skills", "tags", "title"]


def completeness(job: dict) -> int:
    """给一条原始记录打分：字段越全分越高，用于在同一 job_id 的多份快照里挑最好的。"""
    score = 0
    for i, field in enumerate(COMPLETENESS_FIELDS):
        value = str(job.get(field) or "").strip()
        if value and value not in ("-", "面议"):
            score += (len(COMPLETENESS_FIELDS) - i) * 10
    # 技能/标签越多，信息越丰富
    score += min(len(str(job.get("skills") or "")), 60)
    score += min(len(str(job.get("tags") or "")), 40)
    return score


def pick_best(group: list[dict]) -> dict:
    """在同一岗位的多份快照中选出信息最完整的一份，并用其它快照补全缺失字段。"""
    best = max(group, key=completeness)
    merged = dict(best)
    for other in group:
        for field in COMPLETENESS_FIELDS:
            cur = str(merged.get(field) or "").strip()
            alt = str(other.get(field) or "").strip()
            if not cur and alt and alt not in ("-", "面议"):
                merged[field] = alt
    return merged


def load_all() -> tuple[dict[str, list[dict]], Counter, Counter]:
    """读取所有批次，按 job_id 归组；同时统计关键词/城市。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    keywords: Counter = Counter()
    cities: Counter = Counter()
    # 跳过 consolidate_archive.py 生成的派生汇总文件：它的内容就是各批次之和，
    # 纳入统计会把同一岗位重复计入「被重复抓到几次」。
    paths = [
        path for path in sorted(ARCHIVE.rglob(app_config.JOB_BATCH_GLOB))
        if not app_config.is_derived_archive(path)
    ]
    if not paths:
        raise ArchiveLoadError(f"在归档目录中没有找到 boss_jobs_*.json：{ARCHIVE}")

    failures = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"{path}: {exc}")
            continue

        if isinstance(payload, dict):
            jobs = payload.get("jobs")
            kw = payload.get("keyword", "?")
            ct = payload.get("city", "?")
        elif isinstance(payload, list):
            jobs = payload
            kw = "?"
            ct = "?"
        else:
            failures.append(f"{path}: 顶层 JSON 必须是对象或列表")
            continue

        if not isinstance(jobs, list):
            failures.append(f"{path}: jobs 字段必须是列表")
            continue
        if any(not isinstance(job, dict) for job in jobs):
            failures.append(f"{path}: jobs 中包含非对象记录")
            continue

        kw = str(kw or "?")
        ct = str(ct or "?")
        keywords[kw] += 1
        cities[ct] += 1
        for job in jobs:
            jid = str(job.get("job_id") or "").strip()
            if not jid:
                continue
            groups[jid].append(job)
    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise ArchiveLoadError(
            f"有 {len(failures)} 个归档文件无法读取，已停止合并，未写入 CSV：\n{details}"
        )
    return groups, keywords, cities


def convert(job: dict, fallback_city: str = "") -> dict | None:
    """复用 boss_import 的解析规则，把一条原始记录转成标准字段。"""
    low, high = bi.parse_salary(job.get("salary"))
    if low is None or high is None:
        return None
    tags = job.get("tags") or job.get("job_labels") or ""
    return {
        "job_name": str(job.get("title") or "").strip(),
        "company": str(job.get("boss_name") or "").strip(),
        "city": bi.parse_city(job.get("location"), fallback_city),
        "salary_low": low,
        "salary_high": high,
        "education": bi.parse_education(tags),
        "work_years": bi.parse_experience(tags, job.get("title")),
        "category": str(job.get("company_industry") or "未知").strip(),
        "skills": bi.parse_skills(job.get("skills")),
        "description": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="按 job_id 合并归档批次")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        groups, keywords, cities = load_all()
    except ArchiveLoadError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    raw_total = sum(len(v) for v in groups.values())
    print("批次涉及的关键词数 %d，城市数 %d" % (len(keywords), len(cities)))
    print("原始记录（有 job_id）: %d" % raw_total)
    print("唯一岗位数（job_id）: %d" % len(groups))
    print("平均每个岗位被重复抓到: %.1f 次" % (raw_total / max(len(groups), 1)))
    print()

    records = []
    skipped_no_salary = 0
    for jid, group in groups.items():
        best = pick_best(group)
        rec = convert(best)
        if rec is None:
            skipped_no_salary += 1
            continue
        records.append(rec)

    print("合并后有效记录: %d" % len(records))
    print("因无薪资/面议被跳过: %d 个岗位" % skipped_no_salary)
    print()

    # 再按业务主键兜底去重（同公司同城同薪资同名，视为同一岗位）
    seen: set = set()
    final = []
    for r in records:
        key = (r["job_name"], r["company"], r["city"], r["salary_low"], r["salary_high"])
        if key in seen:
            continue
        seen.add(key)
        final.append(r)
    print("业务主键兜底去重后: %d 条（又去掉 %d 条）" % (len(final), len(records) - len(final)))
    print()

    city_counter = Counter(r["city"] for r in final)
    cat_counter = Counter(r["category"] for r in final)
    print("城市数: %d" % len(city_counter))
    print("城市 Top12:", dict(city_counter.most_common(12)))
    print("行业数: %d  Top8: %s" % (len(cat_counter), dict(cat_counter.most_common(8))))
    avg_low = sum(r["salary_low"] for r in final) / max(len(final), 1)
    avg_high = sum(r["salary_high"] for r in final) / max(len(final), 1)
    print("平均月薪: %.0f - %.0f 元" % (avg_low, avg_high))
    print("学历 Top6:", dict(Counter(r["education"] for r in final).most_common(6)))
    print("经验分布:", dict(Counter(r["work_years"] for r in final)))
    print("有技能标签的记录: %d (%.0f%%)" % (
        sum(1 for r in final if r["skills"]),
        100.0 * sum(1 for r in final if r["skills"]) / max(len(final), 1)))

    if args.dry_run:
        print("\n[预览模式] 未写入文件")
        return 0

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    bi.write_csv(final, out)
    print("\n已写入 %s（%d 条）" % (out, len(final)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
