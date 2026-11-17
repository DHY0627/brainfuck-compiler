@echo off
rem Full build. ASCII only: cmd.exe reads .bat files in the OEM code page.
setlocal
cd /d "%~dp0"

echo [1/7] building the C++ compiler
g++ -std=c++17 -O2 -Wall -Wextra -static -static-libgcc -static-libstdc++ -o bfc.exe bfc.cpp
if errorlevel 1 goto :fail

echo [2/7] regenerating the Brainfuck compiler sources
python tools\gen_mkstage0.py
if errorlevel 1 goto :fail
python tools\gen_bfc0.py
if errorlevel 1 goto :fail
python tools\gen_bfc1.py
if errorlevel 1 goto :fail
python tools\gen_bfc2.py
if errorlevel 1 goto :fail

echo [3/7] bfc.exe compiles the Brainfuck-written compilers
bfc.exe bfsrc\bfc0.bf -o bfsrc\bfc0.exe
if errorlevel 1 goto :fail
bfc.exe bfsrc\bfc1.bf -o bfsrc\bfc1.exe
if errorlevel 1 goto :fail
bfc.exe bfsrc\bfc2.bf -o bfsrc\bfc2_seed.exe
if errorlevel 1 goto :fail

echo [4/7] bfc.exe compiles the examples
bfc.exe examples\hello.bf -o examples\hello.exe
bfc.exe examples\cat.bf -o examples\cat.exe
bfc.exe examples\nested.bf -o examples\nested.exe
bfc.exe examples\zeroing.bf -o examples\zeroing.exe
if errorlevel 1 goto :fail

echo [5/7] bfc0 compiles a demo program
bfsrc\bfc0.exe < examples\bfc0_hello.bf > examples\bfc0_hello.exe
if errorlevel 1 goto :fail

echo [6/7] gcc-free self-hosting: bfc2 writes its own successor exe
bfsrc\bfc2_seed.exe < bfsrc\bfc2.bf > bfsrc\bfc2.exe
if errorlevel 1 goto :fail
bfsrc\bfc2.exe < bfsrc\bfc2.bf > "%TEMP%\bfc2_stage3.exe"
if errorlevel 1 goto :fail
fc /b bfsrc\bfc2.exe "%TEMP%\bfc2_stage3.exe" >nul
if errorlevel 1 goto :failpoint
echo       fixed point reached: stage3 == bfc2.exe
del "%TEMP%\bfc2_stage3.exe" >nul 2>nul

echo [7/7] bfc1 self-hosting smoke test (C target, needs gcc)
bfsrc\bfc1.exe < bfsrc\bfc1.bf > "%TEMP%\bfc1_stage1.c"
if errorlevel 1 goto :fail
del "%TEMP%\bfc1_stage1.c" >nul 2>nul

echo.
echo done.
echo   bfc.exe              C++ compiler
echo   bfsrc\bfc0.exe       Brainfuck-written compiler (buffers, no loops)
echo   bfsrc\bfc1.exe       Brainfuck-written compiler (outputs C, needs gcc)
echo   bfsrc\bfc2.exe       Brainfuck-written compiler (outputs full .exe, no gcc)
echo   examples\*.exe       compiled programs
echo.
echo Self-hosting proofs: python bootstrap\selfhost.py
echo                      python bootstrap\selfhost_pak.py
endlocal
exit /b 0

:failpoint
echo FIXED POINT BROKEN
endlocal
exit /b 1

:fail
echo BUILD FAILED
endlocal
exit /b 1
