@echo off
chcp 65001 >nul
rem DubForge dev launcher (double-click entry; logic lives in scripts\dev.ps1)
title DubForge Dev
echo [DubForge] 正在启动控制台，请稍候...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" %*
set "DubForgeExitCode=%ERRORLEVEL%"
if errorlevel 1 (
    echo [DubForge] 启动失败，请查看上方错误信息
    pause
)
exit /b %DubForgeExitCode%
