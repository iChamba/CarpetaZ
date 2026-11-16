@echo off
setlocal
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo Se requiere Python 3.10 o superior.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 (
  echo No se pudo crear el entorno virtual.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo Instalacion completada.
pause
