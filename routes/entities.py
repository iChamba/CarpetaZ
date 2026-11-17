import mimetypes
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from catalog import catalog_status, parse_pagination
from database import connect_db
from documents import extract_legacy_doc_text, extract_pdf_text, parse_docx, safe_relative_path
from extraction import extract_license_details, extract_operation_license
from settings import ALLOWED_EXTENSIONS, APP_DATA, ENTITY_ROOT

IMPORTABLE_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}
PREVIEWABLE_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".jpg", ".jpeg", ".png"}
ENTITY_IMPORT_LOCK = threading.Lock()
ENTITY_IMPORT_STATE_LOCK = threading.Lock()
ENTITY_IMPORT_STATE = {
    "has_run": False,
    "running": False,
    "total": 0,
    "processed": 0,
    "imported": 0,
    "updated": 0,
    "without_text": 0,
    "without_license": 0,
    "errors": 0,
    "error_details": [],
    "message": "Importación de licencias sin iniciar",
}


def _entity_name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _catalog_path_bounds(relative_path: str) -> tuple[str, str]:
    prefix = relative_path.rstrip("/") + "/"
    return prefix, prefix + "\U0010ffff"


def _is_numbered_division(name: str) -> bool:
    return bool(re.match(r"^\s*\d+(?:[.)-]\s*|\s+)\S", name))


def _entity_areas() -> tuple[list[dict[str, str | Path]], int]:
    if not ENTITY_ROOT.is_dir():
        raise FileNotFoundError("No se encontró la carpeta de entidades usuarias.")

    warnings = 0

    def directories(folder: Path) -> list[Path]:
        nonlocal warnings
        try:
            with os.scandir(folder) as entries:
                return sorted(
                    (
                        Path(entry.path)
                        for entry in entries
                        if not entry.name.startswith(".")
                        and entry.is_dir(follow_symlinks=False)
                    ),
                    key=lambda path: path.name.casefold(),
                )
        except OSError:
            warnings += 1
            return []

    areas: list[dict[str, str | Path]] = []
    app_data = os.path.normcase(str(APP_DATA))

    def is_entity(children: list[Path]) -> bool:
        return bool(children) and sum(
            _is_numbered_division(folder.name) for folder in children
        ) > len(children) / 2

    def walk(folder: Path, practice: str, ancestors: list[str]) -> None:
        if os.path.normcase(os.path.abspath(folder)) == app_data:
            return
        children = directories(folder)
        if is_entity(children):
            areas.append({
                "practice": practice,
                "department": ancestors[0] if ancestors else "",
                "locality": ancestors[1] if len(ancestors) > 1 else "",
                "institution": folder.name,
                "path": folder,
                "relative_path": folder.relative_to(ENTITY_ROOT).as_posix(),
            })
            return
        if len(ancestors) >= 4:
            return
        for child in children:
            walk(child, practice, ancestors + [folder.name])

    for practice_folder in directories(ENTITY_ROOT):
        for child in directories(practice_folder):
            if child.name.lstrip().startswith("-"):
                continue
            walk(child, practice_folder.name, [])
    return areas, warnings


def _entity_area_folder(relative_path: object) -> Path:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("La ruta de la institución no es válida.")
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or len(relative.parts) not in {2, 3, 4, 5}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("La ruta de la institución no es válida.")
    folder = ENTITY_ROOT / relative
    current = ENTITY_ROOT
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("No se permiten carpetas de institución que sean enlaces.")
    resolved_root = ENTITY_ROOT.resolve()
    resolved = folder.resolve(strict=True)
    try:
        common = os.path.commonpath((str(resolved_root), str(resolved)))
    except ValueError as exc:
        raise ValueError("La carpeta está fuera de la raíz de entidades usuarias.") from exc
    if os.path.normcase(common) != os.path.normcase(str(resolved_root)):
        raise ValueError("La carpeta está fuera de la raíz de entidades usuarias.")
    if not resolved.is_dir() or os.path.normcase(str(resolved)) == os.path.normcase(str(APP_DATA)):
        raise FileNotFoundError
    return resolved


