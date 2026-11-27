@echo off
chcp 65001 > nul
cd /d "%~dp0"
python main.py --telegram --tg-always --export --open-html
echo.
