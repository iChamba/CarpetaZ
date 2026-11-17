import sqlite3

from settings import DATABASE, INDEX_STATE, INDEX_STATE_LOCK


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db() -> None:
    connection = connect_db()
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pdf_fts "
            "USING fts5(path UNINDEXED, content, tokenize='unicode61')"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS pdf_index_meta ("
            "path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS file_catalog ("
            "root TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, "
            "extension TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', "
            "size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, "
            "created_ns INTEGER NOT NULL DEFAULT 0, "
            "is_authorization_certificate INTEGER NOT NULL DEFAULT 0, "
            "scan_id TEXT NOT NULL, PRIMARY KEY(root, path))"
        )
        catalog_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(file_catalog)")
        }
        if "created_ns" not in catalog_columns:
            connection.execute(
                "ALTER TABLE file_catalog ADD COLUMN created_ns INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_catalog_owner "
            "ON file_catalog(root, owner, path)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_catalog_extension "
            "ON file_catalog(root, extension, path)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS file_catalog_state ("
            "root TEXT PRIMARY KEY, last_success TEXT NOT NULL, "
            "file_count INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS extracted_records ("
            "id INTEGER PRIMARY KEY, source_path TEXT NOT NULL UNIQUE, "
            "created_at TEXT NOT NULL, first_name TEXT NOT NULL DEFAULT '', "
            "last_name TEXT NOT NULL DEFAULT '', identity_number TEXT NOT NULL DEFAULT '', "
            "authorization_number TEXT NOT NULL DEFAULT '', practice TEXT NOT NULL DEFAULT '', "
            "issue_date TEXT NOT NULL DEFAULT '', expiry_date TEXT NOT NULL DEFAULT '', "
            "equipment TEXT NOT NULL DEFAULT '', "
            "is_authorization_certificate INTEGER NOT NULL DEFAULT 0, "
            "needs_review INTEGER NOT NULL DEFAULT 1)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS entity_licenses ("
            "source_path TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "license_number TEXT NOT NULL, representative TEXT NOT NULL DEFAULT '', "
            "authorization_number TEXT NOT NULL DEFAULT '', "
            "issue_date TEXT NOT NULL DEFAULT '', expiry_date TEXT NOT NULL DEFAULT '')"
        )
        license_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(entity_licenses)")
        }
        for column in ("representative", "authorization_number", "issue_date", "expiry_date"):
            if column not in license_columns:
                connection.execute(
                    f"ALTER TABLE entity_licenses ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(extracted_records)")
        }
        for column in ("practice", "issue_date", "expiry_date"):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE extracted_records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        if "is_authorization_certificate" not in columns:
            connection.execute(
                "ALTER TABLE extracted_records ADD COLUMN "
                "is_authorization_certificate INTEGER NOT NULL DEFAULT 0"
            )
        catalog_ready = connection.execute(
            "SELECT 1 FROM file_catalog_state WHERE root = 'documents'"
        ).fetchone() is not None
        with INDEX_STATE_LOCK:
            INDEX_STATE["catalog_ready"] = catalog_ready
        connection.commit()
    finally:
        connection.close()
