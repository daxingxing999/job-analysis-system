"""校准 jobanal.classify 的工种分类规则（只读，不改数据）。

用法
    python3 tools/calibrate_family_classifier.py [--archive 抓取结果] [--top 20]

做三件事
1. 统计归档里每个工种族实际命中的标题数，找出「配了词表却几乎没命中」的幽灵族；
2. 对现有分类器（前缀子串匹配）与修正分类器（最长词优先 + 词边界）逐条比对，
   列出**改判**的标题样本与数量；
3. 给出「other」占比与两侧差异，作为调词的依据。

背景
-----
旧实现在 FAMILY_MATCH_TERMS 里放了 "mobile"，但 JOB_FAMILIES 没有这个族，
于是 family_of_title() 会返回一个报告里根本不存在的族 id，还会让
city_family_covered["某城|mobile"] 给该城虚增一个「已铺族」，
提前触发「该城族数已铺满」而跳过真正该抓的组合。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import smart_crawl  # noqa: E402  只读复用它的「旧实现」做对照
from jobanal.classify import family_of_title_v2, match_detail  # noqa: E402
from jobanal.taxonomy import (  # noqa: E402
    FAMILY_IDS,
    FAMILY_MATCH_ORDER,
    FAMILY_MATCH_TERMS,
    validate_taxonomy,
)


def iter_titles(archive: Path):
    """产出归档里所有 (job_id, title)，按 job_id 去重。"""
    seen: set[str] = set()
    for path in sorted(archive.rglob("boss_jobs_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            jid = str(job.get("job_id") or "").strip()
            title = str(job.get("title") or "").strip()
            if not jid or not title or jid in seen:
                continue
            seen.add(jid)
            yield jid, title


def main() -> int:
    parser = argparse.ArgumentParser(description="校准工种分类器")
    parser.add_argument("--archive", default=str(BASE / "抓取结果"))
    parser.add_argument("--top", type=int, default=15, help="每类展示的样本数")
    args = parser.parse_args()

    archive = Path(args.archive)
    if not archive.exists():
        print(f"[错误] 找不到归档目录：{archive}", file=sys.stderr)
        return 1

    rows = list(iter_titles(archive))
    if not rows:
        print("[错误] 归档里没有可用的岗位标题", file=sys.stderr)
        return 1
    print(f"样本：{len(rows)} 个去重后的岗位标题\n")

    declared = set(FAMILY_IDS)
    matched_terms = set(FAMILY_MATCH_TERMS)
    print("=" * 68)
    print("一、词表一致性")
    print("=" * 68)
    problems = validate_taxonomy()
    print(f"  JOB_FAMILIES 声明 {len(declared)} 个族")
    print(f"  FAMILY_MATCH_TERMS 覆盖 {len(matched_terms)} 个族")
    if problems:
        print("  ✗ 词表自检未通过：")
        for item in problems:
            print(f"      - {item}")
    else:
        print("  ✓ 词表自检通过（无幽灵族、无缺词表的族）")

    old_counts: Counter = Counter()
    new_counts: Counter = Counter()
    changed: list[tuple[str, str, str]] = []
    detail_samples: dict[str, list[tuple[str, str]]] = {}

    for jid, title in rows:
        old = smart_crawl.family_of_title(title)
        new = family_of_title_v2(title)
        old_counts[old] += 1
        new_counts[new] += 1
        if old != new:
            changed.append((title, old, new))
            if len(detail_samples.setdefault(new, [])) < 4:
                detail_samples[new].append((title, old))

    print()
    print("=" * 68)
    print("二、命中分布（旧 -> 新）")
    print("=" * 68)
    print(f"  {'工种':<12}{'旧实现':>10}{'新实现':>10}{'变化':>10}")
    for fid in sorted(set(old_counts) | set(new_counts), key=lambda k: -new_counts.get(k, 0)):
        o, n = old_counts.get(fid, 0), new_counts.get(fid, 0)
        flag = "" if o == n else "  ←"
        print(f"  {fid:<12}{o:>10}{n:>10}{n - o:>+10}{flag}")

    print()
    other_old = old_counts.get("other", 0)
    other_new = new_counts.get("other", 0)
    print(f"  未归类 other：旧 {other_old}（{other_old / len(rows) * 100:.1f}%）"
          f" -> 新 {other_new}（{other_new / len(rows) * 100:.1f}%）")
    print(f"  分类发生改变的岗位：{len(changed)}（{len(changed) / len(rows) * 100:.1f}%）")

    if changed:
        print()
        print("=" * 68)
        print("三、改判样本")
        print("=" * 68)
        for new_fid, samples in sorted(detail_samples.items(), key=lambda kv: -len(kv[1])):
            print(f"\n  → {new_fid}")
            for title, old_fid in samples[: args.top // 3 or 1]:
                print(f"      [{old_fid} -> {new_fid}] {title[:60]}")

    print()
    print("=" * 68)
    print("四、关键词命中细节抽查（前 5 个改判项）")
    print("=" * 68)
    for title, old, new in changed[:5]:
        detail = match_detail(title)
        print(f"  {title[:48]:<50} {old} -> {new}")
        if detail:
            print(f"      命中：{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
