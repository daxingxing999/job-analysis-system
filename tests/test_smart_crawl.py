"""smart_crawl 调度器与工种分类器的回归测试。

锁定的都是会导致「静默丢数据 / 静默跳过」的缺陷：
- 零结果被误判成 saturated，把「城市×族」永久拉黑（C1）
- --max-slices 被当成「每轮补几个」而不是累计上限（C3）
- 已 done 且未触顶的组合变成每轮空转的僵尸（C4）
- 参数填错被读成「任务已收敛」（C15）
- 分类器把 javascript 判成 java、把「测试开发工程师」判成后端（C7）
- 词表里存在族定义没有的幽灵族 mobile（C8）
"""

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import smart_crawl
from jobanal.classify import family_of_title_v2
from jobanal.taxonomy import (
    FAMILY_IDS,
    FAMILY_MATCH_ORDER,
    FAMILY_MATCH_TERMS,
    validate_taxonomy,
)

SMALL_FAMILY = [{"id": "backend", "name": "后端开发", "keywords": ["Python"], "weight": 1.0}]


def make_scheduler(ledger=None, *, max_slices=0, max_attempts=3, max_families=4,
                   city_family_covered=None, cities=None):
    """构造一个最小可用 Scheduler（城市池要足够大，否则会被「同城族数」挡住）。"""
    city_pool = cities or [f"城{i}" for i in range(1, 13)]
    return smart_crawl.Scheduler(
        city_pool, SMALL_FAMILY, ledger if ledger is not None else {"combos": {}, "runs": []},
        Counter({c: 10 for c in city_pool}), Counter({"backend": 10}),
        Counter(),
        total_target=120,
        max_families_per_city=max_families,
        zero_bonus=1.0,
        cooldown_city=0,
        cooldown_family=0,
        max_runs_per_combo=1,
        combo_min_jobs=8,
        city_family_covered=city_family_covered or Counter(),
        nationwide_factor=1.0,
        max_slices=max_slices,
        max_attempts_per_combo=max_attempts,
    )


class SaturatedJudgementTests(unittest.TestCase):
    """C1：零结果不能等于「已饱和」。"""

    def test_saturated_combo_is_blocked_once_slices_done(self):
        ledger = {"combos": {"城1|backend": {"status": "saturated", "runs": 1}}, "runs": []}
        scheduler = make_scheduler(ledger)
        self.assertIsNotNone(scheduler._blocked("城1", "backend"))

    def test_zero_result_is_not_recorded_as_saturated(self):
        """抓取器在「一条都没抓到」时不写文件且退出码 0，旧代码据此判 saturated。

        这里直接调用生产用的 run_scraper_query，而不是复刻它的逻辑。
        """
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "never-created.json"
            with patch.object(smart_crawl, "run_command", lambda *a, **k: 0):
                outcome = smart_crawl.run_scraper_query(
                    python="python", out_file=missing, pages=5, port=9222,
                    keyword="Python", city="杭州", extra_args=[],
                    page_cap=75, baseline=set(),
                )
            self.assertFalse(outcome.ok, "零结果不能算成功")
            self.assertNotEqual(outcome.code, 0, "零结果必须以非零退出码上报")
            self.assertEqual(outcome.raw, 0)
            self.assertEqual(outcome.new_ids, set())
            self.assertFalse(outcome.truncated)

    def test_empty_file_is_also_treated_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "boss_jobs_empty.json"
            empty.write_text('{"jobs": []}', encoding="utf-8")
            with patch.object(smart_crawl, "run_command", lambda *a, **k: 0):
                outcome = smart_crawl.run_scraper_query(
                    python="python", out_file=empty, pages=5, port=9222,
                    keyword="Python", city="杭州", extra_args=[],
                    page_cap=75, baseline=set(),
                )
            self.assertFalse(outcome.ok)
            self.assertNotEqual(outcome.code, 0)

    def test_successful_query_reports_new_ids_and_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "boss_jobs_ok.json"
            jobs = [{"job_id": f"id-{i}", "title": "Python开发"} for i in range(75)]
            out.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            with patch.object(smart_crawl, "run_command", lambda *a, **k: 0):
                outcome = smart_crawl.run_scraper_query(
                    python="python", out_file=out, pages=5, port=9222,
                    keyword="Python", city="杭州", extra_args=[],
                    page_cap=75, baseline={"id-0", "id-1"},
                )
            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.code, 0)
            self.assertEqual(outcome.raw, 75)
            self.assertEqual(len(outcome.new_ids), 73, "已见过的 id 不应算新增")
            self.assertTrue(outcome.truncated, "抓满 page_cap 应判为触顶")

    def test_query_below_cap_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "boss_jobs_short.json"
            out.write_text(json.dumps({"jobs": [{"job_id": "x", "title": "t"}]}),
                           encoding="utf-8")
            with patch.object(smart_crawl, "run_command", lambda *a, **k: 0):
                outcome = smart_crawl.run_scraper_query(
                    python="python", out_file=out, pages=5, port=9222,
                    keyword="Python", city="杭州", extra_args=[],
                    page_cap=75, baseline=set(),
                )
            self.assertTrue(outcome.ok)
            self.assertFalse(outcome.truncated)

    def test_failed_combo_is_retryable_until_attempt_cap(self):
        ledger = {"combos": {"城1|backend": {"status": "failed", "failures": 1}}, "runs": []}
        scheduler = make_scheduler(ledger, max_attempts=3)
        self.assertIsNone(scheduler._blocked("城1", "backend"),
                          "失败 1 次仍应允许重试")

    def test_failed_combo_is_dropped_after_attempt_cap(self):
        ledger = {"combos": {"城1|backend": {"status": "failed", "failures": 3}}, "runs": []}
        scheduler = make_scheduler(ledger, max_attempts=3)
        reason = scheduler._blocked("城1", "backend")
        self.assertIsNotNone(reason)
        self.assertIn("失败", reason)


