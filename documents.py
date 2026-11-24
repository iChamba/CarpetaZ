import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZipFile
from xml.etree import ElementTree

import pymupdf

from catalog import sync_file_catalog
from settings import (
    AppError,
    ALLOWED_EXTENSIONS,
    DOCUMENT_ROOT,
    INDEX_LOCK,
    INDEX_STATE,
    INDEX_STATE_LOCK,
)


_WORD_SESSION = threading.local()


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


def user_folder_from_path(source_path: str) -> str:
    parts = source_path.replace("\\", "/").strip("/").split("/")
    return parts[0] if len(parts) > 1 and parts[0] else "Sin usuario asignado"


def extract_pdf_text(path: Path) -> str:
    with pymupdf.open(path) as document:
        return "\n".join(page.get_text("text", sort=True) for page in document)


def extract_legacy_doc_text(path: Path) -> str:
    return _read_legacy_doc(path, include_tables=False)[0]


def extract_legacy_doc_content(path: Path) -> tuple[str, list[tuple[str, list[list[str]]]]]:
    return _read_legacy_doc(path, include_tables=True)


def _read_legacy_doc(
    path: Path, include_tables: bool,
) -> tuple[str, list[tuple[str, list[list[str]]]]]:
    if os.name != "nt":
        raise AppError("La lectura de archivos Word 2003 requiere Windows y Microsoft Word.")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise AppError(
            "Para leer archivos Word 2003 instala las dependencias de la aplicación "
            "y Microsoft Word en este equipo."
        ) from exc

    shared = getattr(_WORD_SESSION, "active", False)
    word = _WORD_SESSION.word if shared else None
    owns_word = word is None
    if owns_word:
        pythoncom.CoInitialize()
    document = None
    failed = False
    try:
        if owns_word:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            word.AutomationSecurity = 3
            if shared:
                _WORD_SESSION.word = word
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
        text = document.Content.Text
        tables = []
        if include_tables:
            for table in document.Tables:
                context = document.Range(
                    Start=max(0, table.Range.Start - 200), End=table.Range.Start,
                ).Text
                rows: dict[int, dict[int, str]] = {}
                for cell in table.Range.Cells:
                    rows.setdefault(cell.RowIndex, {})[cell.ColumnIndex] = (
                        cell.Range.Text.rstrip("\r\x07")
                    )
                width = max((max(row) for row in rows.values()), default=0)
                tables.append((context, [
                    [row.get(index, "") for index in range(1, width + 1)]
                    for _, row in sorted(rows.items())
                ]))
        return text, tables
    except pythoncom.com_error as exc:
        failed = True
        raise ValueError(f"Microsoft Word no pudo leer el documento: {path.name}") from exc
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if document is not None:
                document.Close(SaveChanges=0)
        except Exception:
            failed = True
            if not shared:
                raise
        finally:
            # Fuera de una sesión compartida, o si Word falló, se cierra la instancia.
            if not shared:
                _quit_word(pythoncom, word, owns_com=True)
            elif failed and _WORD_SESSION.word is not None:
                _quit_word(pythoncom, _WORD_SESSION.word, owns_com=True)
                _WORD_SESSION.word = None
            elif owns_word and _WORD_SESSION.word is None:
                # No se pudo crear Word dentro de la sesión.
                pythoncom.CoUninitialize()


def _quit_word(pythoncom, word, owns_com: bool) -> None:
    try:
        if word is not None:
            word.Quit(SaveChanges=0)
    finally:
        if owns_com:
            pythoncom.CoUninitialize()


@contextmanager
def word_session():
    """Reutiliza una sola instancia de Word para leer varios .doc en el hilo actual."""
    if getattr(_WORD_SESSION, "active", False):
        yield
        return
    _WORD_SESSION.active = True
    _WORD_SESSION.word = None
    try:
        yield
    finally:
        word = _WORD_SESSION.word
        _WORD_SESSION.active = False
        _WORD_SESSION.word = None
        if word is not None:
            import pythoncom

            _quit_word(pythoncom, word, owns_com=True)


def start_catalog() -> bool:
    if not INDEX_LOCK.acquire(blocking=False):
        return False
    with INDEX_STATE_LOCK:
        INDEX_STATE.update({
            "catalog_running": True,
            "catalog_processed": 0,
            "catalog_total": 0,
            "catalog_errors": 0,
            "catalog_message": "Preparando el catálogo",
        })

    def update_catalog() -> None:
        try:
            sync_file_catalog()
        except Exception as exc:
            logging.exception("No se pudo actualizar el catálogo")
            with INDEX_STATE_LOCK:
                INDEX_STATE["catalog_errors"] = max(1, INDEX_STATE["catalog_errors"])
                INDEX_STATE["catalog_message"] = f"No se pudo completar el catálogo: {exc}"
        finally:
            with INDEX_STATE_LOCK:
                INDEX_STATE["catalog_running"] = False
            INDEX_LOCK.release()

    try:
        threading.Thread(target=update_catalog, daemon=True).start()
    except Exception:
        with INDEX_STATE_LOCK:
            INDEX_STATE["catalog_running"] = False
        INDEX_LOCK.release()
        raise
    return True


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


def extract_docx_tables(path: Path) -> list[tuple[str, list[list[str]]]]:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as archive:
        xml = ElementTree.fromstring(archive.read("word/document.xml"))
    body = xml.find("w:body", namespace)
    if body is None:
        raise ValueError("El documento Word no contiene un cuerpo válido.")
    tables = []
    context = ""
    for node in body:
        if node.tag.endswith("}p"):
            context = "".join(part.text or "" for part in node.findall(".//w:t", namespace))
        elif node.tag.endswith("}tbl"):
            rows = []
            for row in node.findall("./w:tr", namespace):
                cells = []
                for cell in row.findall("./w:tc", namespace):
                    cells.append("\n".join(
                        "".join(part.text or "" for part in paragraph.findall(".//w:t", namespace))
                        for paragraph in cell.findall(".//w:p", namespace)
                    ).strip())
                    span = cell.find("./w:tcPr/w:gridSpan", namespace)
                    if span is not None:
                        width = int(span.get(f"{{{namespace['w']}}}val", "1"))
                        cells.extend([""] * (width - 1))
                rows.append(cells)
            tables.append((context, rows))
            context = ""
    return tables
