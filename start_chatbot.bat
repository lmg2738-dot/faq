@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FAQ Chatbot (port 8080)
python app.py
if errorlevel 1 pause
