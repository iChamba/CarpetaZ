@echo off
setlocal
set /p DOCUMENT_ROOT=Ruta de la carpeta de PRUEBA (usa una copia local):
if not exist "%DOCUMENT_ROOT%\." (
  echo La carpeta indicada no existe.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Falta el entorno. Ejecuta setup.bat primero.
  pause
  exit /b 1
)
set "HOST=127.0.0.1"
set "PORT=8765"
".venv\Scripts\python.exe" app.py
pause
