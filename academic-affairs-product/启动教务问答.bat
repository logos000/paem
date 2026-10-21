@echo off
chcp 65001 >nul
title ZJUI Academic Affairs QA
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" %*
echo.
pause
