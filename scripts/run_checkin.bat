@echo off
rem ===== 定时任务入口：确保模拟器运行 -> 虚拟定位 -> 执行签到动作 =====
rem 本脚本被 Windows 计划任务调用，也可以手动运行测试。
call "%~dp0env.bat"
cd /d "%ROOT%"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
echo [%date% %time%] AutoCheckin start >> logs\run_history.log
python "%ROOT%\scripts\checkin.py" "%ROOT%\config\checkin.json" >> logs\run_history.log 2>&1
set RUN_EXIT=%errorlevel%
echo [%date% %time%] AutoCheckin end exit=%RUN_EXIT% >> logs\run_history.log
exit /b %RUN_EXIT%
