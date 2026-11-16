import shutil
from io import BytesIO
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
import pymupdf

TEMP_ROOT = tempfile.TemporaryDirectory()
os.environ["DOCUMENT_ROOT"] = TEMP_ROOT.name
os.environ["APP_DATA_DIR"] = str(Path(TEMP_ROOT.name) / "appdata")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from database import connect_db, initialize_db
from documents import fts_expression, safe_relative_path, user_folder_from_path
from extraction import (
    extract_candidate_fields,
    extract_certificate_fields,
    is_authorization_individual,
    is_authorization_certificate,
)
import routes.records as records_module
from routes.records import RecordRoutes
from routes.documents import DocumentRoutes
from routes.users import UserRoutes
from workbook_service import cell_input_value


class ExtractionTests(unittest.TestCase):
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

    def test_pdf_search_splits_punctuation_in_numbers(self):
        self.assertEqual(fts_expression("AB12/34/5"), '"AB12" AND "34" AND "5"')

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

    def test_cell_edit_preserves_number_type_and_rejects_formulas(self):
        self.assertEqual(cell_input_value(12, "13"), 13)
        with self.assertRaises(ValueError):
            cell_input_value(None, "=1+1")


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

    def test_user_list_includes_authorization_filter_data(self):
        source_path = (
            "USUARIOprueba/2025/"
            "Certificado_Autorizacion_Individual_ARNR.pdf"
        )
        (self.user_folder / "2025" / Path(source_path).name).write_bytes(b"test")
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

    def test_update_keeps_content_detected_authorization_review_fields(self):
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE extracted_records SET is_authorization_certificate = 1, "
                "last_name = '', "
                "practice = 'RADIOLOGÍA', issue_date = '01/01/2026', "
                "expiry_date = '01/01/2028', equipment = 'Equipo: TAC' "
                "WHERE source_path = 'USUARIOprueba/2025/autorizacion.pdf'"
            )
            record_id = connection.execute(
                "SELECT id FROM extracted_records "
                "WHERE source_path = 'USUARIOprueba/2025/autorizacion.pdf'"
            ).fetchone()["id"]
            connection.commit()
        finally:
            connection.close()

        response = {}
        handler = object.__new__(RecordRoutes)
        handler.send_json = lambda payload, status=200: response.update(payload)
        RecordRoutes.update_record(
            handler,
            {"id": record_id, "fields": {"practice": "RADIOLOGÍA INTERVENCIONISTA"}},
        )

        self.assertFalse(response["record"]["needs_review"])

    def test_user_list_marks_unimported_authorization_certificate(self):
        certificate = (
            self.user_folder / "2025" /
            "Certificado_Autorizacion_Individual_ARNR.pdf"
        )
        certificate.write_bytes(b"test")

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
        self.assertEqual(response["files"][1]["path"], "USUARIOprueba/2025/nota.txt")
        self.assertEqual(pdf["record"]["first_name"], "NOMBRE")

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

    def test_txt_document_can_be_imported_from_user_ficha(self):
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

        self.assertEqual(response["record"]["source_path"], "USUARIOprueba/2025/nota.txt")
        self.assertFalse(response["already_extracted"])

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
        RecordRoutes.update_record(
            handler,
            {
                "id": record["id"],
                "fields": {"practice": "Práctica corregida", "first_name": "Nombre corregido"},
            },
        )
        self.assertEqual(response["record"]["practice"], "Práctica corregida")

        response.clear()
        RecordRoutes.extract_record(
            handler,
            {"path": "USUARIOprueba/2025/Certificado_Autorizacion_Individual_ARNR.pdf"},
        )
        self.assertTrue(response["updated"])
        self.assertEqual(response["record"]["practice"], "RADIOLOGÍA INTERVENCIONISTA")
        self.assertEqual(response["record"]["first_name"], "Nombre corregido")


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


class DatabaseMigrationTests(unittest.TestCase):
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
