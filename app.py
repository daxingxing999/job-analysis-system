from __future__ import annotations

import io
import os
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

try:
    import jieba
except ImportError:  # pragma: no cover - exercised when the optional dependency is absent
    jieba = None

from jobanal import store as job_store

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = BASE_DIR / "data" / "boss_jobs.csv"
# 可用环境变量指定其它标准岗位 CSV。
DATA_PATH = Path(os.environ.get("ZOUYE_DATA_FILE") or DEFAULT_DATA_PATH).expanduser()
# 可选：SQLite 数据库（由 cli.py import 生成）。存在时看板多出「采集趋势」，
# CSV 仍是默认数据源，所以没建库的环境行为完全不变。
DB_PATH = Path(os.environ.get("ZOUYE_DB_FILE") or (BASE_DIR / "data" / "jobs.db")).expanduser()
REQUIRED_COLUMNS = (
    "job_name",
    "company",
    "city",
    "salary_low",
    "salary_high",
    "education",
    "work_years",
    "category",
    "skills",
)
JOB_COLUMNS = [
    "job_id",
    "job_name",
    "company",
    "city",
    "salary_low",
    "salary_high",
    "avg_salary",
    "education",
    "work_years",
    "experience",
    "category",
    "skills",
    "description",
]
EXPERIENCE_ORDER = ["应届/无经验", "1-3年", "3-5年", "5年以上", "未知"]
SALARY_LABELS = ["10k以下", "10k-15k", "15k-20k", "20k-25k", "25k-30k", "30k以上"]
SALARY_BINS = [0, 10000, 15000, 20000, 25000, 30000, float("inf")]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
uploaded_data = None
_data_cache_lock = threading.RLock()
_data_cache_path: Path | None = None
_data_cache_signature: tuple[int, int, int, int] | None = None
_data_cache_frame: pd.DataFrame | None = None
# 分词结果缓存：键是帧内容指纹，见 keyword_counts()
_keyword_cache_lock = threading.RLock()
_keyword_cache: dict[tuple, list[dict]] = {}


def _text(value, default="未知") -> str:
    """Return a trimmed, display-safe string for a CSV cell."""
    if pd.isna(value) or str(value).strip() in {"", "nan", "None"}:
        return default
    return str(value).strip()


def _parse_salary(value) -> float | None:
    """Parse 10k-15k, 10000-15000, or a single salary value."""
    if pd.isna(value):
        return None
    text = str(value).strip().lower().replace("，", ",")
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if not numbers:
        return None
    values = [float(number) for number in numbers[:2]]
    if "k" in text or "千" in text:
        values = [number * 1000 for number in values]
    return values[0]


def _salary_pair(row: pd.Series) -> tuple[float | None, float | None]:
    low = _parse_salary(row.get("salary_low"))
    high = _parse_salary(row.get("salary_high"))
    if low is None and high is None:
        salary_text = row.get("salary")
        if not pd.isna(salary_text):
            numbers = re.findall(r"\d+(?:\.\d+)?", str(salary_text).lower())
            multiplier = 1000 if ("k" in str(salary_text).lower() or "千" in str(salary_text)) else 1
            if numbers:
                low = float(numbers[0]) * multiplier
                high = float(numbers[1]) * multiplier if len(numbers) > 1 else low
    elif low is None:
        low = high
    elif high is None:
        high = low
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high


def experience_label(value) -> str:
    try:
        years = float(str(value).replace("年", "").strip())
    except (TypeError, ValueError):
        text = _text(value)
        return text if text != "未知" else "未知"
    if years <= 0:
        return "应届/无经验"
    if years <= 3:
        return "1-3年"
    if years <= 5:
        return "3-5年"
    return "5年以上"


