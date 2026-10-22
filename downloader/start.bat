@echo off
title TurboTube
cd /d "%~dp0"
python -m app serve
if errorlevel 1 pause