def _entity_files(folder: Path) -> tuple[list[Path], int]:
    files: list[Path] = []
    warnings = 0

    def on_error(_error: OSError) -> None:
        nonlocal warnings
        warnings += 1

    for current, directories, filenames in os.walk(folder, onerror=on_error):
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and not (Path(current) / name).is_symlink()
        ]
        for filename in filenames:
            path = Path(current) / filename
            if path.suffix.lower() in ALLOWED_EXTENSIONS and not path.is_symlink():
                files.append(path)
    files.sort(key=lambda path: path.as_posix().casefold())
    return files, warnings


def _extract_text(path: Path) -> str:
    extension = path.suffix.lower()
    if extension == ".pdf":
        return extract_pdf_text(path)
    if extension == ".doc":
        return extract_legacy_doc_text(path)
    if extension == ".docx":
        return parse_docx(path)
    if extension == ".txt":
        return path.read_text(encoding="utf-8-sig")
    raise ValueError("La extracción de licencias está disponible para PDF, DOCX y TXT.")


def _save_license(relative: str, license_number: str, details: dict[str, str] | None = None) -> bool:
    details = details or {}
    connection = connect_db()
    try:
        previous = connection.execute(
            "SELECT license_number FROM entity_licenses WHERE source_path = ?",
            (relative,),
        ).fetchone()
        connection.execute(
            "INSERT INTO entity_licenses(source_path, created_at, license_number, "
            "representative, authorization_number, issue_date, expiry_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_path) DO UPDATE SET "
            "created_at = excluded.created_at, license_number = excluded.license_number, "
            "representative = excluded.representative, "
            "authorization_number = excluded.authorization_number, "
            "issue_date = excluded.issue_date, expiry_date = excluded.expiry_date",
            (
                relative,
                datetime.now().isoformat(timespec="seconds"),
                license_number,
                details.get("representative", ""),
                details.get("authorization_number", ""),
                details.get("issue_date", ""),
                details.get("expiry_date", ""),
            ),
        )
        connection.commit()
        return previous is not None
    finally:
        connection.close()


def _parse_card_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except ValueError:
        return datetime.min


def _build_license_cards(rows) -> list[dict]:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["license_number"], []).append(row)
    cards = []
    for number, sources in groups.items():
        # El documento de la licencia (con vencimiento) y el más reciente aportan los datos.
        ordered = sorted(
            sources,
            key=lambda row: (
                bool(row["expiry_date"]),
                _parse_card_date(row["issue_date"]),
                row["path"].casefold(),
            ),
            reverse=True,
        )

        def pick(field: str) -> str:
            return next((row[field] for row in ordered if row[field]), "")

        cards.append({
            "license_number": number,
            "representative": pick("representative"),
            "authorization_number": pick("authorization_number"),
            "issue_date": pick("issue_date"),
            "expiry_date": pick("expiry_date"),
            "source_path": ordered[0]["path"],
            "documents": [
                {"path": row["path"], "name": row["name"]}
                for row in sorted(sources, key=lambda row: row["path"].casefold())
            ],
        })
    return cards


def _authorization_folders(area_folder: Path) -> list[Path]:
    try:
        with os.scandir(area_folder) as entries:
            return [
                Path(entry.path)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
                and re.match(
                    r"^\s*0*1\s*[.)-]?\s*autorizaci[oó]n",
                    unicodedata.normalize("NFKC", entry.name),
                    re.IGNORECASE,
                )
            ]
    except OSError:
        return []


