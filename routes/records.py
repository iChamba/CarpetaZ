import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

from catalog import iter_tree_files
from database import connect_db
from documents import (
    extract_legacy_doc_text,
    parse_docx,
    safe_relative_path,
    user_folder_from_path,
    word_session,
)
from extraction import (
    extract_certificate_fields,
    is_authorization_individual,
    read_pdf_authorization,
    is_authorization_certificate,
)
from settings import APP_DATA, DOCUMENT_ROOT

CERTIFICATE_IMPORT_LOCK = threading.Lock()
CERTIFICATE_IMPORT_STATE_LOCK = threading.Lock()
CERTIFICATE_IMPORT_STATE = {
    "has_run": False,
    "running": False,
    "total": 0,
    "processed": 0,
    "imported": 0,
    "updated": 0,
    "unchanged": 0,
    "without_text": 0,
    "errors": 0,
    "error_details": [],
    "message": "Importación de certificados sin iniciar",
}
IMPORTABLE_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}
# Increment when certificate detection or extraction changes.
CERTIFICATE_PARSER_VERSION = 1


def _save_extracted_record(relative: str, fields: dict[str, str], is_certificate: bool) -> dict:
    if is_certificate:
        review_fields = [
            "identity_number", "authorization_number", "practice",
            "issue_date", "expiry_date", "equipment",
        ]
    else:
        review_fields = [
            "first_name", "last_name", "identity_number",
            "authorization_number", "equipment",
        ]
    needs_review = any(not fields[field] for field in review_fields)
    connection = connect_db()
    try:
        existing = connection.execute(
            "SELECT * FROM extracted_records WHERE source_path = ?",
            (relative,),
        ).fetchone()
        already_extracted = existing is not None
        updated_existing = False
        if existing and is_certificate:
            merged = {
                field: fields[field] or existing[field]
                for field in review_fields
            }
            needs_review = any(not merged[field] for field in review_fields)
            connection.execute(
                "UPDATE extracted_records SET identity_number = ?, "
                "authorization_number = ?, practice = ?, issue_date = ?, "
                "expiry_date = ?, equipment = ?, is_authorization_certificate = 1, "
                "needs_review = ? WHERE id = ?",
                (
                    merged["identity_number"],
                    merged["authorization_number"],
                    merged["practice"],
                    merged["issue_date"],
                    merged["expiry_date"],
                    merged["equipment"],
                    int(needs_review),
                    existing["id"],
                ),
            )
            updated_existing = True
        else:
            cursor = connection.execute(
                "INSERT INTO extracted_records "
                "(source_path, created_at, first_name, last_name, identity_number, "
                "authorization_number, practice, issue_date, expiry_date, equipment, "
                "is_authorization_certificate, needs_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_path) DO NOTHING",
                (
                    relative,
                    datetime.now().isoformat(timespec="seconds"),
                    fields["first_name"],
                    fields["last_name"],
                    fields["identity_number"],
                    fields["authorization_number"],
                    fields["practice"],
                    fields["issue_date"],
                    fields["expiry_date"],
                    fields["equipment"],
                    int(is_certificate),
                    int(needs_review),
                ),
            )
            already_extracted = cursor.rowcount == 0
        row = connection.execute(
            "SELECT * FROM extracted_records WHERE source_path = ?",
            (relative,),
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    return {
        "record": dict(row),
        "already_extracted": already_extracted,
        "updated": updated_existing,
    }


def _collect_importable_user_documents() -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    errors: list[str] = []

    def on_walk_error(error: OSError) -> None:
        errors.append(str(error))

    for entry in iter_tree_files(DOCUMENT_ROOT, on_walk_error, APP_DATA):
        path = Path(entry.path)
        if path.suffix.lower() not in IMPORTABLE_DOCUMENT_EXTENSIONS:
            continue
        try:
            relative_parts = path.relative_to(DOCUMENT_ROOT).parts
        except ValueError:
            continue
        if len(relative_parts) >= 2:
            files.append(path)
    files.sort(key=lambda path: path.as_posix().casefold())
    return files, errors


def _certificate_import_worker() -> None:
    # Una sola instancia de Word para todos los .doc de la revisión.
    with word_session():
        _run_certificate_import()


def _run_certificate_import() -> None:
    try:
        files, walk_errors = _collect_importable_user_documents()
        with CERTIFICATE_IMPORT_STATE_LOCK:
            CERTIFICATE_IMPORT_STATE.update({
                "total": len(files),
                "processed": 0,
                "imported": 0,
                "updated": 0,
                "unchanged": 0,
                "without_text": 0,
                "errors": len(walk_errors),
                "error_details": walk_errors[:20],
                "message": "Revisando archivos en búsqueda de autorizaciones individuales",
            })

        cache = connect_db()
        root = str(DOCUMENT_ROOT)
        try:
            # Se cargan una vez en memoria para no hacer una consulta por archivo.
            cached_outcomes = {
                row["path"]: (row["size"], row["modified_ns"], row["parser_version"], row["outcome"])
                for row in cache.execute(
                    "SELECT path, size, modified_ns, parser_version, outcome "
                    "FROM certificate_import_cache WHERE root = ?",
                    (root,),
                )
            }
            certificate_paths = {
                row["source_path"]
                for row in cache.execute(
                    "SELECT source_path FROM extracted_records "
                    "WHERE is_authorization_certificate = 1"
                )
            }
            if not walk_errors:
                present = {path.relative_to(DOCUMENT_ROOT).as_posix() for path in files}
                obsolete = [
                    (root, path) for path in cached_outcomes if path not in present
                ]
                if obsolete:
                    cache.executemany(
                        "DELETE FROM certificate_import_cache WHERE root = ? AND path = ?",
                        obsolete,
                    )
                    cache.commit()

            for path in files:
                relative = path.relative_to(DOCUMENT_ROOT).as_posix()
                try:
                    stat = path.stat()
                    cached = cached_outcomes.get(relative)
                    if cached and cached[:3] == (
                        stat.st_size, stat.st_mtime_ns, CERTIFICATE_PARSER_VERSION,
                    ) and (cached[3] != "certificate" or relative in certificate_paths):
                        with CERTIFICATE_IMPORT_STATE_LOCK:
                            CERTIFICATE_IMPORT_STATE["unchanged"] += 1
                            if cached[3] == "without_text":
                                CERTIFICATE_IMPORT_STATE["without_text"] += 1
                        continue

                    safe_path, relative = safe_relative_path(relative)
                    stat = safe_path.stat()
                    extension = safe_path.suffix.lower()
                    pdf_fields = None
                    if extension == ".pdf":
                        text, pdf_fields = read_pdf_authorization(
                            safe_path,
                            lambda value: (
                                is_authorization_certificate(safe_path)
                                or is_authorization_individual(value)
                            ),
                        )
                    elif extension == ".doc":
                        text = extract_legacy_doc_text(safe_path)
                    elif extension == ".docx":
                        text = parse_docx(safe_path)
                    else:
                        text = safe_path.read_text(encoding="utf-8-sig")

                    outcome = "other"
                    fields = None
                    if not text.strip():
                        outcome = "without_text"
                    else:
                        if extension == ".pdf":
                            fields = pdf_fields
                        elif is_authorization_certificate(safe_path) or is_authorization_individual(text):
                            fields = extract_certificate_fields(text, [])
                    if fields is not None:
                        if not any(fields.values()):
                            raise ValueError("No se pudieron extraer datos de la autorización individual.")
                        outcome = "certificate"

                    current = safe_path.stat()
                    if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                        raise ValueError("El archivo cambió durante la revisión; vuelve a intentarlo.")

                    if fields is not None:
                        result = _save_extracted_record(relative, fields, True)
                        with CERTIFICATE_IMPORT_STATE_LOCK:
                            if result["updated"] or result["already_extracted"]:
                                CERTIFICATE_IMPORT_STATE["updated"] += 1
                            else:
                                CERTIFICATE_IMPORT_STATE["imported"] += 1
                    elif outcome == "without_text":
                        with CERTIFICATE_IMPORT_STATE_LOCK:
                            CERTIFICATE_IMPORT_STATE["without_text"] += 1

                    cache.execute(
                        "INSERT INTO certificate_import_cache "
                        "(root, path, size, modified_ns, parser_version, outcome) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(root, path) DO UPDATE SET "
                        "size = excluded.size, modified_ns = excluded.modified_ns, "
                        "parser_version = excluded.parser_version, outcome = excluded.outcome",
                        (root, relative, stat.st_size, stat.st_mtime_ns,
                         CERTIFICATE_PARSER_VERSION, outcome),
                    )
                    cache.commit()
                except Exception as exc:
                    cache.rollback()
                    with CERTIFICATE_IMPORT_STATE_LOCK:
                        CERTIFICATE_IMPORT_STATE["errors"] += 1
                        if len(CERTIFICATE_IMPORT_STATE["error_details"]) < 20:
                            CERTIFICATE_IMPORT_STATE["error_details"].append(f"{relative}: {exc}")
                finally:
                    with CERTIFICATE_IMPORT_STATE_LOCK:
                        CERTIFICATE_IMPORT_STATE["processed"] += 1
                        CERTIFICATE_IMPORT_STATE["message"] = (
                            f"Revisados {CERTIFICATE_IMPORT_STATE['processed']} "
                            f"de {CERTIFICATE_IMPORT_STATE['total']} archivos"
                        )
        finally:
            cache.close()
        with CERTIFICATE_IMPORT_STATE_LOCK:
            CERTIFICATE_IMPORT_STATE["message"] = "Revisión de archivos finalizada"
    except Exception as exc:
        with CERTIFICATE_IMPORT_STATE_LOCK:
            CERTIFICATE_IMPORT_STATE["errors"] += 1
            CERTIFICATE_IMPORT_STATE["message"] = f"No se pudo completar la importación: {exc}"
            if len(CERTIFICATE_IMPORT_STATE["error_details"]) < 20:
                CERTIFICATE_IMPORT_STATE["error_details"].append(str(exc))
    finally:
        with CERTIFICATE_IMPORT_STATE_LOCK:
            CERTIFICATE_IMPORT_STATE["running"] = False
        CERTIFICATE_IMPORT_LOCK.release()


class RecordRoutes:
    def extract_record(self, data: dict) -> None:
        path, relative = safe_relative_path(str(data.get("path", "")))
        extension = path.suffix.lower()
        pdf_fields = None
        if extension == ".pdf":
            text, pdf_fields = read_pdf_authorization(
                path,
                lambda value: is_authorization_certificate(path) or is_authorization_individual(value),
            )
        elif extension == ".docx":
            text = parse_docx(path)
        elif extension == ".doc":
            text = extract_legacy_doc_text(path)
        elif extension == ".txt":
            text = path.read_text(encoding="utf-8-sig")
        else:
            raise ValueError("La importación de datos está disponible para PDF, DOC, DOCX y TXT.")
        if not text.strip():
            return self.send_error_json(
                "Este archivo no contiene texto utilizable. Si es un PDF escaneado, se necesitaría OCR.",
                422,
            )
        if extension == ".pdf":
            fields = pdf_fields
        elif is_authorization_individual(text):
            fields = extract_certificate_fields(text, [])
        else:
            fields = None
        if fields is None:
            return self.send_error_json(
                "No se reconoció una autorización individual en este documento.", 422
            )
        if not any(fields.values()):
            return self.send_error_json(
                "No se pudieron extraer datos de la autorización individual.", 422
            )
        result = _save_extracted_record(relative, fields, True)
        self.send_json(result, 201)

    def start_certificate_import(self) -> None:
        if not CERTIFICATE_IMPORT_LOCK.acquire(blocking=False):
            return self.send_error_json("La importación masiva ya está en curso.", 409)
        with CERTIFICATE_IMPORT_STATE_LOCK:
            CERTIFICATE_IMPORT_STATE.update({
                "has_run": True,
                "running": True,
                "total": 0,
                "processed": 0,
                "imported": 0,
                "updated": 0,
                "unchanged": 0,
                "without_text": 0,
                "errors": 0,
                "error_details": [],
                "message": "Buscando documentos para revisar",
            })
        try:
            threading.Thread(target=_certificate_import_worker, daemon=True).start()
        except Exception:
            with CERTIFICATE_IMPORT_STATE_LOCK:
                CERTIFICATE_IMPORT_STATE["running"] = False
            CERTIFICATE_IMPORT_LOCK.release()
            raise
        self.send_json({"started": True}, 202)

    def certificate_import_status(self) -> None:
        with CERTIFICATE_IMPORT_STATE_LOCK:
            state = dict(CERTIFICATE_IMPORT_STATE)
            state["error_details"] = list(CERTIFICATE_IMPORT_STATE["error_details"])
        self.send_json(state)

    def list_records(self) -> None:
        connection = connect_db()
        try:
            rows = connection.execute(
                "SELECT * FROM extracted_records ORDER BY created_at DESC, id DESC"
            ).fetchall()
        finally:
            connection.close()
        items = []
        for row in rows:
            item = dict(row)
            item["user_folder"] = user_folder_from_path(item["source_path"])
            items.append(item)
        items.sort(key=lambda item: (
            item["user_folder"].casefold(),
            item["last_name"].casefold(),
            item["first_name"].casefold(),
        ))
        self.send_json({"items": items})

    def export_records(self) -> None:
        connection = connect_db()
        try:
            rows = connection.execute(
                "SELECT source_path, first_name, last_name, identity_number, "
                "authorization_number, practice, issue_date, expiry_date, equipment, created_at "
                "FROM extracted_records ORDER BY created_at DESC, id DESC"
            ).fetchall()
        finally:
            connection.close()

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Datos extraídos"
        headers = [
            "Usuario",             "Documento de origen", "Nombre", "Apellido", "Cédula de identidad",
            "Número de autorización", "Práctica", "Expedición", "Vencimiento",
            "Equipos asociados", "Fecha de extracción",
        ]
        sheet.append(headers)
        ordered_rows = sorted(
            rows,
            key=lambda row: (
                user_folder_from_path(row["source_path"]).casefold(),
                row["last_name"].casefold(),
                row["first_name"].casefold(),
            ),
        )
        for row in ordered_rows:
            sheet.append([user_folder_from_path(row["source_path"]), *list(row)])
            # El texto viene de documentos externos: se guarda siempre como texto
            # para que un valor que empiece con "=" no se ejecute como fórmula en Excel.
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1D4ED8")
            cell.alignment = Alignment(vertical="center")
        widths = [26, 48, 24, 28, 22, 28, 32, 18, 18, 60, 22]
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[chr(64 + index)].width = width
        for row in sheet.iter_rows(min_row=2):
            row[9].alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
        if rows:
            table = Table(displayName="DatosExtraidos", ref=f"A1:K{len(rows) + 1}")
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )
            sheet.add_table(table)
        output = BytesIO()
        workbook.save(output)
        content = output.getvalue()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", 'attachment; filename="datos_extraidos.xlsx"')
        self.end_headers()
        self.wfile.write(content)
