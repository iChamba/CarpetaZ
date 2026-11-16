@echo off
setlocal
set /p DOCUMENT_ROOT=Ruta de la carpeta compartida (por ejemplo \\PC-SERVIDOR\Documentos):
if not exist "%DOCUMENT_ROOT%\." (
  echo No se puede acceder a la carpeta indicada desde esta PC.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Falta el entorno. Ejecuta setup.bat primero.
  pause
  exit /b 1
)
set /p APP_PASSWORD=Contrasena de acceso para usuarios de la web:
if not defined APP_PASSWORD (
  echo La contrasena no puede estar vacia.
  pause
  exit /b 1
)
set "HOST=0.0.0.0"
set "PORT=8765"
".venv\Scripts\python.exe" app.py
pause
