import json
import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from auth import (
    LOGIN_THROTTLE,
    authenticate,
    create_session,
    get_session_user,
    revoke_session,
)
from database import connect_db, initialize_db
from documents import start_catalog
from routes.documents import DocumentRoutes
from routes.entities import EntityRoutes
from routes.inspections import InspectionRoutes
from routes.records import RecordRoutes
from routes.users import UserRoutes
from routes.workflows import WorkflowRoutes
from settings import (
    APP_DATA,
    APP_DIR,
    AppError,
    APP_PASSWORD,
    DOCUMENT_ROOT,
    HOST,
    INDEX_STATE,
    INDEX_STATE_LOCK,
    MAX_REQUEST_BYTES,
    PORT,
    SESSION_COOKIE_SECURE,
)


class BaseRequestHandler(BaseHTTPRequestHandler):
    server_version = "DocumentosLocales/1.0"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        self.current_user = None
        super().__init__(*args, **kwargs)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def authenticated(self) -> bool:
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get("session")
            self.current_user = get_session_user(morsel.value if morsel else "")
        except (CookieError, KeyError, ValueError):
            self.current_user = None
        return self.current_user is not None

    def handle_unexpected_error(self, exc: Exception) -> None:
        if isinstance(exc, AppError):
            return self.send_error_json(str(exc), 500)
        # El detalle puede incluir rutas internas: queda en el registro, no en la respuesta.
        logging.error(
            "Error inesperado en %s %s",
            self.command,
            self.path.split("?", 1)[0],
            exc_info=exc,
        )
        return self.send_error_json(
            "No se pudo completar la operación. El detalle quedó en el registro del servidor.",
            500,
        )

    def reject_authentication(self) -> bool:
        self.send_error_json("Inicia sesión para continuar.", 401)
        return False

    def login(self) -> None:
        data = self.read_json()
        username = data.get("username")
        password = data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("Ingresa usuario y contraseña.")
        address = self.client_address[0]
        if LOGIN_THROTTLE.retry_after(username, address):
            return self.send_error_json(
                "Demasiados intentos fallidos. Espera 15 minutos e inténtalo de nuevo.", 429
            )
        user = authenticate(username, password)
        if not user:
            LOGIN_THROTTLE.record_failure(username, address)
            return self.send_error_json("Usuario o contraseña incorrectos.", 401)
        LOGIN_THROTTLE.record_success(username, address)
        token = create_session(user["id"])
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"
            f"{'; Secure' if SESSION_COOKIE_SECURE else ''}",
        )
        body = json.dumps({"user": user}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def logout(self) -> None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("session")
        if morsel:
            revoke_session(morsel.value)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            f"{'; Secure' if SESSION_COOKIE_SECURE else ''}",
        )
        body = b'{"logged_out":true}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            public_assets = {
                "/login.css": ("login.css", "text/css; charset=utf-8"),
                "/js/login.js": ("js/login.js", "text/javascript; charset=utf-8"),
            }
            if parsed.path in public_assets:
                return self.serve_static(*public_assets[parsed.path])
            if parsed.path == "/" and not self.authenticated():
                return self.serve_static("login.html", "text/html; charset=utf-8")
            if not self.authenticated():
                return self.reject_authentication()
            if parsed.path == "/":
                return self.serve_static("index.html", "text/html; charset=utf-8")
            static_assets = {
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/theme/tokens.css": ("theme/tokens.css", "text/css; charset=utf-8"),
                "/js/main.js": ("js/main.js", "text/javascript; charset=utf-8"),
                "/js/common.js": ("js/common.js", "text/javascript; charset=utf-8"),
                "/js/documents.js": ("js/documents.js", "text/javascript; charset=utf-8"),
                "/js/records.js": ("js/records.js", "text/javascript; charset=utf-8"),
                "/js/users.js": ("js/users.js", "text/javascript; charset=utf-8"),
                "/js/entities.js": ("js/entities.js", "text/javascript; charset=utf-8"),
                "/js/workflows.js": ("js/workflows.js", "text/javascript; charset=utf-8"),
                "/js/inspections.js": ("js/inspections.js", "text/javascript; charset=utf-8"),
            }
            if parsed.path in static_assets:
                return self.serve_static(*static_assets[parsed.path])
            if parsed.path == "/api/status":
                with INDEX_STATE_LOCK:
                    state = dict(INDEX_STATE)
                return self.send_json(state)
            if parsed.path == "/api/session":
                return self.send_json({"user": self.current_user})
            if parsed.path == "/api/inspections":
                return self.get_inspections()
            if parsed.path == "/api/workflow":
                return self.get_workflow_data(params)
            if parsed.path == "/api/workflow/export":
                return self.export_workflows()
            if parsed.path == "/api/users":
                return self.list_users(params)
            if parsed.path == "/api/entities":
                return self.list_entities(params)
            if parsed.path == "/api/entity/licenses":
                return self.list_entity_licenses(params)
            if parsed.path == "/api/entity":
                return self.get_entity(params)
            if parsed.path == "/api/entity/import/status":
                return self.entity_import_status()
            if parsed.path == "/api/entity/preview":
                return self.entity_preview(params)
            if parsed.path == "/api/user":
                return self.get_user(params)
            if parsed.path == "/api/files":
                return self.list_files(params)
            if parsed.path == "/api/preview":
                return self.preview(params)
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
            return self.handle_unexpected_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/login":
                return self.login()
            if not self.authenticated():
                return self.reject_authentication()
            if parsed.path == "/api/logout":
                return self.logout()
            if parsed.path == "/api/catalog/refresh":
                if not start_catalog():
                    return self.send_error_json("Ya hay una actualización de catálogo en curso.", 409)
                return self.send_json({"started": True}, 202)
            if parsed.path == "/api/certificates/import":
                return self.start_certificate_import()
            if parsed.path == "/api/entity/import":
                return self.start_entity_import()
            data = self.read_json()
            if parsed.path == "/api/inspections":
                return self.create_inspection(data)
            if parsed.path == "/api/inspections/action":
                return self.act_on_inspection(data)
            if parsed.path == "/api/workflow/users":
                return self.create_workflow_user(data)
            if parsed.path == "/api/workflow/groups":
                return self.create_workflow_group(data)
            if parsed.path == "/api/workflow/definitions":
                return self.create_workflow(data)
            if parsed.path == "/api/workflow/transitions":
                return self.set_workflow_transition(data)
            if parsed.path == "/api/workflow/stage-assignment":
                return self.set_stage_assignment(data)
            if parsed.path == "/api/workflow/cases":
                return self.create_workflow_case(data)
            if parsed.path == "/api/workflow/cases/link-folder":
                return self.link_workflow_folder(data)
            if parsed.path == "/api/workflow/cases/transition":
                return self.transition_workflow_case(data)
            if parsed.path == "/api/workflow/cases/deliver":
                return self.deliver_workflow_case(data)
            if parsed.path == "/api/workflow/cases/assignment":
                return self.assign_workflow_case(data)
            if parsed.path == "/api/workflow/cases/claim":
                return self.claim_workflow_case(data)
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
            return self.send_error_json("No se encontró la ruta solicitada.", 404)
        except ValueError as exc:
            return self.send_error_json(str(exc), 400)
        except FileNotFoundError:
            return self.send_error_json("El archivo ya no existe o fue movido.", 404)
        except Exception as exc:
            return self.handle_unexpected_error(exc)

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
    InspectionRoutes,
    WorkflowRoutes,
    DocumentRoutes,
    EntityRoutes,
    RecordRoutes,
    UserRoutes,
    BaseRequestHandler,
):
    pass


def configure_logging() -> None:
    handler = RotatingFileHandler(
        APP_DATA / "servidor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def main() -> None:
    configure_logging()
    if APP_PASSWORD and len(APP_PASSWORD) < 12:
        raise SystemExit("APP_PASSWORD debe tener al menos 12 caracteres.")
    try:
        initialize_db()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"SQLite no pudo preparar la base de datos: {exc}") from exc
    if not APP_PASSWORD:
        connection = connect_db()
        try:
            has_admin = connection.execute(
                "SELECT 1 FROM workflow_users WHERE role = 'admin' AND active = 1 LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if not has_admin:
            raise SystemExit(
                "Configura APP_PASSWORD para crear la cuenta administradora inicial."
            )
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"Carpeta de documentos: {DOCUMENT_ROOT}")
    print(f"Servidor: http://{HOST}:{PORT}")
    print("Cierra esta ventana para detener la aplicación.")
    start_catalog()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
