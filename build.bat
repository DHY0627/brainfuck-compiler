@echo off
rem Build bfc.exe (requires MinGW-w64 g++, clang, or any C++17 compiler).
rem NOTE: this file is intentionally ASCII-only: cmd.exe reads .bat files in the
rem OEM code page, so UTF-8 comments would be mis-decoded and break the script.
setlocal
cd /d "%~dp0"
g++ -std=c++17 -O2 -Wall -Wextra -static -static-libgcc -static-libstdc++ -o bfc.exe bfc.cpp
if errorlevel 1 (
  echo build failed
  exit /b 1
)
echo built: %~dp0bfc.exe
endlocal
