@echo off
rem ===== 安装 apps/ 目录下的所有 APK =====
call "%~dp0env.bat"
set APK_DIR=%ROOT%\apps
set FOUND=0
for %%f in ("%APK_DIR%\*.apk") do (
    echo 正在安装: %%~nxf ...
    "%ADB%" -s %ANDROID_SERIAL% install -r -t "%%f"
    set FOUND=1
)
if "%FOUND%"=="0" (
    echo [提示] apps/ 目录下没有 APK。请把要安装的 APK 复制到 %APK_DIR% 后重新运行本脚本。
    exit /b 1
)
echo 安装完成，已安装的第三方应用：
"%ADB%" -s %ANDROID_SERIAL% shell pm list packages -3
