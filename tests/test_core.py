import shutil
from io import BytesIO
import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, closing
from datetime import date, timedelta
from types import ModuleType
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

from openpyxl import load_workbook
import pymupdf

TEMP_ROOT = tempfile.TemporaryDirectory()
os.environ["DOCUMENT_ROOT"] = TEMP_ROOT.name
os.environ["APP_DATA_DIR"] = str(Path(TEMP_ROOT.name) / "appdata")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import documents as documents_module
from catalog import sync_file_catalog
from database import connect_db, initialize_db
from documents import (
    extract_legacy_doc_text,
    safe_relative_path,
    user_folder_from_path,
)
from extraction import (
    extract_candidate_fields,
    extract_certificate_fields,
    extract_operation_license,
    is_authorization_individual,
    is_authorization_certificate,
)
import routes.records as records_module
import routes.entities as entities_module
from routes.entities import EntityRoutes
from routes.records import RecordRoutes
from routes.documents import DocumentRoutes
from routes.users import UserRoutes


class CatalogTests(unittest.TestCase):
    def test_catalog_refresh_and_removed_features(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as processes:
            root = Path(temp)
            documents = root / "documents"
            entities = root / "entities"
            documents.mkdir()
            entities.mkdir()

            def create_pdf(name, text):
                with pymupdf.open() as pdf:
                    pdf.new_page().insert_text((72, 72), text)
                    pdf.save(documents / name)

            create_pdf("first.pdf", "primerejemplo")
            (entities / "entity.txt").write_text("entity preview", encoding="utf-8")
            workbook_path = documents / "book.xlsx"
            workbook_path.write_bytes(b"workbook fixture")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            env.update({
                "DOCUMENT_ROOT": str(documents), "ENTITY_ROOT": str(entities),
                "APP_DATA_DIR": str(root / "data"), "HOST": "127.0.0.1", "PORT": str(port),
                "APP_USER": "admin", "APP_PASSWORD": "test-password-123",
            })
            base = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )

            def request(path, body=None):
                payload = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(
                    base + path, data=payload, headers={"Content-Type": "application/json"}
                )
                with opener.open(req, timeout=5) as response:
                    return json.load(response)

            def start_server():
                process = subprocess.Popen(
                    [sys.executable, "-u", "app.py"], cwd=Path(__file__).resolve().parents[1],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                processes.callback(stop_server, process)
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail("El servidor de prueba terminó durante el inicio.")
                    try:
                        request("/api/login", {"username": "admin", "password": "test-password-123"})
                        return process
                    except urllib.error.URLError:
                        time.sleep(0.1)
                self.fail("El servidor de prueba no inició.")

            def stop_server(process):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)

            def wait_for_idle():
                for _ in range(200):
                    status = request("/api/status")
                    if not status["catalog_running"]:
                        self.assertEqual(status["catalog_errors"], 0)
                        return
                    time.sleep(0.1)
                self.fail("La actualización no terminó.")

            def catalog_count():
                connection = sqlite3.connect(root / "data" / "documentos.sqlite3")
                try:
                    return connection.execute(
                        "SELECT COUNT(*) FROM file_catalog WHERE root = 'documents'"
                    ).fetchone()[0]
                finally:
                    connection.close()

            process = start_server()
            wait_for_idle()
            self.assertEqual(catalog_count(), 2)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/workflow/export", timeout=5)
            self.assertEqual(error.exception.code, 401)
            self.assertTrue(request("/api/workflow")["supports_inspector_folder_link"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/workflow/cases/link-folder", {"case_id": "invalid"})
            self.assertEqual(error.exception.code, 400)
            self.assertIn("trámite", json.load(error.exception)["error"])
            with opener.open(base + "/", timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertNotIn('id="pane-search"', page)
            self.assertNotIn('id="pane-workbooks"', page)
            self.assertIn('href="/api/records/export"', page)
            self.assertIn('href="/api/workflow/export"', page)
            with opener.open(base + "/api/workflow/export", timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(),
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.assertTrue(response.read().startswith(b"PK"))
            self.assertIn('id="catalog-status"', page)
            create_pdf("second.pdf", "segundoejemplo")
            request("/api/catalog/refresh", {})
            wait_for_idle()
            self.assertEqual(catalog_count(), 3)
            for path, body in (
                ("/api/search?q=segundoejemplo", None),
                ("/api/workbooks", None),
                ("/api/workbook?path=book.xlsx", None),
                ("/api/download?path=book.xlsx", None),
                ("/api/entity/download?path=entity.txt", None),
                ("/api/index", {}),
                ("/api/workbook/save", {"path": "book.xlsx"}),
                ("/api/records/update", {"id": 1, "fields": {"practice": "changed"}}),
            ):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    request(path, body)
                self.assertEqual(error.exception.code, 404)
            self.assertEqual(workbook_path.read_bytes(), b"workbook fixture")
            with opener.open(base + "/api/preview?path=first.pdf", timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(), "application/pdf")
                self.assertTrue(response.read().startswith(b"%PDF"))
            with opener.open(base + "/api/entity/preview?path=entity.txt", timeout=5) as response:
                self.assertEqual(response.read(), b"entity preview")
            empty_licenses = request("/api/entity/licenses")
            self.assertEqual(empty_licenses["items"], [])
            self.assertEqual(
                empty_licenses["summary"],
                {"valid": 0, "soon": 0, "expired": 0, "unknown": 0},
            )
            with opener.open(base + "/api/records/export", timeout=5) as response:
                self.assertTrue(response.read().startswith(b"PK"))
            stop_server(process)
            start_server()
            wait_for_idle()
            self.assertEqual(catalog_count(), 3)
            connection = sqlite3.connect(root / "data" / "documentos.sqlite3")
            try:
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'pdf_fts'"
                ).fetchone())
            finally:
                connection.close()


class ExtractionTests(unittest.TestCase):
    def test_reads_legacy_doc_text_through_word_read_only(self):
        path = Path(TEMP_ROOT.name) / "legacy.doc"
        path.write_bytes(b"mock Word document")
        document = Mock()
        document.Content.Text = "Texto de Word 2003"
        word = Mock()
        word.Documents.Open.return_value = document
        client = ModuleType("win32com.client")
        client.DispatchEx = Mock(return_value=word)
        win32com = ModuleType("win32com")
        win32com.__path__ = []
        win32com.client = client
        pythoncom = ModuleType("pythoncom")
        pythoncom.CoInitialize = Mock()
        pythoncom.CoUninitialize = Mock()
        pythoncom.com_error = type("com_error", (Exception,), {})
        modules = {
            "pythoncom": pythoncom,
            "win32com": win32com,
            "win32com.client": client,
        }

        # Word se simula, así que la prueba también corre fuera de Windows.
        with patch.dict(sys.modules, modules), patch.object(documents_module.os, "name", "nt"):
            self.assertEqual(extract_legacy_doc_text(path), "Texto de Word 2003")

        word.Documents.Open.assert_called_once_with(
            FileName=str(path.resolve()),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Revert=False,
            Visible=False,
            OpenAndRepair=True,
            NoEncodingDialog=True,
        )
        self.assertEqual(word.AutomationSecurity, 3)
        document.Close.assert_called_once_with(SaveChanges=0)
        word.Quit.assert_called_once_with(SaveChanges=0)
        pythoncom.CoUninitialize.assert_called_once()

    def test_extracts_operation_license_format(self):
        self.assertEqual(
            extract_operation_license("Licencia de operación: LO-X-1234-5678-ab"),
            "LO-X-1234-5678-AB",
        )
        self.assertEqual(extract_operation_license("LO-X-123-5678-AB"), "")

    def test_extracts_labeled_fields_and_equipment_lines(self):
        text = """\
Datos personales
Nombres
ANA EJEMPLO
Apellidos
PRUEBA LOCAL
Tipo documento C.I. Nro documento 12345678
Número de autorización
AB12/34/5
Equipos habilitados
Equipo: Tomógrafo
Equipo: Resonador
Firma responsable
"""
        fields = extract_candidate_fields(text)

        self.assertEqual(fields["first_name"], "ANA EJEMPLO")
        self.assertEqual(fields["last_name"], "PRUEBA LOCAL")
        self.assertEqual(fields["identity_number"], "12345678")
        self.assertEqual(fields["authorization_number"], "AB12/34/5")
        self.assertEqual(fields["equipment"], "Equipo: Tomógrafo\nEquipo: Resonador")

    def test_extracts_certificate_fields_and_equipment_rows(self):
        text = """\
Montevideo, 19 de abril de 2024
AUTORIZACIÓN INDIVIDUAL
NOMBRE: EJEMPLO PERSONA
DOCUMENTO DE IDENTIDAD: 0.000.000-0
Ha sido autorizado/a para la práctica de:
RADIOLOGÍA INTERVENCIONISTA
de acuerdo al detalle que aparece en el Anexo
Autorización N°: 891/0/0
Vencimiento: 18/04/2029
"""
        equipment_rows = [
            ["Equipos generadores de radiaciones ionizantes", None, None, None, None],
            ["Tipo de equipo", "Fabricante", "Modelo", "Tipo(s) radiación que emite", "Práctica"],
            ["Angiógrafo", "General Electric", "Innova 3100 (g-1577)", "rayos X", "Cardiología Intervencionista"],
        ]

        fields = extract_certificate_fields(text, equipment_rows)

        self.assertEqual(fields["authorization_number"], "891/0/0")
        self.assertEqual(fields["identity_number"], "0.000.000-0")
        self.assertEqual(fields["practice"], "RADIOLOGÍA INTERVENCIONISTA")
        self.assertEqual(fields["issue_date"], "19/04/2024")
        self.assertEqual(fields["expiry_date"], "18/04/2029")
        self.assertIn("Modelo: Innova 3100 (g-1577)", fields["equipment"])
        self.assertTrue(
            is_authorization_certificate(Path("Certificado_Autorizacion_Individual_ARNR (1).pdf"))
        )

    def test_extracts_authorization_number_with_renewal_and_modification_counts(self):
        fields = extract_candidate_fields("Número de autorización: 0100/1/2")

        self.assertEqual(fields["authorization_number"], "0100/1/2")

    def test_detects_renewal_certificates_but_not_renewal_requests(self):
        self.assertTrue(
            is_authorization_certificate(
                Path("Renovacion_AutorizacionIndividual_ARNR_(1).pdf")
            )
        )
        self.assertTrue(
            is_authorization_certificate(
                Path("AutorizacionIndividual_ARNR_Renov_2025.pdf")
            )
        )
        self.assertFalse(
            is_authorization_certificate(
                Path("Solicitud_renovacion_autorizacion_individual.pdf")
            )
        )

    def test_detects_authorization_from_content_not_filename(self):
        certificate = """\
AUTORIZACIÓN INDIVIDUAL
Ha sido autorizado/a para la práctica de:
RADIOLOGÍA
de acuerdo al detalle del Anexo
Número de autorización: 0100/1/2
Expedición: 01/01/2026
Vencimiento: 01/01/2028
"""
        request = """\
Solicitud de renovación de autorización individual
Número de autorización: 0100/1/2
"""

        self.assertTrue(is_authorization_individual(certificate))
        self.assertFalse(is_authorization_individual(request))


class ValidationTests(unittest.TestCase):
    def test_rejects_paths_outside_document_root(self):
        with self.assertRaises(ValueError):
            safe_relative_path("../outside.pdf")

    def test_derives_user_group_from_first_folder(self):
        self.assertEqual(
            user_folder_from_path("USUARIOprueba/2025/autorizacion.pdf"),
            "USUARIOprueba",
        )
        self.assertEqual(user_folder_from_path("archivo.pdf"), "Sin usuario asignado")


class RecordGroupingTests(unittest.TestCase):
    def setUp(self):
        initialize_db()
        connection = connect_db()
        try:
            connection.execute("DELETE FROM extracted_records")
            connection.executemany(
                "INSERT INTO extracted_records "
                "(source_path, created_at, first_name, last_name, identity_number, "
                "authorization_number, equipment, needs_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                [
                    ("USUARIOprueba/2025/documento.pdf", "2026-01-02", "NOMBRE", "APELLIDO", "ID-PRUEBA-1", "A/1/1", "TAC"),
                    ("USUARIOsecundario/documento.pdf", "2026-01-01", "NOMBRE2", "APELLIDO2", "ID-PRUEBA-2", "B/1/1", "RM"),
                ],
            )
            connection.commit()
        finally:
            connection.close()
    def test_records_are_sorted_and_labeled_by_user_folder(self):
        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)

        RecordRoutes.list_records(handler)

        self.assertEqual(
            [record["user_folder"] for record in response["items"]],
            ["USUARIOprueba", "USUARIOsecundario"],
        )

    def test_export_contains_user_column_and_groups_rows(self):
        handler = object.__new__(RecordRoutes)
        handler.wfile = BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        RecordRoutes.export_records(handler)
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()), read_only=True)
        try:
            sheet = workbook["Datos extraídos"]
            self.assertEqual(sheet.cell(row=1, column=1).value, "Usuario")
            self.assertEqual(sheet.cell(row=1, column=7).value, "Práctica")
            self.assertEqual(sheet.cell(row=1, column=8).value, "Expedición")
            self.assertEqual(sheet.cell(row=1, column=9).value, "Vencimiento")
            self.assertEqual(sheet.max_column, 11)
            self.assertEqual(sheet.cell(row=2, column=1).value, "USUARIOprueba")
            self.assertEqual(sheet.cell(row=3, column=1).value, "USUARIOsecundario")
        finally:
            workbook.close()


