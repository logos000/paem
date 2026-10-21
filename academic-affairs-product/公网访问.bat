@echo off
chcp 65001 >nul
title 公网访问隧道（关闭本窗口即断开）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\public_tunnel.ps1"
pause
