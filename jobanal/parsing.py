"""字段解析与清洗规则的唯一入口。

设计约束：**只用标准库**。

原因：仓库里的解析规则此前散落在 `boss_import.py`（用于抓取结果 JSON）与
`app.py`（用于标准 CSV）两处，量纲处理并不一致（例如 `app._parse_salary`
不识别「元/天」「元/时」，会把日薪当成月薪）。本模块把规则收敛到一处，
并保证在没有 pandas 的环境下也能被导入与测试。

对外主要接口
    parse_salary(text) -> SalaryResult     抓取口径的薪资解析（含量纲与剔除判定）
    parse_city / parse_experience / parse_education / parse_skills
    display_company / is_placeholder_company
    experience_label_from_years
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 常量

# 折算基准：日薪 × 21.75 个工作日，时薪 × 8 小时 × 21.75
WORK_DAYS_PER_MONTH = 21.75
WORK_HOURS_PER_DAY = 8

# 月薪合理区间。超出即视为量纲错误或脏数据，隔离而不进主表。
# 上限取 10 万：实测 p99 约 3.5 万，正常月薪极少超过 10 万；而
# 「95700-104400」（疑似年包）与「250000-260000」这类值会直接把看板
# 平均薪资抬到失真。逐条复核入口见 boss_import 输出的 salary_quarantine.csv。
MONTHLY_MIN = 1_000.0
MONTHLY_MAX = 100_000.0
# 同一岗位上下限比例上限：超过说明有一端解析错了（例如「500-20000」）
MAX_SPREAD_RATIO = 50.0
# 日薪 / 时薪的合理区间（折算前）
DAILY_RANGE = (100.0, 5_000.0)
HOURLY_RANGE = (10.0, 500.0)

NO_SALARY_WORDS = {"", "-", "--", "面议", "薪资面议", "面议薪资", "none", "nan", "null"}

# 经验标签 -> 系统分箱。顺序不可调整：先匹配更特殊的「在校/应届」。
EXPERIENCE_MAP = {
    "在校/应届": "应届/无经验",
    "在校生": "应届/无经验",
    "应届生": "应届/无经验",
    "经验不限": "应届/无经验",
    "1年以内": "1-3年",
    "1-3年": "1-3年",
    "3-5年": "3-5年",
    "5-10年": "5年以上",
    "10年以上": "5年以上",
}
EXPERIENCE_PATTERN = re.compile(
    "在校/应届|在校生|应届生|经验不限|1年以内|1-3年|3-5年|5-10年|10年以上"
)
EDUCATION_PATTERN = re.compile(
    "初中及以下|中专/中技|中专|中技|高中及以下|高中|大专|本科|硕士|博士|学历不限"
)
SKILL_SPLIT_PATTERN = re.compile(r"[|｜/、,，;；]+")
EXPERIENCE_ORDER = ["应届/无经验", "1-3年", "3-5年", "5年以上", "未知"]
# 匿名招聘方：BOSS 上猎头/保密职位只给「某知名互联网公司」这类名号。
# 实测 8274 条数据里以「某」开头共 88 种写法，全部形如「某+规模/行业+公司」；
# 反过来，"某某科技有限公司""某某有限公司"这类更可能是真实企业名，不在此列。
PLACEHOLDER_COMPANY_PATTERN = re.compile(
    r"^某(?!某)(?=[\u4e00-\u9fffA-Za-z0-9/、\-·]{1,18}(公司|企业|集团|机构|单位|银行|基金))"
)


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class SalaryResult:
    """一次薪资解析的完整结果，保留原始文本以便离线复核。"""

    low: float | None
    high: float | None
    raw: str
    unit: str          # monthly / daily / hourly / unknown
    reason: str        # 空字符串表示可用；否则为剔除原因

    @property
    def ok(self) -> bool:
        return self.low is not None and self.high is not None and not self.reason

    @property
    def avg(self) -> float | None:
        if self.low is None or self.high is None:
            return None
        return (self.low + self.high) / 2


# ---------------------------------------------------------------- 薪资


def _numbers(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]


def parse_salary_result(text) -> SalaryResult:
    """把 BOSS 明文薪资解析为月薪区间，并判定是否可用。

    支持：15-30K、30-60K·15薪、1.5-2万、8000-12000、300-500元/天、50元/时。
    无法解析或超出合理区间时给出 ``reason``，由调用方隔离而不是静默进主表。
    """
    raw = "" if text is None else str(text).strip()
    if raw.lower() in NO_SALARY_WORDS:
        return SalaryResult(None, None, raw, "unknown", "negotiable")

    value = raw.replace("，", ",").replace("～", "-").replace("~", "-").replace("至", "-")
    value = re.sub(r"[·•]\s*\d+\s*薪", "", value)  # 去掉「·15薪」
    value = re.sub(r"\d+\s*薪", "", value)

    numbers = _numbers(value)
    if not numbers:
        return SalaryResult(None, None, raw, "unknown", "unparsable")

    low_input = numbers[0]
    high_input = numbers[1] if len(numbers) > 1 else numbers[0]

    # ---- 量纲：必须先判 天/时，再判 万/K，否则「500-550元/天」会被误当月薪
    is_hourly = bool(re.search(r"/\s*(小时|时)|每\s*小时|时薪", value))
    is_daily = bool(re.search(r"(天|日)结|/\s*(天|日)|每\s*(天|日)|日薪", value))
    has_wan = "万" in value
    # 只用「K/k 独立跟在数字后」判千：避免把中文词里的字母吃进来
    has_k = bool(re.search(r"\d\s*[kK](?![A-Za-z])", value)) or "千" in value

    if is_hourly and is_daily:
        return SalaryResult(None, None, raw, "unknown", "unit_conflict")

    if is_hourly:
        unit = "hourly"
        factor = WORK_HOURS_PER_DAY * WORK_DAYS_PER_MONTH
        bound = HOURLY_RANGE
    elif is_daily:
        unit = "daily"
        factor = WORK_DAYS_PER_MONTH
        bound = DAILY_RANGE
    else:
        unit = "monthly"
        factor = 10_000.0 if has_wan else (1_000.0 if has_k else 1.0)
        bound = (MONTHLY_MIN / factor, MONTHLY_MAX / factor)

    if low_input < bound[0] or high_input < bound[0]:
        return SalaryResult(None, None, raw, unit, "value_too_low")
    if low_input > bound[1] or high_input > bound[1]:
        return SalaryResult(None, None, raw, unit, "value_too_high")
    if low_input > high_input:
        low_input, high_input = high_input, low_input

    low = round(low_input * factor, 2)
    high = round(high_input * factor, 2)

    if unit == "monthly":
        if high > MONTHLY_MAX:
            return SalaryResult(None, None, raw, unit, "monthly_too_high")
        if low < MONTHLY_MIN:
            return SalaryResult(None, None, raw, unit, "monthly_too_low")
    if low > 0 and high / low > MAX_SPREAD_RATIO:
        return SalaryResult(None, None, raw, unit, "spread_too_wide")

    return SalaryResult(low, high, raw, unit, "")


def parse_salary_loose(text) -> tuple[float | None, float | None]:
    """只要区间、不关心剔除原因的便捷包装。"""
    result = parse_salary_result(text)
    return result.low, result.high


def parse_monthly_pair(low_value, high_value) -> tuple[float | None, float | None, str]:
    """解析**已是数值**的月薪上下限（标准 CSV 走这条路）。

    返回 (low, high, reason)。reason 非空表示这一行不可用。
    """
    def to_float(item):
        if item is None:
            return None
        text = str(item).strip()
        if text.lower() in NO_SALARY_WORDS:
            return None
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group(0))
        # 带「万」的写法要乘回 10000，否则「1万」会被当成 1 元而误判为过低
        if "万" in text:
            number *= 10_000.0
        elif re.search(r"[kK千]", text):
            number *= 1_000.0
        return number

    low = to_float(low_value)
    high = to_float(high_value)
    if low is None and high is None:
        return None, None, "missing"
    if low is None:
        low = high
    if high is None:
        high = low
    if low > high:
        low, high = high, low
    if high < MONTHLY_MIN:
        return None, None, "monthly_too_low"
    if high > MONTHLY_MAX:
        return None, None, "monthly_too_high"
    if low > 0 and high / low > MAX_SPREAD_RATIO:
        return None, None, "spread_too_wide"
    return low, high, ""


# ---------------------------------------------------------------- 其它字段


def parse_city(location, fallback: str = "") -> str:
    """location 形如「杭州·滨江区·长河」，取第一段作为城市。"""
    text = str(location or "").strip()
    if not text:
        return fallback or "未知"
    return text.split("·")[0].strip() or fallback or "未知"


def parse_experience(tags, title: str = "") -> str:
    """解析经验标签；实习/应届岗常只标学历，此时按标题回退。"""
    match = EXPERIENCE_PATTERN.search(str(tags or ""))
    if match:
        return EXPERIENCE_MAP.get(match.group(0), "未知")
    if re.search(r"实习|应届|在校", str(title or "")):
        return "应届/无经验"
    return "未知"


def parse_education(tags) -> str:
    match = EDUCATION_PATTERN.search(str(tags or ""))
    return match.group(0) if match else "未知"


def parse_skills(text) -> str:
    if not text:
        return ""
    parts = SKILL_SPLIT_PATTERN.split(str(text))
    return ",".join(part.strip() for part in parts if part.strip())


def clean_description(text) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def experience_label_from_years(value) -> str:
    """把「3年」「3-5年」或数字统一成系统分箱标签。"""
    text = str(value or "").strip()
    if not text:
        return "未知"
    if text in EXPERIENCE_MAP:
        return EXPERIENCE_MAP[text]
    if text in EXPERIENCE_ORDER:
        return text
    numbers = _numbers(text)
    if not numbers:
        return text if text != "未知" else "未知"
    years = numbers[0]
    if re.search(r"不限", text):
        return "应届/无经验"
    if years <= 0:
        return "应届/无经验"
    if years <= 3:
        return "1-3年"
    if years <= 5:
        return "3-5年"
    return "5年以上"


# ---------------------------------------------------------------- 公司名


def is_placeholder_company(name) -> bool:
    """判断是否为「某知名电子商务公司」这类匿名招聘方。"""
    text = str(name or "").strip()
    return bool(text) and bool(PLACEHOLDER_COMPANY_PATTERN.match(text))


def display_company(job: dict, detail: dict | None = None) -> str:
    """统一公司名口径：详情公司 > 匿名标记 > boss_name。

    此前 `boss_import`（详情优先）与 `merge_jobs`（只用 boss_name）口径不同，
    两个脚本又写同一个输出文件，导致同一份 CSV 的 company 列取决于谁最后运行。
    """
    detail = detail or {}
    name = str(detail.get("company") or "").strip()
    if name:
        return name
    boss_name = str(job.get("boss_name") or "").strip()
    if not boss_name:
        return "未知"
    if is_placeholder_company(boss_name):
        return f"{boss_name}（匿名招聘）"
    return boss_name
