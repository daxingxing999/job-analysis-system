"""jobanal.parsing 的单元测试：薪资量纲、离群值隔离、字段归一。

这些用例锁定的是评审发现的具体缺陷，尤其是：
- 「500-550元/天」曾被当成年薪/月薪量纲处理
- 「95700-104400」（疑似年包）曾直接进主表并把平均薪资抬高
- company 字段在 boss_import 与 merge_jobs 两个脚本里口径不一致
"""

import unittest

from jobanal import parsing


class SalaryUnitTests(unittest.TestCase):
    """量纲判定：先认 天/时，再认 万/K。"""

    def test_monthly_k_range(self):
        result = parsing.parse_salary_result("15-30K·14薪")
        self.assertTrue(result.ok)
        self.assertEqual((result.low, result.high), (15000.0, 30000.0))
        self.assertEqual(result.unit, "monthly")

    def test_monthly_wan_range(self):
        result = parsing.parse_salary_result("1.5-2万")
        self.assertEqual((result.low, result.high), (15000.0, 20000.0))

    def test_plain_monthly_range(self):
        result = parsing.parse_salary_result("8000-12000")
        self.assertEqual((result.low, result.high), (8000.0, 12000.0))

    def test_daily_rate_is_converted_not_treated_as_monthly(self):
        result = parsing.parse_salary_result("300-500元/天")
        self.assertTrue(result.ok)
        self.assertEqual(result.unit, "daily")
        self.assertEqual((result.low, result.high), (6525.0, 10875.0))

    def test_daily_rate_high_value_is_not_absorbed_by_k_branch(self):
        """回归：曾因先判 K 把「500-550元/天」当月薪，量纲整错。"""
        result = parsing.parse_salary_result("500-550元/天")
        self.assertEqual(result.unit, "daily")
        self.assertGreater(result.low, 10000)

    def test_hourly_rate(self):
        result = parsing.parse_salary_result("50元/时")
        self.assertEqual(result.unit, "hourly")
        self.assertEqual((result.low, result.high), (8700.0, 8700.0))

    def test_reversed_range_is_normalized(self):
        result = parsing.parse_salary_result("30-15K")
        self.assertEqual((result.low, result.high), (15000.0, 30000.0))


class SalaryQuarantineTests(unittest.TestCase):
    """无法可靠确定的薪资必须给出原因，而不是伪造一个值。"""

    def test_negotiable_is_marked(self):
        result = parsing.parse_salary_result("面议")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "negotiable")
        self.assertIsNone(result.low)

    def test_empty_and_dash_are_marked(self):
        for text in (None, "", "-", "  "):
            self.assertEqual(parsing.parse_salary_result(text).reason, "negotiable")

    def test_garbage_is_unparsable(self):
        self.assertEqual(parsing.parse_salary_result("待遇优厚").reason, "unparsable")

    def test_suspected_annual_package_is_rejected(self):
        """「95700-104400」疑似年包，进主表会把平均薪资抬到不真实。"""
        result = parsing.parse_salary_result("95700-104400")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "value_too_high")

    def test_absurd_high_is_rejected(self):
        result = parsing.parse_salary_result("250000-260000")
        self.assertFalse(result.ok)

    def test_tiny_weekly_pay_is_rejected(self):
        result = parsing.parse_salary_result("50-80")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "value_too_low")

    def test_over_wide_spread_is_rejected(self):
        """「500-20000」两端差 40 倍，说明一端解析错了。"""
        result = parsing.parse_salary_result("500-20000")
        self.assertFalse(result.ok)
        self.assertIn(result.reason, {"value_too_low", "spread_too_wide"})

    def test_hourly_and_daily_conflict_is_rejected(self):
        result = parsing.parse_salary_result("50元/时-500元/天")
        self.assertFalse(result.ok)

    def test_avg_helper(self):
        self.assertEqual(parsing.parse_salary_result("10-20K").avg, 15000.0)
        self.assertIsNone(parsing.parse_salary_result("面议").avg)

    def test_loose_helper_keeps_old_contract(self):
        self.assertEqual(parsing.parse_salary_loose("15-30K"), (15000.0, 30000.0))
        self.assertEqual(parsing.parse_salary_loose("面议"), (None, None))