class DocumentRoutesTests(unittest.TestCase):
    def test_previews_legacy_word_document_as_text(self):
        target = Path(TEMP_ROOT.name) / "word2003.doc"
        target.write_bytes(b"mock Word document")
        self.addCleanup(target.unlink)
        response_headers = {}
        handler = object.__new__(DocumentRoutes)
        handler.wfile = BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: response_headers.update({name: value})
        handler.end_headers = lambda: None

        with patch("routes.documents.extract_legacy_doc_text", return_value="Texto de Word"):
            DocumentRoutes.preview(handler, {"path": ["word2003.doc"]})

        self.assertEqual(handler.wfile.getvalue(), b"Texto de Word")
        self.assertEqual(response_headers["Content-Type"], "text/plain; charset=utf-8")

    def test_open_location_selects_only_a_valid_file_under_document_root(self):
        target = Path(TEMP_ROOT.name) / "folder" / "documento.pdf"
        target.parent.mkdir()
        target.write_bytes(b"test")
        self.addCleanup(shutil.rmtree, target.parent)
        response = {}
        handler = object.__new__(DocumentRoutes)
        handler.send_json = lambda payload, status=200: response.update(
            payload, status=status
        )

        with patch("routes.documents.os.name", "nt"), patch(
            "routes.documents.subprocess.Popen"
        ) as open_explorer:
            DocumentRoutes.open_location(handler, {"path": "folder/documento.pdf"})

        open_explorer.assert_called_once_with(["explorer.exe", "/select,", str(target.resolve())])
        self.assertEqual(response["status"], 202)
        self.assertEqual(response["path"], "folder/documento.pdf")

    def test_open_folder_selects_only_a_valid_user_folder(self):
        target = Path(TEMP_ROOT.name) / "USUARIOprueba"
        target.mkdir()
        self.addCleanup(shutil.rmtree, target)
        response = {}
        handler = object.__new__(DocumentRoutes)
        handler.send_json = lambda payload, status=200: response.update(
            payload, status=status
        )

        with patch("routes.documents.os.name", "nt"), patch(
            "routes.documents.subprocess.Popen"
        ) as open_explorer:
            DocumentRoutes.open_folder(handler, {"name": "USUARIOprueba"})

        open_explorer.assert_called_once_with(["explorer.exe", str(target.resolve())])
        self.assertEqual(response["status"], 202)
        self.assertEqual(response["name"], "USUARIOprueba")

    def test_open_folder_rejects_path_traversal(self):
        handler = object.__new__(DocumentRoutes)
        with patch("routes.documents.os.name", "nt"), patch(
            "routes.documents.subprocess.Popen"
        ) as open_explorer:
            with self.assertRaises(ValueError):
                DocumentRoutes.open_folder(handler, {"name": ".."})
        open_explorer.assert_not_called()

    def test_open_location_rejects_paths_outside_document_root(self):
        handler = object.__new__(DocumentRoutes)
        with patch("routes.documents.os.name", "nt"), patch(
            "routes.documents.subprocess.Popen"
        ) as open_explorer:
            with self.assertRaises(ValueError):
                DocumentRoutes.open_location(handler, {"path": "../outside.pdf"})
        open_explorer.assert_not_called()


