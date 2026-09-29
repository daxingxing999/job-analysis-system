"""批量抓取：多关键词 × 多城市，把样本量堆上去。

单次抓取最多 10 页（约 150 条），所以要多堆数据只能靠"关键词 × 城市"的组合数。
本脚本按顺序跑完所有组合，支持断点续跑（已完成的组合记录在 data/.crawl_progress.json）。

浏览器只启动一次、全程复用同一个 CDP 端口——浏览器进程会随命令结束而退出，
所以启动、循环抓取、归档、导入必须在同一次执行里完成。

用法
    py batch_crawl.py --max-combos 8                  # 先跑 8 个组合试试
    py batch_crawl.py --cities 杭州,上海 --max-combos 20
    py batch_crawl.py --status                        # 查看进度
    py batch_crawl.py --reset                         # 清空进度重来

注意
    每个组合约 2-4 分钟（10 页 + 翻页等待 12-22 秒/页）。请先小批量试跑确认稳定，
    再加大 --max-combos。长时间高频抓取存在风控风险，请控制节奏。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crawl_and_import import (  # noqa: E402
    ARCHIVE_DIR,
    BASE_DIR,
    JOB_RESULT_DIR,
    SCRAPER_SCRIPT,
    archive_new_results,
    build_env,
    snapshot,
)

# 覆盖各行业/专业的关键词，可按需要增减
DEFAULT_KEYWORDS = [
    "Python", "Java", "前端开发", "算法工程师", "测试工程师", "运维工程师",
    "数据分析师", "产品经理", "UI设计师", "市场营销", "新媒体运营",
    "人力资源", "财务会计", "销售经理", "教师",
]
DEFAULT_CITIES = [
    "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安",
]

PROGRESS_FILE = BASE_DIR / "data" / ".crawl_progress.json"
DEFAULT_OUTPUT = BASE_DIR / "data" / "boss_jobs.csv"


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"done": [], "failed": {}, "stats": {}}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def combo_key(keyword: str, city: str) -> str:
    return f"{keyword}|{city}"


def cdp_alive(port: int) -> bool:
    import urllib.request

    env_backup = {}
    try:
        import os

        env_backup["NO_PROXY"] = os.environ.get("NO_PROXY", "")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3)
        return True
    except Exception:
        return False
    finally:
        import os

        if env_backup.get("NO_PROXY") is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = env_backup["NO_PROXY"]


def run(command: list[str], timeout: int = 900) -> int:
    print("$ " + " ".join(str(part) for part in command), flush=True)
    try:
        result = subprocess.run(
            command, cwd=str(BASE_DIR), env=build_env(), timeout=timeout
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"  [超时] 超过 {timeout}s，跳过该组合", flush=True)
        return -1


def start_browser(python: str, port: int, browser: str) -> bool:
    flag = "--setup-edge" if browser == "edge" else "--setup-chrome"
    print("启动专用浏览器并等待登录…", flush=True)
    code = run(
        [python, str(SCRAPER_SCRIPT), flag, "--cdp-port", str(port), "--login-timeout", "300"]
    )
    return code == 0 and cdp_alive(port)


def do_import(python: str, output: str) -> None:
    archive_new_results({})  # 空快照 = 归档 job-result 下所有尚未归档的文件
    run([python, str(BASE_DIR / "boss_import.py"), "--source", str(ARCHIVE_DIR), "--output", output])
    try:
        import csv

        with open(output, encoding="utf-8-sig") as handle:
            print(f"  当前 CSV 累计 {sum(1 for _ in handle) - 1} 条", flush=True)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量抓取 BOSS 直聘岗位数据")
    parser.add_argument("--keywords", help="关键词，逗号分隔（默认覆盖 15 个行业）")
    parser.add_argument("--cities", help="城市，逗号分隔（默认 8 个城市）")
    parser.add_argument("--pages", type=int, default=10, help="每个组合抓取页数，上限 10")
    parser.add_argument("--max-combos", type=int, default=8, help="本次最多跑多少个组合")
    parser.add_argument("--import-every", type=int, default=4, help="每多少个组合导入一次")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--status", action="store_true", help="查看进度后退出")
    parser.add_argument("--reset", action="store_true", help="清空进度后退出")
    args = parser.parse_args(argv)

    progress = load_progress()

    if args.reset:
        save_progress({"done": [], "failed": {}, "stats": {}})
        print("进度已清空")
        return 0

    if args.status:
        print(f"已完成组合：{len(progress['done'])}")
        print(f"失败组合：{len(progress['failed'])}")
        for key, count in sorted(progress["stats"].items(), key=lambda x: -x[1]):
            print(f"  {key}: {count} 条")
        return 0

    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()] or DEFAULT_KEYWORDS
    cities = [c.strip() for c in (args.cities or "").split(",") if c.strip()] or DEFAULT_CITIES

    # 对角线交错排列：前 N 个组合就覆盖到不同的关键词和城市，
    # 避免小批量跑完只拿到同一个城市的样本
    combos = []
    seen_combo = set()
    for offset in range(max(len(keywords), len(cities))):
        for index, keyword in enumerate(keywords):
            city = cities[(offset + index) % len(cities)]
            if (keyword, city) not in seen_combo:
                seen_combo.add((keyword, city))
                combos.append((keyword, city))
    pending = [(k, c) for k, c in combos if combo_key(k, c) not in progress["done"]]
    pending = pending[: args.max_combos]

    if not pending:
        print("没有待抓取的组合了（全部完成或已达上限）")
        return 0

    total_possible = len(combos)
    print(f"关键词 {len(keywords)} 个 × 城市 {len(cities)} 个 = {total_possible} 个组合")
    print(f"已完成 {len(progress['done'])} 个，本次计划抓取 {len(pending)} 个\n")

    python = args.python
    if not cdp_alive(args.cdp_port):
        if not start_browser(python, args.cdp_port, args.browser):
            print("[错误] 浏览器启动失败或登录态不可用", file=sys.stderr)
            return 1
    else:
        print("浏览器已在运行，复用现有 CDP 连接\n")

    started = time.time()
    consecutive_failures = 0
    for index, (keyword, city) in enumerate(pending, 1):
        print(f"\n===== [{index}/{len(pending)}] {keyword} @ {city} =====", flush=True)
        before = snapshot(JOB_RESULT_DIR)
        code = run(
            [
                python, str(SCRAPER_SCRIPT),
                "--keyword", keyword,
                "--city", city,
                "--pages", str(args.pages),
                "--no-detail",
                "--format", "csv",
                "--cdp-port", str(args.cdp_port),
            ]
        )
        key = combo_key(keyword, city)
        if code == 0:
            archived = archive_new_results(before)
            progress["done"].append(key)
            progress["stats"][key] = archived
            save_progress(progress)
            consecutive_failures = 0
        else:
            progress["failed"][key] = f"exit={code}"
            save_progress(progress)
            consecutive_failures += 1
            print(f"  [失败] {key}（连续失败 {consecutive_failures} 次）", flush=True)
            if consecutive_failures >= 3:
                print("\n连续失败 3 次，可能是风控或浏览器异常，提前停止。", flush=True)
                break
            continue

        if index % args.import_every == 0:
            do_import(python, args.output)

    do_import(python, args.output)

    elapsed = (time.time() - started) / 60
    print(f"\n本次耗时 {elapsed:.1f} 分钟")
    print(f"累计完成组合 {len(progress['done'])} / {total_possible}")
    print("继续跑：再次执行 py batch_crawl.py --max-combos N（自动跳过已完成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
