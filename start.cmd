@echo off
chcp 65001 >nul
cd /d "%~dp0"
python cafe_ollama_stock.py gui
if errorlevel 1 pause
