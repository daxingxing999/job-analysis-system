import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import merge_jobs


class MergeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.archive = Path(self.temp_dir.name) / "archive"
        self.archive.mkdir()
        self.original_archive = merge_jobs.ARCHIVE
        merge_jobs.ARCHIVE = self.archive

    def tearDown(self):
        merge_jobs.ARCHIVE = self.original_archive
        self.temp_dir.cleanup()

    def write_batch(self, name, value):
        path = self.archive / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_load_all_groups_valid_records_by_job_id(self):
        self.write_batch(
            "boss_jobs_first.json",
            {
                "keyword": "Python",
                "city": "杭州",
                "jobs": [{"job_id": "job-1", "title": "数据分析师"}],
            },
        )
        self.write_batch(
            "boss_jobs_second.json",
            {
                "keyword": "数据分析",
                "city": "上海",
                "jobs": [{"job_id": "job-1", "skills": "SQL"}],
            },
        )

        groups, keywords, cities = merge_jobs.load_all()

        self.assertEqual(len(groups["job-1"]), 2)
        self.assertEqual(keywords["Python"], 1)
        self.assertEqual(cities["上海"], 1)

    def test_load_all_reports_corrupt_archive_instead_of_silently_skipping(self):
        bad_file = self.archive / "boss_jobs_corrupt.json"
        bad_file.write_text("{ invalid json", encoding="utf-8")

        with self.assertRaises(merge_jobs.ArchiveLoadError) as context:
            merge_jobs.load_all()

        self.assertIn(str(bad_file), str(context.exception))
        self.assertIn("已停止合并", str(context.exception))

    def test_load_all_rejects_invalid_payload_shapes(self):
        bad_file = self.write_batch("boss_jobs_bad_shape.json", {"jobs": "not-a-list"})

        with self.assertRaisesRegex(merge_jobs.ArchiveLoadError, str(bad_file)):
            merge_jobs.load_all()

    def test_load_all_rejects_non_object_job_rows(self):
        bad_file = self.write_batch("boss_jobs_bad_row.json", {"jobs": [None]})

        with self.assertRaisesRegex(merge_jobs.ArchiveLoadError, str(bad_file)):
            merge_jobs.load_all()

    def test_load_all_fails_when_no_batches_exist(self):
        with self.assertRaisesRegex(merge_jobs.ArchiveLoadError, "没有找到"):
            merge_jobs.load_all()

    def test_main_does_not_write_output_when_any_archive_is_invalid(self):
        self.write_batch(
            "boss_jobs_valid.json",
            {"jobs": [{"job_id": "job-1", "title": "数据分析师"}]},
        )
        (self.archive / "boss_jobs_broken.json").write_text("{", encoding="utf-8")
        output = self.archive / "merged.csv"
        stderr = io.StringIO()

        with patch.object(sys, "argv", ["merge_jobs.py", "--output", str(output)]):
            with contextlib.redirect_stderr(stderr):
                result = merge_jobs.main()

        self.assertEqual(result, 1)
        self.assertFalse(output.exists())
        self.assertIn("boss_jobs_broken.json", stderr.getvalue())
        self.assertIn("未写入 CSV", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
