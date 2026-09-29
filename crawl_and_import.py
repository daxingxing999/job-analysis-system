"""一键完成：BOSS 直聘真实数据抓取 -> 导入分析系统。

内部串联两个环节：
    1. 调用 scraper/scripts/boss_cdp_raw.py 通过 CDP 抓取真实岗位
    2. 调用 boss_import.py 把抓取结果裁剪为系统标准字段并写入 CSV

关于速度（默认"只取系统用得上的字段"）
    系统只需要 10 个字段：job_name / company / city / salary_low / salary_high /
    education / work_years / category / skills / description。
    其中 9 个在**列表页**就已齐全（skills 也在列表里），只有 description 需要进详情页。

    详情页每条要加载页面、模拟滚动、间隔等待，单条 10-25 秒，20 条就是 8-10 分钟，
    是绝大多数耗时来源。因此本脚本**默认不抓详情**，3 页约 1 分钟即可完成。
    需要 JD 做文本分词/词云时，再用 --details N 抓少量即可。

前置条件
    1. 已安装依赖：pip install websocket-client requests
    2. 已启动专用浏览器并登录 BOSS 直聘（首次只需做一次，登录态会持久化）：
         py crawl_and_import.py --setup-only --browser edge
       没装 Chrome 的 Windows 机器用 --setup-edge；装了 Chrome 可用 --setup-chrome。

用法
    py crawl_and_import.py --keyword "Python" --city 杭州 --pages 3      # 约 1 分钟
    py crawl_and_import.py --keyword "Java" --city 上海 --pages 2 --details 5
    py crawl_and_import.py --setup-only          # 只启动浏览器等待登录
    py crawl_and_import.py --check               # 只做环境检查
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCRAPER_SCRIPT = BASE_DIR / "scraper" / "scripts" / "boss_cdp_raw.py"
DEFAULT_OUTPUT = BASE_DIR / "data" / "boss_jobs.csv"
JOB_RESULT_DIR = Path.home() / ".boss-zhipin-scraper" / "job-result"
# 抓取结果归档目录（项目内），每次抓取都追加进来，保证历史数据可累加
ARCHIVE_DIR = BASE_DIR / "抓取结果"


def snapshot(directory: Path) -> dict[str, float]:
    """记录目录内文件的修改时间，用于识别本次抓取新增的文件。"""
    if not directory.is_dir():
        return {}
    return {path.name: path.stat().st_mtime for path in directory.iterdir() if path.is_file()}


def archive_new_results(before: dict[str, float]) -> int:
    """把本次新增的抓取结果归档到项目内的 抓取结果/ 目录。

    抓取脚本固定输出到 ~/.boss-zhipin-scraper/job-result，若直接导入该目录，
    只会覆盖成"最近一两次"的数据。归档后再导入即可与历史批次累加。
    """
    after = snapshot(JOB_RESULT_DIR)
    new_names = [name for name, mtime in after.items() if before.get(name) != mtime]
    if not new_names:
        print("未发现本次新增的结果文件，跳过归档")
        return 0
    target = ARCHIVE_DIR / datetime.now().strftime("%Y-%m-%d_%H%M")
    target.mkdir(parents=True, exist_ok=True)
    for name in new_names:
        shutil.copy2(JOB_RESULT_DIR / name, target / name)
    print(f"归档本次结果 -> {target}（{len(new_names)} 个文件）")
    return len(new_names)


def build_env() -> dict[str, str]:
    """为子进程准备环境，强制本地地址不走 HTTP 代理。

    机器设置了 HTTP_PROXY 时，CDP 的 127.0.0.1:9222 请求会被代理拦截并返回 502，
    表现为"CDP 已就绪"却无法收发消息、最终 Network.enable 超时。这里把
    127.0.0.1 / localhost 加进 NO_PROXY 规避该问题。
    """
    env = os.environ.copy()
    hosts = [item for item in env.get("NO_PROXY", "").split(",") if item.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in hosts:
            hosts.append(host)
    env["NO_PROXY"] = ",".join(hosts)
    env["no_proxy"] = env["NO_PROXY"]
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(command: list[str]) -> int:
    """执行命令并实时透传输出。"""
    print("$ " + " ".join(command))
    print("-" * 60)
    result = subprocess.run(command, cwd=str(BASE_DIR), env=build_env())
    print("-" * 60)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 BOSS 直聘真实数据并导入分析系统")
    parser.add_argument("--keyword", default="Python", help="搜索关键词")
    parser.add_argument("--city", default="杭州", help="城市名（中文）")
    parser.add_argument("--pages", type=int, default=3, help="抓取页数（上限 10，每页 15 条）")
    parser.add_argument(
        "--details",
        type=int,
        default=0,
        help="抓取 JD 详情的条数；默认 0 即不抓（最快）。仅词云/文本分词需要，代价是每条 10-25 秒",
    )
    parser.add_argument("--cdp-port", type=int, default=9222, help="CDP 端口")
    parser.add_argument("--browser", choices=["chrome", "edge"], default="edge", help="浏览器类型")
    parser.add_argument("--python", default=sys.executable, help="运行抓取脚本的解释器")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="导入后的 CSV 路径")
    parser.add_argument("--check", action="store_true", help="只做环境检查")
    parser.add_argument("--setup-only", action="store_true", help="只启动浏览器并等待登录")
    args = parser.parse_args(argv)

    if not SCRAPER_SCRIPT.exists():
        print(f"[错误] 找不到抓取脚本：{SCRAPER_SCRIPT}", file=sys.stderr)
        return 1

    python = args.python
    scraper = str(SCRAPER_SCRIPT)

    if args.check:
        return run([python, scraper, "--check", "--cdp-port", str(args.cdp_port)])

    if args.setup_only:
        flag = "--setup-edge" if args.browser == "edge" else "--setup-chrome"
        print("即将打开专用浏览器，请在其中登录 BOSS 直聘，登录完成后本脚本会自动继续。")
        return run([python, scraper, flag, "--cdp-port", str(args.cdp_port), "--login-timeout", "600"])

    # 第 1 步：抓取（默认不抓详情，只取列表页即可覆盖系统 10 个字段中的 9 个）
    before = snapshot(JOB_RESULT_DIR)
    if args.details > 0:
        print(f"详情抓取：开启（{args.details} 条，预计额外耗时 {args.details * 20 // 60} 分钟以上）")
    else:
        print("详情抓取：关闭（列表页已含岗位/公司/城市/薪资/学历/经验/行业/技能，约 1 分钟完成）")
        print("         需要职位描述做词云时再加 --details N")

    command = [
        python, scraper,
        "--keyword", args.keyword,
        "--city", args.city,
        "--pages", str(args.pages),
        "--format", "csv",
        "--cdp-port", str(args.cdp_port),
    ]
    if args.details > 0:
        command += ["--detail", "--max-details", str(args.details)]
    else:
        command.append("--no-detail")

    code = run(command)
    if code != 0:
        print(f"\n[失败] 抓取脚本退出码 {code}。若提示未登录，请先执行：")
        print(f"  py crawl_and_import.py --setup-only --browser {args.browser}")
        return code

    # 第 2 步：归档本次结果，然后按归档目录整体导入（与历史批次累加）
    archive_new_results(before)
    source = ARCHIVE_DIR if ARCHIVE_DIR.exists() else JOB_RESULT_DIR
    code = run([python, str(BASE_DIR / "boss_import.py"), "--source", str(source), "--output", args.output])
    if code != 0:
        return code

    try:
        from boss_import import OUTPUT_COLUMNS
        print("\n已裁剪为系统所需字段：" + ", ".join(OUTPUT_COLUMNS))
    except ImportError:
        pass
    print(f"\n完成：{args.output}")
    print("设置环境变量后重启系统即可查看：")
    print(f"  set ZOUYE_DATA_FILE={args.output}")
    print("  py app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
