import mimetypes
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from database import connect_db
from documents import fts_expression, parse_docx, safe_relative_path
from settings import (
    ALLOWED_EXTENSIONS,
    APP_DATA,
    DOCUMENT_ROOT,
    INDEX_STATE,
    INDEX_STATE_LOCK,
    MAX_SEARCH_RESULTS,
)


class DocumentRoutes:
    def list_files(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip().casefold()
        extension = params.get("ext", [""])[0].lower()
        try:
            offset = max(0, int(params.get("offset", ["0"])[0]))
            limit = min(200, max(1, int(params.get("limit", ["100"])[0])))
        except ValueError:
            raise ValueError("La página solicitada no es válida.") from None

        matches: list[dict] = []
        errors = 0

        def on_error(_error: OSError) -> None:
            nonlocal errors
            errors += 1

        for current, directories, filenames in os.walk(DOCUMENT_ROOT, onerror=on_error):
            directories[:] = [
                name for name in directories
                if not name.startswith(".") and not (Path(current) / name).is_symlink()
            ]
            for filename in filenames:
                path = Path(current) / filename
                if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    continue
                if extension and path.suffix.lower() != extension:
                    continue
                relative = path.relative_to(DOCUMENT_ROOT).as_posix()
                if query and query not in relative.casefold():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    errors += 1
                    continue
                matches.append({
                    "path": relative,
                    "name": path.name,
                    "extension": path.suffix.lower(),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })

        matches.sort(key=lambda item: item["path"].casefold())
        self.send_json({
            "items": matches[offset:offset + limit],
            "total": len(matches),
            "offset": offset,
            "limit": limit,
            "warnings": errors,
        })

    def list_workbooks(self) -> None:
        items = []
        for current, directories, filenames in os.walk(DOCUMENT_ROOT):
            directories[:] = [
                name for name in directories
                if not name.startswith(".") and not (Path(current) / name).is_symlink()
            ]
            for filename in filenames:
                path = Path(current) / filename
                if path.suffix.lower() == ".xlsx" and not path.name.startswith("~$"):
                    items.append(path.relative_to(DOCUMENT_ROOT).as_posix())
        self.send_json({"items": sorted(items, key=str.casefold)})

    def search_pdfs(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip()
        if len(query) > 200:
            raise ValueError("La búsqueda admite hasta 200 caracteres.")
        expression = fts_expression(query)
        try:
            limit = min(MAX_SEARCH_RESULTS, max(1, int(params.get("limit", ["100"])[0])))
        except ValueError:
            raise ValueError("El límite de resultados no es válido.") from None

        connection = connect_db()
        try:
            rows = connection.execute(
                "SELECT path, snippet(pdf_fts, 1, '', '', ' … ', 18) AS excerpt "
                "FROM pdf_fts WHERE pdf_fts MATCH ? LIMIT ?",
                (expression, limit),
            ).fetchall()
        finally:
            connection.close()
        with INDEX_STATE_LOCK:
            status = dict(INDEX_STATE)
        self.send_json({"items": [dict(row) for row in rows], "index": status})

    def open_location(self, data: dict) -> None:
        relative_path = data.get("path")
        if not isinstance(relative_path, str):
            raise ValueError("Falta la ruta del archivo.")
        if os.name != "nt":
            raise RuntimeError("Abrir una ubicación en el Explorador requiere Windows.")

        path, relative = safe_relative_path(relative_path)
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except OSError as exc:
            raise RuntimeError("No se pudo iniciar el Explorador de archivos.") from exc
        self.send_json({"opened": True, "path": relative}, 202)

    def open_folder(self, data: dict) -> None:
        name = data.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("La carpeta de usuario indicada no es válida.")
        if os.name != "nt":
            raise RuntimeError("Abrir una carpeta en el Explorador requiere Windows.")

        folder = DOCUMENT_ROOT / name
        if folder.is_symlink():
            raise ValueError("No se permiten carpetas de usuario que sean enlaces.")
        resolved = folder.resolve(strict=True)
        try:
            if os.path.commonpath((str(DOCUMENT_ROOT), str(resolved))) != str(DOCUMENT_ROOT):
                raise ValueError("La carpeta está fuera de la carpeta configurada.")
        except ValueError as exc:
            raise ValueError("La carpeta está fuera de la carpeta configurada.") from exc
        if (
            not resolved.is_dir()
            or os.path.normcase(str(resolved)) == os.path.normcase(str(APP_DATA))
        ):
            raise FileNotFoundError

        try:
            subprocess.Popen(["explorer.exe", str(resolved)])
        except OSError as exc:
            raise RuntimeError("No se pudo iniciar el Explorador de archivos.") from exc
        self.send_json({"opened": True, "name": name}, 202)

    def preview(self, params: dict[str, list[str]]) -> None:
        path, _ = safe_relative_path(params.get("path", [""])[0])
        extension = path.suffix.lower()
        if extension == ".docx":
            content = parse_docx(path).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif extension == ".txt":
            content = path.read_bytes()
            content_type = "text/plain; charset=utf-8"
        elif extension in {".pdf", ".jpg", ".jpeg", ".png"}:
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        else:
            raise ValueError("Este formato se puede descargar, pero no previsualizar.")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(path.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def download(self, params: dict[str, list[str]]) -> None:
        path, _ = safe_relative_path(params.get("path", [""])[0])
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)
