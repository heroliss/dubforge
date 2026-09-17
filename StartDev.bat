@echo off
rem DubForge dev launcher (double-click entry; logic lives in scripts\dev.ps1)
title DubForge Dev
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1"
