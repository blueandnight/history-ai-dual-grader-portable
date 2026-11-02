@echo off
setlocal

rem Keep this BAT ASCII-only; PowerShell resolves the Unicode launcher name.
set "LAUNCHER=%~dp0start.ps1"
if not exist "%LAUNCHER%" (
  echo Portable launcher was not found: "%LAUNCHER%"
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