def clean_data(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize the CSV source and discard unusable rows without requiring MySQL."""
    aliases = {
        "职位名称": "job_name",
        "职位": "job_name",
        "公司名称": "company",
        "工作地点": "city",
        "学历要求": "education",
        "经验要求": "work_years",
        "行业": "category",
        "行业类别": "category",
        "industry": "category",
        "岗位类别": "category",
        "技能需求": "skills",
        "技能": "skills",
        "薪资": "salary",
        "薪资范围": "salary",
        "职位描述": "description",
        "job_desc": "description",
        "job_description": "description",
    }
    raw = raw.rename(columns={column: aliases.get(column, column) for column in raw.columns})
    for column in REQUIRED_COLUMNS:
        if column not in raw.columns:
            raw[column] = ""
    if "description" not in raw.columns:
        raw["description"] = ""

    rows = []
    for _, row in raw.iterrows():
        low, high = _salary_pair(row)
        job_name = _text(row.get("job_name"), "")
        if not job_name or low is None or high is None:
            continue
        rows.append(
            {
                "job_name": job_name,
                "company": _text(row.get("company")),
                "city": _text(row.get("city")),
                "salary_low": low,
                "salary_high": high,
                "education": _text(row.get("education")),
                "work_years": _text(row.get("work_years"), "未知"),
                "category": _text(row.get("category")),
                "skills": _text(row.get("skills"), ""),
                "description": _text(row.get("description"), ""),
                # 可选列：只有导入链路生成的 CSV 才有，缺失时留空
                "job_key": _text(row.get("job_key"), ""),
                "job_link": _text(row.get("job_link"), ""),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=JOB_COLUMNS)
    df = df.drop_duplicates(
        subset=["job_name", "company", "city", "salary_low", "salary_high"]
    ).reset_index(drop=True)
    df["salary_low"] = pd.to_numeric(df["salary_low"], errors="coerce")
    df["salary_high"] = pd.to_numeric(df["salary_high"], errors="coerce")
    df["avg_salary"] = (df["salary_low"] + df["salary_high"]) / 2
    df["experience"] = df["work_years"].map(experience_label)
    # job_id 是行号，重新导入就会整体漂移（详情链接随之失效）；job_key 是抓取器
    # 给的稳定标识（md5 前 16 位），有它就优先用它定位。
    df["job_id"] = range(1, len(df) + 1)
    for column in ("job_key", "job_link"):
        if column not in df.columns:
            df[column] = ""
    return df[JOB_COLUMNS]


def load_data() -> pd.DataFrame:
    if uploaded_data is not None:
        return uploaded_data.copy()
    global _data_cache_path, _data_cache_signature, _data_cache_frame

    path = DATA_PATH.expanduser()
    with _data_cache_lock:
        for _ in range(3):
            try:
                stat = path.stat()
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"岗位数据文件不存在：{path}。"
                    "请先运行 boss_import.py 导入抓取归档，"
                    "或通过 ZOUYE_DATA_FILE 指定已有的岗位 CSV。"
                ) from exc

            resolved_path = path.resolve()
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            if (
                _data_cache_frame is not None
                and _data_cache_path == resolved_path
                and _data_cache_signature == signature
            ):
                return _data_cache_frame.copy()

            try:
                raw = pd.read_csv(path, encoding="utf-8-sig")
            except pd.errors.EmptyDataError:
                cleaned = pd.DataFrame(columns=JOB_COLUMNS)
            except UnicodeDecodeError:
                raw = pd.read_csv(path, encoding="gb18030")
                cleaned = clean_data(raw)
            else:
                cleaned = clean_data(raw)

            try:
                after_read = path.stat()
            except FileNotFoundError:
                continue
            after_signature = (
                after_read.st_mtime_ns,
                after_read.st_ctime_ns,
                after_read.st_size,
                after_read.st_ino,
            )
            if after_signature != signature:
                continue

            _data_cache_path = resolved_path
            _data_cache_signature = signature
            _data_cache_frame = cleaned
            return cleaned.copy()

    raise RuntimeError(f"岗位数据文件在读取过程中持续变化，未能安全加载：{path}")


def invalidate_data_cache() -> None:
    global _data_cache_path, _data_cache_signature, _data_cache_frame
    with _data_cache_lock:
        _data_cache_path = None
        _data_cache_signature = None
        _data_cache_frame = None
    with _keyword_cache_lock:
        _keyword_cache.clear()


def read_uploaded_file(file_storage) -> pd.DataFrame:
    filename = (file_storage.filename or "").lower()
    if filename.endswith(".csv"):
        try:
            raw = pd.read_csv(file_storage, encoding="utf-8-sig")
        except UnicodeDecodeError:
            file_storage.stream.seek(0)
            raw = pd.read_csv(file_storage, encoding="gb18030")
    elif filename.endswith((".xlsx", ".xls")):
        raw = pd.read_excel(file_storage)
    else:
        raise ValueError("只支持 CSV、XLSX 或 XLS 文件")
    cleaned = clean_data(raw)
    if cleaned.empty:
        raise ValueError("文件中没有可用岗位数据，请检查岗位名称和薪资字段")
    return cleaned


def normalize_skills(skill_text: str) -> list[str]:
    return [skill.strip() for skill in str(skill_text).split(",") if skill.strip()]


# 分词结果需同时满足：是英文技术词，或至少两个汉字；单字与标点一律丢弃
TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+#.\-]*$|^[\u4e00-\u9fff]{2,}$")
STOP_WORDS = {
    "岗位", "职位", "相关", "工作", "经验", "负责", "要求", "熟悉", "优先", "具备",
    "进行", "使用", "完成", "参与", "以及", "并且", "我们", "公司", "团队", "能力",
    "以上", "以下", "其他", "可以", "能够", "根据", "通过", "对于", "这个", "一种",
    "的", "了", "和", "与", "及", "在", "有", "等", "对", "并", "以", "为", "或",
}


def tokenize_text(text: str) -> list[str]:
    """Use jieba when installed; retain a deterministic regex fallback."""
    text = str(text or "").strip()
    if not text:
        return []
    if jieba is not None:
        words = jieba.lcut(text)
    else:
        words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*|[\u4e00-\u9fff]{2,}", text)
    tokens = []
    for word in words:
        word = word.strip()
        if not word or word in STOP_WORDS or not TOKEN_PATTERN.match(word):
            continue
        tokens.append(word)
    return tokens


def keyword_counts(df: pd.DataFrame, limit: int = 20) -> list[dict]:
    """技能 + 职位描述的高频词。

    结果按「帧内容指纹」缓存：以前每个请求都对全量 description 跑一遍
    jieba，一旦按要求把 JD 抓回来，这里就会变成明显的热点。
    """
    if df is None or df.empty:
        return []
    signature = _frame_signature(df)
    with _keyword_cache_lock:
        cached = _keyword_cache.get(signature)
        if cached is not None:
            return cached[:limit]

    counter = Counter()
    for _, row in df.iterrows():
        counter.update(normalize_skills(row.get("skills", "")))
        counter.update(tokenize_text(row.get("description", "")))
    ranked = [{"name": name, "value": int(value)} for name, value in counter.most_common()]

    with _keyword_cache_lock:
        _keyword_cache.clear()
        _keyword_cache[signature] = ranked
    return ranked[:limit]


def _frame_signature(df: pd.DataFrame) -> tuple:
    """给一帧算一个便宜且足够区分的内容指纹。

    只取「行数 + 首末行的技能/描述」而不是哈希整列：后者每个请求都要扫
    全表并拼接大字符串，省下的 jieba 开销又被它吃回去。
    """
    def cell(index: int, column: str) -> str:
        try:
            return str(df.iloc[index].get(column, "") or "")
        except (IndexError, KeyError):
            return ""

    return (
        len(df),
        cell(0, "skills"), cell(0, "description"),
        cell(len(df) - 1, "skills"), cell(len(df) - 1, "description"),
        str(df.iloc[0].get("city", "")) if len(df) else "",
    )


def make_counts(series: pd.Series) -> list[dict]:
    if series is None or series.empty:
        return []
    counts = series.map(lambda value: _text(value)).value_counts()
    return [{"name": str(key), "value": int(value)} for key, value in counts.items()]


def _contains(series: pd.Series, value: str) -> pd.Series:
    return series.fillna("").astype(str).str.contains(value, case=False, regex=False, na=False)


def apply_filters(df: pd.DataFrame, params) -> pd.DataFrame:
    """Apply exact dimension filters and literal keyword matching safely."""
    for field in ("city", "education", "experience", "category"):
        value = str(params.get(field, "") or "").strip()
        if value and field in df:
            df = df[df[field].astype(str) == value]
    keyword = str(params.get("keyword", "") or "").strip()
    if keyword and not df.empty:
        searchable = (
            _contains(df["job_name"], keyword)
            | _contains(df["company"], keyword)
            | _contains(df["category"], keyword)
            | _contains(df["skills"], keyword)
            | _contains(df["description"], keyword)
        )
        df = df[searchable]
    min_salary = _parse_salary(params.get("min_salary"))
    max_salary = _parse_salary(params.get("max_salary"))
    if min_salary is not None:
        df = df[df["avg_salary"] >= min_salary]
    if max_salary is not None:
        df = df[df["avg_salary"] <= max_salary]
    return df.reset_index(drop=True)


def _limit(default: int = 10, maximum: int = 100) -> int:
    try:
        value = int(request.args.get("limit", default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _offset() -> int:
    try:
        value = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


# 允许的排序字段 -> 实际列名。白名单，避免把请求参数拼进排序表达式。
SORT_COLUMNS = {
    "salary": "avg_salary",
    "salary_low": "salary_low",
    "salary_high": "salary_high",
    "city": "city",
    "company": "company",
    "job_name": "job_name",
    "education": "education",
    "experience": "experience",
    "category": "category",
}


def sort_jobs(df: pd.DataFrame) -> pd.DataFrame:
    """按 sort/order 参数排序（白名单字段，非法值退回按平均薪资降序）。"""
    field = str(request.args.get("sort", "") or "").strip()
    order = str(request.args.get("order", "desc") or "desc").strip().lower()
    column = SORT_COLUMNS.get(field, "avg_salary")
    if column not in df.columns:
        column = "avg_salary"
    ascending = order == "asc"
    if df.empty:
        return df
    return df.sort_values(by=column, ascending=ascending, kind="mergesort").reset_index(drop=True)


def salary_distribution(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return [{"name": label, "value": 0} for label in SALARY_LABELS]
    buckets = pd.cut(df["avg_salary"], bins=SALARY_BINS, labels=SALARY_LABELS, right=False)
    counts = buckets.value_counts().reindex(SALARY_LABELS, fill_value=0)
    return [{"name": label, "value": int(counts.get(label, 0))} for label in SALARY_LABELS]


def job_records(df: pd.DataFrame, limit: int) -> list[dict]:
    if df.empty:
        return []
    result = df.head(limit).copy()
    result = result.where(pd.notna(result), None)
    return result.to_dict(orient="records")


def job_page(df: pd.DataFrame, offset: int, limit: int) -> list[dict]:
    """按 offset/limit 切片并转成可 JSON 化的记录。"""
    if df.empty:
        return []
    result = df.iloc[offset:offset + limit].copy()
    if result.empty:
        return []
    result = result.where(pd.notna(result), None)
    return result.to_dict(orient="records")


def dashboard_payload(df: pd.DataFrame, limit: int) -> dict:
    skills = Counter(
        skill for value in df["skills"] for skill in normalize_skills(value)
    ) if not df.empty else Counter()
    top_skill = skills.most_common(1)[0][0] if skills else "无"
    experience = [
        {"name": name, "value": int((df["experience"] == name).sum())}
        for name in EXPERIENCE_ORDER
        if not df.empty and (df["experience"] == name).any()
    ]
    return {
        "summary": {
            "total_jobs": int(len(df)),
            "avg_salary": round(float(df["avg_salary"].mean()), 2) if not df.empty else 0,
            "city_count": int(df["city"].nunique()) if not df.empty else 0,
            "category_count": int(df["category"].nunique()) if not df.empty else 0,
            "top_skill": top_skill,
            "text_analyzer": "jieba" if jieba is not None else "规则分词降级",
        },
        "salary": salary_distribution(df),
        "education": make_counts(df["education"]),
        "experience": experience,
        "city": make_counts(df["city"])[:10],
        "category": make_counts(df["category"])[:10],
        "skills": [
            {"name": name, "value": int(value)} for name, value in skills.most_common(12)
        ],
        "keywords": keyword_counts(df, 20),
        "top_companies": make_counts(df["company"])[:8],
        "jobs": job_records(df, limit),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data-source")
def api_data_source():
    return jsonify(
        {
            "source": "上传文件" if uploaded_data is not None else DATA_PATH.name,
            "count": int(len(load_data())),
        }
    )


@app.route("/api/upload", methods=["POST"])
def api_upload():
    global uploaded_data
    file_storage = request.files.get("file")
    if file_storage is None or not file_storage.filename:
        return jsonify({"error": "请选择 CSV 或 Excel 文件"}), 400
    try:
        cleaned = read_uploaded_file(file_storage)
    except (ValueError, pd.errors.ParserError, ImportError) as exc:
        return jsonify({"error": str(exc)}), 400
    uploaded_data = cleaned
    invalidate_data_cache()
    return jsonify({"source": "上传文件", "count": int(len(uploaded_data))})


@app.route("/api/reset-data", methods=["POST"])
def api_reset_data():
    global uploaded_data
    uploaded_data = None
    invalidate_data_cache()
    return jsonify({"source": DATA_PATH.name, "count": int(len(load_data()))})


@app.route("/api/options")
def api_options():
    df = load_data()
    return jsonify(
        {
            "city": sorted(df["city"].unique().tolist()) if not df.empty else [],
            "education": sorted(df["education"].unique().tolist()) if not df.empty else [],
            "experience": [item for item in EXPERIENCE_ORDER if item in set(df["experience"])]
            if not df.empty
            else [],
            "category": sorted(df["category"].unique().tolist()) if not df.empty else [],
        }
    )


@app.route("/api/summary")
def api_summary():
    return jsonify(dashboard_payload(apply_filters(load_data(), request.args), 0)["summary"])


@app.route("/api/salary-distribution")
def api_salary_distribution():
    return jsonify(salary_distribution(apply_filters(load_data(), request.args)))


@app.route("/api/education")
def api_education():
    return jsonify(make_counts(apply_filters(load_data(), request.args)["education"]))


@app.route("/api/experience")
def api_experience():
    df = apply_filters(load_data(), request.args)
    return jsonify(
        [
            {"name": name, "value": int((df["experience"] == name).sum())}
            for name in EXPERIENCE_ORDER
            if not df.empty and (df["experience"] == name).any()
        ]
    )


@app.route("/api/category-distribution")
def api_category_distribution():
    return jsonify(make_counts(apply_filters(load_data(), request.args)["category"])[:10])


@app.route("/api/city-distribution")
def api_city_distribution():
    return jsonify(make_counts(apply_filters(load_data(), request.args)["city"])[:10])


@app.route("/api/skills")
def api_skills():
    return jsonify(keyword_counts(apply_filters(load_data(), request.args), 12))


@app.route("/api/top-companies")
def api_top_companies():
    return jsonify(make_counts(apply_filters(load_data(), request.args)["company"])[:8])


@app.route("/api/jobs")
def api_jobs():
    return jsonify(job_records(apply_filters(load_data(), request.args), _limit(12)))


@app.route("/api/jobs-page")
def api_jobs_page():
    """分页 + 排序的岗位列表，供看板表格使用。

    原先表格固定只取前 10 条，8000+ 条数据实际看不到第 11 条以后。
    返回 total 以便前端渲染页码；字段保持与 /api/jobs 一致。
    """
    filtered = sort_jobs(apply_filters(load_data(), request.args))
    offset = _offset()
    limit = _limit(default=20, maximum=200)
    return jsonify({
        "items": job_page(filtered, offset, limit),
        "total": int(len(filtered)),
        "offset": offset,
        "limit": limit,
        "sort": str(request.args.get("sort", "") or "salary"),
        "order": str(request.args.get("order", "") or "desc"),
    })


@app.route("/api/jobs/<int:job_id>")
def api_job_detail(job_id: int):
    df = load_data()
    match = df[df["job_id"] == job_id]
    if match.empty:
        return jsonify({"error": "岗位不存在"}), 404
    return jsonify(job_records(match, 1)[0])


@app.route("/api/jobs/by-key/<job_key>")
def api_job_detail_by_key(job_key: str):
    """按稳定标识（抓取器的 job_id / CSV 里的 job_key）查详情。

    行号式的 /api/jobs/<int> 在每次重新导入后都会漂移，分享出去就失效；
    这个接口用跨批次稳定的标识定位，是导入链路生成的数据源的首选。
    """
    df = load_data()
    if "job_key" not in df.columns:
        return jsonify({"error": "当前数据源没有稳定标识字段（job_key）"}), 404
    match = df[df["job_key"].astype(str) == str(job_key)]
    if match.empty:
        return jsonify({"error": "岗位不存在"}), 404
    return jsonify(job_records(match, 1)[0])


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(dashboard_payload(apply_filters(load_data(), request.args), _limit(10)))


@app.route("/api/trends")
def api_trends():
    """采集趋势（技能 / 城市随采集日的变化）。

    数据来自 SQLite（postings 表按 scraped_at 聚合）—— 标准 CSV 里没有
    采集时间字段，所以这一步只能由 `python cli.py import` 落库后提供。
    数据库不存在时返回 available=False，前端据此隐藏图表而不是报错。
    """
    limit = _limit(default=5, maximum=10)
    if not DB_PATH.is_file():
        return jsonify({
            "available": False,
            "reason": "尚未生成 SQLite 数据库，趋势不可用",
            "hint": "python cli.py import --archive 抓取结果",
        })

    conn = None
    try:
        conn = job_store.connect(DB_PATH)
        skill = job_store.trend_by_skill(conn, limit=limit)
        city = job_store.trend_by_city(conn, limit=limit)
        coverage = job_store.coverage_stats(conn)
    except sqlite3.Error as exc:
        return jsonify({"available": False, "reason": f"读取数据库失败：{exc}"}), 503
    finally:
        if conn is not None:
            conn.close()

    return jsonify({
        "available": bool(skill["dates"]),
        "reason": "" if skill["dates"] else "数据库里没有带采集时间的快照",
        "dates": skill["dates"],
        "totals": skill.get("totals", []),
        "skills": skill["series"],
        "cities": city["series"],
        "coverage": coverage,
    })


def export_frame() -> pd.DataFrame:
    df = apply_filters(load_data(), request.args)
    columns = [
        "job_id", "job_name", "company", "city", "salary_low", "salary_high",
        "avg_salary", "education", "experience", "category", "skills", "description",
        "job_key", "job_link",
    ]
    # 旧数据源没有 job_key / job_link 列，避免导出时报 KeyError
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    return df[columns].rename(
        columns={
            "job_id": "岗位编号",
            "job_name": "岗位名称",
            "company": "公司",
            "city": "城市",
            "salary_low": "最低薪资(元/月)",
            "salary_high": "最高薪资(元/月)",
            "avg_salary": "平均薪资(元/月)",
            "education": "学历",
            "experience": "经验",
            "category": "行业/类别",
            "skills": "技能",
            "description": "职位描述",
            "job_key": "稳定标识",
            "job_link": "原始链接",
        }
    )


@app.route("/export/csv")
def export_csv():
    output = io.BytesIO()
    output.write(export_frame().to_csv(index=False).encode("utf-8-sig"))
    output.seek(0)
    return send_file(output, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name="岗位分析结果.csv")


@app.route("/export/excel")
def export_excel():
    output = io.BytesIO()
    try:
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            export_frame().to_excel(writer, index=False, sheet_name="岗位分析")
    except ImportError:
        return jsonify({"error": "Excel 导出需要 openpyxl，请先执行 pip install -r requirements.txt"}), 503
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="岗位分析结果.xlsx",
    )


if __name__ == "__main__":
    if not DATA_PATH.is_file():
        raise SystemExit(
            f"岗位数据文件不存在：{DATA_PATH}\n"
            "请先运行 boss_import.py 导入抓取归档，"
            "或设置 ZOUYE_DATA_FILE 指向已有的岗位 CSV。"
        )
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
    )
