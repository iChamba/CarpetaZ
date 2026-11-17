import os
import re
import threading
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZipFile
from xml.etree import ElementTree

import pymupdf

from catalog import sync_file_catalog
from database import connect_db
from settings import (
    ALLOWED_EXTENSIONS,
    DOCUMENT_ROOT,
    INDEX_LOCK,
    INDEX_STATE,
    INDEX_STATE_LOCK,
)


def safe_relative_path(
    value: str,
    root: Path = DOCUMENT_ROOT,
) -> tuple[Path, str]:
    if not value or "\x00" in value:
        raise ValueError("Falta la ruta del archivo.")

    relative = Path(unquote(value))
    if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise ValueError("La ruta solicitada no es válida.")

    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve(strict=True)
    try:
        common = os.path.commonpath((str(resolved_root), str(candidate)))
        if os.path.normcase(common) != os.path.normcase(str(resolved_root)):
            raise ValueError("La ruta está fuera de la carpeta configurada.")
    except ValueError as exc:
        raise ValueError("La ruta está fuera de la carpeta configurada.") from exc

    if not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("El archivo no existe o su formato no está habilitado.")

    return candidate, candidate.relative_to(resolved_root).as_posix()


def file_version(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def user_folder_from_path(source_path: str) -> str:
    parts = source_path.replace("\\", "/").strip("/").split("/")
    return parts[0] if len(parts) > 1 and parts[0] else "Sin usuario asignado"


def extract_pdf_text(path: Path) -> str:
    with pymupdf.open(path) as document:
        return "\n".join(page.get_text("text", sort=True) for page in document)


def extract_legacy_doc_text(path: Path) -> str:
    if os.name != "nt":
        raise RuntimeError("La lectura de archivos Word 2003 requiere Windows y Microsoft Word.")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            "Para leer archivos Word 2003 instala las dependencias de la aplicación "
            "y Microsoft Word en este equipo."
        ) from exc

    pythoncom.CoInitialize()
    word = None
    document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        word.AutomationSecurity = 3
        document = word.Documents.Open(
            FileName=str(path.resolve()),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Revert=False,
            Visible=False,
            OpenAndRepair=True,
            NoEncodingDialog=True,
        )
        return document.Content.Text
    except pythoncom.com_error as exc:
        raise ValueError(f"Microsoft Word no pudo leer el documento: {path.name}") from exc
    finally:
        try:
            if document is not None:
                document.Close(SaveChanges=0)
        finally:
            try:
                if word is not None:
                    word.Quit(SaveChanges=0)
            finally:
                pythoncom.CoUninitialize()


def update_index() -> None:
    try:
        sync_file_catalog()
        connection = connect_db()
        try:
            total = connection.execute(
                "SELECT COUNT(*) FROM file_catalog "
                "WHERE root = 'documents' AND extension = '.pdf'"
            ).fetchone()[0]
        except Exception:
            connection.close()
            raise

        with INDEX_STATE_LOCK:
            INDEX_STATE.update({
                "processed": 0,
                "total": total,
                "indexed": 0,
                "unchanged": 0,
                "without_text": 0,
                "errors": 0,
                "error_details": [],
                "message": "Indexando archivos PDF",
            })

        try:
            last_path = ""
            while True:
                pdf_batch = connection.execute(
                    "SELECT path, size, modified_ns FROM file_catalog "
                    "WHERE root = 'documents' AND extension = '.pdf' AND path > ? "
                    "ORDER BY path LIMIT 500",
                    (last_path,),
                ).fetchall()
                if not pdf_batch:
                    break
                for pdf in pdf_batch:
                    relative = pdf["path"]
                    path = DOCUMENT_ROOT / Path(relative)
                    try:
                        current = connection.execute(
                            "SELECT size, modified_ns FROM pdf_index_meta WHERE path = ?",
                            (relative,),
                        ).fetchone()
                        unchanged = bool(
                            current
                            and current["size"] == pdf["size"]
                            and current["modified_ns"] == pdf["modified_ns"]
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
                                (relative, pdf["size"], pdf["modified_ns"]),
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
                last_path = pdf_batch[-1]["path"]
                connection.commit()

            connection.execute(
                "DELETE FROM pdf_index_meta WHERE NOT EXISTS ("
                "SELECT 1 FROM file_catalog "
                "WHERE file_catalog.root = 'documents' "
                "AND file_catalog.extension = '.pdf' "
                "AND file_catalog.path = pdf_index_meta.path)"
            )
            connection.execute(
                "DELETE FROM pdf_fts WHERE path NOT IN "
                "(SELECT path FROM pdf_index_meta)"
            )
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
