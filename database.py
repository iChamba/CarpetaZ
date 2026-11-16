import sqlite3

from settings import DATABASE


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
        connection.commit()
    finally:
        connection.close()
