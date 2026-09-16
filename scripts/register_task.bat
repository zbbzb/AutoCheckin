@echo off
rem ===== 注册/取消 Windows 计划任务 =====
rem 用法:
rem   register_task.bat create            注册每天 08:30 自动执行
rem   register_task.bat create 09:15      注册每天 09:15 自动执行
rem   register_task.bat delete            删除计划任务
set ROOT=D:\MYCODES\AutoCheckin
if /i "%1"=="delete" (
    schtasks /Delete /TN "AutoCheckin" /F
    goto :eof
)
set SCHED_TIME=%2
if "%SCHED_TIME%"=="" set SCHED_TIME=08:30
schtasks /Create /F /TN "AutoCheckin" /TR "%ROOT%\scripts\run_checkin.bat" /SC DAILY /ST %SCHED_TIME%
echo.
echo 已注册计划任务: 每天 %SCHED_TIME% 自动执行 %ROOT%\scripts\run_checkin.bat
echo 修改时间: register_task.bat create 09:15    删除任务: register_task.bat delete
echo 立即测试: schtasks /Run /TN "AutoCheckin"