def _entity_import_worker() -> None:
    try:
        files: list[Path] = []
        errors: list[str] = []
        areas, area_warnings = _entity_areas()
        if area_warnings:
            errors.append(f"{area_warnings} ubicación(es) inaccesibles al buscar instituciones")
        for area in areas:
            for folder in _authorization_folders(Path(area["path"])):
                entity_files, warnings = _entity_files(folder)
                files.extend(
                    path for path in entity_files
                    if path.suffix.lower() in IMPORTABLE_EXTENSIONS
                    and not path.name.startswith("~$")
                )
                if warnings:
                    errors.append(
                        f"{area['relative_path']}: {warnings} ubicación(es) inaccesibles"
                    )
        files.sort(key=lambda path: path.as_posix().casefold())
        with ENTITY_IMPORT_STATE_LOCK:
            ENTITY_IMPORT_STATE.update({
                "total": len(files),
                "processed": 0,
                "imported": 0,
                "updated": 0,
                "without_text": 0,
                "without_license": 0,
                "errors": len(errors),
                "error_details": errors[:20],
                "message": "Buscando licencias de operación",
            })

        for path in files:
            relative = path.relative_to(ENTITY_ROOT).as_posix()
            try:
                safe_path, relative = safe_relative_path(relative, ENTITY_ROOT)
                text = _extract_text(safe_path)
                if not text.strip():
                    with ENTITY_IMPORT_STATE_LOCK:
                        ENTITY_IMPORT_STATE["without_text"] += 1
                    continue
                license_number = (
                    extract_operation_license(path.name) or extract_operation_license(text)
                )
                if not license_number:
                    with ENTITY_IMPORT_STATE_LOCK:
                        ENTITY_IMPORT_STATE["without_license"] += 1
                    continue
                if _save_license(relative, license_number, extract_license_details(text)):
                    with ENTITY_IMPORT_STATE_LOCK:
                        ENTITY_IMPORT_STATE["updated"] += 1
                else:
                    with ENTITY_IMPORT_STATE_LOCK:
                        ENTITY_IMPORT_STATE["imported"] += 1
            except Exception as exc:
                with ENTITY_IMPORT_STATE_LOCK:
                    ENTITY_IMPORT_STATE["errors"] += 1
                    if len(ENTITY_IMPORT_STATE["error_details"]) < 20:
                        ENTITY_IMPORT_STATE["error_details"].append(f"{relative}: {exc}")
            finally:
                with ENTITY_IMPORT_STATE_LOCK:
                    ENTITY_IMPORT_STATE["processed"] += 1
                    ENTITY_IMPORT_STATE["message"] = (
                        f"Revisados {ENTITY_IMPORT_STATE['processed']} "
                        f"de {ENTITY_IMPORT_STATE['total']} archivos"
                    )
        with ENTITY_IMPORT_STATE_LOCK:
            ENTITY_IMPORT_STATE["message"] = "Revisión de licencias finalizada"
    except Exception as exc:
        with ENTITY_IMPORT_STATE_LOCK:
            ENTITY_IMPORT_STATE["errors"] += 1
            ENTITY_IMPORT_STATE["message"] = f"No se pudo completar la importación: {exc}"
            if len(ENTITY_IMPORT_STATE["error_details"]) < 20:
                ENTITY_IMPORT_STATE["error_details"].append(str(exc))
    finally:
        with ENTITY_IMPORT_STATE_LOCK:
            ENTITY_IMPORT_STATE["running"] = False
        ENTITY_IMPORT_LOCK.release()


