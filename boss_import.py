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
import re
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = BASE_DIR / "抓取结果"
DEFAULT_OUTPUT = BASE_DIR / "data" / "boss_jobs.csv"

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
]

WORK_DAYS_PER_MONTH = 21.75
WORK_HOURS_PER_DAY = 8

# 经验标签 -> 系统 EXPERIENCE_ORDER 中的分箱
EXPERIENCE_MAP = {
    "在校/应届": "应届/无经验",
    "在校生": "应届/无经验",
    "应届生": "应届/无经验",
    "经验不限": "应届/无经验",
    "1年以内": "1-3年",
    "1-3年": "1-3年",
    "3-5年": "3-5年",
    "5-10年": "5年以上",
    "10年以上": "5年以上",
}
EXPERIENCE_PATTERN = re.compile(
    "在校/应届|在校生|应届生|经验不限|1年以内|1-3年|3-5年|5-10年|10年以上"
)
EDUCATION_PATTERN = re.compile(
    "初中及以下|中专/中技|中专|中技|高中及以下|高中|大专|本科|硕士|博士|学历不限"
)
# 顺序不可调整：先匹配更特殊的"在校/应届"、"初中及以下"等
SKILL_SPLIT_PATTERN = re.compile(r"[|｜/、,，;；]+")
NO_SALARY_WORDS = {"", "-", "面议", "薪资面议", "面议薪资", "none", "nan"}


def parse_salary(text) -> tuple[float, float] | tuple[None, None]:
    """把 BOSS 明文薪资解析为月薪下限/上限（单位：元）。

    支持 15-30K、30-60K·15薪、1.5-2万、8000-12000、300-500元/天、50元/时。
    无法解析（如"面议"）时返回 (None, None)，后续由 app.py 按设计过滤。
    """
    if text is None:
        return None, None
    raw = str(text).strip()
    if raw.lower() in NO_SALARY_WORDS:
        return None, None
    value = raw.replace("，", ",").replace("～", "-").replace("~", "-").replace("至", "-")
    value = re.sub(r"[·•]\s*\d+\s*薪", "", value)  # 去掉"·15薪"等多薪信息
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", value)]
    if not numbers:
        return None, None

    if "万" in value:
        unit = 10000
    elif re.search(r"[kK千]", value):
        unit = 1000
    else:
        unit = 1

    low = numbers[0] * unit
    high = numbers[1] * unit if len(numbers) > 1 else low

    if re.search(r"(天|日)结|/\s*(天|日)", value):
        low *= WORK_DAYS_PER_MONTH
        high *= WORK_DAYS_PER_MONTH
    elif re.search(r"/\s*(小时|时)", value):
        low *= WORK_HOURS_PER_DAY * WORK_DAYS_PER_MONTH
        high *= WORK_HOURS_PER_DAY * WORK_DAYS_PER_MONTH

    if low > high:
        low, high = high, low
    return round(low, 2), round(high, 2)


def parse_city(location, fallback: str = "") -> str:
    """location 形如 "杭州·滨江区·长河"，取第一段作为城市。"""
    text = str(location or "").strip()
    if not text:
        return fallback or "未知"
    return text.split("·")[0].strip() or fallback or "未知"


def parse_experience(tags, title: str = "") -> str:
    """解析经验标签；实习/应届岗常只标学历，此时按标题回退到"应届/无经验"。"""
    match = EXPERIENCE_PATTERN.search(str(tags or ""))
    if match:
        return EXPERIENCE_MAP.get(match.group(0), "未知")
    if re.search(r"实习|应届|在校", str(title or "")):
        return "应届/无经验"
    return "未知"


def parse_education(tags) -> str:
    match = EDUCATION_PATTERN.search(str(tags or ""))
    return match.group(0) if match else "未知"


def parse_skills(text) -> str:
    if not text:
        return ""
    parts = SKILL_SPLIT_PATTERN.split(str(text))
    return ",".join(part.strip() for part in parts if part.strip())


def clean_description(text) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


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
    if source.is_file():
        return [source]
    return sorted(source.rglob("boss_jobs_*.json"))


def convert_batch(jobs_path: Path, details_path: Path | None) -> tuple[list[dict], dict]:
    """把一批抓取结果转换为标准记录，并汇报该批次的统计信息。"""
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    details = load_details(details_path) if details_path else {}
    fallback_city = payload.get("city", "") if isinstance(payload, dict) else ""

    records = []
    skipped_no_salary = 0
    for job in jobs:
        low, high = parse_salary(job.get("salary"))
        if low is None or high is None:
            skipped_no_salary += 1
            continue
        tags = job.get("tags") or job.get("job_labels") or ""
        detail = details.get(str(job.get("job_id")), {})
        records.append(
            {
                "job_name": str(job.get("title") or "").strip(),
                "company": str(detail.get("company") or job.get("boss_name") or "").strip(),
                "city": parse_city(job.get("location"), fallback_city),
                "salary_low": low,
                "salary_high": high,
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
        "keyword": payload.get("keyword", "") if isinstance(payload, dict) else "",
        "city": payload.get("city", "") if isinstance(payload, dict) else "",
        "raw": len(jobs),
        "converted": len(records),
        "skipped_no_salary": skipped_no_salary,
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
    for jobs_path in job_files:
        details_path = find_details_file(jobs_path)
        records, stats = convert_batch(jobs_path, details_path)
        batch_stats.append(stats)
        all_records.extend(records)
        if not details_path:
            detail_note = "无详情文件"
        elif stats["with_jd"]:
            detail_note = details_path.name
        else:
            detail_note = "无匹配 JD"  # 详情文件存在但 job_id 对不上，不强行关联
        print(
            f"  {stats['file']}  "
            f"关键词={stats['keyword'] or '-'}  城市={stats['city'] or '-'}  "
            f"原始 {stats['raw']} 条 -> 有效 {stats['converted']} 条  "
            f"（无薪资跳过 {stats['skipped_no_salary']}，含 JD {stats['with_jd']}，配对 {detail_note}）"
        )

    if args.list:
        return 0

    if args.city:
        all_records = [r for r in all_records if r["city"] == args.city]
    if args.keyword:
        all_records = [r for r in all_records if args.keyword in r["job_name"]]

    unique = dedupe(all_records)
    print(f"\n去重前 {len(all_records)} 条，去重后 {len(unique)} 条")

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
