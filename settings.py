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

APP_DATA = Path(os.environ.get("APP_DATA_DIR", str(APP_DIR / "appdata"))).resolve()
APP_DATA.mkdir(parents=True, exist_ok=True)
DATABASE = APP_DATA / "documentos.sqlite3"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if HOST not in {"127.0.0.1", "localhost", "::1"} and not APP_PASSWORD:
    raise SystemExit("Configura APP_PASSWORD antes de permitir conexiones desde otras PC.")

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".txt", ".xlsx"}
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SEARCH_RESULTS = 500
INDEX_LOCK = threading.Lock()
INDEX_STATE_LOCK = threading.Lock()
WORKBOOK_LOCK = threading.RLock()
INDEX_STATE = {
    "running": False,
    "processed": 0,
    "total": 0,
    "indexed": 0,
    "unchanged": 0,
    "without_text": 0,
    "errors": 0,
    "error_details": [],
    "message": "Índice pendiente",
}
