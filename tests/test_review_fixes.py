"""Pruebas de las correcciones de la revisión de código (rutas, extracción y scripts)."""

import contextlib
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ENV_ROOT = tempfile.TemporaryDirectory(prefix="carpetaz-review-env-")
os.environ.setdefault("DOCUMENT_ROOT", _ENV_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(_ENV_ROOT.name) / "appdata"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import limpiar_indice_pdf
import routes.entities as entities_module
import routes.records as records_module
from documents import safe_relative_path
from extraction import is_authorization_individual
from routes.records import RecordRoutes


class EntityFolderDepthTests(unittest.TestCase):
    def test_open_folder_accepts_institutions_six_levels_deep(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deep = root / "Práctica" / "Depto" / "Localidad" / "Zona" / "Grupo" / "INSTITUCIÓN"
            deep.mkdir(parents=True)
            (deep / "Otra").mkdir()
            with mock.patch.object(entities_module, "ENTITY_ROOT", root):
                folder = entities_module._entity_area_folder(
                    "Práctica/Depto/Localidad/Zona/Grupo/INSTITUCIÓN"
                )
                self.assertEqual(folder.resolve(), deep.resolve())
                with self.assertRaises(ValueError):
                    entities_module._entity_area_folder(
                        "Práctica/Depto/Localidad/Zona/Grupo/INSTITUCIÓN/Otra"
                    )


class EncodedFileNameTests(unittest.TestCase):
    def test_file_with_percent_sequence_in_its_name_is_found(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ANA").mkdir()
            (root / "ANA" / "informe%20final.pdf").write_bytes(b"%PDF-1.4")
            path, relative = safe_relative_path("ANA/informe%20final.pdf", root)
            self.assertEqual(relative, "ANA/informe%20final.pdf")
            self.assertTrue(path.is_file())
            # «%2F» ya no se convierte en «/»: no sirve para salir de la carpeta.
            with self.assertRaises((ValueError, OSError)):
                safe_relative_path("ANA/..%2F..%2Fsecreto.pdf", root)


class AuthorizationDetectionTests(unittest.TestCase):
    BASE = (
        "AUTORIZACIÓN INDIVIDUAL\n"
        "Nombres: ANA\nApellidos: PÉREZ\n"
        "Número de autorización: 0100/1/2\n"
    )

    def test_certificate_without_granted_phrase_is_recognized_by_its_dates(self):
        self.assertTrue(is_authorization_individual(self.BASE + "Vencimiento: 01/01/2028\n"))

    def test_heading_alone_or_a_request_is_not_an_authorization(self):
        self.assertFalse(is_authorization_individual(self.BASE))
        self.assertFalse(is_authorization_individual(
            "Solicitud de " + self.BASE + "Vencimiento: 01/01/2028\n"
        ))


class ManualImportCriteriaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "documentos"
        (self.root / "ANA").mkdir(parents=True)
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.temp.name) / "test.sqlite3"
        database.initialize_db()

    def tearDown(self):
        database.DATABASE = self.original_database
        self.temp.cleanup()

    def test_manual_import_accepts_certificates_by_file_name_like_bulk_import(self):
        name = "Renovación Autorización Individual ANA.txt"
        (self.root / "ANA" / name).write_text(
            "Nombres: ANA\nApellidos: PÉREZ\nNúmero de autorización: 0100/1/2\n",
            encoding="utf-8",
        )
        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload, status=status)
        handler.send_error_json = lambda message, status=400: response.update(error=message, status=status)
        with mock.patch.object(
            records_module, "safe_relative_path",
            side_effect=lambda value: safe_relative_path(value, self.root),
        ):
            RecordRoutes.extract_record(handler, {"path": f"ANA/{name}"})
        self.assertEqual(response.get("status"), 201, response.get("error"))
        self.assertEqual(response["record"]["authorization_number"], "0100/1/2")


class CleanupScriptTests(unittest.TestCase):
    def run_script(self, *args):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            code = limpiar_indice_pdf.main(list(args))
        return code, errors.getvalue()

    def test_refuses_to_run_while_the_app_answers_on_its_port(self):
        with socket.socket() as server, tempfile.TemporaryDirectory() as temp:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            code, errors = self.run_script("--base", str(Path(temp) / "x.sqlite3"), "--puerto", str(port))
        self.assertEqual(code, 1)
        self.assertIn("parece abierta", errors)

    def test_continues_when_nothing_answers(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            code, errors = self.run_script("--base", str(Path(temp) / "x.sqlite3"), "--puerto", str(port))
        self.assertEqual(code, 1)
        self.assertIn("No se encontró la base", errors)


if __name__ == "__main__":
    unittest.main()
