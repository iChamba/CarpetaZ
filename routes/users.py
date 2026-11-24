import os
import re
import unicodedata
from calendar import monthrange
from datetime import datetime
from datetime import date
from functools import cmp_to_key
from pathlib import Path

from catalog import catalog_status, parse_pagination
from database import connect_db
from documents import user_folder_from_path
from extraction import is_authorization_certificate
from settings import ALLOWED_EXTENSIONS, APP_DATA, DOCUMENT_ROOT

IMPORTABLE_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}
PREVIEWABLE_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".jpg", ".jpeg", ".png"}


def _authorization_date(value: str) -> date | None:
    for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value or "", pattern).date()
        except ValueError:
            continue
    return None


def _authorization_version(value: str) -> tuple[str, int, int] | None:
    match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$", value or "")
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def _compare_authorizations(left: dict, right: dict) -> int:
    left_version = _authorization_version(left["authorization_number"])
    right_version = _authorization_version(right["authorization_number"])
    if left_version and right_version and left_version[0] == right_version[0]:
        renewal_order = right_version[1] - left_version[1]
        if renewal_order:
            return renewal_order
        modification_order = right_version[2] - left_version[2]
        if modification_order:
            return modification_order

    left_date = _authorization_date(left["issue_date"]) or _authorization_date(left["expiry_date"])
    right_date = _authorization_date(right["issue_date"]) or _authorization_date(right["expiry_date"])
    if left_date and right_date:
        date_order = (right_date > left_date) - (right_date < left_date)
        if date_order:
            return date_order
    elif left_date:
        return -1
    elif right_date:
        return 1
    left_path = left["source_path"].casefold()
    right_path = right["source_path"].casefold()
    return (right_path > left_path) - (right_path < left_path)


def _authorization_status(user: dict) -> str:
    if not user["has_authorization"]:
        return "none"
    records = user["authorization_records"]
    if not records:
        return "pending"
    latest = sorted(records, key=cmp_to_key(_compare_authorizations))[0]
    expiry_date = _authorization_date(latest["expiry_date"])
    if not expiry_date:
        return "unknown"
    today = date.today()
    if expiry_date < today:
        return "expired"
    target_month = today.month + 2
    target_year = today.year + (target_month - 1) // 12
    target_month = (target_month - 1) % 12 + 1
    threshold = today.replace(
        year=target_year,
        month=target_month,
        day=min(today.day, monthrange(target_year, target_month)[1]),
    )
    return "soon" if expiry_date <= threshold else "valid"


def _normalized_practice(value: str) -> str:
    value = unicodedata.normalize("NFD", value or "")
    return " ".join(
        "".join(character for character in value if not unicodedata.combining(character))
        .casefold()
        .split()
    )


