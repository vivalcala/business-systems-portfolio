@echo off
title PDF Case Splitter
cd /d "%~dp0"
py -V:3.13 --version >nul 2>&1
if %errorlevel%==0 (
  py -V:3.13 split_cases.py
) else (
  py split_cases.py
)
