"""分析各城市/关键词抓取的"收益率"，回答：怎么抓才能拿到更多不重复的岗位。

核心指标
  记录数  : 该维度下抓到的原始条数
  独立岗位: 去重后的 job_id 数
  收益率  : 独立岗位 / 记录数，越低说明重复越严重、越浪费
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "抓取结果"

# (城市, 关键词) -> set(job_id)
combo_jobs = defaultdict(set)
# 城市 -> set(job_id)
city_jobs = defaultdict(set)
# 关键词 -> set(job_id)
kw_jobs = defaultdict(set)
# job_id -> set(城市)
job_cities = defaultdict(set)
# job_id -> set(关键词)
job_kws = defaultdict(set)

records = 0
for path in sorted(ARCHIVE.rglob("boss_jobs_*.json")):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    kw = payload.get("keyword", "?") if isinstance(payload, dict) else "?"
    ct = payload.get("city", "?") if isinstance(payload, dict) else "?"
    for job in jobs:
        jid = str(job.get("job_id") or "").strip()
        if not jid:
            continue
        records += 1
        combo_jobs[(ct, kw)].add(jid)
        city_jobs[ct].add(jid)
        kw_jobs[kw].add(jid)
        job_cities[jid].add(ct)
        job_kws[jid].add(kw)

total_unique = len(job_cities)
print("原始记录 %d 条，独立岗位 %d 个，整体收益率 %.1f%%"
      % (records, total_unique, 100.0 * total_unique / max(records, 1)))
print()

print("=== 按城市：抓了多久、拿到多少、值不值 ===")
print("%-8s %8s %8s %8s" % ("城市", "记录数", "独立岗位", "收益率"))
rows = []
for city, jids in city_jobs.items():
    n = sum(len(v) for (c, _), v in combo_jobs.items() if c == city)
    # 该城市下的记录数需要单独统计
    rows.append((city, jids))
# 重新统计每个城市的记录数
city_records = Counter()
for path in sorted(ARCHIVE.rglob("boss_jobs_*.json")):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    ct = payload.get("city", "?") if isinstance(payload, dict) else "?"
    city_records[ct] += len(jobs)

for city, jids in sorted(city_jobs.items(), key=lambda x: -len(x[1])):
    n = city_records.get(city, 0)
    rate = 100.0 * len(jids) / max(n, 1)
    print("%-8s %8d %8d %7.1f%%" % (city, n, len(jids), rate))
print()

print("=== 按关键词：换关键词到底有没有用 ===")
kw_records = Counter()
for path in sorted(ARCHIVE.rglob("boss_jobs_*.json")):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    kw = payload.get("keyword", "?") if isinstance(payload, dict) else "?"
    kw_records[kw] += len(jobs)

print("%-12s %8s %8s %8s" % ("关键词", "记录数", "独立岗位", "收益率"))
for kw, jids in sorted(kw_jobs.items(), key=lambda x: -len(x[1])):
    n = kw_records.get(kw, 0)
    rate = 100.0 * len(jids) / max(n, 1)
    print("%-12s %8d %8d %7.1f%%" % (kw, n, len(jids), rate))
print()

# 一个岗位平均被几个关键词命中
avg_kw = sum(len(v) for v in job_kws.values()) / max(total_unique, 1)
avg_city = sum(len(v) for v in job_cities.values()) / max(total_unique, 1)
print("平均每个岗位被 %.2f 个关键词命中" % avg_kw)
print("平均每个岗位出现在 %.2f 个城市维度下" % avg_city)
print()

multi_kw = sum(1 for v in job_kws.values() if len(v) > 1)
print("被 2 个以上关键词同时命中的岗位: %d / %d (%.0f%%)"
      % (multi_kw, total_unique, 100.0 * multi_kw / max(total_unique, 1)))
print("  -> 这个比例越高，说明在同一城市里换关键词基本是白抓，重复的是同一批岗位")
