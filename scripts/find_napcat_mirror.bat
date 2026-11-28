@echo off
rem ============================================================================
rem  find_napcat_mirror.bat  --  Windows entry point
rem
rem  Thin wrapper: the real work is in find_napcat_mirror.ps1 (same folder).
rem  Batch is only used because people double-click / type .bat out of habit;
rem  all the HTTP/token logic lives in PowerShell where quotes and timeouts
rem  behave sanely.
rem
rem  Usage:
rem    scripts\find_napcat_mirror.bat
rem    scripts\find_napcat_mirror.bat --write-env
rem    scripts\find_napcat_mirror.bat --pull
rem    scripts\find_napcat_mirror.bat --pull --write-env
rem    set TAGS=v4.18.3 v4.17.45
rem    set MIRRORS=docker.m.daocloud.io
rem
rem  Notes:
rem    * Do NOT go back to "docker manifest inspect": no timeout, and the CLI
rem      hits auth.docker.io which is unreachable here -> it hangs for minutes.
rem      That was the "stuck here" bug on 2026-11-28.
rem    * Result goes into .env, NOT docker-compose.yml (compose keeps
rem      ${NAPCAT_IMAGE:-...} so a dead mirror is never baked into the repo).
rem    * napcat-local:* is a local spare only -- never commit, never push.
rem ============================================================================
setlocal

set "PS1=%~dp0find_napcat_mirror.ps1"
if not exist "%PS1%" (
  echo [X] not found: %PS1%
  exit /b 1
)

where powershell >nul 2>&1
if errorlevel 1 (
  echo [X] powershell not found
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
exit /b %errorlevel%