class MonthlyPairTests(unittest.TestCase):
    """标准 CSV 走的是「已经是数字」的路径。"""

    def test_normal_pair(self):
        self.assertEqual(parsing.parse_monthly_pair(10000, 20000), (10000.0, 20000.0, ""))

    def test_missing_both(self):
        self.assertEqual(parsing.parse_monthly_pair("", ""), (None, None, "missing"))

    def test_one_side_missing_is_expanded(self):
        low, high, reason = parsing.parse_monthly_pair(10000, "")
        self.assertEqual((low, high, reason), (10000.0, 10000.0, ""))

    def test_numeric_strings_with_noise(self):
        low, high, _ = parsing.parse_monthly_pair("1万", "2万")
        self.assertEqual((low, high), (10000.0, 20000.0))

    def test_out_of_range_is_flagged(self):
        self.assertEqual(parsing.parse_monthly_pair(100, 200)[2], "monthly_too_low")
        self.assertEqual(parsing.parse_monthly_pair(400000, 500000)[2], "monthly_too_high")

    def test_reversed_pair_is_normalized(self):
        self.assertEqual(parsing.parse_monthly_pair(20000, 10000)[:2], (10000.0, 20000.0))


class FieldNormalizationTests(unittest.TestCase):
    def test_city_takes_first_segment(self):
        self.assertEqual(parsing.parse_city("杭州·滨江区·长河"), "杭州")

    def test_city_handles_empty_segment(self):
        """真实数据里有「上海··」这种尾巴。"""
        self.assertEqual(parsing.parse_city("上海··"), "上海")

    def test_city_fallback(self):
        self.assertEqual(parsing.parse_city("", "杭州"), "杭州")
        self.assertEqual(parsing.parse_city(None), "未知")

    def test_experience_from_tags(self):
        self.assertEqual(parsing.parse_experience("3-5年 | 本科"), "3-5年")
        self.assertEqual(parsing.parse_experience("在校/应届 | 本科"), "应届/无经验")
        self.assertEqual(parsing.parse_experience("经验不限 | 大专"), "应届/无经验")

    def test_experience_falls_back_to_title(self):
        self.assertEqual(parsing.parse_experience("本科", "数据分析实习生"), "应届/无经验")
        self.assertEqual(parsing.parse_experience("本科", "算法工程师"), "未知")

    def test_experience_label_from_years(self):
        self.assertEqual(parsing.experience_label_from_years("3年"), "1-3年")
        self.assertEqual(parsing.experience_label_from_years(6), "5年以上")
        self.assertEqual(parsing.experience_label_from_years("0"), "应届/无经验")
        self.assertEqual(parsing.experience_label_from_years("1-3年"), "1-3年")

    def test_education(self):
        self.assertEqual(parsing.parse_education("1-3年 | 本科"), "本科")
        self.assertEqual(parsing.parse_education("初中及以下"), "初中及以下")
        self.assertEqual(parsing.parse_education(""), "未知")

    def test_skills_split_and_join(self):
        self.assertEqual(parsing.parse_skills("Java | Spring"), "Java,Spring")
        self.assertEqual(parsing.parse_skills("Python、SQL；Excel"), "Python,SQL,Excel")
        self.assertEqual(parsing.parse_skills(""), "")

    def test_description_whitespace_flattened(self):
        self.assertEqual(parsing.clean_description("a\n\n  b \t c"), "a b c")


class CompanyNameTests(unittest.TestCase):
    def test_placeholder_company_is_marked(self):
        """「某知名电子商务公司」这类匿名招聘方要能被识别。"""
        for name in ("某知名电子商务公司", "某大型互联网上市公司", "某基金公司"):
            self.assertTrue(parsing.is_placeholder_company(name), name)

    def test_real_company_is_not_marked(self):
        for name in ("软通动力", "华为技术有限公司", "某某科技有限公司"):
            self.assertFalse(parsing.is_placeholder_company(name), name)

    def test_detail_company_wins(self):
        job = {"boss_name": "某知名电子商务公司"}
        detail = {"company": "某某科技有限公司"}
        self.assertEqual(parsing.display_company(job, detail), "某某科技有限公司")

    def test_placeholder_is_annotated_when_no_detail(self):
        self.assertEqual(
            parsing.display_company({"boss_name": "某知名电子商务公司"}),
            "某知名电子商务公司（匿名招聘）",
        )

    def test_falls_back_to_boss_name(self):
        self.assertEqual(parsing.display_company({"boss_name": "软通动力"}), "软通动力")

    def test_empty_becomes_unknown(self):
        self.assertEqual(parsing.display_company({}), "未知")

    def test_same_call_used_by_all_importers(self):
        """回归：boss_import 与 merge_jobs 曾用不同口径写同一个 CSV。"""
        import boss_import
        import merge_jobs

        self.assertIs(boss_import.display_company, parsing.display_company)
        self.assertIsNotNone(merge_jobs)


if __name__ == "__main__":
    unittest.main()
