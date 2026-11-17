import base64
import binascii
import hmac
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from database import initialize_db
from documents import start_index
from routes.documents import DocumentRoutes
from routes.entities import EntityRoutes
from routes.records import RecordRoutes
from routes.users import UserRoutes
from routes.workbooks import WorkbookRoutes
from settings import (
    APP_DIR,
    APP_PASSWORD,
    APP_USER,
    DOCUMENT_ROOT,
    HOST,
    INDEX_STATE,
    INDEX_STATE_LOCK,
    MAX_REQUEST_BYTES,
    PORT,
)


class BaseRequestHandler(BaseHTTPRequestHandler):
    server_version = "DocumentosLocales/1.0"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def authenticated(self) -> bool:
        if not APP_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        scheme, separator, credentials = header.partition(" ")
        if not separator or scheme.lower() != "basic":
            return self.reject_authentication()
        try:
            username, password = base64.b64decode(credentials, validate=True).decode("utf-8").split(":", 1)
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return self.reject_authentication()
        if not hmac.compare_digest(username.encode("utf-8"), APP_USER.encode("utf-8")) or not hmac.compare_digest(
            password.encode("utf-8"), APP_PASSWORD.encode("utf-8")
        ):
            return self.reject_authentication()
        return True

    def reject_authentication(self) -> bool:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Documentos locales", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("La solicitud es demasiado grande.")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("El cuerpo de la solicitud no es JSON válido.") from None
        if not isinstance(value, dict):
            raise ValueError("El cuerpo debe ser un objeto JSON.")
        return value

    def do_GET(self) -> None:
        if not self.authenticated():
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                return self.serve_static("index.html", "text/html; charset=utf-8")
            static_assets = {
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/js/main.js": ("js/main.js", "text/javascript; charset=utf-8"),
                "/js/common.js": ("js/common.js", "text/javascript; charset=utf-8"),
                "/js/documents.js": ("js/documents.js", "text/javascript; charset=utf-8"),
                "/js/search.js": ("js/search.js", "text/javascript; charset=utf-8"),
                "/js/records.js": ("js/records.js", "text/javascript; charset=utf-8"),
                "/js/users.js": ("js/users.js", "text/javascript; charset=utf-8"),
                "/js/entities.js": ("js/entities.js", "text/javascript; charset=utf-8"),
                "/js/workbooks.js": ("js/workbooks.js", "text/javascript; charset=utf-8"),
            }
            if parsed.path in static_assets:
                return self.serve_static(*static_assets[parsed.path])
            if parsed.path == "/api/status":
                with INDEX_STATE_LOCK:
                    state = dict(INDEX_STATE)
                return self.send_json(state)
            if parsed.path == "/api/users":
                return self.list_users(params)
            if parsed.path == "/api/entities":
                return self.list_entities(params)
            if parsed.path == "/api/entity":
                return self.get_entity(params)
            if parsed.path == "/api/entity/import/status":
                return self.entity_import_status()
            if parsed.path == "/api/entity/preview":
                return self.entity_preview(params)
            if parsed.path == "/api/entity/download":
                return self.entity_download(params)
            if parsed.path == "/api/user":
                return self.get_user(params)
            if parsed.path == "/api/files":
                return self.list_files(params)
            if parsed.path == "/api/workbooks":
                return self.list_workbooks()
            if parsed.path == "/api/search":
                return self.search_pdfs(params)
            if parsed.path == "/api/preview":
                return self.preview(params)
            if parsed.path == "/api/download":
                return self.download(params)
            if parsed.path == "/api/workbook":
                return self.read_workbook(params)
            if parsed.path == "/api/records":
                return self.list_records()
            if parsed.path == "/api/certificates/import/status":
                return self.certificate_import_status()
            if parsed.path == "/api/records/export":
                return self.export_records()
            return self.send_error_json("No se encontró la ruta solicitada.", 404)
        except ValueError as exc:
            return self.send_error_json(str(exc), 400)
        except FileNotFoundError:
            return self.send_error_json("El archivo ya no existe o fue movido.", 404)
        except Exception as exc:
            return self.send_error_json(f"No se pudo completar la operación: {exc}", 500)

    def do_POST(self) -> None:
        if not self.authenticated():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/index":
                if not start_index():
                    return self.send_error_json("La actualización del índice ya está en curso.", 409)
                return self.send_json({"started": True}, 202)
            if parsed.path == "/api/certificates/import":
                return self.start_certificate_import()
            if parsed.path == "/api/entity/import":
                return self.start_entity_import()
            data = self.read_json()
            if parsed.path == "/api/entity/extract":
                return self.extract_entity_license(data)
            if parsed.path == "/api/entity/open-location":
                return self.open_entity_location(data)
            if parsed.path == "/api/entity/open-folder":
                return self.open_entity_folder(data)
            if parsed.path == "/api/open-location":
                return self.open_location(data)
            if parsed.path == "/api/open-folder":
                return self.open_folder(data)
            if parsed.path == "/api/extract":
                return self.extract_record(data)
            if parsed.path == "/api/records/update":
                return self.update_record(data)
            if parsed.path == "/api/workbook/save":
                return self.save_workbook(data)
            return self.send_error_json("No se encontró la ruta solicitada.", 404)
        except ValueError as exc:
            return self.send_error_json(str(exc), 400)
        except FileNotFoundError:
            return self.send_error_json("El archivo ya no existe o fue movido.", 404)
        except Exception as exc:
            return self.send_error_json(f"No se pudo completar la operación: {exc}", 500)

    def serve_static(self, filename: str, content_type: str) -> None:
        path = APP_DIR / "static" / filename
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; object-src 'none'; "
            "frame-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)


class RequestHandler(
    DocumentRoutes,
    EntityRoutes,
    WorkbookRoutes,
    RecordRoutes,
    UserRoutes,
    BaseRequestHandler,
):
    pass


def main() -> None:
    try:
        initialize_db()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"SQLite no pudo preparar el índice FTS5: {exc}") from exc
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"Carpeta de documentos: {DOCUMENT_ROOT}")
    print(f"Servidor: http://{HOST}:{PORT}")
    print("Cierra esta ventana para detener la aplicación.")
    start_index()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