class UserRoutesTests(unittest.TestCase):
    def setUp(self):
        initialize_db()
        self.user_folder = Path(TEMP_ROOT.name) / "USUARIOprueba"
        shutil.rmtree(self.user_folder, ignore_errors=True)
        document_folder = self.user_folder / "2025"
        document_folder.mkdir(parents=True)
        (document_folder / "autorizacion.pdf").write_bytes(b"test")
        (document_folder / "nota.txt").write_text("texto", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.user_folder, True)

        connection = connect_db()
        try:
            connection.execute("DELETE FROM extracted_records")
            connection.execute(
                "INSERT INTO extracted_records "
                "(source_path, created_at, first_name, last_name, identity_number, "
                "authorization_number, equipment, needs_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    "USUARIOprueba/2025/autorizacion.pdf",
                    "2026-01-01",
                    "NOMBRE",
                    "APELLIDO",
                    "ID-PRUEBA",
                    "A/1/1",
                    "TAC",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))

    def test_word_2003_files_are_previewable_and_importable(self):
        legacy_doc = self.user_folder / "2025" / "documento.doc"
        legacy_doc.write_bytes(b"Word 2003 content")
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))
        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)

        UserRoutes.get_user(handler, {"name": ["USUARIOprueba"]})

        file_data = next(file for file in response["files"] if file["path"].endswith(".doc"))
        self.assertTrue(file_data["can_import"])
        self.assertTrue(file_data["can_preview"])

    def test_user_list_counts_files_and_imports(self):
        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)

        UserRoutes.list_users(handler, {"q": [""]})

        self.assertEqual(len(response["items"]), 1)
        self.assertEqual(response["items"][0]["name"], "USUARIOprueba")
        self.assertEqual(response["items"][0]["document_count"], 2)
        self.assertEqual(response["items"][0]["imported_count"], 1)
        self.assertFalse(response["items"][0]["has_authorization"])
        self.assertEqual(response["items"][0]["authorization_records"], [])

    def test_user_list_returns_only_the_requested_page(self):
        other_user = Path(TEMP_ROOT.name) / "OTRO" / "2025"
        other_user.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, other_user.parent, True)
        (other_user / "nota.txt").write_text("texto", encoding="utf-8")
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))

        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        UserRoutes.list_users(
            handler,
            {"q": [""], "offset": ["1"], "limit": ["1"]},
        )

        self.assertEqual(response["total"], 2)
        self.assertEqual(response["offset"], 1)
        self.assertEqual(response["limit"], 1)
        self.assertEqual(len(response["items"]), 1)
        self.assertEqual(response["items"][0]["name"], "USUARIOprueba")

    def test_user_list_includes_authorization_filter_data(self):
        source_path = (
            "USUARIOprueba/2025/"
            "Certificado_Autorizacion_Individual_ARNR.pdf"
        )
        (self.user_folder / "2025" / Path(source_path).name).write_bytes(b"test")
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))
        connection = connect_db()
        try:
            connection.execute(
                "INSERT INTO extracted_records "
                "(source_path, created_at, authorization_number, issue_date, expiry_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (source_path, "2026-01-02", "0100/1/2", "01/01/2026", "01/01/2027"),
            )
            connection.commit()
        finally:
            connection.close()

        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        UserRoutes.list_users(handler, {"q": [""]})

        user = response["items"][0]
        self.assertTrue(user["has_authorization"])
        authorization_record = user["authorization_records"][0]
        self.assertIsInstance(authorization_record["id"], int)
        self.assertEqual(authorization_record["source_path"], source_path)
        self.assertEqual(authorization_record["authorization_number"], "0100/1/2")
        self.assertEqual(authorization_record["issue_date"], "01/01/2026")
        self.assertEqual(authorization_record["expiry_date"], "01/01/2027")

    def test_user_list_can_search_extracted_identity_for_workflow(self):
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE extracted_records SET identity_number = ?, "
                "authorization_number = ?, first_name = ?, is_authorization_certificate = 1 "
                "WHERE source_path = ?",
                ("1.234.567-8", "100/0/0", "Nombre anterior", "USUARIOprueba/2025/autorizacion.pdf"),
            )
            connection.execute(
                "INSERT INTO extracted_records (source_path, created_at, first_name, identity_number, "
                "authorization_number, is_authorization_certificate) VALUES (?, ?, ?, ?, ?, 1)",
                ("USUARIOprueba/2025/nuevo.pdf", "2026-01-02", "Nombre nuevo", "8.765.432-1", "100/1/0"),
            )
            connection.commit()
        finally:
            connection.close()

        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        for query in ("12345678", "1.234.567-8", "234.567"):
            with self.subTest(query=query):
                UserRoutes.list_users(handler, {"q": [query], "search_identity": ["1"]})
                self.assertEqual([item["name"] for item in response["items"]], ["USUARIOprueba"])
                user = response["items"][0]
                self.assertEqual(user["authorization_records"][0]["first_name"], "Nombre nuevo")
                self.assertEqual(user["matched_authorization"]["first_name"], "Nombre anterior")
        UserRoutes.list_users(handler, {"q": ["12345678"]})
        self.assertEqual(response["items"], [])
        UserRoutes.list_users(handler, {"q": ["---"], "search_identity": ["1"]})
        self.assertEqual(response["items"], [])
        UserRoutes.list_users(handler, {"q": ["usuario"], "search_identity": ["1"]})
        self.assertEqual([item["name"] for item in response["items"]], ["USUARIOprueba"])

    def test_user_list_filters_by_partial_practice(self):
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE extracted_records SET practice = ?, "
                "is_authorization_certificate = 1 "
                "WHERE source_path = ?",
                ("RADIOLOGÍA INTERVENCIONISTA", "USUARIOprueba/2025/autorizacion.pdf"),
            )
            connection.commit()
        finally:
            connection.close()

        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        UserRoutes.list_users(handler, {"q": [""], "practice": ["radiologia"]})

        self.assertEqual([user["name"] for user in response["items"]], ["USUARIOprueba"])

    def test_user_list_marks_unimported_authorization_certificate(self):
        certificate = (
            self.user_folder / "2025" /
            "Certificado_Autorizacion_Individual_ARNR.pdf"
        )
        certificate.write_bytes(b"test")
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))

        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        UserRoutes.list_users(handler, {"q": [""]})

        user = response["items"][0]
        self.assertTrue(user["has_authorization"])
        self.assertEqual(user["authorization_records"], [])

    def test_user_ficha_contains_documents_and_imported_records(self):
        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)

        UserRoutes.get_user(handler, {"name": ["USUARIOprueba"]})

        self.assertEqual(response["user"]["name"], "USUARIOprueba")
        self.assertEqual(response["user"]["imported_count"], 1)
        self.assertEqual(len(response["files"]), 2)
        pdf = next(item for item in response["files"] if item["extension"] == ".pdf")
        self.assertTrue(pdf["can_import"])
        self.assertFalse(pdf["is_authorization_certificate"])
        self.assertIn("USUARIOprueba/2025/nota.txt", [item["path"] for item in response["files"]])
        self.assertEqual(pdf["record"]["first_name"], "NOMBRE")

    def test_user_ficha_returns_a_bounded_file_page(self):
        extra_folder = self.user_folder / "2025" / "extra"
        extra_folder.mkdir()
        (extra_folder / "uno.txt").write_text("1", encoding="utf-8")
        (extra_folder / "dos.txt").write_text("2", encoding="utf-8")
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))

        response = {}
        handler = object.__new__(UserRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        UserRoutes.get_user(
            handler,
            {"name": ["USUARIOprueba"], "offset": ["1"], "limit": ["1"]},
        )

        self.assertEqual(response["user"]["document_count"], 4)
        self.assertEqual(response["offset"], 1)
        self.assertEqual(response["limit"], 1)
        self.assertEqual(len(response["files"]), 1)

    def test_manual_import_detects_authorization_by_content_and_updates_ficha(self):
        certificate = self.user_folder / "2025" / "autorizacion.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text(
            (30, 30),
            "\n".join([
                "AUTORIZACIÓN INDIVIDUAL",
                "Ha sido autorizado/a para la práctica de:",
                "RADIOLOGÍA",
                "de acuerdo al detalle del Anexo",
                "Nombres: NOMBRE",
                "Apellidos: APELLIDO",
                "Documento de identidad: 0.000.000-0",
                "Número de autorización: 0100/1/2",
                "Expedición: 01/01/2026",
                "Vencimiento: 01/01/2028",
                "Equipos habilitados",
                "Equipo: Tomógrafo",
            ]),
        )
        document.save(certificate)
        document.close()
        sync_file_catalog((("documents", Path(TEMP_ROOT.name)),))

        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        handler.send_error_json = lambda message, status=400: self.fail(message)
        RecordRoutes.extract_record(
            handler,
            {"path": "USUARIOprueba/2025/autorizacion.pdf"},
        )

        self.assertTrue(response["record"]["is_authorization_certificate"])
        self.assertEqual(response["record"]["authorization_number"], "0100/1/2")
        self.assertEqual(response["record"]["issue_date"], "01/01/2026")
        self.assertEqual(response["record"]["expiry_date"], "01/01/2028")

        ficha_response = {}
        ficha_handler = object.__new__(UserRoutes)
        ficha_handler.send_json = lambda payload, status=200: ficha_response.update(payload)
        UserRoutes.get_user(ficha_handler, {"name": ["USUARIOprueba"]})
        imported_file = next(
            item for item in ficha_response["files"]
            if item["path"] == "USUARIOprueba/2025/autorizacion.pdf"
        )
        self.assertTrue(imported_file["is_authorization_certificate"])
        self.assertTrue(imported_file["record"]["is_authorization_certificate"])

        users_response = {}
        users_handler = object.__new__(UserRoutes)
        users_handler.send_json = lambda payload, status=200: users_response.update(payload)
        UserRoutes.list_users(users_handler, {"q": ["USUARIOprueba"]})
        listed_user = users_response["items"][0]
        self.assertTrue(listed_user["has_authorization"])
        self.assertEqual(len(listed_user["authorization_records"]), 1)

    def test_user_ficha_rejects_path_traversal(self):
        handler = object.__new__(UserRoutes)
        with self.assertRaises(ValueError):
            UserRoutes.get_user(handler, {"name": [".."]})

    def test_non_authorization_document_is_not_imported(self):
        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        handler.send_error_json = lambda message, status=400: response.update(
            {"error": message, "status": status}
        )

        RecordRoutes.extract_record(
            handler,
            {"path": "USUARIOprueba/2025/nota.txt"},
        )

        self.assertEqual(response["status"], 422)
        connection = connect_db()
        try:
            row = connection.execute(
                "SELECT 1 FROM extracted_records WHERE source_path = ?",
                ("USUARIOprueba/2025/nota.txt",),
            ).fetchone()
            self.assertIsNone(row)
        finally:
            connection.close()

    def test_txt_authorization_can_be_imported(self):
        path = self.user_folder / "2025" / "autorizacion.txt"
        path.write_text(
            "AUTORIZACIÓN INDIVIDUAL\nHa sido autorizado/a para la práctica de:\n"
            "RADIOLOGÍA\nNúmero de autorización: 0100/1/2\n",
            encoding="utf-8",
        )
        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        handler.send_error_json = lambda message, status=400: self.fail(message)
        RecordRoutes.extract_record(handler, {"path": "USUARIOprueba/2025/autorizacion.txt"})
        self.assertTrue(response["record"]["is_authorization_certificate"])
        self.assertEqual(response["record"]["authorization_number"], "0100/1/2")

    def test_certificate_import_saves_specialized_fields(self):
        certificate = self.user_folder / "2025" / "Certificado_Autorizacion_Individual_ARNR.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text(
            (30, 30),
            "\n".join([
                "Montevideo, 19 de abril de 2024",
                "AUTORIZACIÓN INDIVIDUAL",
                "NOMBRE: PERSONA EJEMPLO",
                "DOCUMENTO DE IDENTIDAD: 0.000.000-0",
                "Ha sido autorizado/a para la práctica de:",
                "RADIOLOGÍA INTERVENCIONISTA",
                "de acuerdo al detalle que aparece en el Anexo",
                "Autorización N°: 891/0/0",
                "Vencimiento: 18/04/2029",
                "Equipos habilitados",
                "Equipo: Angiógrafo General Electric",
                "Firma responsable",
            ]),
        )
        document.save(certificate)
        document.close()
        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        handler.send_error_json = lambda message, status=400: self.fail(message)

        RecordRoutes.extract_record(
            handler,
            {"path": "USUARIOprueba/2025/Certificado_Autorizacion_Individual_ARNR.pdf"},
        )

        record = response["record"]
        self.assertEqual(record["authorization_number"], "891/0/0")
        self.assertEqual(record["practice"], "RADIOLOGÍA INTERVENCIONISTA")
        self.assertEqual(record["issue_date"], "19/04/2024")
        self.assertEqual(record["expiry_date"], "18/04/2029")
        self.assertEqual(record["equipment"], "Equipo: Angiógrafo General Electric")
        self.assertFalse(record["needs_review"])

        response.clear()
        RecordRoutes.extract_record(
            handler,
            {"path": "USUARIOprueba/2025/Certificado_Autorizacion_Individual_ARNR.pdf"},
        )
        self.assertTrue(response["updated"])
        self.assertEqual(response["record"]["practice"], "RADIOLOGÍA INTERVENCIONISTA")
        self.assertEqual(response["record"]["first_name"], record["first_name"])


