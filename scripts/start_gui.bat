@echo off
chcp 65001 >nul
title dsv4-cc-proxy GUI
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m dsv4_cc_proxy.gui
