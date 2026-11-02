@echo off
rem Keep this BAT ASCII-only; PowerShell resolves the Unicode launcher name.
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0stop.ps1"
