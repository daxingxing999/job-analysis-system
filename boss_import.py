"""BOSS 直聘抓取结果 -> 岗位分析系统标准 CSV 的导入模块。

本模块只读取本地已抓取的结果文件（boss_jobs_*.json / boss_details_*.json），
不发起任何网络请求、不接触登录态、不绕过任何反爬限制，职责等价于论文 3.3 节
所述"数据采集模块"之后的解析与清洗环节：

    BOSS 抓取 JSON --解析--> 标准字段 CSV --(app.py 清洗)--> 分析与可视化

转换规则
    job_name    <- title
    company     <- details.company 或 boss_name
    city        <- location 的第一段（"杭州·滨江区·长河" -> "杭州"）
    salary_*    <- salary 明文（"15-30K"、"30-60K·15薪"、"300-500元/天"）
    education   <- tags 中的学历标签
    work_years  <- tags 中的经验标签，归一到系统的经验分箱
    category    <- company_industry
    skills      <- skills（"Java | Spring" -> "Java,Spring"）
    description <- details.jd（按 job_id 关联）

用法
    py boss_import.py                          # 合并全部批次 -> data/boss_jobs.csv
    py boss_import.py --list                   # 只列出发现的数据批次
    py boss_import.py --city 杭州              # 只导入指定城市
    py boss_import.py --source 某目录 --output data/x.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from jobanal import config as app_config
from jobanal.parsing import (
    EDUCATION_PATTERN,
    EXPERIENCE_MAP,
    EXPERIENCE_ORDER,
    EXPERIENCE_PATTERN,
    NO_SALARY_WORDS,
    SKILL_SPLIT_PATTERN,
    WORK_DAYS_PER_MONTH,
    WORK_HOURS_PER_DAY,
    clean_description,
    display_company,
    is_placeholder_company,
    parse_city,
    parse_education,
    parse_experience,
    parse_skills,
    parse_salary_result,
)

BASE_DIR = app_config.BASE_DIR
DEFAULT_RESULTS_DIR = app_config.ARCHIVE_DIR
DEFAULT_OUTPUT = app_config.DEFAULT_DATA_FILE
OUTPUT_COLUMNS = app_config.OUTPUT_COLUMNS

__all__ = [
    "OUTPUT_COLUMNS",
    "EXPERIENCE_ORDER",
    "parse_salary",
    "parse_salary_result",
    "parse_city",
    "parse_experience",
    "parse_education",
    "parse_skills",
    "clean_description",
    "display_company",
    "convert_batch",
    "dedupe",
    "write_csv",
    "main",
]


def parse_salary(text) -> tuple[float, float] | tuple[None, None]:
    """兼容旧接口：返回 (low, high)，不可用时为 (None, None)。

    解析规则与量纲校验统一在 :mod:`jobanal.parsing`，此处只是取区间的薄包装；
    需要知道被剔除的原因时用 :func:`parse_salary_result`。
    """
    result = parse_salary_result(text)
    if not result.ok:
        return None, None
    return result.low, result.high


def load_details(path: Path) -> dict[str, dict]:
    """读取详情 JSON，返回 {job_id: 详情}。兼容 list 与 dict 两种结构。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        data = data.get("details") or data.get("jobs") or []
    if not isinstance(data, list):
        return {}
    return {str(item.get("job_id")): item for item in data if item.get("job_id")}


def find_details_file(jobs_path: Path) -> Path | None:
    """为列表文件挑选配对的详情文件：同时间戳优先，其次同目录最新。"""
    candidates = sorted(jobs_path.parent.glob("boss_details_*.json"))
    if not candidates:
        return None
    stamp = re.search(r"(\d{8}_\d{4,6})", jobs_path.name)
    if stamp:
        for candidate in candidates:
            if stamp.group(1) in candidate.name:
                return candidate
    return candidates[-1]


