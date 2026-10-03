@echo off
chcp 65001 >nul
title PyLauncher Pro
cd /d "%~dp0"

rem Prefer a windowed Python (no console window), then the py launcher, then python.exe.
where pythonw >nul 2>nul && ( start "" pythonw "%~dp0launcher_pro.py" & exit /b )
where pyw >nul 2>nul && ( start "" pyw -3 "%~dp0launcher_pro.py" & exit /b )
start "" python "%~dp0launcher_pro.py"
