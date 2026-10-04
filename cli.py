#!/usr/bin/env python3
"""jobanal 统一命令行入口。

以前 9 个脚本平铺在仓库根目录、各有各的参数风格（有的叫 --source 有的叫
--archive，有的用 py 有的用 python3），这里收敛成一个入口、一种风格：

    python cli.py import   --archive 抓取结果 --db data/jobs.db   # 归档入库
    python cli.py import-csv --csv data/boss_jobs.csv             # CSV 入库
    python cli.py verify   --db data/jobs.db                      # 数据质量与对账
    python cli.py trends   --db data/jobs.db                      # 打印趋势
    python cli.py coverage --db data/jobs.db                      # 覆盖与收益概览
    python cli.py csv      --db data/jobs.db --output out.csv     # 从库里导出 CSV

兼容入口：原有的 boss_import.py / merge_jobs.py / smart_crawl.py 等脚本保持可用，
本文件只是新增门面，不替代它们。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from jobanal import config, store  # noqa: E402


def _cmd_import(args) -> int:
    archive = Path(args.archive)
    db = Path(args.db)
    stats = store.import_archive(
        db, archive,
        include_derived=args.include_derived,
        skip_undated=args.skip_undated,
        quarantine_path=Path(args.quarantine) if args.quarantine else None,
    )
    print(f"数据源：{archive}")
    print(stats.summary())
    if stats.quarantine_reasons:
        print("薪资异常隔离原因：", dict(stats.quarantine_reasons))
    if stats.skipped_undated:
        print(f"跳过（无时间戳）：{stats.skipped_undated}")
    if stats.unparsable_files:
        print(f"[警告] {len(stats.unparsable_files)} 个文件无法解析：")
        for item in stats.unparsable_files[:5]:
            print("   -", item)
    print(f"数据库：{db}")
    if args.include_derived:
        print("提示：已包含派生汇总文件（boss_jobs_all.json）。它是一次性整合快照，"
              "在趋势里会被算成同一天，看趋势时请知悉这一点。")
    return 0


def _cmd_import_csv(args) -> int:
    db = Path(args.db)
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"[错误] 找不到 CSV：{csv_path}", file=sys.stderr)
        return 1
    stats = store.import_csv(db, csv_path)
    print(f"数据源：{csv_path}")
    print(stats.summary())
    print("提示：标准 CSV 不含采集时间，这些岗位不会进入趋势图；"
          "要时间维度请用 `import --archive 抓取结果`。")
    return 0


def _cmd_verify(args) -> int:
    db = Path(args.db)
    if not db.is_file():
        print(f"[错误] 数据库不存在：{db}\n先执行：python cli.py import", file=sys.stderr)
        return 1
    conn = store.connect(db)
    try:
        cov = store.coverage_stats(conn)
    finally:
        conn.close()

    print("=" * 66)
    print(f"  数据质量核对：{db}")
    print("=" * 66)
    print(f"  岗位总数        : {cov['jobs']}")
    print(f"  快照记录        : {cov['postings']}")
    print(f"  采集时间跨度    : {cov['earliest'][:19] or '无'} ~ {cov['latest'][:19] or '无'}")
    print(f"  平均月薪        : {cov['salary_avg'] or '无'} 元"
          f"（区间 {cov['salary_min']} ~ {cov['salary_max']}）")
    print()
    print("  —— 完整性 ——")
    print(f"  有职位描述(JD)  : {cov['jobs_with_description']}"
          f"  ({_pct(cov['jobs_with_description'], cov['jobs'])})")
    print(f"  匿名招聘公司    : {cov['anonymous_company']}"
          f"  ({_pct(cov['anonymous_company'], cov['jobs'])})")
    print(f"  被重复抓到≥2次  : {cov['jobs_seen_twice_or_more']}"
          f"  ({_pct(cov['jobs_seen_twice_or_more'], cov['jobs'])})")

    warnings: list[str] = []
    if cov["jobs"] == 0:
        warnings.append("库里没有岗位数据")
    if cov["jobs"] and cov["jobs_with_description"] == 0:
        warnings.append(
            "全库没有职位描述：抓取链路写死了 --no-detail，"
            "「职位文本/词云」类功能实际上拿不到数据（见评审报告第一节）"
        )
    if not cov["earliest"]:
        warnings.append("没有采集时间，趋势分析不可用")
    print()
    if warnings:
        print("  —— 需要知道的问题 ——")
        for item in warnings:
            print(f"  ⚠ {item}")
    else:
        print("  ✓ 未发现明显的数据完整性问题")
    return 0


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "0%"
    return f"{part / whole * 100:.1f}%"


def _cmd_trends(args) -> int:
    db = Path(args.db)
    if not db.is_file():
        print(f"[错误] 数据库不存在：{db}", file=sys.stderr)
        return 1
    conn = store.connect(db)
    try:
        skill = store.trend_by_skill(conn, limit=args.limit)
        city = store.trend_by_city(conn, limit=args.limit)
    finally:
        conn.close()

    print("=" * 66)
    print("  技能需求趋势（按采集日）")
    print("=" * 66)
    if not skill["dates"]:
        print("  没有带采集时间的快照，无法生成趋势。")
        print("  提示：先运行 `python cli.py import --archive 抓取结果`")
    else:
        width = max((len(s["name"]) for s in skill["series"]), default=8)
        header = "  " + "技能".ljust(width) + "".join(f"{d[5:]:>10}" for d in skill["dates"])
        print(header)
        for series in skill["series"]:
            row = "  " + series["name"].ljust(width)
            row += "".join(f"{v:>10}" for v in series["data"])
            print(row)
        print("  " + "当日岗位数".ljust(width)
              + "".join(f"{v:>10}" for v in skill.get("totals", [])))

    print()
    print("=" * 66)
    print("  城市岗位量趋势（按采集日）")
    print("=" * 66)
    if city["dates"]:
        width = max((len(s["name"]) for s in city["series"]), default=8)
        print("  " + "城市".ljust(width) + "".join(f"{d[5:]:>10}" for d in city["dates"]))
        for series in city["series"]:
            row = "  " + series["name"].ljust(width)
            row += "".join(f"{v:>10}" for v in series["data"])
            print(row)
    else:
        print("  无数据")
    return 0


def _cmd_coverage(args) -> int:
    db = Path(args.db)
    if not db.is_file():
        print(f"[错误] 数据库不存在：{db}", file=sys.stderr)
        return 1
    conn = store.connect(db)
    try:
        rows = conn.execute(
            "SELECT city, COUNT(*) AS n FROM jobs GROUP BY city ORDER BY n DESC"
        ).fetchall()
        families = conn.execute(
            "SELECT category, COUNT(*) AS n FROM jobs GROUP BY category ORDER BY n DESC LIMIT 12"
        ).fetchall()
        top = store.fetch_jobs(conn, limit=args.limit)
        multi = conn.execute(
            "SELECT title, company, snapshot_count, first_seen, last_seen FROM jobs "
            "WHERE snapshot_count > 1 ORDER BY snapshot_count DESC, last_seen DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    finally:
        conn.close()

    print("=" * 66)
    print(f"  城市覆盖（共 {len(rows)} 座）")
    print("=" * 66)
    for row in rows[: args.limit]:
        print(f"  {row['city']:14} {row['n']:>7}")

    print()
    print("=" * 66)
    print("  行业/类别 Top")
    print("=" * 66)
    for row in families:
        print(f"  {row['category']:24} {row['n']:>7}")

    print()
    print("=" * 66)
    print(f"  薪资最高的 {len(top)} 个岗位")
    print("=" * 66)
    for job in top:
        print(f"  {job['title'][:26]:28} {job['city']:8} "
              f"{job['salary_low']:>7.0f}-{job['salary_high']:>7.0f}  {job['company'][:18]}")

    print()
    print("=" * 66)
    print("  多次抓到的岗位（可用来估挂岗时长）")
    print("=" * 66)
    if not multi:
        print("  没有重复抓到的岗位（说明目前没有跨批次重叠的样本）")
    for row in multi:
        print(f"  {row['title'][:26]:28} {row['snapshot_count']} 次  "
              f"{str(row['first_seen'])[:10]} -> {str(row['last_seen'])[:10]}")
    return 0


def _cmd_csv(args) -> int:
    db = Path(args.db)
    if not db.is_file():
        print(f"[错误] 数据库不存在：{db}", file=sys.stderr)
        return 1
    conn = store.connect(db)
    try:
        jobs = store.fetch_jobs(conn, limit=args.limit, order_by="avg_salary DESC")
    finally:
        conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = ["job_id", "title", "company", "city", "salary_low", "salary_high",
               "avg_salary", "education", "work_years", "experience", "category",
               "skills", "first_seen", "last_seen", "snapshot_count"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    print(f"已导出 {len(jobs)} 条 -> {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="岗位分析系统统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python cli.py import --archive 抓取结果\n"
               "  python cli.py verify\n"
               "  python cli.py trends --limit 6\n",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="把抓取归档导入 SQLite")
    p_import.add_argument("--archive", default=str(config.ARCHIVE_DIR),
                          help="归档目录或单个批次文件")
    p_import.add_argument("--db", default=str(config.DEFAULT_DB_FILE))
    p_import.add_argument("--quarantine", default=str(config.DATA_DIR / "salary_quarantine.csv"),
                          help="薪资异常隔离明细的输出路径（默认 data/salary_quarantine.csv）")
    p_import.add_argument("--include-derived", action="store_true",
                          help="同时导入 consolidate_archive.py 的汇总文件"
                               "（boss_jobs_all.json）。它是一次性整合快照，"
                               "会把那部分岗位算到同一个采集日")
    p_import.add_argument("--skip-undated", action="store_true",
                          help="时间戳缺失的批次直接跳过（默认按目录名兜底）")
    p_import.set_defaults(func=_cmd_import)

    p_csv = sub.add_parser("import-csv", help="把标准 CSV 导入 SQLite（无时间维度）")
    p_csv.add_argument("--csv", default=str(config.DEFAULT_DATA_FILE))
    p_csv.add_argument("--db", default=str(config.DEFAULT_DB_FILE))
    p_csv.set_defaults(func=_cmd_import_csv)

    p_verify = sub.add_parser("verify", help="核对数据质量与完整性")
    p_verify.add_argument("--db", default=str(config.DEFAULT_DB_FILE))
    p_verify.set_defaults(func=_cmd_verify)

    p_trends = sub.add_parser("trends", help="打印技能/城市的采集趋势")
    p_trends.add_argument("--db", default=str(config.DEFAULT_DB_FILE))
    p_trends.add_argument("--limit", type=int, default=6)
    p_trends.set_defaults(func=_cmd_trends)

    p_cov = sub.add_parser("coverage", help="城市/行业覆盖与顶部岗位")
    p_cov.add_argument("--db", default=str(config.DEFAULT_DB_FILE))
    p_cov.add_argument("--limit", type=int, default=10)
    p_cov.set_defaults(func=_cmd_coverage)

    p_export = sub.add_parser("csv", help="从库里导出 CSV")
    p_export.add_argument("--db", default=str(config.DEFAULT_DB_FILE))
    p_export.add_argument("--output", default=str(config.DATA_DIR / "jobs_export.csv"))
    p_export.add_argument("--limit", type=int, default=None)
    p_export.set_defaults(func=_cmd_csv)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
