@echo off
rem ===== AutoCheckin 统一环境变量，其他脚本请 call 本文件 =====
set ROOT=D:\MYCODES\AutoCheckin
set ANDROID_HOME=%ROOT%\android-sdk
set ANDROID_AVD_HOME=%ROOT%\avds
set ANDROID_USER_HOME=%ROOT%\avds\.android
set ADB=%ANDROID_HOME%\platform-tools\adb.exe
set EMULATOR=%ANDROID_HOME%\emulator\emulator.exe
set AVD_NAME=AutoCheckin
rem 固定操作模拟器，避免机器上插着真机时 adb 命令误发到真机
set ANDROID_SERIAL=emulator-5554
rem 模拟器访问外网(Google服务)走的宿主机代理；代理软件没开时请留空
set EMU_PROXY=http://127.0.0.1:7890