class UserRoutes:
    def list_users(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip().casefold()
        search_identity = params.get("search_identity", [""])[0] == "1"
        identity_query = (
            re.sub(r"[^0-9]", "", query)
            if search_identity and re.fullmatch(r"[0-9.\-\s]+", query)
            else ""
        )
        authorization_filter = params.get("authorization", ["all"])[0]
        status_filter = params.get("status", [""])[0]
        practice_filter = _normalized_practice(params.get("practice", [""])[0])
        offset, limit = parse_pagination(params)
        connection = connect_db()
        try:
            catalog_users = connection.execute(
                "SELECT owner, COUNT(*) AS document_count "
                "FROM file_catalog WHERE root = 'documents' AND owner != '' "
                "GROUP BY owner ORDER BY owner COLLATE NOCASE"
            ).fetchall()
            certificate_paths = connection.execute(
                "SELECT owner, path FROM file_catalog "
                "WHERE root = 'documents' AND is_authorization_certificate = 1"
            ).fetchall()
            records = connection.execute(
                "SELECT id, source_path, first_name, last_name, identity_number, "
                "authorization_number, practice, issue_date, expiry_date, equipment, "
                "is_authorization_certificate "
                "FROM extracted_records "
                # Los datos de archivos que ya no están en la carpeta no se muestran
                # (se conservan en la base por si el archivo vuelve).
                "WHERE source_path IN (SELECT path FROM file_catalog WHERE root = 'documents')"
            ).fetchall()
        finally:
            connection.close()

        imported_counts: dict[str, int] = {}
        authorization_records_by_owner: dict[str, list[dict]] = {}
        authorization_paths_by_owner: dict[str, set[str]] = {}
        for item in certificate_paths:
            authorization_paths_by_owner.setdefault(item["owner"], set()).add(item["path"])
        for record in records:
            name = user_folder_from_path(record["source_path"])
            imported_counts[name] = imported_counts.get(name, 0) + 1
            if record["is_authorization_certificate"] or (
                Path(record["source_path"]).suffix.lower() == ".pdf"
                and is_authorization_certificate(Path(record["source_path"]))
            ):
                authorization_paths_by_owner.setdefault(name, set()).add(record["source_path"])
                authorization_records_by_owner.setdefault(name, []).append({
                    "id": record["id"],
                    "source_path": record["source_path"],
                    "first_name": record["first_name"],
                    "last_name": record["last_name"],
                    "identity_number": record["identity_number"],
                    "authorization_number": record["authorization_number"],
                    "practice": record["practice"],
                    "issue_date": record["issue_date"],
                    "expiry_date": record["expiry_date"],
                    "equipment": record["equipment"],
                })

        users = []
        for row in catalog_users:
            name = row["owner"]
            user = {
                "name": name,
                "document_count": row["document_count"],
                "imported_count": imported_counts.get(name, 0),
                "has_authorization": bool(authorization_paths_by_owner.get(name)),
                "authorization_records": sorted(
                    authorization_records_by_owner.get(name, []),
                    key=cmp_to_key(_compare_authorizations),
                ),
            }
            matched_authorization = next(
                (record for record in user["authorization_records"]
                 if identity_query and identity_query in re.sub(
                     r"[^0-9]", "", record["identity_number"]
                 )),
                None,
            )
            if query and query not in name.casefold() and not matched_authorization:
                continue
            if matched_authorization:
                user["matched_authorization"] = matched_authorization
            if authorization_filter == "with" and not user["has_authorization"]:
                continue
            if authorization_filter == "without" and user["has_authorization"]:
                continue
            if status_filter and _authorization_status(user) != status_filter:
                continue
            if practice_filter and not any(
                practice_filter in _normalized_practice(record["practice"])
                for record in user["authorization_records"]
            ):
                continue
            users.append(user)

        total = len(users)
        summary = {value: 0 for value in ("valid", "soon", "expired", "unknown", "pending", "none")}
        for user in users:
            summary[_authorization_status(user)] += 1
        self.send_json({
            "items": users[offset:offset + limit],
            "total": total,
            "offset": offset,
            "limit": limit,
            "summary": summary,
            "warnings": catalog_status()["errors"],
            "catalog": catalog_status(),
        })

    def get_user(self, params: dict[str, list[str]]) -> None:
        name = params.get("name", [""])[0]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("El usuario indicado no es válido.")

        folder = DOCUMENT_ROOT / name
        if folder.is_symlink():
            raise ValueError("No se permiten carpetas de usuario que sean enlaces.")
        resolved_folder = folder.resolve(strict=True)
        if not resolved_folder.is_dir() or os.path.commonpath(
            (str(DOCUMENT_ROOT), str(resolved_folder))
        ) != str(DOCUMENT_ROOT):
            raise FileNotFoundError
        if os.path.normcase(str(resolved_folder)) == os.path.normcase(str(APP_DATA)):
            raise FileNotFoundError

        offset, limit = parse_pagination(params)
        connection = connect_db()
        try:
            records = connection.execute(
                "SELECT * FROM extracted_records "
                "WHERE substr(source_path, 1, instr(source_path, '/') - 1) = ? "
                "ORDER BY created_at DESC, id DESC",
                (name,),
            ).fetchall()
            total_files = connection.execute(
                "SELECT COUNT(*) FROM file_catalog WHERE root = 'documents' AND owner = ?",
                (name,),
            ).fetchone()[0]
            catalog_files = connection.execute(
                "SELECT path, name, extension, size, modified_ns, created_ns, "
                "is_authorization_certificate FROM file_catalog "
                "WHERE root = 'documents' AND owner = ? "
                "ORDER BY created_ns DESC, path COLLATE NOCASE LIMIT ? OFFSET ?",
                (name, limit, offset),
            ).fetchall()
            certificate_files = connection.execute(
                "SELECT path, name, extension, size, modified_ns, created_ns, "
                "is_authorization_certificate FROM file_catalog "
                "WHERE root = 'documents' AND owner = ? "
                "AND is_authorization_certificate = 1 "
                "ORDER BY created_ns DESC, path COLLATE NOCASE",
                (name,),
            ).fetchall()
            authorization_records = connection.execute(
                "SELECT record.* FROM extracted_records AS record "
                "JOIN file_catalog AS file ON file.root = 'documents' "
                "AND file.path = record.source_path "
                "WHERE file.owner = ? AND (record.is_authorization_certificate = 1 "
                "OR file.is_authorization_certificate = 1)",
                (name,),
            ).fetchall()
            content_certificate_files = connection.execute(
                "SELECT DISTINCT file.path, file.name, file.extension, file.size, "
                "file.modified_ns, file.created_ns, file.is_authorization_certificate "
                "FROM file_catalog AS file JOIN extracted_records AS record "
                "ON record.source_path = file.path "
                "WHERE file.root = 'documents' AND file.owner = ? "
                "AND record.is_authorization_certificate = 1",
                (name,),
            ).fetchall()
        finally:
            connection.close()
        record_by_path = {
            record["source_path"]: dict(record)
            for record in records
        }

        def file_item(row) -> dict:
            extension = row["extension"]
            record = record_by_path.get(row["path"])
            return {
                "path": row["path"],
                "name": row["name"],
                "extension": extension,
                "size": row["size"],
                "modified": datetime.fromtimestamp(row["modified_ns"] / 1_000_000_000)
                .strftime("%Y-%m-%d %H:%M"),
                "created": datetime.fromtimestamp(row["created_ns"] / 1_000_000_000)
                .strftime("%Y-%m-%d %H:%M"),
                "can_import": extension in IMPORTABLE_EXTENSIONS,
                "can_preview": extension in PREVIEWABLE_EXTENSIONS,
                "is_authorization_certificate": (
                    bool(row["is_authorization_certificate"])
                    or bool(record and record["is_authorization_certificate"])
                ),
                "record": record,
            }

        files = [file_item(row) for row in catalog_files]
        authorization_files_by_path = {
            row["path"]: file_item(row) for row in certificate_files
        }
        authorization_files_by_path.update({
            row["path"]: file_item(row) for row in content_certificate_files
        })
        authorization_records = [dict(record) for record in authorization_records]
        authorization_records.sort(key=cmp_to_key(_compare_authorizations))

        self.send_json({
            "user": {
                "name": name,
                "document_count": total_files,
                "imported_count": len(record_by_path),
            },
            "files": files,
            "records": [item["record"] for item in files if item["record"]],
            "authorization_files": list(authorization_files_by_path.values()),
            "authorization_records": authorization_records,
            "offset": offset,
            "limit": limit,
            "warnings": catalog_status()["errors"],
        })
