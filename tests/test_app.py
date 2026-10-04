import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - 取决于本地是否装了 pandas
    raise unittest.SkipTest(
        "本模块测试 Flask + pandas 看板，需要先安装 pandas："
        "pip install -r requirements.txt（原始错误：%s）" % exc
    ) from exc

import app
import boss_import


class AppDataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.temp_dir.name) / "jobs.csv"
        self.original_data_path = app.DATA_PATH
        self.original_uploaded_data = app.uploaded_data
        app.DATA_PATH = self.data_path
        app.uploaded_data = None
        app.invalidate_data_cache()

    def tearDown(self):
        app.DATA_PATH = self.original_data_path
        app.uploaded_data = self.original_uploaded_data
        app.invalidate_data_cache()
        self.temp_dir.cleanup()

    @staticmethod
    def sample_rows():
        return pd.DataFrame(
            [
                {
                    "job_name": "数据分析师",
                    "company": "甲公司",
                    "city": "杭州",
                    "salary_low": 10000,
                    "salary_high": 20000,
                    "education": "本科",
                    "work_years": "1-3年",
                    "category": "互联网",
                    "skills": "Python,SQL",
                    "description": "数据分析",
                },
                {
                    "job_name": "数据分析师",
                    "company": "甲公司",
                    "city": "杭州",
                    "salary_low": 10000,
                    "salary_high": 20000,
                    "education": "本科",
                    "work_years": "1-3年",
                    "category": "互联网",
                    "skills": "Python,SQL",
                    "description": "数据分析",
                },
                {
                    "job_name": "数据分析师",
                    "company": "甲公司",
                    "city": "杭州",
                    "salary_low": 3000,
                    "salary_high": 5000,
                    "education": "大专",
                    "work_years": "应届/无经验",
                    "category": "互联网",
                    "skills": "Excel",
                    "description": "",
                },
                {
                    "job_name": "运营专员",
                    "company": "乙公司",
                    "city": "上海",
                    "salary_low": "面议",
                    "salary_high": "",
                    "education": "大专",
                    "work_years": "未知",
                    "category": "运营",
                    "skills": "",
                    "description": "",
                },
            ]
        )

    def write_sample_csv(self):
        self.sample_rows().to_csv(self.data_path, index=False, encoding="utf-8-sig")

    def test_load_data_cleans_and_deduplicates_while_preserving_distinct_salary(self):
        self.write_sample_csv()

        frame = app.load_data()

        self.assertEqual(len(frame), 2)
        self.assertEqual(frame["salary_low"].tolist(), [10000.0, 3000.0])

    def test_load_data_caches_csv_reads_and_returns_independent_frames(self):
        self.write_sample_csv()
        original_read_csv = pd.read_csv

        with patch.object(app.pd, "read_csv", wraps=original_read_csv) as read_csv:
            first = app.load_data()
            first.loc[0, "job_name"] = "caller mutation"
            second = app.load_data()

        self.assertEqual(read_csv.call_count, 1)
        self.assertEqual(second.loc[0, "job_name"], "数据分析师")

    def test_keyword_counts_are_cached_between_requests(self):
        """回归：以前每个请求都对全量描述跑一遍 jieba，补上 JD 后会成为热点。"""
        self.write_sample_csv()
        frame = app.load_data()
        original_tokenize = app.tokenize_text
        calls = []

        def counting(text):
            calls.append(text)
            return original_tokenize(text)

        with patch.object(app, "tokenize_text", side_effect=counting):
            first = app.keyword_counts(frame, 5)
            after_first = len(calls)
            second = app.keyword_counts(frame, 5)

        self.assertEqual(first, second)
        self.assertEqual(len(calls), after_first, "第二次调用不应重复分词")
        self.assertGreater(after_first, 0)

    def test_keyword_counts_recompute_after_data_changes(self):
        self.write_sample_csv()
        frame = app.load_data()
        app.keyword_counts(frame, 5)

        changed = frame.copy()
        changed.loc[0, "description"] = "完全不同的描述文本"
        app.keyword_counts(changed, 5)

        # 内容变了必须重算，不能继续返回旧结果
        cached = app.keyword_counts(changed, 5)
        self.assertTrue(any(item["name"] for item in cached))

    def test_keyword_counts_handles_empty_frame(self):
        self.assertEqual(app.keyword_counts(pd.DataFrame(), 5), [])
        self.assertEqual(app.keyword_counts(None, 5), [])

    def test_load_data_invalidates_cache_when_csv_changes(self):
        self.write_sample_csv()
        initial = app.load_data()
        changed = pd.concat(
            [
                self.sample_rows().iloc[:1],
                pd.DataFrame(
                    [
                        {
                            "job_name": "产品经理",
                            "company": "丙公司",
                            "city": "北京",
                            "salary_low": 20000,
                            "salary_high": 30000,
                            "education": "本科",
                            "work_years": "3-5年",
                            "category": "互联网",
                            "skills": "产品",
                            "description": "",
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        changed.to_csv(self.data_path, index=False, encoding="utf-8-sig")

        reloaded = app.load_data()

        self.assertEqual(len(initial), 2)
        self.assertEqual(len(reloaded), 2)
        self.assertIn("产品经理", reloaded["job_name"].tolist())

    def test_parallel_requests_share_a_single_csv_load(self):
        self.write_sample_csv()
        original_read_csv = pd.read_csv
        call_count = 0
        count_lock = threading.Lock()

        def slow_read_csv(*args, **kwargs):
            nonlocal call_count
            with count_lock:
                call_count += 1
            time.sleep(0.03)
            return original_read_csv(*args, **kwargs)

        with patch.object(app.pd, "read_csv", side_effect=slow_read_csv):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: len(app.load_data()), range(8)))

        self.assertEqual(results, [2] * 8)
        self.assertEqual(call_count, 1)

    def test_missing_data_path_raises_actionable_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "岗位数据文件不存在"):
            app.load_data()

    def test_dashboard_and_filters_return_expected_counts(self):
        self.write_sample_csv()
        client = app.app.test_client()

        dashboard = client.get("/api/dashboard?city=杭州&min_salary=10000")
        jobs = client.get("/api/jobs?city=杭州&min_salary=10000")
        options = client.get("/api/options")

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.get_json()["summary"]["total_jobs"], 1)
        self.assertEqual(len(jobs.get_json()), 1)
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.get_json()["city"], ["杭州"])

    def _write_paged_csv(self, rows: int = 25):
        """写一份行数较多的样本，用于验证分页/排序。"""
        records = []
        for index in range(1, rows + 1):
            records.append({
                "job_name": f"岗位{index:02d}",
                "company": f"公司{index % 3}",
                "city": ["杭州", "上海", "北京"][index % 3],
                "salary_low": 5000 + index * 100,
                "salary_high": 9000 + index * 100,
                "education": "本科",
                "work_years": "1-3年",
                "category": "互联网",
                "skills": "Python",
                "description": "",
            })
        pd.DataFrame(records).to_csv(self.data_path, index=False, encoding="utf-8-sig")

    def test_jobs_page_returns_total_and_slices(self):
        """回归：表格原先写死只取前 10 条，8000+ 条数据看不到第 11 条以后。"""
        self._write_paged_csv(25)
        client = app.app.test_client()

        first = client.get("/api/jobs-page?limit=10&offset=0").get_json()
        second = client.get("/api/jobs-page?limit=10&offset=10").get_json()
        third = client.get("/api/jobs-page?limit=10&offset=20").get_json()

        self.assertEqual(first["total"], 25)
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(second["items"]), 10)
        self.assertEqual(len(third["items"]), 5, "最后一页应只剩 5 条")
        ids = {item["job_id"] for item in first["items"]} | {
            item["job_id"] for item in second["items"]
        } | {item["job_id"] for item in third["items"]}
        self.assertEqual(len(ids), 25, "三页合起来应覆盖全部岗位且不重复")

    def test_jobs_page_sorts_by_salary_both_orders(self):
        self._write_paged_csv(10)
        client = app.app.test_client()

        desc = client.get("/api/jobs-page?limit=3&sort=salary&order=desc").get_json()
        asc = client.get("/api/jobs-page?limit=3&sort=salary&order=asc").get_json()

        desc_values = [item["avg_salary"] for item in desc["items"]]
        asc_values = [item["avg_salary"] for item in asc["items"]]
        self.assertEqual(desc_values, sorted(desc_values, reverse=True))
        self.assertEqual(asc_values, sorted(asc_values))
        self.assertGreater(desc_values[0], asc_values[0])

    def test_jobs_page_rejects_unknown_sort_field(self):
        """排序字段来自请求参数，必须走白名单，非法值安全回退。"""
        self._write_paged_csv(8)
        client = app.app.test_client()

        response = client.get("/api/jobs-page?sort=avg_salary;DROP TABLE jobs&order=desc")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 8)
        values = [item["avg_salary"] for item in payload["items"]]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_jobs_page_clamps_limit_and_offset(self):
        self._write_paged_csv(5)
        client = app.app.test_client()

        zero = client.get("/api/jobs-page?limit=0").get_json()
        huge = client.get("/api/jobs-page?limit=100000").get_json()
        negative = client.get("/api/jobs-page?offset=-10&limit=2").get_json()

        self.assertEqual(zero["limit"], 1, "limit 下限为 1")
        self.assertEqual(huge["limit"], 200, "limit 上限为 200")
        self.assertEqual(negative["offset"], 0, "offset 不能为负")
        self.assertEqual(len(negative["items"]), 2)

    def test_jobs_page_offset_beyond_total_returns_empty(self):
        self._write_paged_csv(5)
        client = app.app.test_client()

        payload = client.get("/api/jobs-page?limit=10&offset=999").get_json()

        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["total"], 5, "越界时 total 仍应是筛选后的总数")

    def test_jobs_page_respects_filters(self):
        self._write_paged_csv(30)
        client = app.app.test_client()

        payload = client.get("/api/jobs-page?city=杭州&limit=100").get_json()

        self.assertGreater(payload["total"], 0)
        self.assertLess(payload["total"], 30)
        self.assertEqual({item["city"] for item in payload["items"]}, {"杭州"})

    def test_empty_csv_returns_an_empty_normalized_frame(self):
        pd.DataFrame(columns=self.sample_rows().columns).to_csv(
            self.data_path, index=False, encoding="utf-8-sig"
        )

        frame = app.load_data()

        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), app.JOB_COLUMNS)

    def test_salary_parser_handles_monthly_daily_and_negotiable_values(self):
        self.assertEqual(boss_import.parse_salary("1.5-2万"), (15000.0, 20000.0))
        self.assertEqual(
            boss_import.parse_salary("300-500元/天"),
            (6525.0, 10875.0),
        )
        self.assertEqual(boss_import.parse_salary("面议"), (None, None))

    def test_import_dedupes_job_id_and_business_key_without_dropping_salary_variants(self):
        def record(job_id, salary_low, title="数据分析师", company="甲公司"):
            return {
                "_job_id": job_id,
                "job_name": title,
                "company": company,
                "city": "杭州",
                "salary_low": salary_low,
                "salary_high": salary_low + 1000,
            }

        result = boss_import.dedupe(
            [
                record("id-1", 10000),
                record("id-1", 12000),
                record("id-2", 10000),
                record("id-3", 20000),
            ]
        )

        self.assertEqual(len(result), 2)
        self.assertEqual([row["salary_low"] for row in result], [10000, 20000])


if __name__ == "__main__":
    unittest.main()
