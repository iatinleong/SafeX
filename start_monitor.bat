@echo off
REM Double-click this file to start the SafeW OCR monitor with auto-restart on crash.
REM -ExecutionPolicy Bypass only applies to this single invocation; it does not change
REM the system-wide PowerShell execution policy for other scripts.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_monitor_auto_restart.ps1"
pause
