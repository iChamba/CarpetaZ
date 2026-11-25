import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

TEMP_ROOT = tempfile.TemporaryDirectory()
os.environ.setdefault("DOCUMENT_ROOT", TEMP_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(TEMP_ROOT.name) / "appdata"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_refresh
import catalog as catalog_module
import database
import routes.entities as entities_module
from catalog import sync_file_catalog
from settings import INDEX_STATE, INDEX_STATE_LOCK


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "documentos"
        self.root.mkdir()
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.temp.name) / "test.sqlite3"
        database.initialize_db()

    def tearDown(self):
        database.DATABASE = self.original_database
        self.temp.cleanup()

    def catalog_paths(self):
        connection = database.connect_db()
        try:
            return sorted(row[0] for row in connection.execute(
                "SELECT path FROM file_catalog WHERE root = 'documents'"
            ))
        finally:
            connection.close()


class CatalogRemovalTests(DatabaseTestCase):
    def write(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_deleted_files_leave_the_catalog(self):
        self.write("ANA/a.pdf")
        removed = self.write("ANA/b.pdf")
        sync_file_catalog((("documents", self.root),))
        removed.unlink()
        self.write("BETO/c.pdf")
        sync_file_catalog((("documents", self.root),))
        self.assertEqual(self.catalog_paths(), ["ANA/a.pdf", "BETO/c.pdf"])

    def test_unreadable_folder_keeps_its_files_but_others_are_cleaned(self):
        self.write("ANA/a.pdf")
        self.write("ANA/borrado.pdf")
        self.write("BLOQUEADA/sub/x.pdf")
        self.write("BLOQUEADA/y.pdf")
        sync_file_catalog((("documents", self.root),))
        (self.root / "ANA" / "borrado.pdf").unlink()
        blocked = str(self.root / "BLOQUEADA")
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == blocked:
                raise PermissionError(13, "Acceso denegado", blocked)
            return real_scandir(path)

        with patch.object(catalog_module.os, "scandir", side_effect=scandir):
            sync_file_catalog((("documents", self.root),))
        self.assertEqual(
            self.catalog_paths(), ["ANA/a.pdf", "BLOQUEADA/sub/x.pdf", "BLOQUEADA/y.pdf"]
        )
        with INDEX_STATE_LOCK:
            self.assertTrue(INDEX_STATE["catalog_ready"])
            self.assertTrue(INDEX_STATE["catalog_last_finished"])

    def test_unreadable_root_removes_nothing(self):
        self.write("ANA/a.pdf")
        sync_file_catalog((("documents", self.root),))
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == str(self.root):
                raise PermissionError(13, "Acceso denegado", str(self.root))
            return real_scandir(path)

        with patch.object(catalog_module.os, "scandir", side_effect=scandir):
            sync_file_catalog((("documents", self.root),))
        self.assertEqual(self.catalog_paths(), ["ANA/a.pdf"])


class IncrementalLicenseTests(DatabaseTestCase):
    def test_unchanged_license_is_skipped_only_when_processed_after_last_change(self):
        path = self.root / "LO prueba.pdf"
        path.write_bytes(b"x")
        earlier = (datetime.now() - timedelta(minutes=5)).timestamp()
        os.utime(path, (earlier, earlier))
        self.assertFalse(entities_module._license_is_current("LO prueba.pdf", path))
        entities_module._save_license("LO prueba.pdf", "LO-X", {}, [])
        self.assertTrue(entities_module._license_is_current("LO prueba.pdf", path))
        later = (datetime.now() + timedelta(minutes=5)).timestamp()
        os.utime(path, (later, later))
        self.assertFalse(entities_module._license_is_current("LO prueba.pdf", path))

    def test_license_without_technical_tables_is_reprocessed(self):
        path = self.root / "LO vieja.pdf"
        path.write_bytes(b"x")
        entities_module._save_license("LO vieja.pdf", "LO-X", {}, None)
        self.assertFalse(entities_module._license_is_current("LO vieja.pdf", path))


class LicenseImportCacheTests(DatabaseTestCase):
    """La revisión automática compara tamaño y fecha exactos de cada licencia."""

    def setUp(self):
        super().setUp()
        self.area = self.root / "Radioterapia" / "CENTRO PRUEBA"
        self.folder = self.area / "1. Autorización"
        self.folder.mkdir(parents=True)
        (self.area / "2. Inspecciones").mkdir()
        self.reads = []

    def extract(self, path):
        self.reads.append(path.name)
        return path.read_text(encoding="utf-8"), []

    def run_import(self):
        with patch.object(entities_module, "ENTITY_ROOT", self.root), patch.object(
            entities_module, "_extract_license_content", side_effect=self.extract
        ):
            self.reads.clear()
            self.assertTrue(entities_module.run_entity_import_now(incremental=True))
            with entities_module.ENTITY_IMPORT_STATE_LOCK:
                return dict(entities_module.ENTITY_IMPORT_STATE)

    def test_scanned_license_is_not_read_again_until_it_changes(self):
        scanned = self.folder / "LO escaneada.txt"
        scanned.write_text("   ", encoding="utf-8")
        self.assertEqual(self.run_import()["without_text"], 1)
        self.assertEqual(self.reads, ["LO escaneada.txt"])
        status = self.run_import()
        self.assertEqual(self.reads, [])
        self.assertEqual((status["unchanged"], status["without_text"]), (1, 1))
        scanned.write_text("Licencia LO-X-1234-5678-AB", encoding="utf-8")
        self.assertEqual(self.run_import()["imported"], 1)

    def test_replaced_license_with_old_date_is_read_again(self):
        path = self.folder / "LO vigente.txt"
        path.write_text("Licencia LO-X-1234-5678-AB", encoding="utf-8")
        old = (datetime.now() - timedelta(days=30)).timestamp()
        os.utime(path, (old, old))
        self.assertEqual(self.run_import()["imported"], 1)
        self.assertEqual(self.run_import()["unchanged"], 1)
        # Copia corregida con fecha de modificación vieja: antes se ignoraba.
        path.write_text("Licencia corregida LO-Y-1234-5678-CD", encoding="utf-8")
        os.utime(path, (old - 3600, old - 3600))
        status = self.run_import()
        self.assertEqual((status["updated"], self.reads), (1, ["LO vigente.txt"]))
        connection = database.connect_db()
        try:
            row = connection.execute("SELECT license_number FROM entity_licenses").fetchone()
        finally:
            connection.close()
        self.assertEqual(row["license_number"], "LO-Y-1234-5678-CD")

    def test_licenses_processed_before_the_cache_existed_are_not_reread(self):
        path = self.folder / "LO anterior.txt"
        path.write_text("Licencia LO-X-1234-5678-AB", encoding="utf-8")
        earlier = (datetime.now() - timedelta(minutes=5)).timestamp()
        os.utime(path, (earlier, earlier))
        entities_module._save_license("Radioterapia/CENTRO PRUEBA/1. Autorización/LO anterior.txt",
                                      "LO-X-1234-5678-AB", {}, [])
        status = self.run_import()
        self.assertEqual((status["unchanged"], self.reads), (1, []))


class AutoRefreshTests(unittest.TestCase):
    def test_cycle_runs_catalog_then_imports_in_order(self):
        calls = []

        def fake_start_catalog():
            calls.append("catalog")
            with INDEX_STATE_LOCK:
                INDEX_STATE["catalog_running"] = True

            def finish():
                time.sleep(0.05)
                with INDEX_STATE_LOCK:
                    INDEX_STATE["catalog_running"] = False

            threading.Thread(target=finish).start()
            return True

        with patch.object(auto_refresh, "start_catalog", side_effect=fake_start_catalog), \
                patch.object(auto_refresh, "run_certificate_import_now",
                             side_effect=lambda: calls.append("certificates")), \
                patch.object(auto_refresh, "run_entity_import_now",
                             side_effect=lambda incremental: calls.append(("entities", incremental))):
            auto_refresh.run_cycle(threading.Event(), poll_seconds=0.01)
        self.assertEqual(calls, ["catalog", "certificates", ("entities", True)])
        with INDEX_STATE_LOCK:
            self.assertTrue(INDEX_STATE["auto_refresh_last"])

    def test_cycle_waits_for_a_catalog_already_running(self):
        with INDEX_STATE_LOCK:
            INDEX_STATE["catalog_running"] = True
        calls = []

        def finish():
            time.sleep(0.05)
            calls.append("catalog-finished")
            with INDEX_STATE_LOCK:
                INDEX_STATE["catalog_running"] = False

        threading.Thread(target=finish).start()
        with patch.object(auto_refresh, "start_catalog") as start, \
                patch.object(auto_refresh, "run_certificate_import_now",
                             side_effect=lambda: calls.append("certificates")), \
                patch.object(auto_refresh, "run_entity_import_now"):
            auto_refresh.run_cycle(threading.Event(), poll_seconds=0.01)
        start.assert_not_called()
        self.assertEqual(calls, ["catalog-finished", "certificates"])

    def test_scheduler_repeats_and_survives_errors(self):
        calls = []

        def cycle(_stop, _poll):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("falla temporal")

        with patch.object(auto_refresh, "run_cycle", side_effect=cycle):
            with self.assertLogs(level="ERROR"):
                stop = auto_refresh.start_auto_refresh(0.001)
                for _ in range(100):
                    if len(calls) >= 3:
                        break
                    time.sleep(0.02)
                stop.set()
        self.assertGreaterEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