class EntityRoutesTests(unittest.TestCase):
    def test_list_all_entities_is_not_limited_to_a_page(self):
        initialize_db()
        areas = [
            {"institution": f"Centro {index:03}", "practice": "Práctica",
             "department": "Departamento", "locality": "",
             "relative_path": f"Practica/Departamento/Centro {index:03}"}
            for index in range(205)
        ]
        response = {}
        handler = object.__new__(EntityRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        with patch.object(entities_module, "_entity_areas", return_value=(areas, 2)):
            EntityRoutes.list_entities(handler, {})
            self.assertEqual((len(response["items"]), response["total"]), (100, 205))
            response.clear()
            EntityRoutes.list_entities(handler, {"all": ["1"]})
            self.assertEqual((len(response["items"]), response["total"], response["offset"]), (205, 205, 0))
            self.assertGreaterEqual(response["warnings"], 2)
            response.clear()
            EntityRoutes.list_entities(handler, {"all": ["1"], "q": ["Centro 204"]})
            self.assertEqual([item["name"] for item in response["items"]], ["Centro 204"])

    def test_extracts_license_pdf_equipment_table(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "LO prueba.pdf"
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((50, 50), "LO-X-1234-5678-AB")
                for x in (50, 200, 350):
                    page.draw_line((x, 80), (x, 140))
                for y in (80, 110, 140):
                    page.draw_line((50, y), (350, y))
                for x, y, value in (
                    (55, 100, "Tipo de equipo"), (205, 100, "Fabricante"),
                    (55, 130, "Equipo prueba"), (205, 130, "Marca prueba"),
                ):
                    page.insert_text((x, y), value)
                pdf.save(path)
            text, tables = entities_module._extract_license_content(path)
        self.assertIn("LO-X-1234-5678-AB", text)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["title"], "Equipos")
        self.assertEqual(tables[0]["rows"][1], ["Equipo prueba", "Marca prueba"])

    def test_license_technical_tables_are_saved_and_returned_with_source(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        folder = entity_root / "PRACTICA" / "DEPTO" / "CENTRO" / "1. AUTORIZACION"
        folder.mkdir(parents=True)
        document = folder / "LO-X-1234-5678-AB.docx"
        xml = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>LO-X-1234-5678-AB</w:t></w:r></w:p>
<w:p><w:r><w:t>Equipos generadores</w:t></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:p><w:r><w:t>Tipo de equipo</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Fabricante</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>Equipo de prueba</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Fabricante de prueba</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
<w:p><w:r><w:t>Sistema de prueba</w:t></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Sistema de prueba</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>Modelo</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Modelo de prueba</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
<w:p><w:r><w:t>Observaciones</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Sin relación</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""
        with ZipFile(document, "w") as archive:
            archive.writestr("word/document.xml", xml)
        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: None
            handler.send_error_json = lambda message, status=400: self.fail(message)
            EntityRoutes.extract_entity_license(
                handler, {"path": document.relative_to(entity_root).as_posix()}
            )
            result = {}
            handler.send_json = lambda payload, status=200: result.update(payload)
            EntityRoutes.get_entity(handler, {"name": ["CENTRO"]})
            card = result["practices"][0]["license_cards"][0]
            self.assertEqual(card["technical_tables_pending"], 0)
            self.assertEqual([table["title"] for table in card["technical_tables"]], ["Equipos", "Sistemas"])
            self.assertEqual(card["technical_tables"][0]["rows"][1], ["Equipo de prueba", "Fabricante de prueba"])
            self.assertEqual(card["technical_tables"][1]["rows"][0], ["Sistema de prueba", ""])
            self.assertEqual(card["technical_tables"][0]["source_name"], document.name)
            result.clear()
            EntityRoutes.list_entity_licenses(handler, {})
            self.assertEqual(result["items"][0]["technical_tables"], card["technical_tables"])

    def test_legacy_license_table_content_is_not_lost(self):
        with patch.object(
            entities_module, "extract_legacy_doc_content",
            return_value=("LO-X-1234-5678-AB", [("", [
                ["Sistema de prueba", ""], ["Modelo", "Modelo de prueba"],
            ])]),
        ):
            text, tables = entities_module._extract_license_content(Path("LO prueba.doc"))
        self.assertIn("LO-X-1234-5678-AB", text)
        self.assertEqual(tables[0]["title"], "Sistemas")
        self.assertEqual(tables[0]["rows"][1], ["Modelo", "Modelo de prueba"])

    def test_license_summary_counts_all_grouped_licenses_across_pages(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        folder = entity_root / "PRACTICA" / "DEPTO" / "CENTRO" / "1. AUTORIZACION"
        folder.mkdir(parents=True)
        today = date.today()
        cases = [
            ("LO vigente.txt", "LO-X-0001-0001-AA", (today + timedelta(days=100)).strftime("%d/%m/%Y")),
            ("LO copia.txt", "LO-X-0001-0001-AA", (today + timedelta(days=100)).strftime("%d/%m/%Y")),
            ("LO proxima.txt", "LO-X-0002-0002-BB", today.strftime("%d/%m/%Y")),
            ("LO vencida.txt", "LO-X-0003-0003-CC", (today - timedelta(days=1)).strftime("%d/%m/%Y")),
            ("LO sin fecha.txt", "LO-X-0004-0004-DD", ""),
        ]
        for name, _, _ in cases:
            (folder / name).write_text("texto", encoding="utf-8")
        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            for name, number, expiry in cases:
                entities_module._save_license(
                    (folder / name).relative_to(entity_root).as_posix(),
                    number,
                    {"expiry_date": expiry},
                )
            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            statuses = []
            for offset in range(4):
                EntityRoutes.list_entity_licenses(
                    handler, {"offset": [str(offset)], "limit": ["1"]}
                )
                self.assertEqual(response["total"], 4)
                self.assertEqual(
                    response["summary"],
                    {"valid": 1, "soon": 1, "expired": 1, "unknown": 1},
                )
                statuses.append(response["items"][0]["status"])
            self.assertEqual(set(statuses), {"valid", "soon", "expired", "unknown"})

    def test_extracted_licenses_are_grouped_by_practice_and_paginated(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        first = entity_root / "PRACTICA A" / "DEPTO" / "CENTRO" / "1. AUTORIZACION"
        second = entity_root / "PRACTICA B" / "DEPTO" / "CENTRO" / "1. AUTORIZACION"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        files = [
            first / "LO principal.txt",
            first / "LO complemento.txt",
            second / "LO practica B.txt",
            first / "DT dictamen.txt",
        ]
        for path in files:
            path.write_text("texto", encoding="utf-8")
        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            for path in files:
                entities_module._save_license(
                    path.relative_to(entity_root).as_posix(),
                    "LO-X-1234-5678-AB",
                    {"representative": "Responsable", "expiry_date": "20/11/2029"},
                )
            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.list_entity_licenses(handler, {"limit": ["1"]})
            self.assertEqual(response["total"], 2)
            self.assertEqual(response["items"][0]["practice"], "PRACTICA A")
            self.assertEqual(response["items"][0]["institution"], "CENTRO")
            self.assertEqual(response["items"][0]["representative"], "Responsable")
            self.assertEqual(
                {doc["name"] for doc in response["items"][0]["documents"]},
                {"LO principal.txt", "LO complemento.txt"},
            )
            response.clear()
            EntityRoutes.list_entity_licenses(handler, {"offset": ["1"], "limit": ["1"]})
            self.assertEqual(response["items"][0]["practice"], "PRACTICA B")
            self.assertEqual(len(response["items"][0]["documents"]), 1)

    def test_filters_entities_by_license_presence_and_status(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        today = date.today()
        license_files = [
            ("CENTRO-VALIDO", "RADIOLOGIA", "LO-X-0001-0001-AA", today + timedelta(days=90)),
            ("CENTRO-PROXIMO", "RADIOLOGIA", "LO-X-0002-0002-BB", today + timedelta(days=30)),
            ("CENTRO-VENCIDO", "RADIOLOGIA", "LO-X-0003-0003-CC", today - timedelta(days=1)),
            ("CENTRO-SIN-FECHA", "RADIOLOGIA", "LO-X-0004-0004-DD", None),
            ("CENTRO-MIXTO", "RADIOLOGIA", "LO-X-0005-0005-EE", today + timedelta(days=90)),
            ("CENTRO-MIXTO", "TOMOGRAFIA", "LO-X-0006-0006-FF", today - timedelta(days=1)),
            ("CENTRO-PENDIENTE", "RADIOLOGIA", "LO-X-0007-0007-GG", "pending"),
        ]
        imported_paths = []
        for institution, practice, license_number, expiry in license_files:
            document = (
                entity_root / practice / "DEPARTAMENTO" / institution
                / "1. AUTORIZACION" / f"{license_number}.txt"
            )
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_text(license_number, encoding="utf-8")
            if expiry != "pending":
                imported_paths.append((
                    document.relative_to(entity_root).as_posix(),
                    license_number,
                    expiry.strftime("%d/%m/%Y") if expiry else "",
                ))

        no_license = (
            entity_root / "RADIOLOGIA" / "DEPARTAMENTO" / "CENTRO-SIN-LICENCIA"
            / "1. AUTORIZACION" / "nota.txt"
        )
        no_license.parent.mkdir(parents=True, exist_ok=True)
        no_license.write_text("Sin licencia LO", encoding="utf-8")

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            for path, number, expiry in imported_paths:
                entities_module._save_license(path, number, {"expiry_date": expiry})

            def list_entities(params):
                response = {}
                handler = object.__new__(EntityRoutes)
                handler.send_json = lambda payload, status=200: response.update(payload)
                EntityRoutes.list_entities(handler, params)
                return {item["name"] for item in response["items"]}

            self.assertEqual(
                list_entities({"license": ["with"]}),
                {
                    "CENTRO-VALIDO",
                    "CENTRO-PROXIMO",
                    "CENTRO-VENCIDO",
                    "CENTRO-SIN-FECHA",
                    "CENTRO-MIXTO",
                    "CENTRO-PENDIENTE",
                },
            )
            self.assertEqual(
                list_entities({"license": ["without"]}),
                {"CENTRO-SIN-LICENCIA"},
            )
            self.assertEqual(
                list_entities({"status": ["valid"]}),
                {"CENTRO-VALIDO", "CENTRO-MIXTO"},
            )
            self.assertEqual(
                list_entities({"status": ["soon"]}),
                {"CENTRO-PROXIMO"},
            )
            self.assertEqual(
                list_entities({"status": ["expired"]}),
                {"CENTRO-VENCIDO", "CENTRO-MIXTO"},
            )
            self.assertEqual(
                list_entities({"license": ["with"], "status": ["expired"]}),
                {"CENTRO-VENCIDO", "CENTRO-MIXTO"},
            )
            self.assertEqual(
                list_entities({"status": ["unknown"]}),
                {"CENTRO-SIN-FECHA"},
            )
            self.assertEqual(
                list_entities({"status": ["pending"]}),
                {"CENTRO-PENDIENTE"},
            )

    def test_keeps_numbered_divisions_under_their_center(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        center = "CENTRO"
        divisions = [
            "1. AUTORIZACION",
            "2. DOCUMENTACION",
            "3. EMERGENCIA",
            "4. SEGURIDAD",
            "5. INSPECCION",
            "6. VISTAS",
            "7. COERCION",
            "8. LINEA DE TIEMPO",
            "9. HISTORICO",
            "Localidad",
        ]
        for index, division in enumerate(divisions):
            document = (
                entity_root
                / "PRACTICA"
                / "DEPARTAMENTO"
                / center
                / division
                / f"archivo-{index}.txt"
            )
            document.parent.mkdir(parents=True)
            document.write_text("Documento del centro", encoding="utf-8")

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.list_entities(handler, {})

            self.assertEqual([item["name"] for item in response["items"]], [center])
            self.assertEqual(response["items"][0]["document_count"], len(divisions))

            response.clear()
            EntityRoutes.get_entity(handler, {"name": [center]})

        area = response["practices"][0]["areas"][0]
        self.assertEqual(area["locality"], "")
        self.assertEqual(area["path"], "PRACTICA/DEPARTAMENTO/CENTRO")
        self.assertEqual(area["document_count"], len(divisions))

    def test_discovers_institutions_inside_locality_folders(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        institutions = ["CENTRO-A", "CENTRO-B"]
        for institution in institutions:
            document = (
                entity_root
                / "PRACTICA"
                / "DEPARTAMENTO"
                / "LOCALIDAD"
                / institution
                / "1. AUTORIZACION"
                / "licencia.txt"
            )
            document.parent.mkdir(parents=True)
            document.write_text("LO-X-1234-5678-AB", encoding="utf-8")

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.list_entities(handler, {})

            self.assertEqual(
                {item["name"] for item in response["items"]},
                set(institutions),
            )
            self.assertEqual(response["localities"], ["LOCALIDAD"])

            response.clear()
            EntityRoutes.get_entity(handler, {"name": ["CENTRO-A"]})
            area = response["practices"][0]["areas"][0]
            self.assertEqual(area["locality"], "LOCALIDAD")
            self.assertTrue(
                os.path.samefile(
                    entities_module._entity_area_folder(area["path"]),
                    entity_root / "PRACTICA" / "DEPARTAMENTO" / "LOCALIDAD" / "CENTRO-A",
                )
            )

    def test_word_2003_files_are_previewable_and_importable(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        document = (
            entity_root / "PRACTICA" / "DEPARTAMENTO" / "INSTITUCION"
            / "1. AUTORIZACION" / "LO-X-1234-5678-AB.doc"
        )
        document.parent.mkdir(parents=True)
        document.write_bytes(b"Word 2003 content")
        response = {}
        handler = object.__new__(EntityRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["INSTITUCION"],
                    "area": ["PRACTICA/DEPARTAMENTO/INSTITUCION"],
                },
            )

        file_data = response["files"][0]
        self.assertTrue(file_data["can_import"])
        self.assertTrue(file_data["can_preview"])

    def test_entity_area_returns_a_bounded_file_page(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        area = entity_root / "PRACTICA" / "DEPARTAMENTO" / "INSTITUCION"
        division = area / "1. AUTORIZACION"
        division.mkdir(parents=True)
        for index in range(3):
            (division / f"archivo-{index}.txt").write_text(str(index), encoding="utf-8")
        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["INSTITUCION"],
                    "area": ["PRACTICA/DEPARTAMENTO/INSTITUCION"],
                    "offset": ["1"],
                    "limit": ["1"],
                },
            )

        self.assertEqual(response["total"], 3)
        self.assertEqual(response["offset"], 1)
        self.assertEqual(response["limit"], 1)
        self.assertEqual(len(response["files"]), 1)

    def test_entity_files_follow_subfolder_order_instead_of_creation_date(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        area = entity_root / "PRACTICA" / "DEPARTAMENTO" / "INSTITUCION"
        first_file = area / "1. AUTORIZACION" / "documento.txt"
        second_file = area / "2. RENOVACIONES" / "documento.txt"
        first_file.parent.mkdir(parents=True)
        second_file.parent.mkdir(parents=True)
        first_file.write_text("primero", encoding="utf-8")
        second_file.write_text("segundo", encoding="utf-8")
        first_relative = first_file.relative_to(entity_root).as_posix()
        second_relative = second_file.relative_to(entity_root).as_posix()

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            connection = connect_db()
            try:
                connection.execute(
                    "UPDATE file_catalog SET created_ns = ? WHERE root = 'entities' AND path = ?",
                    (1_700_000_000_000_000_000, first_relative),
                )
                connection.execute(
                    "UPDATE file_catalog SET created_ns = ? WHERE root = 'entities' AND path = ?",
                    (1_800_000_000_000_000_000, second_relative),
                )
                connection.commit()
            finally:
                connection.close()

            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["INSTITUCION"],
                    "area": ["PRACTICA/DEPARTAMENTO/INSTITUCION"],
                },
            )

        self.assertEqual(
            [file["path"] for file in response["files"]],
            [first_relative, second_relative],
        )

    def test_entity_area_lists_all_folders_and_loads_each_folder_page(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        area = entity_root / "PRACTICA" / "DEPARTAMENTO" / "INSTITUCION"
        for folder_number in range(1, 10):
            folder = area / f"{folder_number}. CARPETA"
            folder.mkdir(parents=True)
            for index in range(60 if folder_number == 1 else 1):
                (folder / f"archivo-{index:02}.txt").write_text(
                    "contenido",
                    encoding="utf-8",
                )
        root_file = area / "documento-principal.txt"
        root_file.write_text("carpeta principal", encoding="utf-8")
        relative_area = "PRACTICA/DEPARTAMENTO/INSTITUCION"

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            handler = object.__new__(EntityRoutes)
            response = {}
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["INSTITUCION"],
                    "area": [relative_area],
                    "folders_only": ["1"],
                },
            )
            folders = response["folders"]
            self.assertEqual(len(folders), 10)
            self.assertTrue(folders[0]["is_root"])
            self.assertEqual(
                [folder["name"] for folder in folders[1:]],
                [f"{number}. CARPETA" for number in range(1, 10)],
            )
            self.assertEqual(
                [folder["document_count"] for folder in folders[1:]],
                [60, *([1] * 8)],
            )

            response.clear()
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["INSTITUCION"],
                    "area": [relative_area],
                    "folder": ["1. CARPETA"],
                    "offset": ["50"],
                    "limit": ["50"],
                },
            )
            self.assertEqual(response["total"], 60)
            self.assertEqual(response["offset"], 50)
            self.assertEqual(len(response["files"]), 10)
            self.assertTrue(all(
                file["path"].startswith(f"{relative_area}/1. CARPETA/")
                for file in response["files"]
            ))

            response.clear()
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["INSTITUCION"],
                    "area": [relative_area],
                    "root": ["1"],
                },
            )

        self.assertEqual(response["total"], 1)
        self.assertEqual(
            response["files"][0]["path"],
            root_file.relative_to(entity_root).as_posix(),
        )

    def test_bulk_import_only_reads_the_authorization_folder(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        institution = entity_root / "PRACTICA" / "CENTRO"
        authorization = institution / "1. Autorización" / "LO-X-1234-5678-AA.txt"
        technical_opinion = institution / "1. Autorización" / "DT-X-1234-5678-AA.txt"
        history = institution / "9. Histórico" / "LO-X-8765-4321-BB.txt"
        nested_authorization = (
            institution / "2. Apoyo" / "1. Autorización" / "LO-X-1111-2222-CC.txt"
        )
        for path, contents in (
            (authorization, "LO-X-1234-5678-AA"),
            (technical_opinion, "Dictamen técnico basado en LO-X-1234-5678-AA"),
            (history, "LO-X-8765-4321-BB"),
            (nested_authorization, "LO-X-1111-2222-CC"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            self.assertTrue(entities_module.ENTITY_IMPORT_LOCK.acquire(blocking=False))
            entities_module._entity_import_worker()

        connection = connect_db()
        try:
            saved = {
                row["source_path"]
                for row in connection.execute("SELECT source_path FROM entity_licenses")
            }
        finally:
            connection.close()
        self.assertIn(authorization.relative_to(entity_root).as_posix(), saved)
        self.assertNotIn(technical_opinion.relative_to(entity_root).as_posix(), saved)
        self.assertNotIn(history.relative_to(entity_root).as_posix(), saved)
        self.assertNotIn(nested_authorization.relative_to(entity_root).as_posix(), saved)

    def test_entity_profile_returns_license_cards_for_each_practice(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        authorization_folder = (
            entity_root / "PRACTICA" / "DEPARTAMENTO" / "CENTRO" / "1. AUTORIZACION"
        )
        document = authorization_folder / "LO-X-1234-5678-AB.txt"
        technical_opinion = authorization_folder / "DT-X-1234-5678-AB.txt"
        document.parent.mkdir(parents=True)
        document.write_text(
            "Montevideo, 5 de noviembre de 2025\nREPRESENTANTE LEGAL: Ana Perez\n"
            "LO-X-1234-5678-AB\nAutorización No: 373/0/4\nVencimiento: 20/11/2029",
            encoding="utf-8",
        )
        technical_opinion.write_text(
            "Dictamen técnico\nREPRESENTANTE LEGAL: Datos del dictamen\n"
            "LO-X-1234-5678-AB\nVencimiento: 20/11/2039",
            encoding="utf-8",
        )
        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: None
            handler.send_error_json = lambda message, status=400: self.fail(message)
            EntityRoutes.extract_entity_license(
                handler, {"path": document.relative_to(entity_root).as_posix()}
            )
            error_response = {}
            handler.send_error_json = lambda message, status=400: error_response.update(
                {"error": message, "status": status}
            )
            EntityRoutes.extract_entity_license(
                handler, {"path": technical_opinion.relative_to(entity_root).as_posix()}
            )
            entities_module._save_license(
                technical_opinion.relative_to(entity_root).as_posix(),
                "LO-X-1234-5678-AB",
                {
                    "representative": "Datos del dictamen",
                    "expiry_date": "20/11/2039",
                },
            )
            response = {}
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.get_entity(handler, {"name": ["CENTRO"]})

        cards = response["practices"][0]["license_cards"]
        self.assertEqual(error_response["status"], 422)
        self.assertEqual([card["license_number"] for card in cards], ["LO-X-1234-5678-AB"])
        self.assertEqual(cards[0]["documents"][0]["name"], document.name)
        self.assertEqual(cards[0]["representative"], "Ana Perez")
        self.assertEqual(cards[0]["authorization_number"], "373/0/4")
        self.assertEqual(cards[0]["issue_date"], "05/11/2025")
        self.assertEqual(cards[0]["expiry_date"], "20/11/2029")
        area = response["practices"][0]["areas"][0]
        self.assertEqual(area["imported_count"], 1)
        self.assertEqual(
            [item["name"] for item in area["technical_opinions"]],
            [technical_opinion.name],
        )
        list_response = {}
        file_response = {}
        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            handler.send_json = lambda payload, status=200: list_response.update(payload)
            EntityRoutes.list_entities(handler, {})
            handler.send_json = lambda payload, status=200: file_response.update(payload)
            EntityRoutes.get_entity(
                handler,
                {
                    "name": ["CENTRO"],
                    "area": ["PRACTICA/DEPARTAMENTO/CENTRO"],
                    "folder": ["1. AUTORIZACION"],
                },
            )
        self.assertEqual(list_response["items"][0]["imported_count"], 1)
        files_by_name = {item["name"]: item for item in file_response["files"]}
        self.assertTrue(files_by_name[document.name]["can_import"])
        self.assertFalse(files_by_name[technical_opinion.name]["can_import"])
        self.assertTrue(files_by_name[technical_opinion.name]["is_technical_opinion"])
        self.assertEqual(files_by_name[technical_opinion.name]["license_number"], "")

    def test_manual_import_saves_operation_license_in_entity_table(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        document = (
            entity_root
            / f"PRACTICA-{entity_root.name}"
            / f"DEPARTAMENTO-{entity_root.name}"
            / f"INSTITUCION-{entity_root.name}"
            / "LO-X-1234-5678-AB.txt"
        )
        document.parent.mkdir(parents=True)
        document.write_text("Licencia de operación LO-X-1234-5678-AB", encoding="utf-8")
        response = {}
        handler = object.__new__(EntityRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        handler.send_error_json = lambda message, status=400: self.fail(message)

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            EntityRoutes.extract_entity_license(
                handler,
                {"path": document.relative_to(entity_root).as_posix()},
            )

        self.assertEqual(response["license_number"], "LO-X-1234-5678-AB")
        connection = connect_db()
        try:
            row = connection.execute(
                "SELECT license_number FROM entity_licenses WHERE source_path = ?",
                (document.relative_to(entity_root).as_posix(),),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["license_number"], "LO-X-1234-5678-AB")

    def test_groups_institution_areas_and_preserves_license_by_practice(self):
        initialize_db()
        entity_root = Path(tempfile.mkdtemp(dir=TEMP_ROOT.name))
        self.addCleanup(shutil.rmtree, entity_root, True)
        institution = f"CENTRO-{entity_root.name}"
        practice_paths = [
            (
                f"RADIOLOGIA-{entity_root.name}",
                f"DEPARTAMENTO-A-{entity_root.name}",
                "LO-X-1234-5678-AA",
            ),
            (
                f"RADIOTERAPIA-{entity_root.name}",
                f"DEPARTAMENTO-B-{entity_root.name}",
                "LO-X-8765-4321-BB",
            ),
        ]
        for practice, department, license_number in practice_paths:
            document = (
                entity_root / practice / department / institution
                / "1. AUTORIZACION" / f"{license_number}.txt"
            )
            document.parent.mkdir(parents=True)
            document.write_text(license_number, encoding="utf-8")

        with patch.object(entities_module, "ENTITY_ROOT", entity_root):
            sync_file_catalog((("entities", entity_root),))
            for practice, department, _license in practice_paths:
                path = (
                    f"{practice}/{department}/{institution}/1. AUTORIZACION/"
                    f"{_license}.txt"
                )
                handler = object.__new__(EntityRoutes)
                handler.send_json = lambda payload, status=200: None
                handler.send_error_json = lambda message, status=400: self.fail(message)
                EntityRoutes.extract_entity_license(handler, {"path": path})

            response = {}
            handler = object.__new__(EntityRoutes)
            handler.send_json = lambda payload, status=200: response.update(payload)
            EntityRoutes.list_entities(handler, {})
            self.assertEqual(len(response["items"]), 1)
            self.assertEqual(response["items"][0]["name"], institution)
            self.assertEqual(response["items"][0]["area_count"], 2)

            response.clear()
            EntityRoutes.get_entity(handler, {"name": [institution]})

        self.assertEqual(len(response["practices"]), 2)
        self.assertEqual(
            {practice["licenses"][0] for practice in response["practices"]},
            {license_number for _practice, _department, license_number in practice_paths},
        )


class BulkCertificateImportTests(unittest.TestCase):
    def test_scans_all_importable_files_and_imports_authorizations_by_content(self):
        initialize_db()
        certificate = (
            Path(TEMP_ROOT.name)
            / "BULKuser"
            / "2026"
            / "Certificado_Autorizacion_Individual_ARNR.pdf"
        )
        certificate.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, certificate.parents[1], True)
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((30, 30), "Autorización N°: 891/0/0")
        document.save(certificate)
        document.close()
        renewal = certificate.parent / "Renovacion_AutorizacionIndividual_ARNR.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((30, 30), "Número de autorización: 0100/1/2")
        document.save(renewal)
        document.close()
        content_certificate = (
            Path(TEMP_ROOT.name)
            / "BULKuser"
            / "2025 - Modificacion"
            / "Draft"
            / "AI Usuario Ejemplo.pdf"
        )
        content_certificate.parent.mkdir(parents=True, exist_ok=True)
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text(
            (30, 30),
            "AUTORIZACIÓN INDIVIDUAL\n"
            "Ha sido autorizado/a para la práctica de: RADIOLOGÍA\n"
            "de acuerdo al detalle del Anexo\n"
            "Número de autorización: 0100/1/2\n"
            "Expedición: 01/01/2026\n"
            "Vencimiento: 01/01/2028",
        )
        document.save(content_certificate)
        document.close()
        text_certificate = Path(TEMP_ROOT.name) / "BULKuser" / "2025" / "AI renovación.txt"
        text_certificate.parent.mkdir(parents=True, exist_ok=True)
        text_certificate.write_text(
            "Autorización Individual\n"
            "Ha sido autorizado para la práctica de: RADIOTERAPIA\n"
            "de acuerdo al detalle del Anexo\n"
            "Número de autorización: 0200/1/0\n"
            "Expedición: 02/01/2026\n"
            "Vencimiento: 02/01/2028",
            encoding="utf-8",
        )
        ordinary_document = certificate.parent / "informe.pdf"
        document = pymupdf.open()
        document.new_page().insert_text((30, 30), "Informe general de equipos")
        document.save(ordinary_document)
        document.close()
        empty_document = certificate.parent / "documento_sin_texto.pdf"
        document = pymupdf.open()
        document.new_page()
        document.save(empty_document)
        document.close()
        connection = connect_db()
        try:
            connection.execute("DELETE FROM extracted_records")
            connection.commit()
        finally:
            connection.close()

        self.assertTrue(records_module.CERTIFICATE_IMPORT_LOCK.acquire(blocking=False))
        records_module._certificate_import_worker()
        status = dict(records_module.CERTIFICATE_IMPORT_STATE)

        self.assertFalse(status["running"])
        self.assertEqual(status["total"], 6)
        self.assertEqual(status["processed"], 6)
        self.assertEqual(status["imported"], 4)
        self.assertEqual(status["updated"], 0)
        self.assertEqual(status["without_text"], 1)
        self.assertEqual(status["errors"], 0)
        connection = connect_db()
        try:
            row = connection.execute(
                "SELECT authorization_number FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2026/Certificado_Autorizacion_Individual_ARNR.pdf",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["authorization_number"], "891/0/0")
        connection = connect_db()
        try:
            renewal_row = connection.execute(
                "SELECT authorization_number FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2026/Renovacion_AutorizacionIndividual_ARNR.pdf",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(renewal_row["authorization_number"], "0100/1/2")
        connection = connect_db()
        try:
            content_row = connection.execute(
                "SELECT authorization_number, is_authorization_certificate "
                "FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2025 - Modificacion/Draft/AI Usuario Ejemplo.pdf",),
            ).fetchone()
            text_row = connection.execute(
                "SELECT authorization_number, is_authorization_certificate "
                "FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2025/AI renovación.txt",),
            ).fetchone()
            ignored_row = connection.execute(
                "SELECT id FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2026/informe.pdf",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(content_row["authorization_number"], "0100/1/2")
        self.assertTrue(content_row["is_authorization_certificate"])
        self.assertEqual(text_row["authorization_number"], "0200/1/0")
        self.assertTrue(text_row["is_authorization_certificate"])
        self.assertIsNone(ignored_row)

        initialize_db()
        with patch.object(records_module, "read_pdf_authorization") as read_pdf:
            self.assertTrue(records_module.CERTIFICATE_IMPORT_LOCK.acquire(blocking=False))
            records_module._certificate_import_worker()
            read_pdf.assert_not_called()
        status = dict(records_module.CERTIFICATE_IMPORT_STATE)
        self.assertEqual(status["unchanged"], 6)
        self.assertEqual(status["without_text"], 1)
        self.assertEqual(status["imported"], 0)
        self.assertEqual(status["updated"], 0)

        text_certificate.write_text(
            text_certificate.read_text(encoding="utf-8").replace("0200/1/0", "03000/1/0"),
            encoding="utf-8",
        )
        self.assertTrue(records_module.CERTIFICATE_IMPORT_LOCK.acquire(blocking=False))
        records_module._certificate_import_worker()
        status = dict(records_module.CERTIFICATE_IMPORT_STATE)
        self.assertEqual(status["unchanged"], 5)
        self.assertEqual(status["updated"], 1)
        connection = connect_db()
        try:
            row = connection.execute(
                "SELECT authorization_number FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2025/AI renovación.txt",),
            ).fetchone()
            self.assertEqual(row["authorization_number"], "03000/1/0")
            connection.execute(
                "DELETE FROM extracted_records WHERE source_path = ?",
                ("BULKuser/2025/AI renovación.txt",),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertTrue(records_module.CERTIFICATE_IMPORT_LOCK.acquire(blocking=False))
        records_module._certificate_import_worker()
        status = dict(records_module.CERTIFICATE_IMPORT_STATE)
        self.assertEqual(status["unchanged"], 5)
        self.assertEqual(status["imported"], 1)

    def test_errors_and_changes_during_read_are_retried(self):
        initialize_db()
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT.name) as temp:
            path = Path(temp).resolve() / "authorization.txt"
            original = (
                "AUTORIZACIÓN INDIVIDUAL\nHa sido autorizado para la práctica de RADIOLOGÍA\n"
                "Número de autorización: 0100/1/2\n"
            )
            changed = original.replace("0100/1/2", "02000/1/2")
            path.write_text(original, encoding="utf-8")
            relative = path.relative_to(Path(TEMP_ROOT.name).resolve()).as_posix()

            def run():
                self.assertTrue(records_module.CERTIFICATE_IMPORT_LOCK.acquire(blocking=False))
                records_module._certificate_import_worker()
                return dict(records_module.CERTIFICATE_IMPORT_STATE)

            with patch.object(records_module, "_collect_importable_user_documents", return_value=([path], [])):
                with patch.object(records_module, "is_authorization_individual", side_effect=ValueError("error temporal")):
                    self.assertEqual(run()["errors"], 1)

                def change_during_read(_text):
                    path.write_text(changed, encoding="utf-8")
                    return True

                with patch.object(records_module, "is_authorization_individual", side_effect=change_during_read):
                    status = run()
                    self.assertEqual(status["errors"], 1)
                    self.assertIn("cambió durante la revisión", status["error_details"][0])

                connection = connect_db()
                try:
                    self.assertIsNone(connection.execute(
                        "SELECT 1 FROM extracted_records WHERE source_path = ?", (relative,)
                    ).fetchone())
                    self.assertIsNone(connection.execute(
                        "SELECT 1 FROM certificate_import_cache WHERE path = ?", (relative,)
                    ).fetchone())
                finally:
                    connection.close()

                status = run()
                self.assertEqual(status["errors"], 0)
                self.assertEqual(status["imported"], 1)
                connection = connect_db()
                try:
                    row = connection.execute(
                        "SELECT authorization_number FROM extracted_records WHERE source_path = ?",
                        (relative,),
                    ).fetchone()
                    self.assertEqual(row["authorization_number"], "02000/1/2")
                finally:
                    connection.close()

                path.write_text("Documento sin autorización", encoding="utf-8")
                self.assertEqual(run()["unchanged"], 0)
                self.assertEqual(run()["unchanged"], 1)
                path.write_text(original, encoding="utf-8")
                status = run()
                self.assertEqual(status["unchanged"], 0)
                self.assertEqual(status["updated"], 1)
                with patch.object(records_module, "CERTIFICATE_PARSER_VERSION", 2):
                    self.assertEqual(run()["updated"], 1)


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_license_data_survives_technical_table_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy_database = Path(temp) / "legacy.sqlite3"
            with closing(sqlite3.connect(legacy_database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE entity_licenses (source_path TEXT PRIMARY KEY, "
                    "created_at TEXT NOT NULL, license_number TEXT NOT NULL, "
                    "representative TEXT NOT NULL DEFAULT '', "
                    "authorization_number TEXT NOT NULL DEFAULT '', "
                    "issue_date TEXT NOT NULL DEFAULT '', expiry_date TEXT NOT NULL DEFAULT '')"
                )
                connection.execute(
                    "INSERT INTO entity_licenses(source_path, created_at, license_number, representative) "
                    "VALUES (?, ?, ?, ?)",
                    ("ejemplo.pdf", "2025-01-01", "LO-X-1234-5678-AB", "Persona de prueba"),
                )
            with patch.object(database, "DATABASE", legacy_database):
                initialize_db()
                connection = connect_db()
                try:
                    row = connection.execute(
                        "SELECT license_number, representative, technical_tables FROM entity_licenses"
                    ).fetchone()
                    self.assertEqual(tuple(row), ("LO-X-1234-5678-AB", "Persona de prueba", None))
                finally:
                    connection.close()

    def test_existing_pdf_index_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy_database = Path(temp) / "legacy.sqlite3"
            connection = sqlite3.connect(legacy_database)
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE pdf_fts USING fts5(path UNINDEXED, content)"
                )
                connection.execute(
                    "INSERT INTO pdf_fts(path, content) VALUES (?, ?)",
                    ("example.pdf", "existing index content"),
                )
                connection.commit()
            finally:
                connection.close()

            with patch.object(database, "DATABASE", legacy_database):
                initialize_db()
                connection = connect_db()
                try:
                    row = connection.execute(
                        "SELECT path, content FROM pdf_fts"
                    ).fetchone()
                    self.assertEqual(tuple(row), ("example.pdf", "existing index content"))
                finally:
                    connection.close()

    def test_adds_certificate_columns_to_existing_database(self):
        legacy_database = Path(TEMP_ROOT.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute(
                "CREATE TABLE extracted_records ("
                "id INTEGER PRIMARY KEY, source_path TEXT NOT NULL UNIQUE, "
                "created_at TEXT NOT NULL, first_name TEXT NOT NULL DEFAULT '', "
                "last_name TEXT NOT NULL DEFAULT '', identity_number TEXT NOT NULL DEFAULT '', "
                "authorization_number TEXT NOT NULL DEFAULT '', equipment TEXT NOT NULL DEFAULT '', "
                "needs_review INTEGER NOT NULL DEFAULT 1)"
            )
            connection.commit()
        finally:
            connection.close()

        previous_database = database.DATABASE
        database.DATABASE = legacy_database
        try:
            initialize_db()
            connection = connect_db()
            try:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(extracted_records)")
                }
            finally:
                connection.close()
        finally:
            database.DATABASE = previous_database
            legacy_database.unlink(missing_ok=True)

        self.assertTrue({
            "practice",
            "issue_date",
            "expiry_date",
            "is_authorization_certificate",
        }.issubset(columns))


if __name__ == "__main__":
    unittest.main()
