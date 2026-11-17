import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from database import connect_db
from extraction import is_authorization_certificate
from settings import (
    ALLOWED_EXTENSIONS,
    APP_DATA,
    DOCUMENT_ROOT,
    ENTITY_ROOT,
    INDEX_STATE,
    INDEX_STATE_LOCK,
)

BATCH_SIZE = 500
CATALOG_ROOTS = (
    ("documents", DOCUMENT_ROOT),
    ("entities", ENTITY_ROOT),
)


def _update_state(**values: object) -> None:
    with INDEX_STATE_LOCK:
        INDEX_STATE.update(values)


def parse_pagination(
    params: dict[str, list[str]],
    *,
    default_limit: int = 100,
    max_limit: int = 200,
) -> tuple[int, int]:
    try:
        offset = max(0, int(params.get("offset", ["0"])[0]))
        limit = min(max_limit, max(1, int(params.get("limit", [str(default_limit)])[0])))
    except ValueError:
        raise ValueError("La página solicitada no es válida.") from None
    return offset, limit


def catalog_status() -> dict:
    with INDEX_STATE_LOCK:
        return {
            "running": INDEX_STATE["catalog_running"],
            "processed": INDEX_STATE["catalog_processed"],
            "total": INDEX_STATE["catalog_total"],
            "errors": INDEX_STATE["catalog_errors"],
            "ready": INDEX_STATE["catalog_ready"],
            "message": INDEX_STATE["catalog_message"],
        }


def sync_file_catalog(
    roots: tuple[tuple[str, Path], ...] = CATALOG_ROOTS,
) -> None:
    _update_state(
        catalog_running=True,
        catalog_processed=0,
        catalog_total=0,
        catalog_errors=0,
        catalog_message="Catalogando archivos",
    )
    errors = 0
    processed = 0
    documents_ready = False
    connection = connect_db()
    try:
        for root_key, root in roots:
            if not root.is_dir():
                if root_key == "entities":
                    continue
                raise FileNotFoundError(f"No se encontró la carpeta: {root}")

            scan_id = uuid4().hex
            batch: list[tuple[str, str, str, str, str, int, int, int, int, str]] = []
            root_errors = 0

            def on_walk_error(_error: OSError) -> None:
                nonlocal root_errors
                root_errors += 1

            for current, directories, filenames in os.walk(root, onerror=on_walk_error):
                directories[:] = [
                    name for name in directories
                    if not name.startswith(".") and not (Path(current) / name).is_symlink()
                    and os.path.normcase(os.path.abspath(Path(current) / name))
                    != os.path.normcase(str(APP_DATA))
                ]
                for filename in filenames:
                    path = Path(current) / filename
                    extension = path.suffix.lower()
                    if extension not in ALLOWED_EXTENSIONS or path.is_symlink():
                        continue
                    try:
                        stat = path.stat()
                        relative = path.relative_to(root).as_posix()
                    except OSError:
                        root_errors += 1
                        continue
                    parts = Path(relative).parts
                    owner = parts[0] if len(parts) > 1 else ""
                    is_certificate = int(
                        extension == ".pdf" and is_authorization_certificate(path)
                    )
                    batch.append((
                        root_key,
                        relative,
                        path.name,
                        extension,
                        owner,
                        stat.st_size,
                        stat.st_mtime_ns,
                        getattr(stat, "st_birthtime_ns", stat.st_ctime_ns),
                        is_certificate,
                        scan_id,
                    ))
                    processed += 1
                    if len(batch) >= BATCH_SIZE:
                        _save_batch(connection, batch)
                        batch.clear()
                        _update_state(
                            catalog_processed=processed,
                            catalog_errors=errors + root_errors,
                            catalog_message=f"Catalogando archivos: {processed} encontrados",
                        )

            if batch:
                _save_batch(connection, batch)

            errors += root_errors
            if not root_errors:
                connection.execute(
                    "DELETE FROM file_catalog WHERE root = ? AND scan_id != ?",
                    (root_key, scan_id),
                )
                connection.execute(
                    "INSERT INTO file_catalog_state(root, last_success, file_count) "
                    "VALUES (?, ?, (SELECT COUNT(*) FROM file_catalog WHERE root = ?)) "
                    "ON CONFLICT(root) DO UPDATE SET last_success = excluded.last_success, "
                    "file_count = excluded.file_count",
                    (root_key, datetime.now().isoformat(timespec="seconds"), root_key),
                )
                connection.commit()
                if root_key == "documents":
                    documents_ready = True
            else:
                connection.commit()
                _update_state(
                    catalog_message=(
                        f"Catálogo parcial: {root_errors} ubicación(es) inaccesibles"
                    ),
                )
    except Exception as exc:
        connection.rollback()
        errors += 1
        _update_state(catalog_message=f"No se pudo completar el catálogo: {exc}")
        raise
    finally:
        connection.close()
        with INDEX_STATE_LOCK:
            INDEX_STATE.update({
                "catalog_running": False,
                "catalog_processed": processed,
                "catalog_total": processed,
                "catalog_errors": errors,
                "catalog_ready": INDEX_STATE["catalog_ready"] or documents_ready,
                "catalog_message": (
                    "Catálogo actualizado"
                    if errors == 0 and documents_ready
                    else INDEX_STATE["catalog_message"]
                ),
            })


def _save_batch(connection, rows: list[tuple]) -> None:
    connection.executemany(
        "INSERT INTO file_catalog "
        "(root, path, name, extension, owner, size, modified_ns, created_ns, "
        "is_authorization_certificate, scan_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(root, path) DO UPDATE SET name = excluded.name, "
        "extension = excluded.extension, owner = excluded.owner, "
        "size = excluded.size, modified_ns = excluded.modified_ns, "
        "created_ns = excluded.created_ns, "
        "is_authorization_certificate = excluded.is_authorization_certificate, "
        "scan_id = excluded.scan_id",
        rows,
    )
    connection.commit()
