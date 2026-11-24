import mimetypes
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from catalog import catalog_status, parse_pagination
from database import connect_db
from documents import extract_legacy_doc_text, parse_docx, safe_relative_path
from settings import (
    AppError,
    ALLOWED_EXTENSIONS,
    APP_DATA,
    DOCUMENT_ROOT,
)


class DocumentRoutes:
    def list_files(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip().casefold()
        extension = params.get("ext", [""])[0].lower()
        offset, limit = parse_pagination(params)
        conditions = ["root = 'documents'"]
        values: list[object] = []
        if extension:
            conditions.append("extension = ?")
            values.append(extension)
        if query:
            conditions.append("instr(lower(path), lower(?)) > 0")
            values.append(query)
        where = " AND ".join(conditions)
        connection = connect_db()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) FROM file_catalog WHERE {where}",
                values,
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT path, name, extension, size, modified_ns FROM file_catalog "
                f"WHERE {where} ORDER BY path COLLATE NOCASE LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        finally:
            connection.close()
        items = [{
            "path": row["path"],
            "name": row["name"],
            "extension": row["extension"],
            "size": row["size"],
            "modified": datetime.fromtimestamp(row["modified_ns"] / 1_000_000_000)
            .strftime("%Y-%m-%d %H:%M"),
        } for row in rows]
        self.send_json({
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "warnings": catalog_status()["errors"],
        })

    def open_location(self, data: dict) -> None:
        relative_path = data.get("path")
        if not isinstance(relative_path, str):
            raise ValueError("Falta la ruta del archivo.")
        if os.name != "nt":
            raise AppError("Abrir una ubicación en el Explorador requiere Windows.")

        path, relative = safe_relative_path(relative_path)
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except OSError as exc:
            raise AppError("No se pudo iniciar el Explorador de archivos.") from exc
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
            raise AppError("Abrir una carpeta en el Explorador requiere Windows.")

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
            raise AppError("No se pudo iniciar el Explorador de archivos.") from exc
        self.send_json({"opened": True, "name": name}, 202)

    def preview(self, params: dict[str, list[str]]) -> None:
        path, _ = safe_relative_path(params.get("path", [""])[0])
        extension = path.suffix.lower()
        if extension in {".pdf", ".jpg", ".jpeg", ".png"}:
            size = path.stat().st_size
            self.send_response(200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(path.name)}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)
            return
        if extension == ".docx":
            content = parse_docx(path).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif extension == ".doc":
            content = extract_legacy_doc_text(path).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif extension == ".txt":
            content = path.read_bytes()
            content_type = "text/plain; charset=utf-8"
        else:
            raise ValueError("Este formato no se puede previsualizar.")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(path.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)
