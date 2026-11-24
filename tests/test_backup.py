import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backup


def _make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE workflow_cases (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO workflow_cases (title) VALUES ('Trámite de prueba')")
        connection.commit()
    finally:
        connection.close()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "appdata" / "documentos.sqlite3"
        self.database.parent.mkdir()
        _make_database(self.database)
        self.destination = self.root / "respaldos"

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_is_complete_and_readable_while_database_is_open(self):
        open_connection = sqlite3.connect(self.database)
        try:
            open_connection.execute("INSERT INTO workflow_cases (title) VALUES ('Sin cerrar')")
            open_connection.commit()
            target = backup.create_backup(self.database, self.destination)
        finally:
            open_connection.close()
        self.assertTrue(target.name.startswith("auto-documentos-"))
        copy = sqlite3.connect(target)
        try:
            titles = [row[0] for row in copy.execute("SELECT title FROM workflow_cases ORDER BY id")]
        finally:
            copy.close()
        self.assertEqual(titles, ["Trámite de prueba", "Sin cerrar"])
        self.assertEqual([path.name for path in self.destination.iterdir()], [target.name])
        self.assertEqual(
            [path for path in self.database.parent.iterdir() if path.name.startswith(".respaldo-")], []
        )

    def test_failed_verification_leaves_no_backup(self):
        with patch.object(backup, "_integrity_result", return_value="dañada"):
            with self.assertRaisesRegex(RuntimeError, "verificación"):
                backup.create_backup(self.database, self.destination)
        self.assertEqual(backup.list_backups(self.destination), [])

    def test_rotation_keeps_daily_and_weekly_and_ignores_manual_files(self):
        self.destination.mkdir()
        start = datetime(2026, 11, 24, 3, 0, 0)
        for days in range(60):
            for hour in (0, 5):
                moment = start - timedelta(days=days, hours=hour)
                (self.destination / f"auto-documentos-{moment:%Y%m%d-%H%M%S}.sqlite3").write_bytes(b"x")
        manual = self.destination / "documentos-antes-de-migrar.sqlite3"
        manual.write_bytes(b"x")
        backup.rotate_backups(self.destination, keep_daily=7, keep_weekly=4)
        kept = backup.list_backups(self.destination)
        self.assertEqual(len(kept), 11)
        self.assertEqual(kept[0][0], start)
        self.assertEqual(len({moment.date() for moment, _ in kept[:7]}), 7)
        self.assertTrue(all(moment.hour == 3 or moment.hour == 22 for moment, _ in kept))
        self.assertEqual(len({moment.isocalendar()[:2] for moment, _ in kept[7:]}), 4)
        self.assertTrue(manual.exists())

    def test_run_backup_records_status_and_errors(self):
        backup.run_backup(self.database, self.destination, 7, 4)
        status = backup.backup_status()
        self.assertTrue(status["last_backup_file"].startswith("auto-documentos-"))
        self.assertEqual(status["last_error"], "")
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(FileNotFoundError):
                backup.run_backup(self.root / "no-existe.sqlite3", self.destination, 7, 4)
        self.assertIn("No se encontró", backup.backup_status()["last_error"])

    def test_scheduler_skips_when_recent_backup_exists(self):
        backup.create_backup(self.database, self.destination)
        with patch.object(backup, "run_backup") as run:
            stop = backup.start_backup_scheduler(
                self.database, self.destination, 24, 7, 4, check_seconds=0.05,
            )
            time.sleep(0.2)
            stop.set()
        run.assert_not_called()

    def test_scheduler_backs_up_when_none_exists(self):
        stop = backup.start_backup_scheduler(
            self.database, self.destination, 24, 7, 4, check_seconds=0.05,
        )
        try:
            for _ in range(100):
                if backup.list_backups(self.destination):
                    break
                time.sleep(0.05)
        finally:
            stop.set()
        self.assertEqual(len(backup.list_backups(self.destination)), 1)

    def test_command_line_backup(self):
        with patch.dict(os.environ, {"BACKUP_DIR": str(self.destination)}):
            self.assertEqual(backup.main(["--base", str(self.database)]), 0)
        self.assertEqual(len(backup.list_backups(self.destination)), 1)


if __name__ == "__main__":
    unittest.main()
