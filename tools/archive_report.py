#!/usr/bin/env python3
"""归档数据质量与采集收益报告（只读，不改任何数据）。

取代原先三个各扫一遍归档的一次性脚本（``analyze_yield.py`` 扫了 3 遍、
``analyze_duplicates.py`` 扫了 2 遍、``consolidate_archive.py`` 里又有一套统计）。
现在是一次遍历同时回答：

1. 收益率：哪些城市/关键词是「白抓」（重复率高、独立岗位少）
2. 重复度：同一岗位被抓了几次、重复快照之间字段有没有差异（值不值得合并）
3. 数据缺口：无 job_id、无薪资的记录有多少

用法
    python3 tools/archive_report.py                        # 打印到终端
    python3 tools/archive_report.py --output data/archive_report.md
    python3 tools/archive_report.py --include-derived      # 连汇总文件一起统计
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from jobanal import archive as archive_mod  # noqa: E402
from jobanal import config  # noqa: E402


def print_summary(scan: archive_mod.ArchiveScan, top: int) -> None:
    cross = archive_mod.cross_tagging(scan)
    print("=" * 70)
    print("  归档数据质量与采集收益")
    print("=" * 70)
    print(f"  批次文件        : {scan.batches}")
    print(f"  原始记录        : {scan.raw_records}")
    print(f"  唯一岗位        : {scan.unique_jobs}")
    print(f"  整体收益率      : {scan.dedupe_rate:.1f}%")
    print(f"  无 job_id       : {scan.records_without_job_id}")
    print(f"  唯一业务主键    : {len(scan.business_keys)}")
    print(f"  无薪资/面议     : {scan.salary_missing}")
    if scan.unreadable:
        print(f"  ⚠ 无法读取      : {len(scan.unreadable)} 个")
        for item in scan.unreadable[:3]:
            print(f"      {item}")
    print()
    print(f"  平均每个岗位被 {cross['avg_dimensions']:.2f} 个维度命中；"
          f"被 2 个以上维度命中的占 {cross['multi_ratio']:.0f}%")
    print("  （比例越高，说明换关键词基本在重复命中同一批岗位）")
    print()

    print("=" * 70)
    print("  按城市：收益率（独立岗位 / 记录数）")
    print("=" * 70)
    print(f"  {'城市':<12}{'记录数':>9}{'独立岗位':>10}{'收益率':>9}")
    for row in archive_mod.yield_by_city(scan)[:top]:
        print(f"  {row.name:<12}{row.records:>9}{row.unique_jobs:>10}{row.rate:>8.1f}%")
    print()

    print("=" * 70)
    print("  按关键词：换词收益")
    print("=" * 70)
    print(f"  {'关键词':<16}{'记录数':>9}{'独立岗位':>10}{'收益率':>9}")
    for row in archive_mod.yield_by_keyword(scan)[:top]:
        print(f"  {row.name:<16}{row.records:>9}{row.unique_jobs:>10}{row.rate:>8.1f}%")
    print()

    diffs, _ = archive_mod.field_differences(scan)
    print("=" * 70)
    print("  重复快照的字段差异（差异越多，说明「合并取最全」越有价值）")
    print("=" * 70)
    if diffs:
        for name, count in diffs.most_common():
            print(f"  {name:<20}{count:>8} 次")
    else:
        print("  重复快照之间没有字段差异")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="归档数据质量与采集收益报告")
    parser.add_argument("--archive", default=str(config.ARCHIVE_DIR))
    parser.add_argument("--output", help="同时写出 Markdown 报告到该路径")
    parser.add_argument("--include-derived", action="store_true",
                        help="把 consolidate_archive.py 的汇总文件也算进来")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    archive = Path(args.archive)
    if not archive.exists():
        print(f"[错误] 找不到归档目录：{archive}", file=sys.stderr)
        return 1

    scan = archive_mod.scan_archive(archive, include_derived=args.include_derived)
    if scan.batches == 0:
        print(f"[错误] 在 {archive} 下没有找到可读的批次文件", file=sys.stderr)
        return 1

    print_summary(scan, args.top)

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(archive_mod.build_report(scan, top=args.top), encoding="utf-8")
        print(f"Markdown 报告已写入：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