def iter_job_files(source: Path) -> list[Path]:
    """列出待导入的批次文件。

    跳过 consolidate_archive.py 生成的派生汇总文件（boss_jobs_all.json）：
    它的内容就是各批次之和，且没有 scraped_at，导入会被重复计数、
    时间序列也会被顶到「导入当天」。
    """
    if source.is_file():
        return [] if app_config.is_derived_archive(source) else [source]
    return [
        path for path in sorted(source.rglob(app_config.JOB_BATCH_GLOB))
        if not app_config.is_derived_archive(path)
    ]


def convert_batch(
    jobs_path: Path,
    details_path: Path | None,
    *,
    quarantine: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """把一批抓取结果转换为标准记录，并汇报该批次的统计信息。

    薪资无法解析或超出合理区间的记录会被**隔离**而不是静默丢弃：写入
    ``quarantine``（若提供）以便离线复核，同时在 stats 里按原因计数。
    """
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    details = load_details(details_path) if details_path else {}
    fallback_city = payload.get("city", "") if isinstance(payload, dict) else ""
    keyword = payload.get("keyword", "") if isinstance(payload, dict) else ""

    records = []
    reasons: Counter = Counter()
    for job in jobs:
        salary = parse_salary_result(job.get("salary"))
        if not salary.ok:
            reasons[salary.reason] += 1
            if quarantine is not None:
                quarantine.append(
                    {
                        "job_id": str(job.get("job_id") or ""),
                        "title": str(job.get("title") or ""),
                        "company": str(job.get("boss_name") or ""),
                        "city": parse_city(job.get("location"), fallback_city),
                        "salary_raw": salary.raw,
                        "unit_guess": salary.unit,
                        "reason": salary.reason,
                        "source_batch": jobs_path.name,
                        "keyword": keyword,
                    }
                )
            continue
        tags = job.get("tags") or job.get("job_labels") or ""
        detail = details.get(str(job.get("job_id")), {})
        records.append(
            {
                "job_name": str(job.get("title") or "").strip(),
                "company": display_company(job, detail),
                "city": parse_city(job.get("location"), fallback_city),
                "salary_low": salary.low,
                "salary_high": salary.high,
                "education": parse_education(tags),
                "work_years": parse_experience(tags, job.get("title")),
                "category": str(job.get("company_industry") or "未知").strip(),
                "skills": parse_skills(job.get("skills")),
                "description": clean_description(detail.get("jd")),
                "_job_id": str(job.get("job_id") or ""),
            }
        )
    stats = {
        "file": jobs_path.name,
        "keyword": keyword,
        "city": payload.get("city", "") if isinstance(payload, dict) else "",
        "scraped_at": payload.get("scraped_at", "") if isinstance(payload, dict) else "",
        "raw": len(jobs),
        "converted": len(records),
        "skipped_no_salary": sum(reasons.values()),
        "quarantine_reasons": dict(reasons),
        "placeholder_company": sum(1 for r in records if is_placeholder_company(r["company"])),
        "with_jd": sum(1 for r in records if r["description"]),
    }
    return records, stats


def dedupe(records: list[dict]) -> list[dict]:
    """跨批次按 job_id 去重，再用业务主键兜底。"""
    result = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple] = set()
    for record in records:
        job_id = record.get("_job_id")
        key = (
            record["job_name"],
            record["company"],
            record["city"],
            record["salary_low"],
            record["salary_high"],
        )
        if job_id and job_id in seen_ids:
            continue
        if key in seen_keys:
            continue
        if job_id:
            seen_ids.add(job_id)
        seen_keys.add(key)
        result.append(record)
    return result


def write_csv(records: list[dict], output: Path) -> None:
    import csv

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record)


