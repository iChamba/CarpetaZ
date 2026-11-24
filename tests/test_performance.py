import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

TEMP_ROOT = tempfile.TemporaryDirectory()
os.environ.setdefault("DOCUMENT_ROOT", TEMP_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(TEMP_ROOT.name) / "appdata"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import documents as documents_module
import routes.entities as entities_module
from catalog import iter_tree_files, sync_file_catalog
from database import connect_db, initialize_db
from routes.entities import EntityRoutes
from settings import INDEX_STATE, INDEX_STATE_LOCK


def _make_institution(root: Path, name: str) -> Path:
    folder = root / "RADIOLOGÍA" / "MONTEVIDEO" / name
    (folder / "1. Autorización").mkdir(parents=True)
    (folder / "2. Inspecciones").mkdir()
    return folder


class EntityAreasCacheTests(unittest.TestCase):
    def setUp(self):
        initialize_db()
        entities_module.ENTITY_AREAS_CACHE.update({"key": None, "value": None})

    def test_areas_are_reused_until_the_catalog_is_refreshed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _make_institution(root, "CENTRO A")
            with patch.object(entities_module, "ENTITY_ROOT", root):
                first, _ = entities_module._entity_areas()
                self.assertEqual([area["institution"] for area in first], ["CENTRO A"])
                _make_institution(root, "CENTRO B")
                with patch.object(entities_module, "_scan_entity_areas") as scan:
                    cached, _ = entities_module._entity_areas()
                    scan.assert_not_called()
                self.assertEqual([area["institution"] for area in cached], ["CENTRO A"])
                with INDEX_STATE_LOCK:
                    INDEX_STATE["catalog_generation"] += 1
                refreshed, _ = entities_module._entity_areas()
                self.assertEqual(
                    [area["institution"] for area in refreshed], ["CENTRO A", "CENTRO B"]
                )

    def test_scan_with_warnings_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(entities_module, "ENTITY_ROOT", root), patch.object(
                entities_module, "_scan_entity_areas", return_value=([], 1)
            ) as scan:
                entities_module._entity_areas()
                entities_module._entity_areas()
                self.assertEqual(scan.call_count, 2)

    def test_list_entities_counts_documents_and_licenses_per_area(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            center = _make_institution(root, "CENTRO C")
            other = _make_institution(root, "CENTRO D")
            (center / "1. Autorización" / "LO pendiente.pdf").write_bytes(b"x")
            (center / "1. Autorización" / "LO importada.pdf").write_bytes(b"x")
            (center / "2. Inspecciones" / "informe.pdf").write_bytes(b"x")
            (other / "2. Inspecciones" / "informe.pdf").write_bytes(b"x")
            with patch.object(entities_module, "ENTITY_ROOT", root):
                sync_file_catalog((("entities", root),))
                imported = (center / "1. Autorización" / "LO importada.pdf").relative_to(root)
                connection = connect_db()
                try:
                    connection.execute(
                        "INSERT OR REPLACE INTO entity_licenses(source_path, created_at, "
                        "license_number, expiry_date) VALUES (?, '2026-01-01', 'LO-X', '01/01/2000')",
                        (imported.as_posix(),),
                    )
                    connection.commit()
                finally:
                    connection.close()
                result = {}
                handler = object.__new__(EntityRoutes)
                handler.send_json = lambda payload, status=200: result.update(payload)
                EntityRoutes.list_entities(handler, {})
        items = {item["name"]: item for item in result["items"]}
        self.assertEqual(items["CENTRO C"]["document_count"], 3)
        self.assertEqual(items["CENTRO C"]["imported_count"], 1)
        self.assertTrue(items["CENTRO C"]["has_license"])
        self.assertEqual(items["CENTRO C"]["license_statuses"], ["expired", "pending"])
        self.assertEqual(items["CENTRO D"]["document_count"], 1)
        self.assertFalse(items["CENTRO D"]["has_license"])
        self.assertEqual(items["CENTRO D"]["license_statuses"], [])


def _fake_word_modules():
    word = Mock()
    word.Documents.Open.side_effect = lambda **_kwargs: Mock(
        Content=Mock(Text="Texto"), Tables=[]
    )
    client = ModuleType("win32com.client")
    client.DispatchEx = Mock(return_value=word)
    win32com = ModuleType("win32com")
    win32com.__path__ = []
    win32com.client = client
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = Mock()
    pythoncom.CoUninitialize = Mock()
    pythoncom.com_error = type("com_error", (Exception,), {})
    modules = {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client}
    return modules, client, word, pythoncom


class WordSessionTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(TEMP_ROOT.name) / "legacy.doc"
        self.path.write_bytes(b"mock")

    def test_session_reuses_one_word_instance(self):
        modules, client, word, pythoncom = _fake_word_modules()
        with patch.dict(sys.modules, modules), patch.object(documents_module.os, "name", "nt"):
            with documents_module.word_session():
                for _ in range(3):
                    self.assertEqual(documents_module.extract_legacy_doc_text(self.path), "Texto")
                word.Quit.assert_not_called()
            client.DispatchEx.assert_called_once()
            self.assertEqual(word.Documents.Open.call_count, 3)
            word.Quit.assert_called_once_with(SaveChanges=0)
            self.assertEqual(pythoncom.CoInitialize.call_count, 1)
            self.assertEqual(pythoncom.CoUninitialize.call_count, 1)

    def test_without_session_each_read_closes_word(self):
        modules, client, word, pythoncom = _fake_word_modules()
        with patch.dict(sys.modules, modules), patch.object(documents_module.os, "name", "nt"):
            documents_module.extract_legacy_doc_text(self.path)
            documents_module.extract_legacy_doc_text(self.path)
        self.assertEqual(client.DispatchEx.call_count, 2)
        self.assertEqual(word.Quit.call_count, 2)
        self.assertEqual(pythoncom.CoUninitialize.call_count, 2)

    def test_word_error_discards_the_shared_instance(self):
        modules, client, word, pythoncom = _fake_word_modules()
        good_open = word.Documents.Open.side_effect

        def fail_first(**kwargs):
            word.Documents.Open.side_effect = good_open
            raise pythoncom.com_error("Word dejó de responder")

        word.Documents.Open.side_effect = fail_first
        with patch.dict(sys.modules, modules), patch.object(documents_module.os, "name", "nt"):
            with documents_module.word_session():
                with self.assertRaises(ValueError):
                    documents_module.extract_legacy_doc_text(self.path)
                self.assertEqual(documents_module.extract_legacy_doc_text(self.path), "Texto")
        self.assertEqual(client.DispatchEx.call_count, 2)
        self.assertEqual(word.Quit.call_count, 2)
        self.assertEqual(pythoncom.CoInitialize.call_count, 2)
        self.assertEqual(pythoncom.CoUninitialize.call_count, 2)


class TreeWalkTests(unittest.TestCase):
    def test_skips_hidden_excluded_and_linked_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "USUARIO" / "2026").mkdir(parents=True)
            (root / "USUARIO" / "2026" / "a.pdf").write_bytes(b"x")
            (root / "USUARIO" / ".oculta").mkdir()
            (root / "USUARIO" / ".oculta" / "b.pdf").write_bytes(b"x")
            (root / "appdata").mkdir()
            (root / "appdata" / "c.pdf").write_bytes(b"x")
            (root / "raiz.txt").write_text("x")
            (root / ".archivo_oculto.pdf").write_bytes(b"x")
            outside = Path(tempfile.mkdtemp())
            (outside / "d.pdf").write_bytes(b"x")
            try:
                os.symlink(outside, root / "enlace")
                os.symlink(outside / "d.pdf", root / "enlace.pdf")
            except (OSError, NotImplementedError):
                pass
            errors = []
            found = sorted(
                Path(entry.path).relative_to(root).as_posix()
                for entry in iter_tree_files(root, errors.append, root / "appdata")
            )
            self.assertEqual(found, [".archivo_oculto.pdf", "USUARIO/2026/a.pdf", "raiz.txt"])
            self.assertEqual(errors, [])
            for entry in iter_tree_files(root, errors.append):
                if entry.name == "a.pdf":
                    self.assertEqual(entry.stat(follow_symlinks=False).st_size, 1)

    def test_reports_unreadable_folders(self):
        errors = []
        self.assertEqual(list(iter_tree_files(Path("/carpeta/que/no/existe"), errors.append)), [])
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
