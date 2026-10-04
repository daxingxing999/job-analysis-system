"""jobanal.archive（归档单遍分析）的测试。

覆盖原先三个一次性脚本的行为，重点包括：
- 单遍扫描能否同时支撑收益率、重复度、字段差异三类分析
- 派生汇总文件默认排除、显式包含
- 无 job_id 的记录不会被静默算进唯一岗位
"""

import json
import tempfile
import unittest
from pathlib import Path

from jobanal import archive


def job(job_id, title="Python开发", salary="15-30K", **extra):
    payload = {
        "job_id": job_id,
        "title": title,
        "salary": salary,
        "location": "杭州·滨江区",
        "boss_name": "某某科技有限公司",
        "company_industry": "互联网",
        "skills": "Python",
        "tags": "1-3年 | 本科",
    }
    payload.update(extra)
    return payload


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "archive"
        self.archive.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_batch(self, name, jobs, *, city="杭州", keyword="Python", folder=None):
        target = self.archive / folder if folder else self.archive
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(
            json.dumps({"city": city, "keyword": keyword, "jobs": jobs}, ensure_ascii=False),
            encoding="utf-8",
        )


class ScanTests(ArchiveTestCase):
    def test_single_scan_collects_everything(self):
        self.write_batch("boss_jobs_a.json", [job("id-1"), job("id-2")],
                         city="杭州", keyword="Python")
        self.write_batch("boss_jobs_b.json", [job("id-3")],
                         city="上海", keyword="Java")

        scan = archive.scan_archive(self.archive)

        self.assertEqual(scan.batches, 2)
        self.assertEqual(scan.raw_records, 3)
        self.assertEqual(scan.unique_jobs, 3)
        self.assertEqual(scan.cities, {"杭州", "上海"})
        self.assertEqual(scan.keywords, {"Python", "Java"})

    def test_duplicate_job_across_batches_is_counted_once(self):
        self.write_batch("boss_jobs_a.json", [job("id-1", skills="Python")],
                         city="杭州", keyword="Python")
        self.write_batch("boss_jobs_b.json", [job("id-1", skills="Python,SQL")],
                         city="杭州", keyword="Java")

        scan = archive.scan_archive(self.archive)

        self.assertEqual(scan.raw_records, 2)
        self.assertEqual(scan.unique_jobs, 1)
        self.assertEqual(scan.dedupe_rate, 50.0)

    def test_records_without_job_id_are_counted_separately(self):
        """没有 job_id 的记录不能算进唯一岗位，否则会高估覆盖。"""
        self.write_batch("boss_jobs_a.json", [job(""), job("id-2")])
        scan = archive.scan_archive(self.archive)

        self.assertEqual(scan.records_without_job_id, 1)
        self.assertEqual(scan.unique_jobs, 1)
        self.assertEqual(scan.raw_records, 2)

    def test_negotiable_salary_is_counted_as_missing(self):
        self.write_batch("boss_jobs_a.json", [
            job("id-1", salary="面议"),
            job("id-2", salary=""),
            job("id-3", salary="15-30K"),
        ])
        scan = archive.scan_archive(self.archive)
        self.assertEqual(scan.salary_missing, 2)

    def test_unreadable_batch_is_reported_not_swallowed(self):
        self.write_batch("boss_jobs_ok.json", [job("id-1")])
        (self.archive / "boss_jobs_broken.json").write_text("{ bad", encoding="utf-8")

        scan = archive.scan_archive(self.archive)

        self.assertEqual(scan.batches, 1)
        self.assertEqual(len(scan.unreadable), 1)
        self.assertIn("boss_jobs_broken.json", scan.unreadable[0])

    def test_derived_file_excluded_by_default(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")])
        self.write_batch("boss_jobs_all.json", [job("id-2"), job("id-3")])

        default = archive.scan_archive(self.archive)
        included = archive.scan_archive(self.archive, include_derived=True)

        self.assertEqual(default.unique_jobs, 1)
        self.assertEqual(included.unique_jobs, 3)

    def test_business_key_counts_even_without_job_id(self):
        """没有 job_id 时业务主键是唯一兜底口径，必须仍在统计。"""
        self.write_batch("boss_jobs_a.json", [
            job("", title="Python开发"),
            job("", title="Java开发"),
        ])
        scan = archive.scan_archive(self.archive)
        self.assertEqual(len(scan.business_keys), 2)
        self.assertEqual(scan.unique_jobs, 0, "没有 job_id 就不能算进唯一岗位")

    def test_identical_records_share_one_business_key(self):
        """除 job_id 外完全相同的两条记录，业务主键应视为同一个。"""
        self.write_batch("boss_jobs_a.json", [job(""), job("")])
        scan = archive.scan_archive(self.archive)
        self.assertEqual(len(scan.business_keys), 1)
        self.assertEqual(scan.business_keys.most_common(1)[0][1], 2)


class YieldTests(ArchiveTestCase):
    def test_yield_by_city_and_keyword(self):
        self.write_batch("boss_jobs_a.json", [job("id-1"), job("id-2")],
                         city="杭州", keyword="Python")
        self.write_batch("boss_jobs_b.json", [job("id-2"), job("id-3")],
                         city="杭州", keyword="Java")

        scan = archive.scan_archive(self.archive)
        by_city = {row.name: row for row in archive.yield_by_city(scan)}
        by_kw = {row.name: row for row in archive.yield_by_keyword(scan)}

        self.assertEqual(by_city["杭州"].records, 4, "同一岗位被两个批次抓到要算 2 条记录")
        self.assertEqual(by_city["杭州"].unique_jobs, 3)
        self.assertEqual(by_city["杭州"].rate, 75.0)
        self.assertEqual(by_kw["Python"].unique_jobs, 2)
        self.assertEqual(by_kw["Java"].unique_jobs, 2)

    def test_cross_tagging_reports_repeat_ratio(self):
        self.write_batch("boss_jobs_a.json", [job("id-1"), job("id-2")],
                         city="杭州", keyword="Python")
        self.write_batch("boss_jobs_b.json", [job("id-1")],
                         city="杭州", keyword="Java")

        scan = archive.scan_archive(self.archive)
        cross = archive.cross_tagging(scan)

        self.assertEqual(cross["total_jobs"], 2)
        self.assertEqual(cross["multi_keyword_jobs"], 1)
        self.assertEqual(cross["multi_ratio"], 50.0)
        self.assertAlmostEqual(cross["avg_dimensions"], 1.5)

    def test_empty_scan_returns_empty_structures(self):
        scan = archive.scan_archive(self.archive)
        self.assertEqual(scan.batches, 0)
        self.assertEqual(archive.yield_by_city(scan), [])
        self.assertEqual(archive.duplicate_distribution(scan), [])
        self.assertEqual(archive.cross_tagging(scan)["total_jobs"], 0)


class DuplicateTests(ArchiveTestCase):
    def test_duplicate_distribution(self):
        self.write_batch("boss_jobs_a.json", [job("id-1"), job("id-2"), job("id-3")])
        self.write_batch("boss_jobs_b.json", [job("id-1"), job("id-2")])
        self.write_batch("boss_jobs_c.json", [job("id-1")])

        scan = archive.scan_archive(self.archive)
        dist = dict(archive.duplicate_distribution(scan))

        self.assertEqual(dist[1], 1, "id-3 只出现一次")
        self.assertEqual(dist[2], 1, "id-2 出现两次")
        self.assertEqual(dist[3], 1, "id-1 出现三次")

    def test_field_differences_detects_varying_fields(self):
        self.write_batch("boss_jobs_a.json", [job("id-1", skills="Python")])
        self.write_batch("boss_jobs_b.json", [job("id-1", skills="Python,SQL")])

        scan = archive.scan_archive(self.archive)
        diffs, samples = archive.field_differences(scan)

        self.assertEqual(diffs["skills"], 1)
        self.assertEqual(len(samples), 1)
        self.assertIn("skills", samples[0][1])

    def test_no_differences_when_snapshots_identical(self):
        self.write_batch("boss_jobs_a.json", [job("id-1")])
        self.write_batch("boss_jobs_b.json", [job("id-1")])
        scan = archive.scan_archive(self.archive)
        diffs, samples = archive.field_differences(scan)
        self.assertEqual(len(diffs), 0)
        self.assertEqual(samples, [])


class ReportTests(ArchiveTestCase):
    def test_report_contains_key_sections(self):
        self.write_batch("boss_jobs_a.json", [job("id-1"), job("id-2")],
                         city="杭州", keyword="Python")
        scan = archive.scan_archive(self.archive)
        report = archive.build_report(scan)

        for section in ("## 一、总览", "## 二、换关键词", "## 三、按城市",
                        "## 四、按关键词", "## 五、重复分布"):
            self.assertIn(section, report)
        self.assertIn("杭州", report)
        self.assertIn("Python", report)

    def test_report_mentions_unreadable_batches(self):
        self.write_batch("boss_jobs_ok.json", [job("id-1")])
        (self.archive / "boss_jobs_bad.json").write_text("{", encoding="utf-8")
        scan = archive.scan_archive(self.archive)
        report = archive.build_report(scan)
        self.assertIn("无法读取", report)
        self.assertIn("boss_jobs_bad.json", report)


if __name__ == "__main__":
    unittest.main()
