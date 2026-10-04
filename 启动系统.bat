@echo off
rem ============================================================
rem  招聘岗位需求分析与可视化系统 —— Windows 启动入口
rem
rem  以前这里硬编码了某个人的解释器路径
rem  （C:\Users\Administrator\.workbuddy\binaries\python\envs\zouye-test\Scripts\python.exe），
rem  换台机器必然启动失败。现在按顺序自动探测解释器。
rem  端口可用 PORT 环境变量覆盖，默认 5000。
rem ============================================================
setlocal enableextensions enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost
if not defined PORT set PORT=5000

set PY=

rem 1) 项目自带虚拟环境（最优先，与 README 的推荐流程一致）
if exist ".venv\Scripts\python.exe" (
    set "PY=%CD%\.venv\Scripts\python.exe"
    goto :found
)

rem 2) Windows 官方启动器 py
where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py"
    goto :found
)

rem 3) PATH 里的 python
where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :found
)

rem 4) 常见安装位置兜底
for %%D in (
    "%LOCALAPPDATA%\Programs\Python"
    "C:\Python313" "C:\Python312" "C:\Python311" "C:\Python310"
) do (
    if exist "%%~D\python.exe" (
        set "PY=%%~D\python.exe"
        goto :found
    )
)

echo.
echo   [错误] 没有找到 Python 解释器。
echo.
echo   请任选一种方式：
echo     1. 安装 Python 3.10+（https://www.python.org/downloads/windows/），
echo        安装时勾选 "Add python.exe to PATH"；
echo     2. 或在项目目录里建虚拟环境后重试：
echo          py -m venv .venv
echo          .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:found
echo.
echo   招聘岗位分析系统正在启动...
echo   解释器：%PY%
echo   地址：  http://127.0.0.1:%PORT%/
echo   保持本窗口打开；关闭窗口即停止服务。
echo.

rem 先校验依赖，缺依赖时给出可直接执行的命令，而不是抛一堆堆栈
"%PY%" -c "import flask, pandas" >nul 2>nul
if errorlevel 1 (
    echo   [错误] 当前解释器缺少 Flask 或 Pandas：
    echo        %PY%
    echo.
    echo   请在项目目录执行：
    echo        "%PY%" -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem 后台启动服务，随后轮询端口，确认起来了再打开浏览器
rem （以前先 start 浏览器再启动服务，浏览器必然先打到「无法访问」）
start "岗位分析系统服务" /b "%PY%" app.py

set /a WAITED=0
:waitloop
timeout /t 1 /nobreak >nul
"%PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(0.6); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)" %PORT% >nul 2>nul
if not errorlevel 1 goto :ready

set /a WAITED+=1
if !WAITED! geq 30 (
    echo.
    echo   [警告] 等待 30 秒仍未监听 %PORT% 端口，服务可能启动失败。
    echo          请查看上面的报错输出；若端口被占用，可先换端口再启动：
    echo              set PORT=5001 ^&^& 启动系统.bat
    echo.
    pause
    exit /b 1
)
goto :waitloop

:ready
echo   服务已就绪，正在打开浏览器...
start "" http://127.0.0.1:%PORT%/
echo.
echo   按任意键停止服务并关闭。
pause >nul

rem 尽力停掉服务：找到监听该端口的进程并结束
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"TCP.*:%PORT% .*LISTENING"') do (
    if not "%%P"=="0" taskkill /pid %%P /f >nul 2>nul
)
endlocal
exit /b 0
