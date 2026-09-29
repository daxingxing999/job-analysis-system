"""把 抓取结果/ 下 556 个批次文件整合为 1 个 JSON + 1 个 CSV。

本脚本只读取和生成，不删除任何东西（删除由 cleanup_archive.py 单独确认后执行）。

产物
    抓取结果/all_jobs.json   整合后的完整数据：每个岗位一份，含原始字段 + JD + 来源批次
    抓取结果/all_jobs.csv    整合后的标准数据：系统用的 10 个字段

去重口径
    按 job_id 去重；同一岗位的多份快照里挑"信息最完整"的，并用其它快照补全缺失字段。
    JD 全局按 job_id 关联（比 boss_import 的"同时间戳配对"能匹配上更多）。
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import boss_import as bi  # 复用解析规则，保证与系统口径一致

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "抓取结果"

JSON_OUT = ARCHIVE / "boss_jobs_all.json"
CSV_OUT = ARCHIVE / "boss_jobs_all.csv"
# 先写临时文件再原子替换：这样扫描时能把上一轮的 boss_jobs_all.json 也当作数据来源，
# 不会因为"跳过自身输出"而把历史数据排除掉（曾经这样丢过 4000+ 条）。
TMP_JSON = ARCHIVE / "_tmp_all.json"
TMP_CSV = ARCHIVE / "_tmp_all.csv"

# 完整性打分用的字段，越靠前权重越高
SCORE_FIELDS = ["salary", "location", "company_industry", "skills", "tags", "title"]


def score(job: dict) -> int:
    value = 0
    for i, field in enumerate(SCORE_FIELDS):
        text = str(job.get(field) or "").strip()
        if text and text not in ("-", "面议"):
            value += (len(SCORE_FIELDS) - i) * 10
    value += min(len(str(job.get("skills") or "")), 60)
    value += min(len(str(job.get("tags") or "")), 40)
    return value


def main() -> int:
    # 1. 收集所有岗位快照（顺带收集记录自带的 jd）
    groups: dict[str, list[dict]] = {}
    origin: dict[str, set] = {}
    jd_map: dict[str, str] = {}
    files = sorted(ARCHIVE.rglob("boss_jobs_*.json"))
    print("扫描 boss_jobs_*.json: %d 个" % len(files))

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        kw = payload.get("keyword", "") if isinstance(payload, dict) else ""
        ct = payload.get("city", "") if isinstance(payload, dict) else ""
        stamp = path.parent.name
        for job in jobs:
            jid = str(job.get("job_id") or "").strip()
            if not jid:
                continue
            groups.setdefault(jid, []).append(job)
            origin.setdefault(jid, set()).add("%s|%s|%s" % (ct, kw, stamp))
            # 记录自带 jd 时也收集（上一轮整合产物里就带着 jd，别弄丢）
            own_jd = str(job.get("jd") or "").strip()
            if own_jd and len(own_jd) > len(jd_map.get(jid, "")):
                jd_map[jid] = own_jd

    total_raw = sum(len(v) for v in groups.values())
    print("原始记录: %d 条 -> 唯一岗位: %d 个" % (total_raw, len(groups)))

    # 2. 补充收集独立的 JD 文件（全局按 job_id 关联）
    for path in sorted(ARCHIVE.rglob("boss_details_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("details") or data.get("jobs") or [] if isinstance(data, dict) else data
        for item in items:
            jid = str(item.get("job_id") or "").strip()
            jd = str(item.get("jd") or "").strip()
            if jid and jd and len(jd) > len(jd_map.get(jid, "")):
                jd_map[jid] = jd
    print("收集到 JD: %d 条" % len(jd_map))

    # 3. 合并每个岗位
    merged_jobs = []
    rows = []
    skipped_no_salary = 0
    with_jd = 0

    for jid, group in groups.items():
        best = max(group, key=score)
        record = dict(best)
        for other in group:
            for field in SCORE_FIELDS:
                cur = str(record.get(field) or "").strip()
                alt = str(other.get(field) or "").strip()
                if not cur and alt and alt not in ("-", "面议"):
                    record[field] = alt

        jd = jd_map.get(jid, "")
        record["jd"] = jd
        record["_sources"] = sorted(origin.get(jid, set()))
        merged_jobs.append(record)

        low, high = bi.parse_salary(record.get("salary"))
        if low is None or high is None:
            skipped_no_salary += 1
            continue
        tags = record.get("tags") or record.get("job_labels") or ""
        rows.append({
            "job_name": str(record.get("title") or "").strip(),
            "company": str(record.get("boss_name") or "").strip(),
            "city": bi.parse_city(record.get("location"), ""),
            "salary_low": low,
            "salary_high": high,
            "education": bi.parse_education(tags),
            "work_years": bi.parse_experience(tags, record.get("title")),
            "category": str(record.get("company_industry") or "未知").strip(),
            "skills": bi.parse_skills(record.get("skills")),
            "description": bi.clean_description(jd),
        })
        if jd:
            with_jd += 1

    # 业务主键兜底去重：同一公司同城同薪资同名视为同一岗位
    # （与 boss_import / merge_jobs 口径一致，保证和 data/boss_jobs.csv 对齐）
    seen: set = set()
    deduped = []
    for r in rows:
        k = (r["job_name"], r["company"], r["city"], r["salary_low"], r["salary_high"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    print("合并后岗位: %d 个（无薪资跳过 %d 个）" % (len(merged_jobs), skipped_no_salary))
    print("有效记录: %d 条 -> 业务主键去重后 %d 条（含 JD %d 条）"
          % (len(rows), len(deduped), with_jd))
    print()
    rows = deduped

    # 4. 写 JSON
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_files": len(files),
        "raw_records": total_raw,
        "unique_jobs": len(merged_jobs),
        "valid_rows": len(rows),
        "jobs_with_jd": len(jd_map),
        "jobs": merged_jobs,
    }
    TMP_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    with TMP_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=bi.OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    # 全部写成功后才替换正式文件，中途失败不会影响已有数据
    TMP_JSON.replace(JSON_OUT)
    TMP_CSV.replace(CSV_OUT)
    print("已写入 %s (%.1f MB)" % (JSON_OUT, JSON_OUT.stat().st_size / 1024 / 1024))
    print("已写入 %s (%.1f KB, %d 条)" % (CSV_OUT, CSV_OUT.stat().st_size / 1024, len(rows)))

    # 6. 概览
    cities = Counter(r["city"] for r in rows)
    print()
    print("城市数: %d  Top8: %s" % (len(cities), dict(cities.most_common(8))))
    print("行业数: %d" % len({r["category"] for r in rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
