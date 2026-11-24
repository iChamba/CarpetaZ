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
echo Si la cuenta administradora ya existe, deja la contrasena vacia y pulsa Enter.
echo Para reemplazar una contrasena existente, abre una consola, ejecuta "set APP_RESET_ADMIN=1" y luego este archivo.
set "APP_PASSWORD="
set /p APP_PASSWORD=Contrasena inicial de la cuenta administradora (minimo 12 caracteres):
if defined APP_PASSWORD if "%APP_PASSWORD:~11,1%"=="" (
  echo La contrasena debe tener al menos 12 caracteres.
  pause
  exit /b 1
)
set "APP_USER=admin"
set "HOST=127.0.0.1"
set "PORT=8765"
".venv\Scripts\python.exe" app.py
pause
