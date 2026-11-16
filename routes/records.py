import os
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

from database import connect_db
from documents import (
    extract_pdf_text,
    parse_docx,
    safe_relative_path,
    user_folder_from_path,
)
from extraction import (
    extract_authorization_certificate,
    extract_candidate_fields,
    extract_certificate_fields,
    is_authorization_individual,
    is_authorization_certificate,
)
from settings import APP_DATA, DOCUMENT_ROOT, INDEX_STATE, INDEX_STATE_LOCK

CERTIFICATE_IMPORT_LOCK = threading.Lock()
CERTIFICATE_IMPORT_STATE_LOCK = threading.Lock()
CERTIFICATE_IMPORT_STATE = {
    "has_run": False,
    "running": False,
    "total": 0,
    "processed": 0,
    "imported": 0,
    "updated": 0,
    "without_text": 0,
    "errors": 0,
    "error_details": [],
    "message": "Importación de certificados sin iniciar",
}
IMPORTABLE_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".txt"}


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

    for current, directories, filenames in os.walk(DOCUMENT_ROOT, onerror=on_walk_error):
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and not (Path(current) / name).is_symlink()
            and os.path.normcase(os.path.abspath(Path(current) / name))
            != os.path.normcase(str(APP_DATA))
        ]
        for filename in filenames:
            path = Path(current) / filename
            if (
                path.suffix.lower() not in IMPORTABLE_DOCUMENT_EXTENSIONS
                or path.is_symlink()
            ):
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
    try:
        while True:
            with INDEX_STATE_LOCK:
                index_running = INDEX_STATE["running"]
            if not index_running:
                break
            with CERTIFICATE_IMPORT_STATE_LOCK:
                CERTIFICATE_IMPORT_STATE["message"] = (
                    "Esperando a que termine la indexación para evitar bloqueos de SQLite"
                )
            time.sleep(2)

        files, walk_errors = _collect_importable_user_documents()
        with CERTIFICATE_IMPORT_STATE_LOCK:
            CERTIFICATE_IMPORT_STATE.update({
                "total": len(files),
                "processed": 0,
                "imported": 0,
                "updated": 0,
                "without_text": 0,
                "errors": len(walk_errors),
                "error_details": walk_errors[:20],
                "message": "Revisando archivos en búsqueda de autorizaciones individuales",
            })

        for path in files:
            relative = path.relative_to(DOCUMENT_ROOT).as_posix()
            try:
                safe_path, relative = safe_relative_path(relative)
                extension = safe_path.suffix.lower()
                if extension == ".pdf":
                    text = extract_pdf_text(safe_path)
                elif extension == ".docx":
                    text = parse_docx(safe_path)
                else:
                    text = safe_path.read_text(encoding="utf-8-sig")
                if not text.strip():
                    with CERTIFICATE_IMPORT_STATE_LOCK:
                        CERTIFICATE_IMPORT_STATE["without_text"] += 1
                    continue

                is_certificate = (
                    is_authorization_certificate(safe_path)
                    or is_authorization_individual(text)
                )
                if not is_certificate:
                    continue
                fields = (
                    extract_authorization_certificate(safe_path)
                    if extension == ".pdf"
                    else extract_certificate_fields(text, [])
                )
                if not any(fields.values()):
                    raise ValueError("No se pudieron extraer datos de la autorización individual.")
                result = _save_extracted_record(relative, fields, True)
                with CERTIFICATE_IMPORT_STATE_LOCK:
                    if result["updated"]:
                        CERTIFICATE_IMPORT_STATE["updated"] += 1
                    elif result["already_extracted"]:
                        CERTIFICATE_IMPORT_STATE["updated"] += 1
                    else:
                        CERTIFICATE_IMPORT_STATE["imported"] += 1
            except Exception as exc:
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
        is_certificate = False
        if extension == ".pdf":
            text = extract_pdf_text(path)
            if not text.strip():
                return self.send_error_json(
                    "Este archivo no contiene texto utilizable. Si es un escaneo, se necesitaría OCR.",
                    422,
                )
            is_certificate = (
                is_authorization_certificate(path) or is_authorization_individual(text)
            )
            if is_certificate:
                fields = extract_authorization_certificate(path)
                if not any(fields.values()):
                    return self.send_error_json(
                        "No se pudo extraer texto utilizable de la autorización individual.",
                        422,
                    )
            else:
                fields = extract_candidate_fields(text)
        elif extension == ".docx":
            text = parse_docx(path)
            is_certificate = is_authorization_individual(text)
            fields = (
                extract_certificate_fields(text, [])
                if is_certificate
                else extract_candidate_fields(text)
            )
        elif extension == ".txt":
            text = path.read_text(encoding="utf-8-sig")
            is_certificate = is_authorization_individual(text)
            fields = (
                extract_certificate_fields(text, [])
                if is_certificate
                else extract_candidate_fields(text)
            )
        else:
            raise ValueError("La importación de datos está disponible para PDF, DOCX y TXT.")
        if extension != ".pdf" and not text.strip():
            return self.send_error_json(
                "Este archivo no contiene texto utilizable. Si es un PDF escaneado, se necesitaría OCR.",
                422,
            )
        result = _save_extracted_record(relative, fields, is_certificate)
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

    def update_record(self, data: dict) -> None:
        try:
            record_id = int(data.get("id"))
        except (TypeError, ValueError):
            raise ValueError("El registro indicado no es válido.") from None
        allowed = {
            "first_name", "last_name", "identity_number",
            "authorization_number", "practice", "issue_date", "expiry_date", "equipment",
        }
        updates = data.get("fields")
        if not isinstance(updates, dict) or not updates or not set(updates).issubset(allowed):
            raise ValueError("No hay campos válidos para guardar.")
        for value in updates.values():
            if not isinstance(value, str) or len(value) > 10000:
                raise ValueError("Cada campo admite texto de hasta 10000 caracteres.")

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        connection = connect_db()
        try:
            current = connection.execute(
                "SELECT source_path, first_name, last_name, identity_number, authorization_number, "
                "practice, issue_date, expiry_date, equipment, is_authorization_certificate "
                "FROM extracted_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if not current:
                raise FileNotFoundError
            merged = dict(current)
            merged.update(updates)
            if current["is_authorization_certificate"] or is_authorization_certificate(
                Path(merged["source_path"])
            ):
                review_fields = [
                    "identity_number", "authorization_number", "practice",
                    "issue_date", "expiry_date", "equipment",
                ]
            else:
                review_fields = [
                    "first_name", "last_name", "identity_number",
                    "authorization_number", "equipment",
                ]
            needs_review = int(any(not merged[field].strip() for field in review_fields))
            cursor = connection.execute(
                f"UPDATE extracted_records SET {assignments}, needs_review = ? WHERE id = ?",
                (*values, needs_review, record_id),
            )
            if cursor.rowcount == 0:
                raise FileNotFoundError
            row = connection.execute(
                "SELECT * FROM extracted_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            connection.commit()
        finally:
            connection.close()
        self.send_json({"record": dict(row)})

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
