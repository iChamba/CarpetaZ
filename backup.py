"""Respaldos automáticos de la base SQLite de la aplicación.

Cada respaldo se hace con la API de backup de SQLite (es seguro con la
aplicación funcionando), se verifica con integrity_check y recién entonces se
copia a la carpeta de destino. Solo se rotan los archivos con el prefijo
"auto-": los respaldos manuales nunca se borran.

Uso manual (sin detener la aplicación):
    .venv\\Scripts\\python.exe backup.py
    .venv\\Scripts\\python.exe backup.py --destino "\\\\SERVIDOR\\Respaldos\\CarpetaZ"
"""
import argparse
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PREFIX = "auto-documentos-"
NAME_PATTERN = re.compile(rf"^{PREFIX}(\d{{8}}-\d{{6}})\.sqlite3$")
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

STATE_LOCK = threading.Lock()
STATE = {
    "last_backup": "",
    "last_backup_file": "",
    "last_error": "",
}


def _timestamp_of(path: Path) -> datetime | None:
    match = NAME_PATTERN.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), TIMESTAMP_FORMAT)
    except ValueError:
        return None


def list_backups(destination: Path) -> list[tuple[datetime, Path]]:
    if not destination.is_dir():
        return []
    backups = []
    for path in destination.iterdir():
        moment = _timestamp_of(path)
        if moment is not None and path.is_file():
            backups.append((moment, path))
    backups.sort(reverse=True)
    return backups


def latest_backup_time(destination: Path) -> datetime | None:
    backups = list_backups(destination)
    return backups[0][0] if backups else None


def _integrity_result(connection: sqlite3.Connection) -> str:
    return connection.execute("PRAGMA integrity_check").fetchone()[0]


def create_backup(database: Path, destination: Path, now: datetime | None = None) -> Path:
    """Copia la base a destination y devuelve la ruta del respaldo verificado."""
    if not database.is_file():
        raise FileNotFoundError(f"No se encontró la base de datos: {database}")
    destination.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    target = destination / f"{PREFIX}{now.strftime(TIMESTAMP_FORMAT)}.sqlite3"

    # Primero se genera y verifica en una carpeta local; la carpeta de destino
    # puede ser de red, donde SQLite es lento y menos confiable.
    with tempfile.TemporaryDirectory(dir=database.parent, prefix=".respaldo-") as work:
        local_copy = Path(work) / target.name
        source = sqlite3.connect(database, timeout=60)
        try:
            copy = sqlite3.connect(local_copy)
            try:
                source.backup(copy)
                result = _integrity_result(copy)
                if result != "ok":
                    raise RuntimeError(f"El respaldo no pasó la verificación: {result}")
                copy.execute("PRAGMA journal_mode=DELETE")
            finally:
                copy.close()
        finally:
            source.close()

        partial = destination / (target.name + ".parcial")
        shutil.copyfile(local_copy, partial)
        if partial.stat().st_size != local_copy.stat().st_size:
            partial.unlink(missing_ok=True)
            raise RuntimeError("La copia del respaldo quedó incompleta.")
        os.replace(partial, target)
    return target


def rotate_backups(destination: Path, keep_daily: int, keep_weekly: int) -> list[Path]:
    """Conserva el más reciente de cada uno de los últimos keep_daily días y de
    las keep_weekly semanas anteriores; borra el resto de los respaldos "auto-"."""
    keep: set[Path] = set()
    days: list = []
    weeks: list = []
    for moment, path in list_backups(destination):
        day = moment.date()
        week = moment.isocalendar()[:2]
        if day not in days and len(days) < keep_daily:
            days.append(day)
            keep.add(path)
            continue
        if day in days:
            continue
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.append(week)
            keep.add(path)
    removed = []
    for _, path in list_backups(destination):
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def run_backup(database: Path, destination: Path, keep_daily: int, keep_weekly: int) -> Path:
    try:
        target = create_backup(database, destination)
        removed = rotate_backups(destination, keep_daily, keep_weekly)
    except Exception as exc:
        logging.exception("No se pudo crear el respaldo de la base de datos")
        with STATE_LOCK:
            STATE["last_error"] = f"{datetime.now().isoformat(timespec='seconds')}: {exc}"
        raise
    logging.info(
        "Respaldo creado: %s (%d respaldo(s) antiguo(s) eliminados)", target, len(removed)
    )
    with STATE_LOCK:
        STATE.update({
            "last_backup": datetime.now().isoformat(timespec="seconds"),
            "last_backup_file": target.name,
            "last_error": "",
        })
    return target


def start_backup_scheduler(
    database: Path,
    destination: Path,
    interval_hours: float,
    keep_daily: int,
    keep_weekly: int,
    check_seconds: float = 3600,
) -> threading.Event:
    """Hace un respaldo al iniciar si el último tiene más de interval_hours y
    luego revisa cada hora. Devuelve un evento para detener el hilo."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                latest = latest_backup_time(destination)
                age = (datetime.now() - latest).total_seconds() if latest else None
                if age is None or age >= interval_hours * 3600:
                    run_backup(database, destination, keep_daily, keep_weekly)
            except Exception:
                pass  # Ya quedó en el registro; se reintenta en la próxima revisión.
            stop.wait(check_seconds)

    threading.Thread(target=loop, name="respaldos", daemon=True).start()
    return stop


def backup_status() -> dict:
    with STATE_LOCK:
        return dict(STATE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crea un respaldo verificado de la base.")
    parser.add_argument(
        "--base",
        default=os.environ.get("APP_DATA_DIR", str(APP_DIR / "appdata")) + "/documentos.sqlite3",
        help="Ruta de documentos.sqlite3",
    )
    parser.add_argument(
        "--destino",
        default=os.environ.get("BACKUP_DIR", ""),
        help="Carpeta de respaldos (por defecto BACKUP_DIR o appdata/respaldos/automaticos)",
    )
    args = parser.parse_args(argv)
    database = Path(args.base).expanduser().resolve()
    destination = Path(args.destino).expanduser() if args.destino else (
        database.parent / "respaldos" / "automaticos"
    )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        target = run_backup(
            database,
            destination,
            int(os.environ.get("BACKUP_KEEP_DAILY", "7")),
            int(os.environ.get("BACKUP_KEEP_WEEKLY", "4")),
        )
    except Exception as exc:
        print(f"No se pudo crear el respaldo: {exc}", file=sys.stderr)
        return 1
    print(f"Respaldo verificado: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