class MaxSlicesSemanticsTests(unittest.TestCase):
    """C3：--max-slices 是累计上限。"""

    def test_combo_with_no_slices_done_is_still_schedulable(self):
        """max_slices>0 时，已 done 但没补过切片的组合必须还能被排进来。"""
        ledger = {"combos": {"城1|backend": {
            "status": "done", "runs": 1, "truncated": True}}, "runs": []}
        scheduler = make_scheduler(ledger, max_slices=3)
        self.assertIsNone(scheduler._blocked("城1", "backend"))

    def test_combo_is_blocked_only_after_slices_exhausted(self):
        ledger = {
            "combos": {
                "城1|backend": {"status": "done", "runs": 1, "truncated": True},
                "城1|backend|exp-1": {"status": "done"},
                "城1|backend|exp-2": {"status": "done"},
                "城1|backend|exp-3": {"status": "done"},
            },
            "runs": [],
        }
        scheduler = make_scheduler(ledger, max_slices=3)
        self.assertIsNotNone(scheduler._blocked("城1", "backend"),
                             "补满 max_slices 个切片后应判为已抓过")

    def test_remaining_slices_is_cumulative_not_per_run(self):
        """回归：以前 todo 用 [:max_slices]，传 2 就永远只补 2 个。"""
        max_slices = 5
        done_slices = 4
        remaining = max(0, max_slices - done_slices)
        self.assertEqual(remaining, 1)
        todo = list(range(6))[:remaining]
        self.assertEqual(len(todo), 1, "只应再补剩余的 1 个，而不是每轮 5 个")


class ParameterValidationTests(unittest.TestCase):
    """C15：配置错误以前会被打印成「没有待抓取的组合，任务已收敛」。"""

    def test_rejects_zero_and_negative(self):
        for name, value, kwargs in [
            ("--budget", 0, {"minimum": 1}),
            ("--combo-min-jobs", 0, {"minimum": 1}),
            ("--pages", 0, {"minimum": 1, "maximum": 10}),
            ("--pages", 11, {"minimum": 1, "maximum": 10}),
        ]:
            with self.assertRaises(SystemExit):
                smart_crawl._positive_int(name, value, **kwargs)

    def test_accepts_valid_values(self):
        self.assertEqual(smart_crawl._positive_int("--pages", 5, minimum=1, maximum=10), 5)


