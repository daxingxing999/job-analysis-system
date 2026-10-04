"""jobanal.store（SQLite 数据层）的测试。

重点覆盖三件在评审里被点名的事：
- 稳定主键：以前 app.py 用行号当 job_id，重新导入就会漂移
- 采集时间维度：CSV 没有时间字段，趋势分析只能靠 postings 表
- 时间戳不许猜：派生汇总文件没有 scraped_at 时不能拿文件 mtime 顶上
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobanal import store


def job(job_id, title="Python开发", salary="15-30K", **extra):
    payload = {
        "job_id": job_id,
        "title": title,
        "salary": salary,
        "location": "杭州·滨江区",
        "tags": "1-3年 | 本科",
        "company_industry": "互联网",
        "skills": "Python | MySQL",
        "boss_name": "某某科技有限公司",
        "job_link": f"https://example.com/{job_id}",
    }
    payload.update(extra)
    return payload


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.db = self.root / "jobs.db"

    def tearDown(self):
        self.tmp.cleanup()

    def write_batch(self, name, jobs, *, scraped_at=None, keyword="Python", folder=None):
        target_dir = self.archive / folder if folder else self.archive
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = {"keyword": keyword, "jobs": jobs}
        if scraped_at:
            payload["scraped_at"] = scraped_at
        (target_dir / name).write_text(json.dumps(payload, ensure_ascii=False),
                                       encoding="utf-8")


class ImportTests(StoreTestCase):
    def test_import_creates_tables_and_rows(self):
        self.write_batch("boss_jobs_a.json",
                         [job("id-1"), job("id-2", title="Java开发")],
                         scraped_at="2026-09-24T10:00:00")
        stats = store.import_archive(self.db, self.archive)

        self.assertEqual(stats.new_jobs, 2)
        self.assertEqual(stats.batches, 1)
        conn = store.connect(self.db)
        try:
            self.assertEqual(store.schema_version(conn), store.SCHEMA_VERSION)
            rows = store.fetch_jobs(conn)
            self.assertEqual(len(rows), 2)
        finally:
            conn.close()

    def test_job_id_is_stable_across_reimport(self):
        """回归：以前用 range(1, n+1) 当 job_id，重跑导入编号就整体漂移。"""
        self.write_batch("boss_jobs_a.json", [job("stable-1")],
                         scraped_at="2026-09-24T10:00:00")
        store.import_archive(self.db, self.archive)
        store.import_archive(self.db, self.archive)

        conn = store.connect(self.db)
        try:
            rows = conn.execute("SELECT job_id FROM jobs").fetchall()
            self.assertEqual([r["job_id"] for r in rows], ["stable-1"])
        finally:
            conn.close()

    def test_second_import_updates_instead_of_duplicating(self):
        self.write_batch("boss_jobs_a.json", [job("id-1", skills="Python")],
                         scraped_at="2026-09-24T10:00:00")
        store.import_archive(self.db, self.archive)

        # 同一岗位第二批补充了更全的字段
        self.write_batch("boss_jobs_b.json",
                         [job("id-1", skills="Python,Spark", education_hint="")],
                         scraped_at="2026-09-28T10:00:00")
        stats = store.import_archive(self.db, self.archive)

        self.assertEqual(stats.new_jobs, 0, "同 job_id 不该新增")
        self.assertEqual(stats.updated_jobs, 1)
        conn = store.connect(self.db)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id='id-1'").fetchone()
            self.assertIn("Spark", row["skills"], "更全的新值应覆盖旧值")
            self.assertEqual(row["snapshot_count"], 2)
            self.assertEqual(row["first_seen"][:10], "2026-09-24")
            self.assertEqual(row["last_seen"][:10], "2026-09-28")
        finally:
            conn.close()

    def test_empty_value_does_not_overwrite_existing(self):
        self.write_batch("boss_jobs_a.json",
                         [job("id-1", skills="Python", company_industry="互联网")],
                         scraped_at="2026-09-24T10:00:00")
        store.import_archive(self.db, self.archive)
        self.write_batch("boss_jobs_b.json",
                         [job("id-1", skills="", company_industry="")],
                         scraped_at="2026-09-28T10:00:00")
        store.import_archive(self.db, self.archive)

        conn = store.connect(self.db)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id='id-1'").fetchone()
            self.assertEqual(row["skills"], "Python")
            self.assertEqual(row["category"], "互联网")
        finally:
            conn.close()

    def test_salary_anomalies_are_quarantined_not_inserted(self):
        self.write_batch("boss_jobs_a.json", [
            job("ok-1", salary="15-30K"),
            job("bad-1", salary="面议"),
            job("bad-2", salary="250000-260000"),
            job("bad-3", salary="500-550元/天"),
        ], scraped_at="2026-09-24T10:00:00")
        quarantine = self.root / "q.csv"
        stats = store.import_archive(self.db, self.archive, quarantine_path=quarantine)

        self.assertEqual(stats.new_jobs, 2, "日薪折算后应入库，面议与极端值应被隔离")
        self.assertEqual(stats.quarantine, 2)
        self.assertTrue(quarantine.exists())
        text = quarantine.read_text(encoding="utf-8-sig")
        self.assertIn("negotiable", text)
        self.assertIn("value_too_high", text)

    def test_anonymous_company_is_flagged(self):
        self.write_batch("boss_jobs_a.json", [
            job("a-1", boss_name="某知名电子商务公司"),
            job("a-2", boss_name="某某科技有限公司"),
        ], scraped_at="2026-09-24T10:00:00")
        store.import_archive(self.db, self.archive)

        conn = store.connect(self.db)
        try:
            rows = {r["job_id"]: r["company_anonymous"]
                    for r in conn.execute("SELECT job_id, company_anonymous FROM jobs")}
            self.assertEqual(rows["a-1"], 1, "匿名招聘方要打标")
            self.assertEqual(rows["a-2"], 0)
        finally:
            conn.close()


class TimestampTests(StoreTestCase):
    """时间戳策略：优先真实字段，绝不拿文件 mtime 顶上。"""

    def test_uses_scraped_at_from_payload(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")],
                         scraped_at="2026-09-24T16:43:37.852210")
        _, scraped_at, _, _ = store.parse_batch(self.archive / "boss_jobs_a.json")
        self.assertTrue(scraped_at.startswith("2026-09-24T16:43"))

    def test_falls_back_to_batch_folder_date(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")],
                         folder="smart_2026-09-24_1641")
        _, scraped_at, _, _ = store.parse_batch(
            self.archive / "smart_2026-09-24_1641" / "boss_jobs_a.json"
        )
        self.assertEqual(scraped_at, "2026-09-24T00:00:00")

    def test_no_timestamp_is_empty_not_file_mtime(self):
        """回归：以前回退到 stat().st_mtime，克隆仓库后变成「今天」，趋势图失真。"""
        self.write_batch("boss_jobs_a.json", [job("id-1")])
        _, scraped_at, _, _ = store.parse_batch(self.archive / "boss_jobs_a.json")
        self.assertEqual(scraped_at, "", "没有可信时间就必须留空，不能猜")

    def test_derived_file_uses_generated_at_only_when_allowed(self):
        self.write_batch("boss_jobs_all.json", [job("id-1")])
        raw = json.loads((self.archive / "boss_jobs_all.json").read_text(encoding="utf-8"))
        raw["generated_at"] = "2026-09-18 17:04:13"
        (self.archive / "boss_jobs_all.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        _, without, _, _ = store.parse_batch(self.archive / "boss_jobs_all.json")
        _, with_derived, _, _ = store.parse_batch(
            self.archive / "boss_jobs_all.json", include_derived=True
        )
        self.assertEqual(without, "", "默认不认 generated_at")
        self.assertTrue(with_derived.startswith("2026-09-18T17:04"))

    def test_derived_file_skipped_by_default(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")],
                         scraped_at="2026-09-24T10:00:00")
        self.write_batch("boss_jobs_all.json", [job("id-2")])
        stats = store.import_archive(self.db, self.archive)

        self.assertEqual(stats.batches, 1, "派生汇总文件默认不导入")
        conn = store.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)
        finally:
            conn.close()

    def test_derived_file_included_when_requested(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")],
                         scraped_at="2026-09-24T10:00:00")
        self.write_batch("boss_jobs_all.json", [job("id-2")])
        raw = json.loads((self.archive / "boss_jobs_all.json").read_text(encoding="utf-8"))
        raw["generated_at"] = "2026-09-18 17:04:13"
        (self.archive / "boss_jobs_all.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        stats = store.import_archive(self.db, self.archive, include_derived=True)
        self.assertEqual(stats.batches, 2)
        self.assertEqual(stats.earliest[:10], "2026-09-18")

    def test_skip_undated_option(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")])  # 无任何时间信息
        stats = store.import_archive(self.db, self.archive, skip_undated=True)
        self.assertEqual(stats.batches, 0)
        self.assertEqual(stats.skipped_undated, ["boss_jobs_a.json"])


class TrendTests(StoreTestCase):
    def test_skill_trend_by_day(self):
        self.write_batch("boss_jobs_a.json",
                         [job("id-1", skills="Python"), job("id-2", skills="Java")],
                         scraped_at="2026-09-24T10:00:00")
        self.write_batch("boss_jobs_b.json",
                         [job("id-3", skills="Python"), job("id-4", skills="Python")],
                         scraped_at="2026-09-28T10:00:00")
        store.import_archive(self.db, self.archive)

        conn = store.connect(self.db)
        try:
            trend = store.trend_by_skill(conn, limit=5)
        finally:
            conn.close()

        self.assertEqual(trend["dates"], ["2026-09-24", "2026-09-28"])
        self.assertEqual(trend["totals"], [2, 2])
        series = {s["name"]: s["data"] for s in trend["series"]}
        self.assertEqual(series["Python"], [1, 2])
        self.assertEqual(series["Java"], [1, 0])

    def test_same_job_in_same_day_batch_counted_once(self):
        """同一岗位在同一天的多个批次里出现时，当日计数不能翻倍。"""
        self.write_batch("boss_jobs_a.json", [job("id-1", skills="Python")],
                         scraped_at="2026-09-24T10:00:00")
        self.write_batch("boss_jobs_b.json", [job("id-1", skills="Python")],
                         scraped_at="2026-09-24T18:00:00")
        store.import_archive(self.db, self.archive)
        conn = store.connect(self.db)
        try:
            trend = store.trend_by_skill(conn, limit=3)
        finally:
            conn.close()
        self.assertEqual(trend["totals"], [1], "同一天同一岗位只算一次")

    def test_empty_db_returns_empty_trend(self):
        conn = store.connect(self.root / "empty.db")
        try:
            store.init_db(conn)
            self.assertEqual(store.trend_by_skill(conn), {"dates": [], "series": []})
        finally:
            conn.close()


class CsvImportTests(StoreTestCase):
    def test_import_csv_with_chinese_headers_and_bom(self):
        csv_path = self.root / "jobs.csv"
        csv_path.write_text(
            "\ufeffjob_name,company,city,salary_low,salary_high,education,work_years,category,skills,description\n"
            "数据分析师,甲公司,杭州,10000,20000,本科,1-3年,互联网,\"Python,SQL\",\n"
            "运营专员,乙公司,上海,面议,,大专,未知,运营,,\n",
            encoding="utf-8",
        )
        stats = store.import_csv(self.db, csv_path)

        self.assertEqual(stats.new_jobs, 1, "「面议」应被隔离")
        self.assertEqual(stats.quarantine, 1)
        conn = store.connect(self.db)
        try:
            row = conn.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(row["title"], "数据分析师")
            self.assertEqual(row["avg_salary"], 15000.0)
            self.assertEqual(row["city"], "杭州")
            self.assertEqual(row["skills"], "Python,SQL")
        finally:
            conn.close()

    def test_csv_reimport_is_idempotent(self):
        csv_path = self.root / "jobs.csv"
        csv_path.write_text(
            "job_name,company,city,salary_low,salary_high,education,work_years,category,skills,description\n"
            "数据分析师,甲公司,杭州,10000,20000,本科,1-3年,互联网,Python,\n",
            encoding="utf-8",
        )
        store.import_csv(self.db, csv_path)
        store.import_csv(self.db, csv_path)
        conn = store.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)
        finally:
            conn.close()


class MigrationTests(StoreTestCase):
    def test_old_database_without_skills_column_is_migrated(self):
        """老库缺 postings.skills 时自动补列，不需要用户删库重建。"""
        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE postings(job_id TEXT, scraped_at TEXT, source_batch TEXT,"
            " keyword TEXT, city TEXT, salary_raw TEXT, raw_json TEXT,"
            " PRIMARY KEY(job_id, scraped_at, source_batch));"
        )
        conn.commit()
        conn.close()

        conn = store.connect(self.db)
        try:
            store.init_db(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(postings)")}
            self.assertIn("skills", columns, "旧库应自动补上 skills 列")
        finally:
            conn.close()


class CoverageTests(StoreTestCase):
    def test_coverage_stats_reports_completeness(self):
        self.write_batch("boss_jobs_a.json", [
            job("id-1", boss_name="某知名电子商务公司"),
            job("id-2", title="没有描述的岗位"),
        ], scraped_at="2026-09-24T10:00:00")
        store.import_archive(self.db, self.archive)

        conn = store.connect(self.db)
        try:
            cov = store.coverage_stats(conn)
        finally:
            conn.close()

        self.assertEqual(cov["jobs"], 2)
        self.assertEqual(cov["postings"], 2)
        self.assertEqual(cov["jobs_with_description"], 0, "抓取器默认不抓 JD")
        self.assertEqual(cov["anonymous_company"], 1)
        self.assertEqual(cov["earliest"][:10], "2026-09-24")

    def test_no_batches_raises_actionable_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "没有找到"):
            store.import_archive(self.db, self.archive)


if __name__ == "__main__":
    unittest.main()
