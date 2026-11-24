import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

_ENV_ROOT = tempfile.TemporaryDirectory(prefix="carpetaz-security-env-")
os.environ.setdefault("DOCUMENT_ROOT", _ENV_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(_ENV_ROOT.name) / "appdata"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook

import auth
import database
import server
from routes.records import RecordRoutes
from settings import AppError


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.root.name) / "test.sqlite3"
        database.initialize_db()

    def tearDown(self):
        database.DATABASE = self.original_database
        self.root.cleanup()

    def admin_hash(self):
        connection = database.connect_db()
        try:
            return connection.execute(
                "SELECT password_hash FROM workflow_users WHERE username = 'admin-prueba'"
            ).fetchone()["password_hash"]
        finally:
            connection.close()


class ExcelExportTests(DatabaseTestCase):
    def test_extracted_text_is_never_exported_as_formula(self):
        connection = database.connect_db()
        try:
            connection.execute(
                "INSERT INTO extracted_records (source_path, created_at, first_name, "
                "last_name, equipment) VALUES (?, '2026-01-01', ?, ?, ?)",
                ("USUARIO/doc.pdf", '=HYPERLINK("http://ejemplo.invalid","x")', "+1+1", "@SUM(A1)"),
            )
            connection.commit()
        finally:
            connection.close()
        handler = object.__new__(RecordRoutes)
        handler.wfile = BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        RecordRoutes.export_records(handler)
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()))
        try:
            sheet = workbook.active
            first_name = sheet.cell(row=2, column=3)
            self.assertEqual(first_name.value, '=HYPERLINK("http://ejemplo.invalid","x")')
            self.assertEqual(first_name.data_type, "s")
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    self.assertNotEqual(cell.data_type, "f")
        finally:
            workbook.close()


class LoginThrottleTests(unittest.TestCase):
    def test_locks_user_after_repeated_failures_and_success_resets(self):
        throttle = auth.LoginThrottle()
        for _ in range(auth.MAX_LOGIN_FAILURES - 1):
            throttle.record_failure("Admin", "10.0.0.1")
        self.assertEqual(throttle.retry_after("admin", "10.0.0.1"), 0)
        throttle.record_success("admin", "10.0.0.1")
        for _ in range(auth.MAX_LOGIN_FAILURES - 1):
            throttle.record_failure("admin", "10.0.0.1")
        self.assertEqual(throttle.retry_after("admin", "10.0.0.1"), 0)
        throttle.record_failure("ADMIN ", "10.0.0.1")
        self.assertGreater(throttle.retry_after("admin", "10.0.0.2"), 0)
        self.assertEqual(throttle.retry_after("otra", "10.0.0.1"), 0)

    def test_locks_address_after_many_failures_across_users(self):
        throttle = auth.LoginThrottle()
        for index in range(auth.MAX_ADDRESS_FAILURES):
            throttle.record_failure(f"usuario{index}", "10.0.0.9")
        self.assertGreater(throttle.retry_after("otra", "10.0.0.9"), 0)
        self.assertEqual(throttle.retry_after("otra", "10.0.0.8"), 0)

    def test_lock_expires(self):
        throttle = auth.LoginThrottle()
        with patch.object(auth.time, "monotonic", return_value=1000.0):
            for _ in range(auth.MAX_LOGIN_FAILURES):
                throttle.record_failure("admin", "10.0.0.1")
            self.assertGreater(throttle.retry_after("admin", "10.0.0.1"), 0)
        with patch.object(
            auth.time, "monotonic", return_value=1000.0 + auth.LOGIN_LOCK_SECONDS + 1
        ):
            self.assertEqual(throttle.retry_after("admin", "10.0.0.1"), 0)


class PasswordPolicyTests(unittest.TestCase):
    def test_new_passwords_need_twelve_characters(self):
        with self.assertRaisesRegex(ValueError, "entre 12 y 256"):
            auth.validate_new_password("x" * 11)
        self.assertEqual(auth.validate_new_password("x" * 12), "x" * 12)


class AdminBootstrapTests(DatabaseTestCase):
    def bootstrap(self, password, reset=False):
        environment = {"APP_RESET_ADMIN": "1"} if reset else {}
        with patch.object(auth, "APP_USER", "admin-prueba"), patch.object(
            auth, "APP_PASSWORD", password
        ), patch.dict(os.environ, environment):
            connection = database.connect_db()
            try:
                auth.bootstrap_admin(connection)
                connection.commit()
            finally:
                connection.close()

    def test_restart_does_not_overwrite_existing_admin_password(self):
        self.bootstrap("contrasena-original-1")
        user = auth.authenticate("admin-prueba", "contrasena-original-1")
        self.assertIsNotNone(user)
        token = auth.create_session(user["id"])
        self.bootstrap("otra-contrasena-2222")
        self.assertIsNotNone(auth.authenticate("admin-prueba", "contrasena-original-1"))
        self.assertIsNone(auth.authenticate("admin-prueba", "otra-contrasena-2222"))
        self.assertIsNotNone(auth.get_session_user(token))

    def test_reset_flag_replaces_password_and_closes_sessions(self):
        self.bootstrap("contrasena-original-1")
        user = auth.authenticate("admin-prueba", "contrasena-original-1")
        token = auth.create_session(user["id"])
        self.bootstrap("otra-contrasena-2222", reset=True)
        self.assertIsNotNone(auth.authenticate("admin-prueba", "otra-contrasena-2222"))
        self.assertIsNone(auth.authenticate("admin-prueba", "contrasena-original-1"))
        self.assertIsNone(auth.get_session_user(token))

    def test_unknown_user_still_checks_a_password_hash(self):
        with patch.object(auth, "verify_password", wraps=auth.verify_password) as verify:
            self.assertIsNone(auth.authenticate("no-existe", "cualquier-clave"))
        verify.assert_called_once()


class SessionCleanupTests(DatabaseTestCase):
    def test_creating_a_session_removes_expired_ones(self):
        connection = database.connect_db()
        try:
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
            connection.execute(
                "INSERT INTO workflow_sessions (token_hash, user_id, expires_at) "
                "VALUES ('vencida', 1, ?)", (past,),
            )
            connection.commit()
        finally:
            connection.close()
        auth.create_session(1)
        connection = database.connect_db()
        try:
            hashes = [row["token_hash"] for row in connection.execute(
                "SELECT token_hash FROM workflow_sessions"
            )]
        finally:
            connection.close()
        self.assertNotIn("vencida", hashes)
        self.assertEqual(len(hashes), 1)


class UnexpectedErrorTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(server.RequestHandler)
        handler.command = "GET"
        handler.path = "/api/preview?path=secreto.pdf"
        handler.responses = []
        handler.send_error_json = lambda message, status=400: handler.responses.append(
            (status, message)
        )
        return handler

    def test_internal_details_are_logged_not_returned(self):
        handler = self.handler()
        with self.assertLogs(level=logging.ERROR) as logs:
            handler.handle_unexpected_error(OSError(r"C:\\ruta\\interna\\secreta"))
        status, message = handler.responses[0]
        self.assertEqual(status, 500)
        self.assertNotIn("ruta", message)
        self.assertIn("registro del servidor", message)
        self.assertIn("/api/preview", logs.output[0])
        self.assertNotIn("secreto.pdf", logs.output[0])

    def test_messages_written_for_users_are_kept(self):
        handler = self.handler()
        handler.handle_unexpected_error(AppError("Abrir una carpeta requiere Windows."))
        self.assertEqual(handler.responses, [(500, "Abrir una carpeta requiere Windows.")])


if __name__ == "__main__":
    unittest.main()
