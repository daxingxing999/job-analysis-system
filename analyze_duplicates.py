"""归档重复情况分析（薄壳）。

实现已合并到 :mod:`jobanal.archive` 与 ``tools/archive_report.py``：
原先本脚本要完整扫描归档 2 遍，``analyze_yield.py`` 又扫 3 遍，且两边
各写了一套关键词/城市统计。现在改为一处实现、单遍扫描。

本文件保留为兼容入口，回答的问题不变：
  1. 原始记录到唯一岗位，损失发生在哪一步？
  2. 重复快照之间字段有没有差异（值不值得合并而不是直接丢弃）？
  3. 有多少记录其实没有 job_id，只能靠业务主键去重？

用法
    python3 analyze_duplicates.py
    python3 tools/archive_report.py --output data/archive_report.md   # 更完整的报告
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

    print("批次文件数:", scan.batches)
    print("原始记录总数:", scan.raw_records)
    print("没有 job_id 的记录:", scan.records_without_job_id)
    print("唯一 job_id 数:", scan.unique_jobs)
    print("唯一业务主键数:", len(scan.business_keys))
    print("无薪资/面议（导入时被过滤）:", scan.salary_missing)
    print()

    print("同一 job_id 出现次数分布（前 12）:")
    for times, count in archive_mod.duplicate_distribution(scan):
        print("   出现 %-3d 次: %d 个岗位" % (times, count))
    print()

    diffs, samples = archive_mod.field_differences(scan)
    print("重复组内字段出现差异的次数统计:")
    if diffs:
        for name, count in diffs.most_common():
            print("   %-18s %d 次" % (name, count))
    else:
        print("   （重复快照之间没有任何字段差异）")
    print()
    for job_id, varying in samples:
        print(f"  --- 重复样本 {job_id} ---")
        for name, values in varying.items():
            print("     %-18s 不同值: %s" % (name, values))
    print()
    print("更完整的报告（含城市/关键词收益率）：python3 tools/archive_report.py --output data/archive_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
