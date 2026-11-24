import os
import threading
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ROOT_VALUE = os.environ.get("DOCUMENT_ROOT", "").strip()
if not ROOT_VALUE:
    raise SystemExit(
        "Falta DOCUMENT_ROOT. Inicia la aplicación con la ruta de una carpeta "
        "de prueba que contenga una copia de tus documentos."
    )

DOCUMENT_ROOT = Path(ROOT_VALUE).expanduser().resolve()
if not DOCUMENT_ROOT.is_dir():
    raise SystemExit(f"La carpeta DOCUMENT_ROOT no existe: {DOCUMENT_ROOT}")

ENTITY_ROOT = Path(
    os.environ.get("ENTITY_ROOT", str(APP_DIR.parent / "3 Entidades Usuarias"))
).expanduser().resolve()

# Planillas de inspección (carpeta 5) para la planificación del Jefe de Inspección.
PLANNING_ROOT = Path(
    os.environ.get("PLANNING_ROOT", str(APP_DIR.parent / "5 Registro de tramites y planillas"))
).expanduser().resolve()

APP_DATA = Path(os.environ.get("APP_DATA_DIR", str(APP_DIR / "appdata"))).resolve()
APP_DATA.mkdir(parents=True, exist_ok=True)
DATABASE = APP_DATA / "documentos.sqlite3"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
# Respaldos automáticos: conviene que BACKUP_DIR esté en otro disco o carpeta de red.
BACKUP_DIR = Path(
    os.environ.get("BACKUP_DIR", "").strip() or str(APP_DATA / "respaldos" / "automaticos")
).expanduser()
# Cada cuántos minutos (desde que termina la anterior) se actualizan catálogo e
# importaciones automáticamente; 0 lo desactiva.
AUTO_REFRESH_MINUTES = float(os.environ.get("AUTO_REFRESH_MINUTES", "15"))
BACKUP_INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_KEEP_DAILY = int(os.environ.get("BACKUP_KEEP_DAILY", "7"))
BACKUP_KEEP_WEEKLY = int(os.environ.get("BACKUP_KEEP_WEEKLY", "4"))


class AppError(RuntimeError):
    """Error cuyo mensaje está escrito para mostrarse al usuario."""


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".txt", ".xlsx"}
MAX_REQUEST_BYTES = 2 * 1024 * 1024
INDEX_LOCK = threading.Lock()
INDEX_STATE_LOCK = threading.Lock()
INDEX_STATE = {
    "catalog_running": False,
    "catalog_processed": 0,
    "catalog_total": 0,
    "catalog_errors": 0,
    "catalog_ready": False,
    "catalog_message": "Catálogo pendiente",
    # Aumenta al terminar cada actualización del catálogo; invalida cachés derivadas.
    "catalog_generation": 0,
    "catalog_last_finished": "",
    "auto_refresh_minutes": 0,
    "auto_refresh_last": "",
}
