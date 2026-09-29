@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost

echo.
echo   Job Analysis System is starting...
echo   Browser will open http://127.0.0.1:5000/
echo   Keep this window open. Close it to stop the server.
echo.

start "" http://127.0.0.1:5000/

"C:\Users\Administrator\.workbuddy\binaries\python\envs\zouye-test\Scripts\python.exe" app.py

pause
