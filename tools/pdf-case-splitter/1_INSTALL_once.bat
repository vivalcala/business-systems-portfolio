@echo off
title PDF Case Splitter - one-time install
echo Installing what the splitter needs (only once, ~3 minutes)...
echo.
rem The OCR library needs Python 3.13 (newer versions may not have it yet).
py install 3.13 >nul 2>&1
py -V:3.13 -m pip install --upgrade pypdf pypdfium2 rapidocr-onnxruntime numpy
if errorlevel 1 (
  echo.
  echo Python 3.13 not available - trying the default Python...
  py -m pip install --upgrade pypdf pypdfium2 rapidocr-onnxruntime numpy
)
echo.
echo Done. From now on just double-click 2_RUN_split.bat
pause