class EntityRoutes:
    def list_entities(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip().casefold()
        practice_filter = _entity_name_key(params.get("practice", [""])[0])
        department_filter = _entity_name_key(params.get("department", [""])[0])
        locality_filter = _entity_name_key(params.get("locality", [""])[0])
        offset, limit = parse_pagination(params)
        areas, warnings = _entity_areas()
        connection = connect_db()
        try:
            area_counts = {}
            for area in areas:
                lower, upper = _catalog_path_bounds(area["relative_path"])
                count = connection.execute(
                    "SELECT COUNT(*) AS document_count, "
                    "SUM(CASE WHEN license.source_path IS NOT NULL THEN 1 ELSE 0 END) "
                    "AS imported_count FROM file_catalog AS file "
                    "LEFT JOIN entity_licenses AS license ON license.source_path = file.path "
                    "WHERE file.root = 'entities' AND file.path >= ? AND file.path < ?",
                    (lower, upper),
                ).fetchone()
                area_counts[area["relative_path"]] = (
                    count["document_count"],
                    count["imported_count"] or 0,
                )
        finally:
            connection.close()

        entities: dict[str, dict] = {}
        for area in areas:
            key = _entity_name_key(area["institution"])
            entity = entities.setdefault(key, {
                "name": area["institution"],
                "document_count": 0,
                "imported_count": 0,
                "practices": set(),
                "departments": set(),
                "localities": set(),
                "area_count": 0,
            })
            document_count, imported_count = area_counts[area["relative_path"]]
            entity["document_count"] += document_count
            entity["area_count"] += 1
            entity["practices"].add(area["practice"])
            entity["departments"].add(area["department"])
            if area["locality"]:
                entity["localities"].add(area["locality"])
            entity["imported_count"] += imported_count

        items = []
        practices = set()
        departments = set()
        localities = set()
        for entity in entities.values():
            entity["practices"] = sorted(entity["practices"], key=str.casefold)
            entity["departments"] = sorted(entity["departments"], key=str.casefold)
            entity["localities"] = sorted(entity["localities"], key=str.casefold)
            practices.update(entity["practices"])
            departments.update(entity["departments"])
            localities.update(entity["localities"])
            search_text = " ".join(
                [
                    entity["name"],
                    *entity["practices"],
                    *entity["departments"],
                    *entity["localities"],
                ]
            ).casefold()
            if query and query not in search_text:
                continue
            if practice_filter and practice_filter not in {
                _entity_name_key(value) for value in entity["practices"]
            }:
                continue
            if department_filter and department_filter not in {
                _entity_name_key(value) for value in entity["departments"]
            }:
                continue
            if locality_filter and locality_filter not in {
                _entity_name_key(value) for value in entity["localities"]
            }:
                continue
            items.append(entity)
        items.sort(key=lambda entity: entity["name"].casefold())
        total = len(items)
        self.send_json({
            "items": items[offset:offset + limit],
            "total": total,
            "offset": offset,
            "limit": limit,
            "practices": sorted(practices, key=str.casefold),
            "departments": sorted(departments, key=str.casefold),
            "localities": sorted(localities, key=str.casefold),
            "warnings": warnings + catalog_status()["errors"],
            "catalog": catalog_status(),
        })

    def get_entity(self, params: dict[str, list[str]]) -> None:
        name = params.get("name", [""])[0]
        if not name or "/" in name or "\\" in name:
            raise ValueError("La institución indicada no es válida.")
        areas, warnings = _entity_areas()
        matched_areas = [
            area for area in areas
            if _entity_name_key(area["institution"]) == _entity_name_key(name)
        ]
        if not matched_areas:
            raise FileNotFoundError
        requested_area = params.get("area", [""])[0]
        if requested_area:
            area = next(
                (item for item in matched_areas if item["relative_path"] == requested_area),
                None,
            )
            if area is None:
                raise ValueError("El área solicitada no pertenece a esta institución.")
            offset, limit = parse_pagination(params, default_limit=50, max_limit=100)
            lower, upper = _catalog_path_bounds(requested_area)
            connection = connect_db()
            try:
                total = connection.execute(
                    "SELECT COUNT(*) FROM file_catalog "
                    "WHERE root = 'entities' AND path >= ? AND path < ?",
                    (lower, upper),
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT file.path, file.name, file.extension, file.size, "
                    "file.modified_ns, file.created_ns, license.license_number "
                    "FROM file_catalog AS file LEFT JOIN entity_licenses AS license "
                    "ON license.source_path = file.path "
                    "WHERE file.root = 'entities' AND file.path >= ? AND file.path < ? "
                    "ORDER BY file.created_ns DESC, file.path COLLATE NOCASE "
                    "LIMIT ? OFFSET ?",
                    (lower, upper, limit, offset),
                ).fetchall()
            finally:
                connection.close()
            return self.send_json({
                "files": [{
                    "path": row["path"],
                    "name": row["name"],
                    "extension": row["extension"],
                    "size": row["size"],
                    "modified": datetime.fromtimestamp(row["modified_ns"] / 1_000_000_000)
                    .strftime("%Y-%m-%d %H:%M"),
                    "created": datetime.fromtimestamp(row["created_ns"] / 1_000_000_000)
                    .strftime("%Y-%m-%d %H:%M"),
                    "can_import": row["extension"] in IMPORTABLE_EXTENSIONS,
                    "can_preview": row["extension"] in PREVIEWABLE_EXTENSIONS,
                    "license_number": row["license_number"] or "",
                } for row in rows],
                "total": total,
                "offset": offset,
                "limit": limit,
            })

        connection = connect_db()
        try:
            area_counts = {}
            for area in matched_areas:
                lower, upper = _catalog_path_bounds(area["relative_path"])
                count = connection.execute(
                    "SELECT COUNT(*) AS document_count, "
                    "SUM(CASE WHEN license.source_path IS NOT NULL THEN 1 ELSE 0 END) "
                    "AS imported_count FROM file_catalog AS file "
                    "LEFT JOIN entity_licenses AS license ON license.source_path = file.path "
                    "WHERE file.root = 'entities' AND file.path >= ? AND file.path < ?",
                    (lower, upper),
                ).fetchone()
                area_counts[area["relative_path"]] = (
                    count["document_count"],
                    count["imported_count"] or 0,
                )
                licenses = connection.execute(
                    "SELECT license.license_number, license.representative, "
                    "license.authorization_number, license.issue_date, license.expiry_date, "
                    "file.path, file.name "
                    "FROM file_catalog AS file JOIN entity_licenses AS license "
                    "ON license.source_path = file.path "
                    "WHERE file.root = 'entities' AND file.path >= ? AND file.path < ?",
                    (lower, upper),
                ).fetchall()
                area_counts[area["relative_path"]] += (
                    sorted({row["license_number"] for row in licenses}),
                    [dict(row) for row in licenses],
                )
        finally:
            connection.close()

        practices: dict[str, dict] = {}
        departments: set[str] = set()
        localities: set[str] = set()
        document_count = 0
        imported_count = 0
        for area in matched_areas:
            area_document_count, area_imported_count, area_licenses, area_cards = (
                area_counts[area["relative_path"]]
            )
            document_count += area_document_count
            imported_count += area_imported_count
            practice_key = _entity_name_key(area["practice"])
            practice = practices.setdefault(practice_key, {
                "name": area["practice"],
                "licenses": set(),
                "license_cards": [],
                "areas": [],
            })
            practice["licenses"].update(area_licenses)
            practice["license_cards"].extend(area_cards)
            practice["areas"].append({
                "department": area["department"],
                "locality": area["locality"],
                "path": area["relative_path"],
                "document_count": area_document_count,
                "imported_count": area_imported_count,
                "licenses": area_licenses,
                "files": [],
            })
            departments.add(area["department"])
            if area["locality"]:
                localities.add(area["locality"])
        practice_items = []
        for practice in practices.values():
            practice["licenses"] = sorted(practice["licenses"])
            practice["license_cards"] = _build_license_cards(practice["license_cards"])
            practice["areas"].sort(
                key=lambda area: (
                    area["department"].casefold(),
                    area["locality"].casefold(),
                    area["path"].casefold(),
                )
            )
            practice_items.append(practice)
        practice_items.sort(key=lambda practice: practice["name"].casefold())
        self.send_json({
            "entity": {
                "name": matched_areas[0]["institution"],
                "document_count": document_count,
                "imported_count": imported_count,
                "practice_count": len(practice_items),
                "area_count": sum(len(practice["areas"]) for practice in practice_items),
                "departments": sorted(departments, key=str.casefold),
                "localities": sorted(localities, key=str.casefold),
            },
            "practices": practice_items,
            "warnings": warnings + catalog_status()["errors"],
            "catalog": catalog_status(),
        })

    def extract_entity_license(self, data: dict) -> None:
        relative = data.get("path")
        if not isinstance(relative, str):
            raise ValueError("Falta la ruta del archivo.")
        path, relative = safe_relative_path(relative, ENTITY_ROOT)
        text = _extract_text(path)
        if not text.strip():
            return self.send_error_json(
                "Este archivo no contiene texto utilizable. Si es un escaneo, se necesitaría OCR.",
                422,
            )
        license_number = (
            extract_operation_license(path.name) or extract_operation_license(text)
        )
        if not license_number:
            return self.send_error_json(
                "No se encontró una licencia con formato LO-X-XXXX-XXXX-XX en el documento.",
                422,
            )
        updated = _save_license(relative, license_number, extract_license_details(text))
        self.send_json({"path": relative, "license_number": license_number, "updated": updated})

    def start_entity_import(self) -> None:
        if not ENTITY_ROOT.is_dir():
            raise FileNotFoundError("No se encontró la carpeta de entidades usuarias.")
        if not ENTITY_IMPORT_LOCK.acquire(blocking=False):
            return self.send_error_json("La revisión de licencias ya está en curso.", 409)
        with ENTITY_IMPORT_STATE_LOCK:
            ENTITY_IMPORT_STATE.update({
                "has_run": True,
                "running": True,
                "total": 0,
                "processed": 0,
                "imported": 0,
                "updated": 0,
                "without_text": 0,
                "without_license": 0,
                "errors": 0,
                "error_details": [],
                "message": "Preparando revisión de licencias",
            })
        try:
            threading.Thread(target=_entity_import_worker, daemon=True).start()
        except Exception:
            with ENTITY_IMPORT_STATE_LOCK:
                ENTITY_IMPORT_STATE["running"] = False
            ENTITY_IMPORT_LOCK.release()
            raise
        self.send_json({"started": True}, 202)

    def entity_import_status(self) -> None:
        with ENTITY_IMPORT_STATE_LOCK:
            state = dict(ENTITY_IMPORT_STATE)
            state["error_details"] = list(ENTITY_IMPORT_STATE["error_details"])
        self.send_json(state)

    def entity_preview(self, params: dict[str, list[str]]) -> None:
        path, _ = safe_relative_path(params.get("path", [""])[0], ENTITY_ROOT)
        extension = path.suffix.lower()
        if extension == ".docx":
            content = parse_docx(path).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif extension == ".doc":
            content = extract_legacy_doc_text(path).encode("utf-8")
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

    def entity_download(self, params: dict[str, list[str]]) -> None:
        path, _ = safe_relative_path(params.get("path", [""])[0], ENTITY_ROOT)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def open_entity_location(self, data: dict) -> None:
        relative = data.get("path")
        if not isinstance(relative, str):
            raise ValueError("Falta la ruta del archivo.")
        if os.name != "nt":
            raise RuntimeError("Abrir una ubicación en el Explorador requiere Windows.")
        path, relative = safe_relative_path(relative, ENTITY_ROOT)
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except OSError as exc:
            raise RuntimeError("No se pudo iniciar el Explorador de archivos.") from exc
        self.send_json({"opened": True, "path": relative}, 202)

    def open_entity_folder(self, data: dict) -> None:
        folder = _entity_area_folder(data.get("path"))
        if os.name != "nt":
            raise RuntimeError("Abrir una carpeta en el Explorador requiere Windows.")
        try:
            subprocess.Popen(["explorer.exe", str(folder)])
        except OSError as exc:
            raise RuntimeError("No se pudo iniciar el Explorador de archivos.") from exc
        self.send_json({
            "opened": True,
            "path": folder.relative_to(ENTITY_ROOT).as_posix(),
        }, 202)
