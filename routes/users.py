import os
from datetime import datetime
from pathlib import Path

from database import connect_db
from documents import user_folder_from_path
from extraction import is_authorization_certificate
from settings import ALLOWED_EXTENSIONS, APP_DATA, DOCUMENT_ROOT

IMPORTABLE_EXTENSIONS = {".pdf", ".docx", ".txt"}
PREVIEWABLE_EXTENSIONS = {".pdf", ".docx", ".txt", ".jpg", ".jpeg", ".png"}


class UserRoutes:
    def list_users(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip().casefold()
        connection = connect_db()
        try:
            records = connection.execute(
                "SELECT id, source_path, first_name, last_name, identity_number, "
                "authorization_number, practice, issue_date, expiry_date, equipment, "
                "is_authorization_certificate "
                "FROM extracted_records"
            ).fetchall()
        finally:
            connection.close()

        imported_counts: dict[str, int] = {}
        authorization_records_by_path = {}
        for record in records:
            name = user_folder_from_path(record["source_path"])
            imported_counts[name] = imported_counts.get(name, 0) + 1
            if record["is_authorization_certificate"] or (
                Path(record["source_path"]).suffix.lower() == ".pdf"
                and is_authorization_certificate(Path(record["source_path"]))
            ):
                authorization_records_by_path[record["source_path"]] = {
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
                }

        users = []
        warnings = 0
        with os.scandir(DOCUMENT_ROOT) as entries:
            directories = sorted(
                (
                    entry for entry in entries
                    if not entry.name.startswith(".")
                    and entry.is_dir(follow_symlinks=False)
                    and os.path.normcase(os.path.abspath(entry.path))
                    != os.path.normcase(str(APP_DATA))
                    and (not query or query in entry.name.casefold())
                ),
                key=lambda entry: entry.name.casefold(),
            )
        for entry in directories:
            folder = Path(entry.path)
            file_count = 0
            authorization_paths = set()

            def on_error(_error: OSError) -> None:
                nonlocal warnings
                warnings += 1

            for _current, subdirectories, filenames in os.walk(folder, onerror=on_error):
                subdirectories[:] = [
                    name for name in subdirectories
                    if not name.startswith(".") and not (Path(_current) / name).is_symlink()
                ]
                for filename in filenames:
                    path = Path(_current) / filename
                    extension = path.suffix.lower()
                    if extension not in ALLOWED_EXTENSIONS:
                        continue
                    file_count += 1
                    relative = path.relative_to(DOCUMENT_ROOT).as_posix()
                    if relative in authorization_records_by_path or (
                        extension == ".pdf" and is_authorization_certificate(path)
                    ):
                        authorization_paths.add(relative)

            authorization_records = [
                authorization_records_by_path[path]
                for path in sorted(authorization_paths)
                if path in authorization_records_by_path
            ]
            users.append({
                "name": entry.name,
                "document_count": file_count,
                "imported_count": imported_counts.get(entry.name, 0),
                "has_authorization": bool(authorization_paths),
                "authorization_records": authorization_records,
            })

        self.send_json({"items": users, "warnings": warnings})

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

        connection = connect_db()
        try:
            records = connection.execute(
                "SELECT * FROM extracted_records ORDER BY created_at DESC, id DESC"
            ).fetchall()
        finally:
            connection.close()
        record_by_path = {
            record["source_path"]: dict(record)
            for record in records
            if user_folder_from_path(record["source_path"]) == name
        }

        files = []
        warnings = 0

        def on_error(_error: OSError) -> None:
            nonlocal warnings
            warnings += 1

        for current, directories, filenames in os.walk(resolved_folder, onerror=on_error):
            directories[:] = [
                directory for directory in directories
                if not directory.startswith(".") and not (Path(current) / directory).is_symlink()
            ]
            for filename in filenames:
                path = Path(current) / filename
                extension = path.suffix.lower()
                if extension not in ALLOWED_EXTENSIONS:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    warnings += 1
                    continue
                relative = path.relative_to(DOCUMENT_ROOT).as_posix()
                record = record_by_path.get(relative)
                files.append({
                    "path": relative,
                    "name": path.name,
                    "extension": extension,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "can_import": extension in IMPORTABLE_EXTENSIONS,
                    "can_preview": extension in PREVIEWABLE_EXTENSIONS,
                    "is_authorization_certificate": (
                        bool(record and record["is_authorization_certificate"])
                        or (extension == ".pdf" and is_authorization_certificate(path))
                    ),
                    "record": record,
                })

        files.sort(key=lambda item: item["path"].casefold())
        self.send_json({
            "user": {
                "name": name,
                "document_count": len(files),
                "imported_count": len(record_by_path),
            },
            "files": files,
            "records": [
                record_by_path[item["path"]]
                for item in files
                if item["path"] in record_by_path
            ],
            "warnings": warnings,
        })
