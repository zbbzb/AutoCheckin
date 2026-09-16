@echo off
rem ===== 启动 AutoCheckin 模拟器 =====
rem 用法: start_emulator.bat        带窗口启动(日常调试)
rem       start_emulator.bat head   无窗口启动(定时任务后台用)
call "%~dp0env.bat"
set HEADLESS=%1
set EXTRA=
if /i "%HEADLESS%"=="head" set EXTRA=-no-window
if not "%EMU_PROXY%"=="" set EXTRA=%EXTRA% -http-proxy %EMU_PROXY%
echo 正在启动模拟器 %AVD_NAME% %EXTRA% ...
start "AutoCheckin-Emulator" /min "%EMULATOR%" -avd %AVD_NAME% -port 5554 %EXTRA% -gpu auto
echo 等待开机完成...
"%ADB%" -s %ANDROID_SERIAL% wait-for-device
:waitboot
for /f %%i in ('"%ADB%" -s %ANDROID_SERIAL% shell getprop sys.boot_completed 2^>nul') do set BOOT=%%i
if not "%BOOT%"=="1" (
    timeout /t 3 /nobreak >nul
    goto waitboot
)
echo 模拟器已启动并完成开机。
"%ADB%" -s %ANDROID_SERIAL% devices