def _write_quarantine(rows: list[dict], output: Path) -> None:
    """把被剔除的薪资异常记录写成独立 CSV，供离线复核（不污染主表）。"""
    import csv

    if not rows:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "job_id", "title", "company", "city",
        "salary_raw", "unit_guess", "reason", "source_batch", "keyword",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 BOSS 直聘抓取结果到岗位分析系统")
    parser.add_argument("--source", help="抓取结果目录或某个 boss_jobs_*.json 文件")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 CSV 路径")
    parser.add_argument("--city", help="只导入指定城市的岗位")
    parser.add_argument("--keyword", help="只导入岗位标题包含指定关键词的岗位")
    parser.add_argument("--list", action="store_true", help="只列出发现的数据批次")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写文件")
    args = parser.parse_args(argv)

    source = Path(args.source) if args.source else DEFAULT_RESULTS_DIR
    if not source.exists():
        print(f"[错误] 找不到数据源：{source}", file=sys.stderr)
        print("请用 --source 指定抓取结果目录或 JSON 文件", file=sys.stderr)
        return 1

    job_files = iter_job_files(source)
    if not job_files:
        print(f"[错误] 在 {source} 下没有找到 boss_jobs_*.json", file=sys.stderr)
        return 1

    print(f"数据源：{source}")
    print(f"发现 {len(job_files)} 个批次\n")

    all_records: list[dict] = []
    batch_stats = []
    quarantine: list[dict] = []
    for jobs_path in job_files:
        details_path = find_details_file(jobs_path)
        records, stats = convert_batch(jobs_path, details_path, quarantine=quarantine)
        batch_stats.append(stats)
        all_records.extend(records)
        if not details_path:
            detail_note = "无详情文件"
        elif stats["with_jd"]:
            detail_note = details_path.name
        else:
            detail_note = "无匹配 JD"  # 详情文件存在但 job_id 对不上，不强行关联
        reason_note = ""
        if stats["quarantine_reasons"]:
            reason_note = "  隔离:" + ",".join(
                f"{k}×{v}" for k, v in sorted(stats["quarantine_reasons"].items())
            )
        print(
            f"  {stats['file']}  "
            f"关键词={stats['keyword'] or '-'}  城市={stats['city'] or '-'}  "
            f"原始 {stats['raw']} 条 -> 有效 {stats['converted']} 条  "
            f"（剔除 {stats['skipped_no_salary']}，含 JD {stats['with_jd']}，配对 {detail_note}）"
            f"{reason_note}"
        )

    if args.list:
        return 0

    if args.city:
        all_records = [r for r in all_records if r["city"] == args.city]
    if args.keyword:
        all_records = [r for r in all_records if args.keyword in r["job_name"]]

    unique = dedupe(all_records)
    print(f"\n去重前 {len(all_records)} 条，去重后 {len(unique)} 条")

    if quarantine:
        reason_total = Counter(item["reason"] for item in quarantine)
        print(f"薪资异常隔离 {len(quarantine)} 条：{dict(reason_total)}")
        quarantine_path = Path(args.output).with_name("salary_quarantine.csv")
        _write_quarantine(quarantine, quarantine_path)
        print(f"隔离明细已写入 {quarantine_path}（可离线复核，不进主表）")

    if not unique:
        print("[错误] 没有可导入的数据", file=sys.stderr)
        return 1

    cities = Counter(r["city"] for r in unique)
    categories = Counter(r["category"] for r in unique)
    avg_low = sum(r["salary_low"] for r in unique) / len(unique)
    avg_high = sum(r["salary_high"] for r in unique) / len(unique)
    print(f"城市分布：{dict(cities.most_common(8))}")
    print(f"行业 Top5：{dict(categories.most_common(5))}")
    print(f"平均月薪区间：{avg_low:,.0f} - {avg_high:,.0f} 元")
    print(f"经验分箱：{dict(Counter(r['work_years'] for r in unique))}")
    print(f"学历分布：{dict(Counter(r['education'] for r in unique).most_common(6))}")

    if args.dry_run:
        print("\n[预览模式] 未写入文件")
        return 0

    output = Path(args.output)
    write_csv(unique, output)
    print(f"\n已写入 {output}（{len(unique)} 条）")
    print("在系统页面上传该 CSV，或设置环境变量 ZOUYE_DATA_FILE 指向它作为默认数据源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
