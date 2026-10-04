"""各城市/关键词的抓取收益率分析（薄壳）。

实现已合并到 :mod:`jobanal.archive` 与 ``tools/archive_report.py``。
原先本脚本把同一个归档目录**完整扫了 3 遍**（28、61、78 行各一次 ``rglob``），
而且第一遍的结果被后面的循环覆盖成了死代码。现在单遍扫描出全部数字。

核心指标
  记录数  : 该维度下抓到的原始条数
  独立岗位: 去重后的 job_id 数
  收益率  : 独立岗位 / 记录数，越低说明重复越严重、越浪费

用法
    python3 analyze_yield.py
    python3 tools/archive_report.py --include-derived   # 连汇总文件一起统计
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from jobanal import archive as archive_mod  # noqa: E402


def main() -> int:
    archive = BASE / "抓取结果"
    if not archive.exists():
        print(f"[错误] 找不到归档目录：{archive}", file=sys.stderr)
        return 1

    scan = archive_mod.scan_archive(archive)
    if scan.batches == 0:
        print(f"[错误] 在 {archive} 下没有找到可读的批次文件", file=sys.stderr)
        return 1

    print("原始记录 %d 条，独立岗位 %d 个，整体收益率 %.1f%%"
          % (scan.raw_records, scan.unique_jobs, scan.dedupe_rate))
    print()

    print("=== 按城市：抓了多久、拿到多少、值不值 ===")
    print("%-8s %8s %8s %8s" % ("城市", "记录数", "独立岗位", "收益率"))
    for row in archive_mod.yield_by_city(scan):
        print("%-8s %8d %8d %7.1f%%" % (row.name, row.records, row.unique_jobs, row.rate))
    print()

    print("=== 按关键词：换关键词到底有没有用 ===")
    print("%-12s %8s %8s %8s" % ("关键词", "记录数", "独立岗位", "收益率"))
    for row in archive_mod.yield_by_keyword(scan):
        print("%-12s %8d %8d %7.1f%%" % (row.name, row.records, row.unique_jobs, row.rate))
    print()

    cross = archive_mod.cross_tagging(scan)
    print("平均每个岗位被 %.2f 个维度命中" % cross["avg_dimensions"])
    print("被 2 个以上关键词同时命中的岗位: %d / %d (%.0f%%)"
          % (cross["multi_keyword_jobs"], cross["total_jobs"], cross["multi_ratio"]))
    print("  -> 这个比例越高，说明在同一城市里换关键词基本是白抓，重复的是同一批岗位")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