class LedgerSafetyTests(unittest.TestCase):
    """C11/C12：账本原子写、tmp 名带 pid、重置前备份。"""

    def test_save_ledger_uses_pid_suffixed_temp_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_file = Path(tmp) / "crawl_ledger.json"
            with patch.object(smart_crawl, "LEDGER_FILE", ledger_file):
                smart_crawl.save_ledger({"combos": {"a|b": {"status": "done"}}, "runs": []})
            data = json.loads(ledger_file.read_text(encoding="utf-8"))
            self.assertIn("combos", data)
            self.assertIn("updated_at", data)
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], "临时文件必须已被 rename 掉")

    def test_reset_backs_up_existing_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_file = Path(tmp) / "crawl_ledger.json"
            ledger_file.write_text('{"combos": {"a|b": {"status": "done"}}, "runs": []}',
                                   encoding="utf-8")
            with patch.object(smart_crawl, "LEDGER_FILE", ledger_file):
                backup = smart_crawl.backup_ledger()
            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertIn("bak_", backup.name)

    def test_backup_is_noop_without_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(smart_crawl, "LEDGER_FILE", Path(tmp) / "nope.json"):
                self.assertIsNone(smart_crawl.backup_ledger())


class TaxonomyConsistencyTests(unittest.TestCase):
    """C8：词表与族定义必须一致，否则长出幽灵族、虚增「已铺族」。"""

    def test_taxonomy_has_no_problems(self):
        self.assertEqual(validate_taxonomy(), [])

    def test_every_match_term_family_is_declared(self):
        declared = set(FAMILY_IDS)
        self.assertEqual(set(FAMILY_MATCH_TERMS) - declared, set())

    def test_every_declared_family_is_in_match_order(self):
        self.assertEqual(set(FAMILY_IDS) - set(FAMILY_MATCH_ORDER), set())

    def test_mobile_is_a_real_family_not_a_ghost(self):
        self.assertIn("mobile", FAMILY_IDS)
        self.assertIn("mobile", FAMILY_MATCH_TERMS)


class FamilyClassifierTests(unittest.TestCase):
    """C7：子串匹配导致的误判必须被修掉。"""

    def test_javascript_is_not_java(self):
        self.assertEqual(family_of_title_v2("javascript前端开发"), "frontend")

    def test_java_is_backend(self):
        self.assertEqual(family_of_title_v2("Java开发工程师"), "backend")

    def test_django_is_not_go(self):
        self.assertEqual(family_of_title_v2("Django后端开发"), "backend")

    def test_test_developer_is_test_not_backend(self):
        self.assertEqual(family_of_title_v2("测试开发工程师"), "test")

    def test_mobile_developer_is_mobile_not_backend(self):
        self.assertEqual(family_of_title_v2("移动端开发工程师"), "mobile")
        self.assertEqual(family_of_title_v2("资深安卓开发工程师"), "mobile")

    def test_fullstack_frontend_wins_over_generic_development(self):
        self.assertEqual(family_of_title_v2("全栈开发工程师（偏前端）"), "frontend")

    def test_training_benefit_does_not_make_it_a_training_job(self):
        """「带薪培训」是福利描述，实测曾被吸进教育族。"""
        self.assertEqual(family_of_title_v2("物流专员（可晋升+带薪培训）"), "logistics")
        self.assertEqual(family_of_title_v2("销售岗（无责5000+带薪培训）"), "sales")

    def test_algo_role_beats_generic_development(self):
        self.assertEqual(family_of_title_v2("视觉算法工程开发工程师"), "ai")

    def test_ui_boundary_is_respected(self):
        self.assertEqual(family_of_title_v2("UI设计师"), "design")

    def test_unmatched_title_is_other(self):
        self.assertEqual(family_of_title_v2("店长助理"), "service")
        self.assertEqual(family_of_title_v2(""), "other")

    def test_smart_crawl_delegates_to_new_classifier(self):
        """smart_crawl.family_of_title 必须与分类器同口径。"""
        for title in ("测试开发工程师", "javascript前端开发", "资深安卓开发工程师"):
            self.assertEqual(
                smart_crawl.family_of_title(title), family_of_title_v2(title), title
            )


if __name__ == "__main__":
    unittest.main()
