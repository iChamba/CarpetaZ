"""Elimina el índice antiguo de búsqueda en PDF (tablas pdf_fts y pdf_index_meta)
y compacta la base. Ninguna parte de la aplicación usa esas tablas.

Cierra la aplicación antes de ejecutarlo:
    .venv\\Scripts\\python.exe limpiar_indice_pdf.py
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from backup import create_backup

APP_DIR = Path(__file__).resolve().parent
OLD_TABLES = ("pdf_fts", "pdf_index_meta")


def _size_mb(path: Path) -> float:
    return path.stat().st_size / 1_048_576 if path.exists() else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default=os.environ.get("APP_DATA_DIR", str(APP_DIR / "appdata")) + "/documentos.sqlite3",
    )
    args = parser.parse_args(argv)
    database = Path(args.base).expanduser().resolve()
    if not database.is_file():
        print(f"No se encontró la base de datos: {database}", file=sys.stderr)
        return 1

    connection = sqlite3.connect(database, timeout=1)
    try:
        present = [
            name for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                OLD_TABLES,
            )
        ]
        if not present:
            print("La base no tiene el índice antiguo de PDF; no hay nada que limpiar.")
            return 0
        try:
            # Falla si la aplicación (u otro programa) tiene la base abierta escribiendo.
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            print("La base está en uso. Cierra la aplicación y vuelve a intentarlo.", file=sys.stderr)
            return 1
    finally:
        connection.close()

    before = _size_mb(database)
    destination = database.parent / "respaldos"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = create_backup(database, destination)
    manual = backup.with_name(f"documentos-antes-limpiar-indice-pdf-{stamp}.sqlite3")
    os.replace(backup, manual)  # Sin el prefijo "auto-": la rotación no lo borra.
    print(f"Respaldo verificado: {manual}")

    connection = sqlite3.connect(database, timeout=1)
    try:
        for name in present:
            connection.execute(f'DROP TABLE IF EXISTS "{name}"')
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        print(f"La verificación falló ({result}). Restaura el respaldo: {manual}", file=sys.stderr)
        return 1
    print(f"Tablas eliminadas: {', '.join(present)}")
    print(f"Tamaño de la base: {before:.1f} MB -> {_size_mb(database):.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
