"""分析归档数据的重复情况，判断"合并"能挽回多少数据。

只读分析，不修改任何文件。回答三个问题：
  1. 25834 条原始 -> 4341 条，损失发生在哪一步？
  2. 重复的记录之间，字段内容有没有差异（是否值得合并而不是直接丢弃）？
  3. 有多少记录其实没有 job_id，只能靠业务主键去重？
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARCHIVE = BASE / "抓取结果"

files = sorted(ARCHIVE.rglob("boss_jobs_*.json"))
print("批次文件数:", len(files))

total = 0
no_job_id = 0
records = []          # (job_id, 业务主键, 原始dict, 来源文件)
per_file = []

for path in files:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print("  读取失败", path.name, exc)
        continue
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    per_file.append((path.name, len(jobs)))
    for job in jobs:
        total += 1
        jid = str(job.get("job_id") or "").strip()
        if not jid:
            no_job_id += 1
        key = (
            str(job.get("title") or "").strip(),
            str(job.get("boss_name") or "").strip(),
            str(job.get("location") or "").split("·")[0].strip(),
            str(job.get("salary") or "").strip(),
        )
        records.append((jid, key, job, path.name))

print("原始记录总数:", total)
print("没有 job_id 的记录:", no_job_id)
print()

# 1. 唯一 job_id
by_id = defaultdict(list)
by_key = defaultdict(list)
for jid, key, job, src in records:
    if jid:
        by_id[jid].append((job, src))
    by_key[key].append((job, src))

print("唯一 job_id 数:", len(by_id))
print("唯一业务主键数:", len(by_key))
print()

# 2. 重复倍数分布
dup = Counter(len(v) for v in by_id.values())
print("同一 job_id 出现次数分布（前 12）:")
for n, c in sorted(dup.items())[:12]:
    print("   出现 %-3d 次: %d 个岗位" % (n, c))
print()

# 3. 重复记录之间字段是否有差异 -> 决定值不值得"合并"
FIELDS = ["title", "salary", "location", "company_industry", "skills", "tags", "boss_name"]
diff_count = Counter()
multi = [v for v in by_id.values() if len(v) > 1]
print("有重复的 job_id 组数:", len(multi))

sample_shown = 0
for group in multi:
    first = group[0][0]
    for job, _ in group[1:]:
        for f in FIELDS:
            a = str(first.get(f) or "").strip()
            b = str(job.get(f) or "").strip()
            if a != b:
                diff_count[f] += 1
    if sample_shown < 3 and len(group) > 1:
        # 展示一组样本看看差异长什么样
        sample_shown += 1
        print()
        print("  --- 重复样本 %d（同一 job_id 出现 %d 次）---" % (sample_shown, len(group)))
        for f in FIELDS:
            vals = []
            for job, _ in group[:4]:
                v = str(job.get(f) or "").strip()
                if v not in vals:
                    vals.append(v)
            if len(vals) > 1:
                print("     %-18s 不同值: %s" % (f, vals[:3]))
print()
print("重复组内字段出现差异的次数统计:")
for f, c in diff_count.most_common():
    print("   %-18s %d 次" % (f, c))
print()

# 4. 无薪资记录数（会被 convert_batch 丢弃）
no_salary = sum(1 for _, _, job, _ in records
                if not str(job.get("salary") or "").strip()
                or str(job.get("salary")).strip() in ("", "-", "面议"))
print("无薪资/面议（导入时被过滤）:", no_salary)
print()

# 5. 每批次的关键词/城市，看看是不是同一组合反复抓
cities = Counter()
keywords = Counter()
for path in files:
    try:
        p = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(p, dict):
            cities[p.get("city", "?")] += 1
            keywords[p.get("keyword", "?")] += 1
    except Exception:
        pass
print("批次涉及城市数:", len(cities), " 关键词数:", len(keywords))
print("城市 Top10:", dict(cities.most_common(10)))
print("关键词:", dict(keywords.most_common(20)))
