import os
import re
import threading
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZipFile
from xml.etree import ElementTree

import pymupdf

from database import connect_db
from settings import (
    ALLOWED_EXTENSIONS,
    DOCUMENT_ROOT,
    INDEX_LOCK,
    INDEX_STATE,
    INDEX_STATE_LOCK,
)


def safe_relative_path(value: str) -> tuple[Path, str]:
    if not value or "\x00" in value:
        raise ValueError("Falta la ruta del archivo.")

    relative = Path(unquote(value))
    if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise ValueError("La ruta solicitada no es válida.")

    candidate = (DOCUMENT_ROOT / relative).resolve(strict=True)
    try:
        if os.path.commonpath((str(DOCUMENT_ROOT), str(candidate))) != str(DOCUMENT_ROOT):
            raise ValueError("La ruta está fuera de la carpeta configurada.")
    except ValueError as exc:
        raise ValueError("La ruta está fuera de la carpeta configurada.") from exc

    if not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("El archivo no existe o su formato no está habilitado.")

    return candidate, candidate.relative_to(DOCUMENT_ROOT).as_posix()


def file_version(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def user_folder_from_path(source_path: str) -> str:
    parts = source_path.replace("\\", "/").strip("/").split("/")
    return parts[0] if len(parts) > 1 and parts[0] else "Sin usuario asignado"


def collect_pdfs() -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    errors: list[str] = []

    def on_walk_error(error: OSError) -> None:
        errors.append(str(error))

    for current, directories, filenames in os.walk(DOCUMENT_ROOT, onerror=on_walk_error):
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and not (Path(current) / name).is_symlink()
        ]
        for filename in filenames:
            if Path(filename).suffix.lower() == ".pdf":
                found.append(Path(current) / filename)
    return found, errors


def extract_pdf_text(path: Path) -> str:
    with pymupdf.open(path) as document:
        return "\n".join(page.get_text("text", sort=True) for page in document)


def update_index() -> None:
    try:
        pdfs, walk_errors = collect_pdfs()
        with INDEX_STATE_LOCK:
            INDEX_STATE.update({
                "processed": 0,
                "total": len(pdfs),
                "indexed": 0,
                "unchanged": 0,
                "without_text": 0,
                "errors": len(walk_errors),
                "error_details": walk_errors[:5],
                "message": "Indexando archivos PDF",
            })

        seen: set[str] = set()
        connection = connect_db()
        try:
            for path in pdfs:
                relative = path.relative_to(DOCUMENT_ROOT).as_posix()
                try:
                    seen.add(relative)
                    stat = path.stat()
                    current = connection.execute(
                        "SELECT size, modified_ns FROM pdf_index_meta WHERE path = ?",
                        (relative,),
                    ).fetchone()
                    unchanged = bool(
                        current
                        and current["size"] == stat.st_size
                        and current["modified_ns"] == stat.st_mtime_ns
                    )
                    indexed_document = None
                    if unchanged:
                        indexed_document = connection.execute(
                            "SELECT CASE WHEN trim(content) = '' THEN 0 ELSE 1 END AS has_text "
                            "FROM pdf_fts WHERE path = ? LIMIT 1",
                            (relative,),
                        ).fetchone()
                        unchanged = indexed_document is not None
                    if unchanged:
                        with INDEX_STATE_LOCK:
                            INDEX_STATE["unchanged"] += 1
                            if not indexed_document["has_text"]:
                                INDEX_STATE["without_text"] += 1
                    else:
                        text = extract_pdf_text(path)
                        connection.execute("DELETE FROM pdf_fts WHERE path = ?", (relative,))
                        connection.execute(
                            "INSERT INTO pdf_fts(path, content) VALUES (?, ?)",
                            (relative, text),
                        )
                        connection.execute(
                            "INSERT INTO pdf_index_meta(path, size, modified_ns) VALUES (?, ?, ?) "
                            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                            "modified_ns=excluded.modified_ns",
                            (relative, stat.st_size, stat.st_mtime_ns),
                        )
                        with INDEX_STATE_LOCK:
                            INDEX_STATE["indexed"] += 1
                            if not text.strip():
                                INDEX_STATE["without_text"] += 1
                except Exception as exc:
                    connection.execute("DELETE FROM pdf_fts WHERE path = ?", (relative,))
                    connection.execute("DELETE FROM pdf_index_meta WHERE path = ?", (relative,))
                    with INDEX_STATE_LOCK:
                        INDEX_STATE["errors"] += 1
                        if len(INDEX_STATE["error_details"]) < 5:
                            INDEX_STATE["error_details"].append(f"{relative}: {exc}")

                with INDEX_STATE_LOCK:
                    INDEX_STATE["processed"] += 1

            existing = connection.execute("SELECT path FROM pdf_index_meta").fetchall()
            stale = [row["path"] for row in existing if row["path"] not in seen]
            for relative in stale:
                connection.execute("DELETE FROM pdf_fts WHERE path = ?", (relative,))
                connection.execute("DELETE FROM pdf_index_meta WHERE path = ?", (relative,))
            connection.commit()
        finally:
            connection.close()

        with INDEX_STATE_LOCK:
            INDEX_STATE["message"] = "Índice actualizado"
    except Exception as exc:
        with INDEX_STATE_LOCK:
            INDEX_STATE["errors"] += 1
            INDEX_STATE["error_details"] = [str(exc)]
            INDEX_STATE["message"] = f"No se pudo completar el índice: {exc}"
    finally:
        with INDEX_STATE_LOCK:
            INDEX_STATE["running"] = False
        INDEX_LOCK.release()


def start_index() -> bool:
    if not INDEX_LOCK.acquire(blocking=False):
        return False
    with INDEX_STATE_LOCK:
        INDEX_STATE["running"] = True
        INDEX_STATE["message"] = "Preparando el índice"
    try:
        threading.Thread(target=update_index, daemon=True).start()
    except Exception:
        with INDEX_STATE_LOCK:
            INDEX_STATE["running"] = False
        INDEX_LOCK.release()
        raise
    return True


def fts_expression(query: str) -> str:
    tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not tokens:
        raise ValueError("Escribe al menos una palabra o un número para buscar.")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def parse_docx(path: Path) -> str:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as archive:
        xml = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in xml.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)
