import atexit
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

_ENV_ROOT = tempfile.TemporaryDirectory(prefix="carpetaz-workflow-env-")
atexit.register(_ENV_ROOT.cleanup)
os.environ.setdefault("DOCUMENT_ROOT", _ENV_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(_ENV_ROOT.name) / "appdata"))

import auth
import database
from auth import hash_password
from database import ENTITY_WORKFLOW_NAMES, connect_db, initialize_db
from routes.workflows import WorkflowRoutes

APPLICATION_DATA = {
    "application_date": "2026-01-02",
    "form_number": "00012",
    "short_code": "C-01",
    "first_name": "Nombre",
    "last_name": "Apellido",
    "identity_number": "00123456",
    "profession": "Profesión",
    "practice": "Práctica",
    "workplace": "Lugar de trabajo",
    "email": "persona@example.test",
    "assigned_code": "00023",
    "digital_signature": False,
}
ENTITY_APPLICATION_DATA = {
    "application_date": "2026-01-02",
    "form_number": "00012",
    "company_name": "Institución de prueba",
    "practice": "Práctica de prueba",
    "code": "00123",
    "digital_signature": False,
}


class WorkflowHandler(WorkflowRoutes):
    def __init__(self, user):
        self.current_user = user
        self.response = None

    def send_json(self, payload, status=200):
        self.response = (status, payload)

    def send_error_json(self, message, status=400):
        self.response = (status, {"error": message})


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = tempfile.TemporaryDirectory()
        cls.original_database = database.DATABASE
        database.DATABASE = Path(cls.temp_root.name) / "workflows.sqlite3"
        initialize_db()

    @classmethod
    def tearDownClass(cls):
        database.DATABASE = cls.original_database
        cls.temp_root.cleanup()

    def setUp(self):
        connection = connect_db()
        try:
            for table in (
                "workflow_events",
                "workflow_case_stage_assignments",
                "workflow_case_application_data",
                "workflow_case_entity_data",
                "workflow_cases",
                "workflow_entity_types",
                "workflow_group_members",
                "workflow_groups",
                "workflow_sessions",
                "workflow_users",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute(
                "DELETE FROM workflow_stages WHERE workflow_id NOT IN "
                "(SELECT id FROM workflow_definitions WHERE name = 'Autorización Individual')"
            )
            connection.execute(
                "DELETE FROM workflow_transitions WHERE workflow_id NOT IN "
                "(SELECT id FROM workflow_definitions WHERE name = 'Autorización Individual')"
            )
            workflow_id = connection.execute(
                "SELECT id FROM workflow_definitions WHERE name = 'Autorización Individual'"
            ).fetchone()["id"]
            connection.execute(
                "DELETE FROM workflow_stages WHERE workflow_id = ?", (workflow_id,)
            )
            connection.execute(
                "DELETE FROM workflow_transitions WHERE workflow_id = ?", (workflow_id,)
            )
            connection.execute("DELETE FROM workflow_definitions")
            connection.commit()
        finally:
            connection.close()
        initialize_db()
        connection = connect_db()
        try:
            self.admin_id = connection.execute(
                "SELECT id FROM workflow_users WHERE role = 'admin' LIMIT 1"
            ).fetchone()
            if self.admin_id:
                self.admin_id = self.admin_id["id"]
            else:
                cursor = connection.execute(
                    "INSERT INTO workflow_users "
                    "(username, display_name, password_hash, role, active, created_at) "
                    "VALUES ('admin-test', 'Administración', ?, 'admin', 1, ?)",
                    (hash_password("password-admin-test"), self.now()),
                )
                self.admin_id = cursor.lastrowid
                connection.commit()
            self.workflow_id = connection.execute(
                "SELECT id FROM workflow_definitions WHERE name = 'Autorización Individual'"
            ).fetchone()["id"]
            connection.execute(
                "UPDATE workflow_stages SET default_assignee_user_id = ? "
                "WHERE workflow_id = ? AND stage_key = 'asignacion_inspector'",
                (self.admin_id, self.workflow_id),
            )
            connection.commit()
        finally:
            connection.close()
        self.admin = {
            "id": self.admin_id,
            "username": "admin-test",
            "display_name": "Administración",
            "role": "admin",
        }

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _create_case(self, actor=None, **overrides):
        connection = connect_db()
        try:
            group = connection.execute(
                "SELECT id FROM workflow_groups WHERE name = 'Inspección'"
            ).fetchone()
            if group:
                group_id = group["id"]
            else:
                group_id = connection.execute(
                    "INSERT INTO workflow_groups (name, created_at) VALUES ('Inspección', ?)",
                    (self.now(),),
                ).lastrowid
            connection.execute(
                "INSERT OR IGNORE INTO workflow_group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, self.admin_id),
            )
            connection.commit()
        finally:
            connection.close()
        handler = WorkflowHandler(actor or self.admin)
        payload = {
            "workflow_id": self.workflow_id,
            "title": "Solicitud de prueba",
            "reference": "EXP-001",
            "description": "Caso de prueba",
            "request_type": "nueva",
            "application_data": APPLICATION_DATA,
        }
        payload.update(overrides)
        handler.create_workflow_case(payload)
        self.assertEqual(handler.response[0], 201)
        return handler.response[1]["id"]

    def test_export_contains_cases_and_complete_ordered_history(self):
        case_id = self._create_case(title='=HYPERLINK("https://example.test","x")')
        legacy_id = self._create_case(title="Trámite anterior")
        handler = WorkflowHandler(self.admin)
        handler.get_workflow_data({"case_id": [str(case_id)]})
        assignment = handler.response[1]["detail"]["transitions"][0]["id"]
        handler.transition_workflow_case({
            "case_id": case_id, "transition_id": assignment, "inspector_id": self.admin_id,
            "comment": "=1+1",
        })
        handler.get_workflow_data({"case_id": [str(case_id)]})
        return_transition = next(
            item for item in handler.response[1]["detail"]["transitions"]
            if item["to_stage_key"] == "carga_datos"
        )
        handler.transition_workflow_case({
            "case_id": case_id, "transition_id": return_transition["id"], "comment": "Corregir",
        })
        connection = connect_db()
        try:
            connection.execute("DELETE FROM workflow_case_application_data WHERE case_id = ?", (legacy_id,))
            connection.commit()
            expected_events = [tuple(row) for row in connection.execute(
                "SELECT case_id, id, created_at FROM workflow_events ORDER BY case_id, id"
            )]
        finally:
            connection.close()

        headers = {}
        handler.wfile = BytesIO()
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda name, value: headers.update({name: value})
        handler.end_headers = lambda: None
        handler.export_workflows()
        self.assertIn("tramites_historial.xlsx", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()), data_only=False)
        try:
            self.assertEqual(workbook.sheetnames, ["Trámites", "Historial"])
            sheet = workbook["Trámites"]
            columns = {cell.value: cell.column for cell in sheet[1]}
            cases = {sheet.cell(row, columns["ID trámite"]).value: row
                     for row in range(2, sheet.max_row + 1)}
            self.assertEqual(set(cases), {case_id, legacy_id})
            row = cases[case_id]
            self.assertEqual(sheet.cell(row, columns["Estado"]).value, "Observado")
            self.assertEqual(sheet.cell(row, columns["Número de formulario"]).value, "00012")
            self.assertEqual(sheet.cell(row, columns["CI"]).value, "00123456")
            self.assertEqual(sheet.cell(row, columns["Firma digital"]).value, "No")
            self.assertEqual(sheet.cell(row, columns["Título"]).value, '=HYPERLINK("https://example.test","x")')
            self.assertEqual(sheet.cell(row, columns["Título"]).data_type, "s")
            self.assertIsNone(sheet.cell(cases[legacy_id], columns["Nombre"]).value)
            self.assertIsNone(sheet.cell(cases[legacy_id], columns["Firma digital"]).value)
            history = workbook["Historial"]
            event_columns = {cell.value: cell.column for cell in history[1]}
            self.assertNotIn("Duración de etapa (segundos)", event_columns)
            actual_events = [(history.cell(row, event_columns["ID trámite"]).value,
                              history.cell(row, event_columns["ID evento"]).value,
                              history.cell(row, event_columns["Fecha (UTC)"]).value)
                             for row in range(2, history.max_row + 1)]
            self.assertEqual(actual_events, expected_events)
            self.assertTrue(all(history.cell(row, event_columns["Tiempo hasta la entrega (segundos)"]).value is None
                                for row in range(2, history.max_row + 1)))
            comment_cell = next(history.cell(row, event_columns["Comentario"])
                                for row in range(2, history.max_row + 1)
                                if history.cell(row, event_columns["Comentario"]).value == "=1+1")
            self.assertEqual(comment_cell.data_type, "s")
            self.assertEqual(history.cell(comment_cell.row, event_columns["Funcionario"]).value,
                             self.admin["display_name"])
            self.assertEqual(history.cell(comment_cell.row, event_columns["Etapa destino"]).value,
                             "Evaluación del inspector")
        finally:
            workbook.close()

    def test_case_list_includes_fields_for_search(self):
        case_id = self._create_case()
        handler = WorkflowHandler(self.admin)
        handler.get_workflow_data({})
        self.assertTrue(handler.response[1]["supports_workflow_case_search"])
        case = next(item for item in handler.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual(case["identity_number"], "00123456")
        self.assertEqual(case["form_number"], "00012")
        self.assertEqual(case["short_code"], "C-01")
        self.assertEqual(case["assigned_code"], "00023")

    def test_deadline_history_pauses_and_restarts_only_after_observation(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        at = lambda days: (base + timedelta(days=days)).isoformat(timespec="seconds")
        with patch("routes.workflows._now", return_value=at(0)):
            case_id = self._create_case()
        handler = WorkflowHandler(self.admin)
        connection = connect_db()
        try:
            transitions = {
                (row["source"], row["target"]): row["id"]
                for row in connection.execute(
                    "SELECT transitions.id, source.stage_key AS source, target.stage_key AS target "
                    "FROM workflow_transitions AS transitions "
                    "JOIN workflow_stages AS source ON source.id = transitions.from_stage_id "
                    "JOIN workflow_stages AS target ON target.id = transitions.to_stage_id "
                    "WHERE transitions.workflow_id = ?", (self.workflow_id,),
                )
            }
        finally:
            connection.close()

        def transition(source, target, day, **extra):
            with patch("routes.workflows._now", return_value=at(day)):
                handler.transition_workflow_case({
                    "case_id": case_id, "transition_id": transitions[(source, target)], **extra,
                })
            self.assertEqual(handler.response, (200, {"saved": True}))

        def deadline():
            handler.get_workflow_data({})
            self.assertTrue(handler.response[1]["supports_observation_deadline"])
            case = next(item for item in handler.response[1]["cases"] if item["id"] == case_id)
            return case["deadline_started_at"], case["deadline_paused_at"]

        transition("asignacion_inspector", "evaluacion_inspector", 1, inspector_id=self.admin_id)
        self.assertEqual(deadline(), (at(0), None))
        transition("evaluacion_inspector", "carga_datos", 10, comment="Falta documentación")
        self.assertEqual(deadline(), (at(0), at(10)))
        transition("carga_datos", "evaluacion_inspector", 15, inspector_id=self.admin_id)
        self.assertEqual(deadline(), (at(15), None))
        transition("evaluacion_inspector", "jefe_licenciamiento", 16)
        transition("jefe_licenciamiento", "evaluacion_inspector", 17)
        self.assertEqual(deadline(), (at(15), None))
        transition("evaluacion_inspector", "carga_datos", 20, comment="Falta firma")
        self.assertEqual(deadline(), (at(15), at(20)))
        transition("carga_datos", "evaluacion_inspector", 25, inspector_id=self.admin_id)
        transition("evaluacion_inspector", "rechazado", 28, comment="Motivo")
        self.assertEqual(deadline(), (at(25), None))
        handler.get_workflow_data({"case_id": [str(case_id)]})
        case = next(item for item in handler.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual((case["status"], case["completed_at"]), ("completed", at(28)))

    def test_deadline_history_supports_entity_and_legacy_observation_via_chief(self):
        base = datetime(2026, 2, 1, tzinfo=timezone.utc)
        at = lambda days: (base + timedelta(days=days)).isoformat(timespec="seconds")
        with patch("routes.workflows._now", return_value=at(0)):
            individual_id = self._create_case()
        handler = WorkflowHandler(self.admin)
        connection = connect_db()
        try:
            entity_id = connection.execute(
                "SELECT workflow_id FROM workflow_entity_types ORDER BY workflow_id LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        with patch("routes.workflows._now", return_value=at(0)):
            handler.create_workflow_case({
                "workflow_id": entity_id, "request_type": "nueva",
                "application_data": ENTITY_APPLICATION_DATA, "inspector_id": self.admin_id,
            })
        entity_case = handler.response[1]["id"]

        connection = connect_db()
        try:
            for case_id in (individual_id, entity_case):
                workflow_id = connection.execute(
                    "SELECT workflow_id FROM workflow_cases WHERE id = ?", (case_id,),
                ).fetchone()[0]
                stages = {row["stage_key"]: row["id"] for row in connection.execute(
                    "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?", (workflow_id,),
                )}
                chief = "asignacion_inspector" if "asignacion_inspector" in stages else "jefe_licenciamiento"
                for source, target, day in (
                    ("evaluacion_inspector", "carga_datos", 3),
                    ("carga_datos", chief, 4),
                ):
                    connection.execute(
                        "INSERT INTO workflow_events "
                        "(case_id, actor_id, event_type, from_stage_id, to_stage_id, created_at) "
                        "VALUES (?, ?, 'transitioned', ?, ?, ?)",
                        (case_id, self.admin_id, stages[source], stages[target], at(day)),
                    )
            connection.commit()
        finally:
            connection.close()
        handler.get_workflow_data({})
        cases = {item["id"]: item for item in handler.response[1]["cases"]}
        for case_id in (individual_id, entity_case):
            self.assertEqual((cases[case_id]["deadline_started_at"], cases[case_id]["deadline_paused_at"]),
                             (at(0), at(3)))

        connection = connect_db()
        try:
            for case_id in (individual_id, entity_case):
                workflow_id = connection.execute(
                    "SELECT workflow_id FROM workflow_cases WHERE id = ?", (case_id,),
                ).fetchone()[0]
                stages = {row["stage_key"]: row["id"] for row in connection.execute(
                    "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?", (workflow_id,),
                )}
                chief = "asignacion_inspector" if "asignacion_inspector" in stages else "jefe_licenciamiento"
                connection.execute(
                    "INSERT INTO workflow_events "
                    "(case_id, actor_id, event_type, from_stage_id, to_stage_id, created_at) "
                    "VALUES (?, ?, 'transitioned', ?, ?, ?)",
                    (case_id, self.admin_id, stages[chief], stages["evaluacion_inspector"], at(6)),
                )
            connection.commit()
        finally:
            connection.close()
        handler.get_workflow_data({})
        cases = {item["id"]: item for item in handler.response[1]["cases"]}
        for case_id in (individual_id, entity_case):
            self.assertEqual((cases[case_id]["deadline_started_at"], cases[case_id]["deadline_paused_at"]),
                             (at(6), None))

    def test_export_contains_every_application_field_for_all_workflow_types(self):
        individual_fields = {**APPLICATION_DATA, "profession": "=2+2", "digital_signature": True}
        individual_id = self._create_case(
            application_data=individual_fields, description="Descripción individual",
        )
        handler = WorkflowHandler(self.admin)
        handler.create_workflow_user({
            "username": "export-inspector", "display_name": "Inspector de exportación",
            "password": "password-test-role", "role": "inspeccion",
        })
        inspector_id = handler.response[1]["id"]
        handler.get_workflow_data({})
        workflows = [item for item in handler.response[1]["workflows"] if item["is_entity_workflow"]]
        self.assertEqual({item["name"] for item in workflows}, set(ENTITY_WORKFLOW_NAMES))
        entity_ids = []
        with patch("routes.workflows._entity_areas", return_value=([{"institution": "Entidad vinculada"}], 0)):
            for workflow in workflows:
                handler.create_workflow_case({
                    "workflow_id": workflow["id"], "request_type": "renovacion",
                    "application_data": ENTITY_APPLICATION_DATA, "inspector_id": inspector_id,
                    "linked_entity_name": "Entidad vinculada", "description": "Descripción entidad",
                })
                self.assertEqual(handler.response[0], 201)
                entity_ids.append(handler.response[1]["id"])

        handler.wfile = BytesIO()
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.export_workflows()
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()), data_only=False)
        try:
            sheet = workbook["Trámites"]
            columns = {cell.value: cell.column for cell in sheet[1]}
            cases = {sheet.cell(row, columns["ID trámite"]).value: row
                     for row in range(2, sheet.max_row + 1)}
            individual_columns = {
                "application_date": "Fecha de solicitud", "form_number": "Número de formulario",
                "short_code": "Código corto", "first_name": "Nombre", "last_name": "Apellido",
                "identity_number": "CI", "profession": "Profesión", "practice": "Práctica",
                "workplace": "Lugar de trabajo", "email": "Correo", "assigned_code": "Código asignado",
            }
            row = cases[individual_id]
            for key, heading in individual_columns.items():
                with self.subTest(workflow="individual", field=key):
                    self.assertEqual(sheet.cell(row, columns[heading]).value, individual_fields[key])
            self.assertEqual(sheet.cell(row, columns["Profesión"]).data_type, "s")
            self.assertEqual(sheet.cell(row, columns["Firma digital"]).value, "Sí")
            self.assertEqual(sheet.cell(row, columns["Descripción"]).value, "Descripción individual")
            self.assertIsNone(sheet.cell(row, columns["Empresa/institución"]).value)

            entity_columns = {
                "application_date": "Fecha de solicitud", "form_number": "Número de formulario",
                "company_name": "Empresa/institución", "practice": "Práctica", "code": "Código de entidad",
            }
            for case_id in entity_ids:
                row = cases[case_id]
                for key, heading in entity_columns.items():
                    with self.subTest(case_id=case_id, field=key):
                        self.assertEqual(sheet.cell(row, columns[heading]).value, ENTITY_APPLICATION_DATA[key])
                self.assertEqual(sheet.cell(row, columns["Firma digital"]).value, "No")
                self.assertEqual(sheet.cell(row, columns["Carpeta de entidad"]).value, "Entidad vinculada")
                self.assertEqual(sheet.cell(row, columns["Inspector"]).value, "Inspector de exportación")
                self.assertEqual(sheet.cell(row, columns["Solicitud"]).value, "renovacion")
                self.assertEqual(sheet.cell(row, columns["Descripción"]).value, "Descripción entidad")
                self.assertIsNone(sheet.cell(row, columns["CI"]).value)
        finally:
            workbook.close()

    def test_export_without_cases_has_both_headers(self):
        handler = WorkflowHandler(self.admin)
        handler.wfile = BytesIO()
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.export_workflows()
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()), read_only=True)
        try:
            self.assertEqual(workbook.sheetnames, ["Trámites", "Historial"])
            self.assertEqual(workbook["Trámites"].max_row, 1)
            self.assertEqual(workbook["Historial"].max_row, 1)
        finally:
            workbook.close()

    def test_export_uses_one_snapshot_for_cases_and_history(self):
        case_id = self._create_case()
        reader = connect_db()
        inserted = []

        class ConcurrentWriter:
            def execute(inner_self, sql, *args):
                if sql.startswith("SELECT events.*"):
                    writer = connect_db()
                    try:
                        event = writer.execute(
                            "INSERT INTO workflow_events (case_id, actor_id, event_type, created_at) "
                            "VALUES (?, ?, 'assigned', ?)",
                            (case_id, self.admin_id, self.now()),
                        )
                        inserted.append(event.lastrowid)
                        writer.commit()
                    finally:
                        writer.close()
                return reader.execute(sql, *args)

            def close(inner_self):
                reader.close()

        handler = WorkflowHandler(self.admin)
        handler.wfile = BytesIO()
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        with patch("routes.workflows.connect_db", return_value=ConcurrentWriter()):
            handler.export_workflows()
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()), read_only=True)
        try:
            history = workbook["Historial"]
            ids = [row[1] for row in history.iter_rows(min_row=2, values_only=True)]
            self.assertEqual(len(inserted), 1)
            self.assertNotIn(inserted[0], ids)
        finally:
            workbook.close()

    def test_new_authorization_is_sent_to_chief_with_history(self):
        admin = WorkflowHandler(self.admin)
        admin.create_workflow_user({
            "username": "chief", "display_name": "Jefe",
            "password": "password-test-role", "role": "jefe_licenciamiento",
        })
        chief = admin.response[1]
        admin.create_workflow_user({
            "username": "secretary", "display_name": "Secretaría",
            "password": "password-test-role", "role": "administracion",
        })
        secretary = admin.response[1]
        connection = connect_db()
        try:
            stage_id = connection.execute(
                "SELECT id FROM workflow_stages WHERE workflow_id = ? AND stage_key = 'asignacion_inspector'",
                (self.workflow_id,),
            ).fetchone()["id"]
        finally:
            connection.close()
        admin.set_stage_assignment({"stage_id": stage_id, "user_id": chief["id"]})
        handler = WorkflowHandler(secretary)
        handler.create_workflow_case({
            "workflow_id": self.workflow_id, "title": "Solicitud", "request_type": "nueva",
            "application_data": APPLICATION_DATA,
        })
        self.assertEqual(handler.response[0], 201)
        case_id = handler.response[1]["id"]
        handler.get_workflow_data({"case_id": [str(case_id)]})
        case = next(row for row in handler.response[1]["cases"] if row["id"] == case_id)
        events = handler.response[1]["detail"]["events"]
        self.assertEqual((case["stage_key"], case["assigned_user_id"]), ("asignacion_inspector", chief["id"]))
        self.assertIsNone(case["inspector_id"])
        self.assertEqual([event["event_type"] for event in events], ["created", "transitioned"])
        self.assertEqual(events[1]["actor_name"], "Secretaría")
        self.assertEqual(events[1]["from_stage_name"], "Carga de datos")
        self.assertEqual(events[1]["to_user_name"], "Jefe")

    def test_assignment_comment_is_optional_and_recorded_when_provided(self):
        for comment, expected in ((None, ""), ("  ", ""), ("Revisar datos", "Revisar datos")):
            with self.subTest(comment=comment):
                case_id = self._create_case()
                handler = WorkflowHandler(self.admin)
                handler.get_workflow_data({"case_id": [str(case_id)]})
                transition_id = handler.response[1]["detail"]["transitions"][0]["id"]
                payload = {
                    "case_id": case_id, "transition_id": transition_id, "inspector_id": self.admin_id,
                }
                if comment is not None:
                    payload["comment"] = comment
                handler.transition_workflow_case(payload)
                self.assertEqual(handler.response, (200, {"saved": True}))
                handler.get_workflow_data({"case_id": [str(case_id)]})
                case = next(item for item in handler.response[1]["cases"] if item["id"] == case_id)
                self.assertEqual((case["stage_key"], case["assigned_user_id"]),
                                 ("evaluacion_inspector", self.admin_id))
                self.assertEqual(handler.response[1]["detail"]["events"][-1]["comment"], expected)

    def test_delivery_requires_and_records_valid_method(self):
        labels = {"presencial": "Presencial", "apia": "APIA", "mail": "Mail"}
        handler = WorkflowHandler(self.admin)
        for method, label in labels.items():
            with self.subTest(method=method):
                case_id = self._create_case()
                connection = connect_db()
                try:
                    connection.execute(
                        "UPDATE workflow_cases SET status = 'completed', current_stage_id = "
                        "(SELECT id FROM workflow_stages WHERE workflow_id = ? AND stage_key = 'finalizado'), "
                        "completed_at = ? WHERE id = ?",
                        (self.workflow_id, self.now(), case_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
                for invalid in (None, "", "otro", []):
                    with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "modalidad"):
                        handler.deliver_workflow_case({"case_id": case_id, "delivery_method": invalid})
                handler.deliver_workflow_case({"case_id": case_id, "delivery_method": method})
                self.assertEqual(handler.response, (200, {"saved": True}))
                handler.get_workflow_data({"case_id": [str(case_id)]})
                case = next(item for item in handler.response[1]["cases"] if item["id"] == case_id)
                self.assertEqual(case["delivery_method"], method)
                event = handler.response[1]["detail"]["events"][-1]
                self.assertEqual((event["event_type"], event["comment"]), ("delivered", f"Modalidad: {label}"))

    def test_new_authorization_requires_active_chief_assignment(self):
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE workflow_stages SET default_assignee_user_id = NULL "
                "WHERE workflow_id = ? AND stage_key = 'asignacion_inspector'",
                (self.workflow_id,),
            )
            connection.commit()
        finally:
            connection.close()
        handler = WorkflowHandler(self.admin)
        with self.assertRaisesRegex(ValueError, "responsable"):
            handler.create_workflow_case({
                "workflow_id": self.workflow_id, "title": "Solicitud", "request_type": "nueva",
                "application_data": APPLICATION_DATA,
            })
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE workflow_stages SET default_assignee_user_id = ? "
                "WHERE workflow_id = ? AND stage_key = 'asignacion_inspector'",
                (self.admin_id, self.workflow_id),
            )
            connection.execute("UPDATE workflow_users SET active = 0 WHERE id = ?", (self.admin_id,))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "inactiva"):
            handler.create_workflow_case({
                "workflow_id": self.workflow_id, "title": "Solicitud", "request_type": "nueva",
                "application_data": APPLICATION_DATA,
            })
        connection = connect_db()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_cases").fetchone()[0], 0)
        finally:
            connection.close()

    def test_new_authorization_requires_route_to_inspection(self):
        connection = connect_db()
        try:
            connection.execute(
                "DELETE FROM workflow_transitions WHERE workflow_id = ? "
                "AND from_stage_id = (SELECT id FROM workflow_stages "
                "WHERE workflow_id = ? AND stage_key = 'asignacion_inspector') "
                "AND to_stage_id = (SELECT id FROM workflow_stages "
                "WHERE workflow_id = ? AND stage_key = 'evaluacion_inspector')",
                (self.workflow_id, self.workflow_id, self.workflow_id),
            )
            connection.commit()
        finally:
            connection.close()
        handler = WorkflowHandler(self.admin)
        with self.assertRaisesRegex(ValueError, "envío a inspección"):
            handler.create_workflow_case({
                "workflow_id": self.workflow_id, "title": "Solicitud", "request_type": "nueva",
                "application_data": APPLICATION_DATA,
            })
        connection = connect_db()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_cases").fetchone()[0], 0)
        finally:
            connection.close()

    def test_seeded_authorization_workflow_has_review_and_return_routes(self):
        connection = connect_db()
        try:
            stages = {
                row["stage_key"]: row["id"]
                for row in connection.execute(
                    "SELECT stage_key, id FROM workflow_stages WHERE workflow_id = ?",
                    (self.workflow_id,),
                )
            }
            routes = {
                (row["from_stage_id"], row["to_stage_id"]): row["label"]
                for row in connection.execute(
                    "SELECT from_stage_id, to_stage_id, label "
                    "FROM workflow_transitions WHERE workflow_id = ?",
                    (self.workflow_id,),
                )
            }
        finally:
            connection.close()
        self.assertIn((stages["carga_datos"], stages["asignacion_inspector"]), routes)
        self.assertIn((stages["asignacion_inspector"], stages["evaluacion_inspector"]), routes)
        self.assertIn((stages["carga_datos"], stages["evaluacion_inspector"]), routes)
        self.assertIn((stages["evaluacion_inspector"], stages["carga_datos"]), routes)
        self.assertIn((stages["evaluacion_inspector"], stages["jefe_licenciamiento"]), routes)
        self.assertIn((stages["evaluacion_inspector"], stages["rechazado"]), routes)
        self.assertIn((stages["jefe_licenciamiento"], stages["evaluacion_inspector"]), routes)
        self.assertNotIn((stages["abogado"], stages["evaluacion_inspector"]), routes)
        self.assertIn((stages["abogado"], stages["jefe_licenciamiento"]), routes)
        self.assertIn((stages["director"], stages["evaluacion_inspector"]), routes)
        self.assertIn((stages["director"], stages["jefe_licenciamiento"]), routes)
        self.assertEqual(len(routes), 13)

    def test_inspection_rejection_requires_reason_and_closes_case(self):
        admin = WorkflowHandler(self.admin)
        for username in ("inspector", "other-inspector"):
            admin.create_workflow_user({
                "username": username, "display_name": username,
                "password": "password-test-role", "role": "inspeccion",
            })
            if username == "inspector":
                inspector_user = admin.response[1]
            else:
                other_user = admin.response[1]
        case_id = self._create_case()
        admin.get_workflow_data({"case_id": [str(case_id)]})
        assignment = next(item for item in admin.response[1]["detail"]["transitions"]
                          if item["to_stage_key"] == "evaluacion_inspector")
        admin.transition_workflow_case({
            "case_id": case_id, "transition_id": assignment["id"], "inspector_id": inspector_user["id"],
        })
        inspector = WorkflowHandler(inspector_user)
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        transitions = inspector.response[1]["detail"]["transitions"]
        self.assertEqual({item["to_stage_key"] for item in transitions},
                         {"carga_datos", "jefe_licenciamiento", "rechazado"})
        reject = next(item["id"] for item in transitions if item["to_stage_key"] == "rechazado")
        other = WorkflowHandler(other_user)
        other.transition_workflow_case({"case_id": case_id, "transition_id": reject, "comment": "No corresponde"})
        self.assertEqual(other.response[0], 403)
        for comment in ("", "  \n"):
            with self.subTest(comment=comment), self.assertRaisesRegex(ValueError, "motivo"):
                inspector.transition_workflow_case({"case_id": case_id, "transition_id": reject, "comment": comment})
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        case = next(item for item in inspector.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual((case["status"], case["stage_key"]), ("active", "evaluacion_inspector"))
        inspector.transition_workflow_case({
            "case_id": case_id, "transition_id": reject, "comment": "  Documentación insuficiente  ",
        })
        self.assertEqual(inspector.response, (200, {"saved": True}))
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        case = next(item for item in inspector.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual((case["status"], case["stage_key"]), ("completed", "rechazado"))
        self.assertIsNotNone(case["completed_at"])
        self.assertIsNone(case["assigned_user_id"])
        self.assertIsNone(case["delivered_at"])
        self.assertEqual(case["inspector_id"], inspector_user["id"])
        event = inspector.response[1]["detail"]["events"][-1]
        self.assertEqual((event["actor_name"], event["to_stage_name"], event["comment"]),
                         ("inspector", "Rechazado", "Documentación insuficiente"))
        self.assertEqual(inspector.response[1]["detail"]["transitions"], [])
        with self.assertRaisesRegex(ValueError, "finalizado"):
            inspector.transition_workflow_case({"case_id": case_id, "transition_id": reject, "comment": "Motivo"})
        with self.assertRaisesRegex(ValueError, "finalizadas"):
            admin.deliver_workflow_case({"case_id": case_id, "delivery_method": "presencial"})
        admin.wfile = BytesIO()
        admin.send_response = lambda status: self.assertEqual(status, 200)
        admin.send_header = lambda name, value: None
        admin.end_headers = lambda: None
        admin.export_workflows()
        workbook = load_workbook(BytesIO(admin.wfile.getvalue()), read_only=True)
        try:
            rows = list(workbook["Trámites"].values)
            self.assertEqual(rows[1][rows[0].index("Estado")], "Rechazado")
        finally:
            workbook.close()

    def test_entity_workflows_send_directly_to_assigned_inspector(self):
        admin = WorkflowHandler(self.admin)
        admin.create_workflow_user({
            "username": "entity-inspector", "display_name": "Inspector de prueba",
            "password": "password-test-role", "role": "inspeccion",
        })
        inspector = admin.response[1]
        admin.create_workflow_user({
            "username": "entity-secretary", "display_name": "Secretaría de prueba",
            "password": "password-test-role", "role": "administracion",
        })
        secretary = WorkflowHandler(admin.response[1])
        admin.get_workflow_data({})
        workflows = {item["name"]: item for item in admin.response[1]["workflows"]
                     if item["is_entity_workflow"]}
        self.assertEqual(set(workflows), set(ENTITY_WORKFLOW_NAMES))
        for name, workflow in workflows.items():
            with self.subTest(workflow=name):
                payload = {
                    "workflow_id": workflow["id"], "request_type": "nueva",
                    "application_data": ENTITY_APPLICATION_DATA, "inspector_id": inspector["id"],
                }
                secretary.create_workflow_case(payload)
                self.assertEqual(secretary.response[0], 201)
                self.assertTrue(secretary.response[1]["sent_to_inspection"])
                case_id = secretary.response[1]["id"]
                secretary.get_workflow_data({"case_id": [str(case_id)]})
                case = next(item for item in secretary.response[1]["cases"] if item["id"] == case_id)
                detail = secretary.response[1]["detail"]
                self.assertEqual((case["stage_key"], case["assigned_user_id"], case["inspector_id"]),
                                 ("evaluacion_inspector", inspector["id"], inspector["id"]))
                self.assertEqual(case["applicant_company_name"], "Institución de prueba")
                self.assertEqual(case["required_role"], "inspeccion")
                self.assertEqual(detail["entity_application_data"]["code"], "00123")
                self.assertEqual([event["event_type"] for event in detail["events"]],
                                 ["created", "transitioned"])
                self.assertEqual(detail["events"][-1]["to_user_name"], "Inspector de prueba")
                self.assertEqual({transition["to_stage_key"] for transition in detail["transitions"]},
                                 {"carga_datos", "jefe_licenciamiento", "rechazado"})

    def test_entity_workflow_migration_copies_defaults_once_without_changing_cases(self):
        case_id = self._create_case()
        connection = connect_db()
        try:
            ids = [row[0] for row in connection.execute("SELECT workflow_id FROM workflow_entity_types")]
            for workflow_id in ids:
                connection.execute("DELETE FROM workflow_transitions WHERE workflow_id = ?", (workflow_id,))
                connection.execute("DELETE FROM workflow_stages WHERE workflow_id = ?", (workflow_id,))
                connection.execute("DELETE FROM workflow_entity_types WHERE workflow_id = ?", (workflow_id,))
                connection.execute("DELETE FROM workflow_definitions WHERE id = ?", (workflow_id,))
            connection.execute(
                "UPDATE workflow_stages SET default_assignee_user_id = ? "
                "WHERE workflow_id = ? AND stage_key IN ('jefe_licenciamiento', 'abogado', 'director')",
                (self.admin_id, self.workflow_id),
            )
            before = tuple(connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,),
            ).fetchone())
            before_events = [tuple(row) for row in connection.execute(
                "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY id", (case_id,),
            )]
            connection.commit()
        finally:
            connection.close()
        initialize_db()
        initialize_db()
        connection = connect_db()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_entity_types").fetchone()[0], 4)
            self.assertEqual(tuple(connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,),
            ).fetchone()), before)
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY id", (case_id,),
            )], before_events)
            rows = connection.execute(
                "SELECT stages.default_assignee_user_id FROM workflow_stages AS stages "
                "JOIN workflow_entity_types AS entity ON entity.workflow_id = stages.workflow_id "
                "WHERE stages.stage_key IN ('jefe_licenciamiento', 'abogado', 'director')"
            ).fetchall()
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(row[0] == self.admin_id for row in rows))
        finally:
            connection.close()

    def test_entity_workflow_validation_and_entity_link(self):
        admin = WorkflowHandler(self.admin)
        admin.create_workflow_user({
            "username": "entity-inspector", "display_name": "Inspector de prueba",
            "password": "password-test-role", "role": "inspeccion",
        })
        inspector_id = admin.response[1]["id"]
        connection = connect_db()
        try:
            workflow_id = connection.execute(
                "SELECT workflow_id FROM workflow_entity_types ORDER BY workflow_id LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        payload = {
            "workflow_id": workflow_id, "request_type": "nueva",
            "application_data": ENTITY_APPLICATION_DATA, "inspector_id": inspector_id,
        }
        for key in ("application_date", "form_number", "company_name", "practice", "code", "digital_signature"):
            invalid = dict(ENTITY_APPLICATION_DATA)
            invalid.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                admin.create_workflow_case({**payload, "application_data": invalid})
        for value in (None, 999999, self.admin_id):
            with self.subTest(inspector=value), self.assertRaises(ValueError):
                admin.create_workflow_case({**payload, "inspector_id": value})
        for kind in ("modificacion", "renovacion"):
            with self.subTest(request_type=kind), self.assertRaisesRegex(ValueError, "entidad"):
                admin.create_workflow_case({**payload, "request_type": kind})
        with patch("routes.workflows._entity_areas", return_value=([{"institution": "Entidad prueba"}], 0)):
            with self.assertRaisesRegex(ValueError, "no existe"):
                admin.create_workflow_case({**payload, "linked_entity_name": "Otra entidad"})
            for kind in ("nueva", "modificacion", "renovacion"):
                with self.subTest(request_type=kind):
                    admin.create_workflow_case({
                        **payload, "request_type": kind, "linked_entity_name": "Entidad prueba",
                    })
                    case_id = admin.response[1]["id"]
                    admin.get_workflow_data({"case_id": [str(case_id)]})
                    self.assertEqual(admin.response[1]["detail"]["entity_application_data"]["linked_entity_name"],
                                     "Entidad prueba")

    def test_entity_workflow_observation_return_rejection_delivery_and_export(self):
        admin = WorkflowHandler(self.admin)
        users = {}
        for role, name in (("inspeccion", "inspector"), ("inspeccion", "otro"),
                           ("administracion", "secretaria")):
            admin.create_workflow_user({
                "username": f"entity-{name}", "display_name": name,
                "password": "password-test-role", "role": role,
            })
            users[name] = admin.response[1]
        connection = connect_db()
        try:
            workflow_id = connection.execute(
                "SELECT workflow_id FROM workflow_entity_types ORDER BY workflow_id LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        secretary = WorkflowHandler(users["secretaria"])
        secretary.create_workflow_case({
            "workflow_id": workflow_id, "request_type": "nueva",
            "application_data": ENTITY_APPLICATION_DATA, "inspector_id": users["inspector"]["id"],
        })
        case_id = secretary.response[1]["id"]
        secretary.get_workflow_data({})
        case = next(item for item in secretary.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual(case["form_number"], "00012")
        self.assertEqual(case["entity_code"], "00123")
        self.assertEqual(case["applicant_company_name"], "Institución de prueba")

        def transition(actor, target, **extra):
            handler = WorkflowHandler(actor)
            handler.get_workflow_data({"case_id": [str(case_id)]})
            candidate = next(item for item in handler.response[1]["detail"]["transitions"]
                             if item["to_stage_key"] == target)
            handler.transition_workflow_case({"case_id": case_id, "transition_id": candidate["id"], **extra})
            self.assertEqual(handler.response[0], 200)

        inspector = WorkflowHandler(users["inspector"])
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        observe = next(item["id"] for item in inspector.response[1]["detail"]["transitions"]
                       if item["to_stage_key"] == "carga_datos")
        for empty in ("", "   "):
            with self.subTest(comment=empty), self.assertRaisesRegex(ValueError, "motivo de la observación"):
                inspector.transition_workflow_case({"case_id": case_id, "transition_id": observe, "comment": empty})
        with self.assertRaisesRegex(ValueError, "motivo de la observación"):
            inspector.transition_workflow_case({"case_id": case_id, "transition_id": observe})
        transition(users["inspector"], "carga_datos", comment="Falta información")
        secretary.get_workflow_data({"case_id": [str(case_id)]})
        case = next(item for item in secretary.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual((case["is_observed"], case["assigned_user_id"]), (1, users["secretaria"]["id"]))
        transition(users["secretaria"], "evaluacion_inspector", inspector_id=users["otro"]["id"])
        secretary.get_workflow_data({"case_id": [str(case_id)]})
        case = next(item for item in secretary.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual((case["is_observed"], case["assigned_user_id"]), (0, users["otro"]["id"]))
        transition(users["otro"], "jefe_licenciamiento")
        transition(self.admin, "abogado")
        transition(self.admin, "director")
        transition(self.admin, "finalizado")
        secretary.deliver_workflow_case({"case_id": case_id, "delivery_method": "mail"})
        self.assertEqual(secretary.response[0], 200)
        secretary.get_workflow_data({"case_id": [str(case_id)]})
        self.assertEqual(secretary.response[1]["detail"]["events"][-1]["event_type"], "delivered")
        secretary.wfile = BytesIO()
        secretary.send_response = lambda status: self.assertEqual(status, 200)
        secretary.send_header = lambda name, value: None
        secretary.end_headers = lambda: None
        secretary.export_workflows()
        workbook = load_workbook(BytesIO(secretary.wfile.getvalue()), read_only=True)
        try:
            rows = list(workbook["Trámites"].values)
            record = next(row for row in rows[1:] if row[0] == case_id)
            self.assertEqual(record[rows[0].index("Empresa/institución")], "Institución de prueba")
            self.assertEqual(record[rows[0].index("Código de entidad")], "00123")
            self.assertEqual(record[rows[0].index("Estado")], "Entregado")
            history = list(workbook["Historial"].values)
            duration_column = history[0].index("Tiempo hasta la entrega (segundos)")
            action_column = history[0].index("Código de acción")
            delivery = next(row for row in history[1:] if row[action_column] == "delivered")
            self.assertGreaterEqual(delivery[duration_column], 0)
        finally:
            workbook.close()

        secretary.create_workflow_case({
            "workflow_id": workflow_id, "request_type": "nueva",
            "application_data": ENTITY_APPLICATION_DATA, "inspector_id": users["inspector"]["id"],
        })
        case_id = secretary.response[1]["id"]
        inspector = WorkflowHandler(users["inspector"])
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        reject = next(item["id"] for item in inspector.response[1]["detail"]["transitions"]
                      if item["to_stage_key"] == "rechazado")
        with self.assertRaisesRegex(ValueError, "motivo"):
            inspector.transition_workflow_case({"case_id": case_id, "transition_id": reject, "comment": " "})
        inspector.transition_workflow_case({
            "case_id": case_id, "transition_id": reject, "comment": "Motivo de prueba",
        })
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        rejected = next(item for item in inspector.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual((rejected["status"], rejected["stage_key"]), ("completed", "rechazado"))
        with self.assertRaisesRegex(ValueError, "finalizadas"):
            secretary.deliver_workflow_case({"case_id": case_id, "delivery_method": "mail"})

    def test_rejection_migration_preserves_existing_cases_and_history(self):
        case_id = self._create_case()
        connection = connect_db()
        try:
            rejected_id = connection.execute(
                "SELECT id FROM workflow_stages WHERE workflow_id = ? AND stage_key = 'rechazado'",
                (self.workflow_id,),
            ).fetchone()["id"]
            connection.execute("DELETE FROM workflow_case_stage_assignments WHERE stage_id = ?", (rejected_id,))
            connection.execute("DELETE FROM workflow_transitions WHERE to_stage_id = ?", (rejected_id,))
            connection.execute("DELETE FROM workflow_stages WHERE id = ?", (rejected_id,))
            before_case = tuple(connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,),
            ).fetchone())
            before_events = [tuple(row) for row in connection.execute(
                "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY id", (case_id,),
            )]
            before_version = connection.execute(
                "SELECT version FROM workflow_definitions WHERE id = ?", (self.workflow_id,),
            ).fetchone()[0]
            connection.commit()
        finally:
            connection.close()
        initialize_db()
        initialize_db()
        connection = connect_db()
        try:
            after_case = tuple(connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,),
            ).fetchone())
            after_events = [tuple(row) for row in connection.execute(
                "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY id", (case_id,),
            )]
            self.assertEqual(after_case, before_case)
            self.assertEqual(after_events, before_events)
            self.assertEqual(connection.execute(
                "SELECT version FROM workflow_definitions WHERE id = ?", (self.workflow_id,),
            ).fetchone()[0], before_version + 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM workflow_transitions AS t "
                "JOIN workflow_stages AS src ON src.id = t.from_stage_id "
                "JOIN workflow_stages AS dst ON dst.id = t.to_stage_id "
                "WHERE t.workflow_id = ? AND src.stage_key = 'evaluacion_inspector' "
                "AND dst.stage_key = 'rechazado'", (self.workflow_id,),
            ).fetchone()[0], 1)
        finally:
            connection.close()

    def test_lawyer_return_migration_preserves_cases_and_history(self):
        case_id = self._create_case()
        connection = connect_db()
        try:
            stages = {
                row["stage_key"]: row["id"]
                for row in connection.execute(
                    "SELECT stage_key, id FROM workflow_stages WHERE workflow_id = ?", (self.workflow_id,)
                )
            }
            old_transition = connection.execute(
                "SELECT id FROM workflow_transitions WHERE workflow_id = ? AND from_stage_id = ? "
                "AND to_stage_id = ?",
                (self.workflow_id, stages["abogado"], stages["jefe_licenciamiento"]),
            ).fetchone()["id"]
            connection.execute(
                "UPDATE workflow_transitions SET to_stage_id = ?, label = ? WHERE id = ?",
                (stages["evaluacion_inspector"], "Devolver a Inspección", old_transition),
            )
            connection.execute(
                "UPDATE workflow_cases SET current_stage_id = ?, assigned_user_id = ? WHERE id = ?",
                (stages["abogado"], self.admin_id, case_id),
            )
            before = tuple(connection.execute("SELECT * FROM workflow_cases WHERE id = ?", (case_id,)).fetchone())
            events_before = [
                tuple(row) for row in connection.execute(
                    "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY id", (case_id,)
                )
            ]
            version_before = connection.execute(
                "SELECT version FROM workflow_definitions WHERE id = ?", (self.workflow_id,)
            ).fetchone()["version"]
            connection.commit()
        finally:
            connection.close()
        initialize_db()
        initialize_db()
        connection = connect_db()
        try:
            self.assertEqual(tuple(connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,)
            ).fetchone()), before)
            self.assertEqual([
                tuple(row) for row in connection.execute(
                    "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY id", (case_id,)
                )
            ], events_before)
            self.assertEqual(connection.execute(
                "SELECT version FROM workflow_definitions WHERE id = ?", (self.workflow_id,)
            ).fetchone()["version"], version_before + 1)
            targets = [
                row["to_stage_id"] for row in connection.execute(
                    "SELECT to_stage_id FROM workflow_transitions WHERE workflow_id = ? AND from_stage_id = ?",
                    (self.workflow_id, stages["abogado"]),
                )
            ]
            self.assertCountEqual(targets, [stages["jefe_licenciamiento"], stages["director"]])
        finally:
            connection.close()
        handler = WorkflowHandler(self.admin)
        with self.assertRaisesRegex(ValueError, "no está disponible"):
            handler.transition_workflow_case({"case_id": case_id, "transition_id": old_transition})

    def test_lawyer_cannot_configure_return_to_inspection(self):
        connection = connect_db()
        try:
            stages = {
                row["stage_key"]: row["id"]
                for row in connection.execute(
                    "SELECT stage_key, id FROM workflow_stages WHERE workflow_id = ?", (self.workflow_id,)
                )
            }
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "devolver.*jefe"):
            WorkflowHandler(self.admin).set_workflow_transition({
                "workflow_id": self.workflow_id, "from_stage_id": stages["abogado"],
                "to_stage_id": stages["evaluacion_inspector"], "label": "Devolver",
            })

    def test_transitions_record_actor_and_elapsed_stage_time(self):
        case_id = self._create_case()
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE workflow_cases SET entered_stage_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), case_id),
            )
            transition_id = connection.execute(
                "SELECT id FROM workflow_transitions WHERE workflow_id = ? "
                "AND from_stage_id = (SELECT current_stage_id FROM workflow_cases WHERE id = ?) "
                "AND to_stage_id = (SELECT id FROM workflow_stages "
                "WHERE workflow_id = ? AND stage_key = 'evaluacion_inspector')",
                (self.workflow_id, case_id, self.workflow_id),
            ).fetchone()["id"]
            connection.commit()
        finally:
            connection.close()

        handler = WorkflowHandler(self.admin)
        handler.transition_workflow_case({
            "case_id": case_id,
            "transition_id": transition_id,
            "inspector_id": self.admin_id,
            "comment": "Datos completos",
        })
        self.assertEqual(handler.response, (200, {"saved": True}))

        connection = connect_db()
        try:
            event = connection.execute(
                "SELECT actor_id, event_type, comment, duration_seconds "
                "FROM workflow_events WHERE case_id = ? AND event_type = 'transitioned' AND comment = 'Datos completos'",
                (case_id,),
            ).fetchone()
            case = connection.execute(
                "SELECT status, workflow_version FROM workflow_cases WHERE id = ?",
                (case_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(event["actor_id"], self.admin_id)
        self.assertEqual(event["comment"], "Datos completos")
        self.assertGreaterEqual(event["duration_seconds"], 3599)
        self.assertEqual(case["status"], "active")

    def test_group_assignee_must_claim_case_before_transition(self):
        connection = connect_db()
        try:
            cursor = connection.execute(
                "INSERT INTO workflow_users "
                "(username, display_name, password_hash, role, workflow_role, active, created_at) "
                "VALUES ('inspector', 'Inspector de prueba', ?, 'user', 'administracion', 1, ?)",
                (hash_password("password-inspector"), self.now()),
            )
            user_id = cursor.lastrowid
            cursor = connection.execute(
                "INSERT INTO workflow_groups (name, created_at) VALUES ('Administración', ?)",
                (self.now(),),
            )
            group_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO workflow_group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, user_id),
            )
            connection.commit()
        finally:
            connection.close()
        case_id = self._create_case()
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE workflow_cases SET current_stage_id = "
                "(SELECT id FROM workflow_stages WHERE workflow_id = ? AND stage_key = 'carga_datos'), "
                "assigned_user_id = NULL, assigned_group_id = ? WHERE id = ?",
                (self.workflow_id, group_id, case_id),
            )
            transition_id = connection.execute(
                "SELECT id FROM workflow_transitions WHERE workflow_id = ? "
                "AND from_stage_id = (SELECT current_stage_id FROM workflow_cases WHERE id = ?) "
                "AND to_stage_id = (SELECT id FROM workflow_stages "
                "WHERE workflow_id = ? AND stage_key = 'asignacion_inspector')",
                (self.workflow_id, case_id, self.workflow_id),
            ).fetchone()["id"]
            connection.commit()
        finally:
            connection.close()
        inspector = {
            "id": user_id,
            "username": "inspector",
            "display_name": "Inspector de prueba",
            "role": "user",
            "workflow_role": "administracion",
        }

        handler = WorkflowHandler(inspector)
        handler.transition_workflow_case({
            "case_id": case_id,
            "transition_id": transition_id,
        })
        self.assertEqual(handler.response[0], 403)

        handler.claim_workflow_case({"case_id": case_id})
        self.assertEqual(handler.response, (200, {"saved": True}))
        handler.get_workflow_data({"case_id": [str(case_id)]})
        self.assertEqual([item["to_stage_key"] for item in handler.response[1]["detail"]["transitions"]],
                         ["asignacion_inspector"])
        connection = connect_db()
        try:
            direct_transition = connection.execute(
                "SELECT id FROM workflow_transitions WHERE workflow_id = ? "
                "AND from_stage_id = (SELECT current_stage_id FROM workflow_cases WHERE id = ?) "
                "AND to_stage_id = (SELECT id FROM workflow_stages "
                "WHERE workflow_id = ? AND stage_key = 'evaluacion_inspector')",
                (self.workflow_id, case_id, self.workflow_id),
            ).fetchone()["id"]
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "no tiene inspector"):
            handler.transition_workflow_case({"case_id": case_id, "transition_id": direct_transition})
        handler.transition_workflow_case({
            "case_id": case_id,
            "transition_id": transition_id,
        })
        self.assertEqual(handler.response, (200, {"saved": True}))

    def test_return_preserves_inspector_who_claimed_group_case(self):
        admin = WorkflowHandler(self.admin)
        admin.create_workflow_user({
            "username": "claiming-inspector", "display_name": "Inspector",
            "password": "password-test-role", "role": "inspeccion",
        })
        inspector = WorkflowHandler(admin.response[1])
        admin.create_workflow_user({
            "username": "secretary", "display_name": "Secretaría",
            "password": "password-test-role", "role": "administracion",
        })
        secretary = WorkflowHandler(admin.response[1])
        case_id = self._create_case(actor=secretary.current_user)
        admin.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in admin.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual((case["status"], case["is_observed"]), ("active", 0))
        assignment_transition = admin.response[1]["detail"]["transitions"][0]["id"]
        group_id = next(group["id"] for group in admin.response[1]["groups"] if group["name"] == "Inspección")
        admin.transition_workflow_case({
            "case_id": case_id, "transition_id": assignment_transition,
            "inspector_id": self.admin_id, "comment": "Asignado",
        })
        admin.assign_workflow_case({"case_id": case_id, "group_id": group_id})
        inspector.claim_workflow_case({"case_id": case_id})
        self.assertEqual(inspector.response, (200, {"saved": True}))
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        return_transition = next(
            item["id"] for item in inspector.response[1]["detail"]["transitions"]
            if item["to_stage_key"] == "carga_datos"
        )
        inspector.transition_workflow_case({
            "case_id": case_id, "transition_id": return_transition, "comment": "Falta información",
        })
        secretary.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in secretary.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual((case["status"], case["is_observed"]), ("active", 1))
        secretary.assign_workflow_case({"case_id": case_id, "user_id": secretary.current_user["id"]})
        secretary.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in secretary.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual(case["is_observed"], 1)
        self.assertEqual([item["to_stage_key"] for item in secretary.response[1]["detail"]["transitions"]],
                         ["evaluacion_inspector"])
        direct_transition = secretary.response[1]["detail"]["transitions"][0]["id"]
        secretary.transition_workflow_case({"case_id": case_id, "transition_id": direct_transition})
        secretary.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in secretary.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual(case["assigned_user_id"], inspector.current_user["id"])
        self.assertIsNone(case["assigned_group_id"])
        self.assertEqual((case["status"], case["is_observed"]), ("active", 0))

    def test_existing_cases_keep_their_stage_assignment_snapshot(self):
        connection = connect_db()
        try:
            users = []
            for username in ("first-inspector", "second-inspector"):
                cursor = connection.execute(
                    "INSERT INTO workflow_users "
                    "(username, display_name, password_hash, role, workflow_role, active, created_at) "
                    "VALUES (?, ?, ?, 'user', 'jefe_licenciamiento', 1, ?)",
                    (username, username, hash_password("password-inspector"), self.now()),
                )
                users.append(cursor.lastrowid)
            stage_id = connection.execute(
                "SELECT id FROM workflow_stages WHERE workflow_id = ? "
                "AND stage_key = 'jefe_licenciamiento'",
                (self.workflow_id,),
            ).fetchone()["id"]
            connection.commit()
        finally:
            connection.close()

        handler = WorkflowHandler(self.admin)
        handler.set_stage_assignment({"stage_id": stage_id, "user_id": users[0]})
        self.assertEqual(handler.response, (200, {"saved": True}))
        case_id = self._create_case()
        connection = connect_db()
        try:
            workflow_version = connection.execute(
                "SELECT workflow_version FROM workflow_cases WHERE id = ?", (case_id,)
            ).fetchone()["workflow_version"]
        finally:
            connection.close()

        handler.set_stage_assignment({"stage_id": stage_id, "user_id": users[1]})
        connection = connect_db()
        try:
            transition_id = connection.execute(
                "SELECT id FROM workflow_transitions WHERE workflow_id = ? "
                "AND from_stage_id = (SELECT current_stage_id FROM workflow_cases WHERE id = ?)",
                (self.workflow_id, case_id),
            ).fetchone()["id"]
        finally:
            connection.close()
        handler.transition_workflow_case({
            "case_id": case_id, "transition_id": transition_id, "inspector_id": self.admin_id,
            "comment": "Asignado",
        })
        self.assertEqual(handler.response, (200, {"saved": True}))
        connection = connect_db()
        try:
            transition_id = connection.execute(
                "SELECT id FROM workflow_transitions WHERE workflow_id = ? "
                "AND from_stage_id = (SELECT current_stage_id FROM workflow_cases WHERE id = ?) "
                "AND to_stage_id = ?",
                (self.workflow_id, case_id, stage_id),
            ).fetchone()["id"]
        finally:
            connection.close()
        handler.transition_workflow_case({"case_id": case_id, "transition_id": transition_id})
        self.assertEqual(handler.response, (200, {"saved": True}))
        connection = connect_db()
        try:
            case = connection.execute(
                "SELECT workflow_version, assigned_user_id "
                "FROM workflow_cases WHERE id = ?",
                (case_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(case["workflow_version"], workflow_version)
        self.assertEqual(case["assigned_user_id"], users[0])

    def test_request_type_is_validated_when_creating_case(self):
        handler = WorkflowHandler(self.admin)
        base = {"workflow_id": self.workflow_id, "title": "Prueba", "application_data": APPLICATION_DATA}
        handler.create_workflow_case({**base, "request_type": "nueva"})
        self.assertEqual(handler.response[0], 201)
        handler.get_workflow_data({"case_id": [str(handler.response[1]["id"])]})
        self.assertIsNone(handler.response[1]["cases"][0]["inspector_id"])
        for request_type in ("", "desconocido"):
            with self.subTest(request_type=request_type), self.assertRaises(ValueError):
                handler.create_workflow_case({**base, "request_type": request_type})
        for request_type in ("renovacion", "modificacion"):
            with self.subTest(request_type=request_type), self.assertRaises(ValueError):
                handler.create_workflow_case({**base, "request_type": request_type})
        with self.assertRaises(ValueError):
            handler.create_workflow_case({**base, "request_type": "nueva", "linked_user_name": "Carpeta"})
        with self.assertRaises(ValueError):
            handler.create_workflow_case({**base, "request_type": "nueva", "inspector_id": 999999})
        with self.assertRaises(ValueError):
            handler.create_workflow_case({**base, "request_type": "modificacion", "linked_user_name": "Inexistente"})

    def test_application_data_is_saved_and_returned_for_each_signature_choice(self):
        handler = WorkflowHandler(self.admin)
        for signature in (False, True):
            fields = {**APPLICATION_DATA, "digital_signature": signature}
            case_id = self._create_case(application_data=fields)
            handler.get_workflow_data({"case_id": [str(case_id)]})
            saved = handler.response[1]["detail"]["application_data"]
            self.assertEqual(saved, {**fields, "digital_signature": int(signature)})

    def test_case_without_subject_exposes_applicant_name(self):
        handler = WorkflowHandler(self.admin)
        handler.create_workflow_case({
            "workflow_id": self.workflow_id,
            "request_type": "nueva",
            "application_data": APPLICATION_DATA,
        })
        case_id = handler.response[1]["id"]
        handler.get_workflow_data({"case_id": [str(case_id)]})
        case = next(row for row in handler.response[1]["cases"] if row["id"] == case_id)
        self.assertEqual(case["title"], "Autorización Individual")
        self.assertEqual(
            (case["applicant_first_name"], case["applicant_last_name"]),
            ("Nombre", "Apellido"),
        )
        self.assertEqual(case["reference"], "")

    def test_application_data_is_required_and_validated(self):
        handler = WorkflowHandler(self.admin)
        base = {"workflow_id": self.workflow_id, "title": "Prueba", "request_type": "nueva"}
        invalid_values = [
            None,
            *({key: value for key, value in APPLICATION_DATA.items() if key != missing}
              for missing in APPLICATION_DATA),
            {**APPLICATION_DATA, "application_date": "2026-02-30"},
            {**APPLICATION_DATA, "email": "correo-invalido"},
            {**APPLICATION_DATA, "digital_signature": 0},
            {**APPLICATION_DATA, "first_name": "  "},
        ]
        for fields in invalid_values:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                handler.create_workflow_case({**base, "application_data": fields})
        connection = connect_db()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_cases").fetchone()[0], 0)
        finally:
            connection.close()

    def test_linked_renewal_and_inspector_assignment_survive_return(self):
        connection = connect_db()
        try:
            connection.execute(
                "INSERT INTO file_catalog "
                "(root, path, name, extension, owner, size, modified_ns, scan_id) "
                "VALUES ('documents', 'Persona/archivo.txt', 'archivo.txt', '.txt', 'Persona', 0, 0, 'test')"
            )
            connection.commit()
        finally:
            connection.close()
        case_id = self._create_case(request_type="renovacion", linked_user_name="Persona")
        modification_id = self._create_case(request_type="modificacion", linked_user_name="Persona")
        handler = WorkflowHandler(self.admin)
        connection = connect_db()
        try:
            stages = {
                row["stage_key"]: row["id"]
                for row in connection.execute(
                    "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?",
                    (self.workflow_id,),
                )
            }
            transitions = {
                (row["from_stage_id"], row["to_stage_id"]): row["id"]
                for row in connection.execute(
                    "SELECT id, from_stage_id, to_stage_id FROM workflow_transitions WHERE workflow_id = ?",
                    (self.workflow_id,),
                )
            }
        finally:
            connection.close()
        for source, target in [
            ("asignacion_inspector", "evaluacion_inspector"),
            ("evaluacion_inspector", "jefe_licenciamiento"),
            ("jefe_licenciamiento", "evaluacion_inspector"),
        ]:
            handler.transition_workflow_case({
                "case_id": case_id,
                "transition_id": transitions[(stages[source], stages[target])],
                "inspector_id": self.admin_id,
                "comment": "Asignado",
            })
            self.assertEqual(handler.response, (200, {"saved": True}))
        handler.get_workflow_data({"case_id": [str(case_id)]})
        case = next(item for item in handler.response[1]["cases"] if item["id"] == case_id)
        self.assertEqual(case["request_type"], "renovacion")
        self.assertEqual(case["linked_user_name"], "Persona")
        self.assertEqual(case["assigned_user_id"], self.admin_id)
        self.assertEqual(case["inspector_name"], "Administración")
        modification = next(item for item in handler.response[1]["cases"] if item["id"] == modification_id)
        self.assertEqual(modification["request_type"], "modificacion")
        self.assertEqual(modification["linked_user_name"], "Persona")

    def test_assigned_inspector_can_link_missing_user_and_entity_folders(self):
        admin = WorkflowHandler(self.admin)
        inspectors = []
        for username in ("link-inspector", "link-other"):
            admin.create_workflow_user({
                "username": username, "display_name": username,
                "password": "password-test-role", "role": "inspeccion",
            })
            inspectors.append(admin.response[1])
        inspector, other = (WorkflowHandler(user) for user in inspectors)
        user_case = self._create_case()
        connection = connect_db()
        try:
            connection.execute(
                "INSERT INTO file_catalog "
                "(root, path, name, extension, owner, size, modified_ns, scan_id) "
                "VALUES ('documents', 'Carpeta prueba enlace/archivo.txt', 'archivo.txt', "
                "'.txt', 'Carpeta prueba enlace', 0, 0, 'test-link')"
            )
            transition_id = connection.execute(
                "SELECT transitions.id FROM workflow_transitions AS transitions "
                "JOIN workflow_stages AS source ON source.id = transitions.from_stage_id "
                "JOIN workflow_stages AS target ON target.id = transitions.to_stage_id "
                "WHERE transitions.workflow_id = ? AND source.stage_key = 'asignacion_inspector' "
                "AND target.stage_key = 'evaluacion_inspector'", (self.workflow_id,),
            ).fetchone()[0]
            entity_workflow = connection.execute(
                "SELECT workflow_id FROM workflow_entity_types ORDER BY workflow_id LIMIT 1"
            ).fetchone()[0]
            connection.commit()
        finally:
            connection.close()

        payload = {"case_id": user_case, "kind": "user", "name": "Carpeta prueba enlace"}
        with self.assertRaisesRegex(ValueError, "durante la evaluación"):
            inspector.link_workflow_folder(payload)
        admin.transition_workflow_case({
            "case_id": user_case, "transition_id": transition_id, "inspector_id": inspectors[0]["id"],
        })
        self.assertEqual(admin.response[0], 200)
        other.link_workflow_folder(payload)
        self.assertEqual(other.response[0], 403)
        with self.assertRaisesRegex(ValueError, "no existe"):
            inspector.link_workflow_folder({**payload, "name": "No existe"})
        with self.assertRaisesRegex(ValueError, "no corresponde"):
            with patch("routes.workflows._entity_areas", return_value=([{"institution": "Entidad prueba enlace"}], 0)):
                inspector.link_workflow_folder({**payload, "kind": "entity", "name": "Entidad prueba enlace"})
        inspector.link_workflow_folder(payload)
        self.assertEqual(inspector.response, (200, {"saved": True}))
        with self.assertRaisesRegex(ValueError, "ya tiene"):
            inspector.link_workflow_folder(payload)

        admin.create_workflow_case({
            "workflow_id": entity_workflow, "request_type": "nueva",
            "application_data": ENTITY_APPLICATION_DATA, "inspector_id": inspectors[0]["id"],
        })
        entity_case = admin.response[1]["id"]
        entity_payload = {"case_id": entity_case, "kind": "entity", "name": "Entidad prueba enlace"}
        with patch("routes.workflows._entity_areas", return_value=([{"institution": "Entidad prueba enlace"}], 0)):
            other.link_workflow_folder(entity_payload)
            self.assertEqual(other.response[0], 403)
            inspector.link_workflow_folder(entity_payload)
            self.assertEqual(inspector.response, (200, {"saved": True}))
            with self.assertRaisesRegex(ValueError, "ya tiene"):
                inspector.link_workflow_folder(entity_payload)

        for case_id, key, expected in (
            (user_case, "linked_user_name", "Carpeta prueba enlace"),
            (entity_case, "linked_entity_name", "Entidad prueba enlace"),
        ):
            inspector.get_workflow_data({"case_id": [str(case_id)]})
            case = next(item for item in inspector.response[1]["cases"] if item["id"] == case_id)
            self.assertEqual(case[key], expected)
            event = inspector.response[1]["detail"]["events"][-1]
            self.assertEqual(event["event_type"], "folder_linked")
            self.assertEqual(event["actor_name"], "link-inspector")
            self.assertIn(expected, event["comment"])
        self.assertEqual(
            inspector.response[1]["detail"]["entity_application_data"]["linked_entity_name"],
            "Entidad prueba enlace",
        )

        inspector.wfile = BytesIO()
        inspector.send_response = lambda status: self.assertEqual(status, 200)
        inspector.send_header = lambda name, value: None
        inspector.end_headers = lambda: None
        inspector.export_workflows()
        workbook = load_workbook(BytesIO(inspector.wfile.getvalue()), read_only=True)
        try:
            cases = list(workbook["Trámites"].values)
            columns = {name: index for index, name in enumerate(cases[0])}
            rows = {row[columns["ID trámite"]]: row for row in cases[1:]}
            self.assertEqual(rows[user_case][columns["Carpeta vinculada"]], "Carpeta prueba enlace")
            self.assertEqual(rows[entity_case][columns["Carpeta de entidad"]], "Entidad prueba enlace")
            history = list(workbook["Historial"].values)
            labels = {name: index for index, name in enumerate(history[0])}
            self.assertEqual(sum(row[labels["Acción"]] == "Carpeta vinculada" for row in history[1:]), 2)
        finally:
            workbook.close()

    def test_existing_cases_are_preserved_during_request_type_migration(self):
        original_database = database.DATABASE
        legacy_database = Path(self.temp_root.name) / "legacy-workflow.sqlite3"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute(
                "CREATE TABLE workflow_cases (id INTEGER PRIMARY KEY, workflow_id INTEGER, status TEXT, "
                "updated_at TEXT, delivered_at TEXT, delivered_by INTEGER)"
            )
            connection.execute(
                "INSERT INTO workflow_cases (id, status, updated_at) VALUES (9, 'active', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO workflow_cases (id, status, updated_at, delivered_at, delivered_by) "
                "VALUES (10, 'completed', '2026-01-01', '2026-01-02', 7)"
            )
            connection.commit()
        finally:
            connection.close()
        try:
            database.DATABASE = legacy_database
            initialize_db()
            initialize_db()
            connection = connect_db()
            try:
                row = connection.execute(
                    "SELECT id, request_type, delivered_at, delivered_by, delivery_method "
                    "FROM workflow_cases WHERE id = 9"
                ).fetchone()
                self.assertEqual((row["id"], row["request_type"]), (9, ""))
                self.assertIsNone(row["delivered_at"])
                self.assertIsNone(row["delivered_by"])
                self.assertIsNone(row["delivery_method"])
                old_delivery = connection.execute(
                    "SELECT delivered_at, delivered_by, delivery_method FROM workflow_cases WHERE id = 10"
                ).fetchone()
                self.assertEqual(tuple(old_delivery), ("2026-01-02", 7, None))
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM workflow_case_application_data").fetchone()[0],
                    0,
                )
            finally:
                connection.close()
        finally:
            database.DATABASE = original_database
            legacy_database.unlink(missing_ok=True)

    def test_role_permissions_and_all_authorization_returns(self):
        admin = WorkflowHandler(self.admin)
        users = {}
        for role in ("administracion", "inspeccion", "jefe_licenciamiento", "abogado", "director"):
            admin.create_workflow_user({
                "username": role,
                "display_name": role,
                "password": "password-test-role",
                "role": role,
            })
            self.assertEqual(admin.response[0], 201)
            users[role] = admin.response[1]
        admin.create_workflow_user({
            "username": "other-inspector", "display_name": "Otro inspector",
            "password": "password-test-role", "role": "inspeccion",
        })
        other_inspector = admin.response[1]
        admin.get_workflow_data({})
        inspection_group = next(group for group in admin.response[1]["groups"] if group["name"] == "Inspección")
        self.assertEqual({member["id"] for member in inspection_group["members"]},
                         {users["inspeccion"]["id"], other_inspector["id"]})

        connection = connect_db()
        try:
            stages = {
                row["stage_key"]: row["id"]
                for row in connection.execute(
                    "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?",
                    (self.workflow_id,),
                )
            }
            transitions = {
                (row["from_stage_id"], row["to_stage_id"]): row["id"]
                for row in connection.execute(
                    "SELECT id, from_stage_id, to_stage_id FROM workflow_transitions WHERE workflow_id = ?",
                    (self.workflow_id,),
                )
            }
        finally:
            connection.close()
        for stage, role in (
            ("carga_datos", "administracion"),
            ("asignacion_inspector", "jefe_licenciamiento"),
            ("jefe_licenciamiento", "jefe_licenciamiento"),
            ("abogado", "abogado"),
            ("director", "director"),
        ):
            admin.set_stage_assignment({"stage_id": stages[stage], "user_id": users[role]["id"]})

        inspector = WorkflowHandler(users["inspeccion"])
        inspector.create_workflow_case({"workflow_id": self.workflow_id, "title": "Sin permiso"})
        self.assertEqual(inspector.response, (403, {
            "error": "Solo Secretaría o un administrador pueden iniciar trámites."
        }))
        administration = WorkflowHandler(users["administracion"])
        administration.create_workflow_case({
            "workflow_id": self.workflow_id, "title": "Trámite",
            "request_type": "nueva",
            "application_data": APPLICATION_DATA,
        })
        case_id = administration.response[1]["id"]
        with self.assertRaisesRegex(ValueError, "finalizadas"):
            administration.deliver_workflow_case({"case_id": case_id, "delivery_method": "presencial"})

        def transition(actor, source, target, expected=200, inspector_id=None):
            handler = WorkflowHandler(actor)
            handler.transition_workflow_case({
                "case_id": case_id, "transition_id": transitions[(stages[source], stages[target])],
                "inspector_id": inspector_id, "comment": "Asignado",
            })
            self.assertEqual(handler.response[0], expected)

        transition(users["administracion"], "asignacion_inspector", "evaluacion_inspector", 403,
                   users["inspeccion"]["id"])
        assignment_transition = transitions[(stages["asignacion_inspector"], stages["evaluacion_inspector"])]
        chief = WorkflowHandler(users["jefe_licenciamiento"])
        chief.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in chief.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual(case["stage_key"], "asignacion_inspector")
        self.assertIsNone(case["inspector_id"])
        self.assertEqual(len(chief.response[1]["detail"]["events"]), 2)
        for invalid in (None, 999999, users["abogado"]["id"]):
            with self.subTest(inspector=invalid), self.assertRaises(ValueError):
                chief.transition_workflow_case({
                    "case_id": case_id, "transition_id": assignment_transition, "inspector_id": invalid,
                    "comment": "Asignado",
                })
        transition(users["jefe_licenciamiento"], "asignacion_inspector", "evaluacion_inspector",
                   inspector_id=users["inspeccion"]["id"])
        transition(other_inspector, "evaluacion_inspector", "jefe_licenciamiento", 403)
        inspector.assign_workflow_case({"case_id": case_id, "user_id": other_inspector["id"]})
        self.assertEqual(inspector.response[0], 403)
        with self.assertRaises(ValueError):
            admin.assign_workflow_case({"case_id": case_id, "user_id": users["abogado"]["id"]})
        transition(users["inspeccion"], "evaluacion_inspector", "carga_datos")
        administration.get_workflow_data({"case_id": [str(case_id)]})
        available = administration.response[1]["detail"]["transitions"]
        self.assertEqual([item["to_stage_key"] for item in available], ["evaluacion_inspector"])
        with self.assertRaisesRegex(ValueError, "inspector"):
            administration.transition_workflow_case({
                "case_id": case_id,
                "transition_id": transitions[(stages["carga_datos"], stages["asignacion_inspector"])],
            })
        connection = connect_db()
        try:
            connection.execute("UPDATE workflow_users SET active = 0 WHERE id = ?", (users["inspeccion"]["id"],))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "inactiva"):
            administration.transition_workflow_case({
                "case_id": case_id,
                "transition_id": transitions[(stages["carga_datos"], stages["evaluacion_inspector"])],
            })
        return_transition = transitions[(stages["carga_datos"], stages["evaluacion_inspector"])]
        for invalid in (999999, users["abogado"]["id"], users["inspeccion"]["id"]):
            with self.subTest(reassignment=invalid), self.assertRaises(ValueError):
                administration.transition_workflow_case({
                    "case_id": case_id, "transition_id": return_transition, "inspector_id": invalid,
                })
        administration.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in administration.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual((case["stage_key"], case["inspector_id"], case["is_observed"]),
                         ("carga_datos", users["inspeccion"]["id"], 1))
        inspector.transition_workflow_case({
            "case_id": case_id, "transition_id": return_transition, "inspector_id": other_inspector["id"],
        })
        self.assertEqual(inspector.response[0], 403)
        administration.transition_workflow_case({
            "case_id": case_id, "transition_id": return_transition,
            "inspector_id": other_inspector["id"], "comment": "Corregido",
        })
        self.assertEqual(administration.response[0], 200)
        administration.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in administration.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual((case["assigned_user_id"], case["inspector_id"]),
                         (other_inspector["id"], other_inspector["id"]))
        event = administration.response[1]["detail"]["events"][-1]
        self.assertEqual((event["to_user_name"], event["comment"]), ("Otro inspector", "Corregido"))
        transition(other_inspector, "evaluacion_inspector", "carga_datos")
        connection = connect_db()
        try:
            connection.execute("UPDATE workflow_users SET active = 1 WHERE id = ?", (users["inspeccion"]["id"],))
            connection.commit()
        finally:
            connection.close()
        transition(users["administracion"], "carga_datos", "evaluacion_inspector",
                   inspector_id=users["inspeccion"]["id"])
        administration.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in administration.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual(case["assigned_user_id"], users["inspeccion"]["id"])
        event = administration.response[1]["detail"]["events"][-1]
        self.assertEqual((event["actor_name"], event["to_user_name"]),
                         ("administracion", "inspeccion"))
        transition(users["inspeccion"], "evaluacion_inspector", "jefe_licenciamiento")
        transition(users["jefe_licenciamiento"], "jefe_licenciamiento", "abogado")
        transition(users["abogado"], "abogado", "jefe_licenciamiento")
        transition(users["jefe_licenciamiento"], "jefe_licenciamiento", "abogado")
        transition(users["abogado"], "abogado", "director")
        transition(users["director"], "director", "jefe_licenciamiento")
        transition(users["jefe_licenciamiento"], "jefe_licenciamiento", "abogado")
        transition(users["abogado"], "abogado", "director")
        transition(users["director"], "director", "evaluacion_inspector")
        admin.assign_workflow_case({"case_id": case_id, "user_id": other_inspector["id"]})
        self.assertEqual(admin.response, (200, {"saved": True}))
        transition(users["inspeccion"], "evaluacion_inspector", "jefe_licenciamiento", 403)
        transition(self.admin, "evaluacion_inspector", "jefe_licenciamiento")
        transition(users["jefe_licenciamiento"], "jefe_licenciamiento", "abogado")
        transition(users["abogado"], "abogado", "jefe_licenciamiento")
        transition(users["jefe_licenciamiento"], "jefe_licenciamiento", "evaluacion_inspector")
        transition(other_inspector, "evaluacion_inspector", "jefe_licenciamiento")
        transition(users["jefe_licenciamiento"], "jefe_licenciamiento", "abogado")
        transition(users["abogado"], "abogado", "director")
        transition(users["director"], "director", "finalizado")
        inspector.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in inspector.response[1]["cases"] if case["id"] == case_id)
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["inspector_name"], "Otro inspector")
        self.assertIsNone(case["delivered_at"])
        inspector.deliver_workflow_case({"case_id": case_id, "delivery_method": "presencial"})
        self.assertEqual(inspector.response[0], 403)
        admin.create_workflow_user({
            "username": "other-secretary", "display_name": "Otra Secretaría",
            "password": "password-test-role", "role": "administracion",
        })
        other_secretary = WorkflowHandler(admin.response[1])
        connection = connect_db()
        try:
            connection.execute(
                "UPDATE workflow_cases SET entered_stage_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), case_id),
            )
            connection.commit()
        finally:
            connection.close()
        other_secretary.deliver_workflow_case({"case_id": case_id, "delivery_method": "presencial"})
        self.assertEqual(other_secretary.response, (200, {"saved": True}))
        other_secretary.get_workflow_data({"case_id": [str(case_id)]})
        case = next(case for case in other_secretary.response[1]["cases"] if case["id"] == case_id)
        self.assertIsNotNone(case["delivered_at"])
        self.assertEqual(case["delivered_by_name"], "Otra Secretaría")
        event = other_secretary.response[1]["detail"]["events"][-1]
        self.assertEqual((event["event_type"], event["actor_name"]), ("delivered", "Otra Secretaría"))
        self.assertGreaterEqual(event["duration_seconds"], 7199)
        with self.assertRaisesRegex(ValueError, "entregado"):
            administration.deliver_workflow_case({"case_id": case_id, "delivery_method": "presencial"})
        administration.get_workflow_data({"case_id": [str(case_id)]})
        self.assertEqual(
            sum(event["event_type"] == "delivered" for event in administration.response[1]["detail"]["events"]), 1
        )

    def test_assignment_stage_migrates_existing_cases_without_changing_them(self):
        case_id = self._create_case()
        connection = connect_db()
        try:
            stages = {
                row["stage_key"]: row
                for row in connection.execute(
                    "SELECT * FROM workflow_stages WHERE workflow_id = ?", (self.workflow_id,)
                )
            }
            assignment = stages["asignacion_inspector"]
            inspection = stages["evaluacion_inspector"]
            chief = stages["jefe_licenciamiento"]
            connection.execute(
                "UPDATE workflow_stages SET default_assignee_user_id = ? WHERE id = ?",
                (self.admin_id, chief["id"]),
            )
            connection.execute(
                "UPDATE workflow_case_stage_assignments SET user_id = ? WHERE case_id = ? "
                "AND stage_id IN (?, ?)",
                (self.admin_id, case_id, inspection["id"], chief["id"]),
            )
            connection.execute(
                "UPDATE workflow_cases SET current_stage_id = ?, assigned_user_id = ? WHERE id = ?",
                (inspection["id"], self.admin_id, case_id),
            )
            connection.execute(
                "DELETE FROM workflow_case_stage_assignments WHERE stage_id = ?", (assignment["id"],)
            )
            connection.execute(
                "DELETE FROM workflow_transitions WHERE from_stage_id = ?", (assignment["id"],)
            )
            connection.execute(
                "DELETE FROM workflow_transitions WHERE from_stage_id = ? AND to_stage_id = ?",
                (stages["carga_datos"]["id"], inspection["id"]),
            )
            connection.execute(
                "UPDATE workflow_transitions SET to_stage_id = ? WHERE to_stage_id = ?",
                (inspection["id"], assignment["id"]),
            )
            connection.execute("DELETE FROM workflow_stages WHERE id = ?", (assignment["id"],))
            for stage in sorted(stages.values(), key=lambda row: row["position"]):
                if stage["position"] > assignment["position"]:
                    connection.execute(
                        "UPDATE workflow_stages SET position = ? WHERE id = ?",
                        (stage["position"] - 1, stage["id"]),
                    )
            before = tuple(connection.execute(
                "SELECT current_stage_id, assigned_user_id, workflow_version FROM workflow_cases WHERE id = ?",
                (case_id,),
            ).fetchone())
            connection.commit()
        finally:
            connection.close()
        initialize_db()
        initialize_db()
        connection = connect_db()
        try:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM workflow_transitions WHERE workflow_id = ?",
                (self.workflow_id,),
            ).fetchone()["n"]
            stages = connection.execute(
                "SELECT stage_key, position FROM workflow_stages WHERE workflow_id = ? ORDER BY position",
                (self.workflow_id,),
            ).fetchall()
            snapshots = {
                row["stage_key"]: row["user_id"]
                for row in connection.execute(
                    "SELECT stages.stage_key, assignments.user_id FROM workflow_case_stage_assignments "
                    "AS assignments JOIN workflow_stages AS stages ON stages.id = assignments.stage_id "
                    "WHERE assignments.case_id = ?", (case_id,),
                )
            }
            after = tuple(connection.execute(
                "SELECT current_stage_id, assigned_user_id, workflow_version FROM workflow_cases WHERE id = ?",
                (case_id,),
            ).fetchone())
        finally:
            connection.close()
        self.assertEqual(count, 13)
        self.assertEqual([row["position"] for row in stages], list(range(1, 9)))
        self.assertEqual([row["stage_key"] for row in stages][:3],
                         ["carga_datos", "asignacion_inspector", "evaluacion_inspector"])
        self.assertEqual(after, before)
        self.assertEqual(snapshots["evaluacion_inspector"], self.admin_id)
        self.assertEqual(snapshots["asignacion_inspector"], self.admin_id)

    def test_generic_workflow_creation_remains_available(self):
        handler = WorkflowHandler(self.admin)
        handler.create_workflow({"name": "Otro trámite", "stages": ["Inicio", "Fin"]})
        workflow_id = handler.response[1]["id"]
        handler.create_workflow_case({"workflow_id": workflow_id, "title": "Prueba"})
        self.assertEqual(handler.response[0], 201)
        handler.create_workflow_case({"workflow_id": workflow_id})
        case_id = handler.response[1]["id"]
        handler.get_workflow_data({"case_id": [str(case_id)]})
        case = next(row for row in handler.response[1]["cases"] if row["id"] == case_id)
        self.assertEqual(case["title"], "Otro trámite")
        self.assertIsNone(handler.response[1]["detail"]["application_data"])

    def test_created_user_password_length_and_authentication(self):
        handler = WorkflowHandler(self.admin)
        payload = {"username": "password-boundary", "display_name": "Usuario prueba", "role": "inspeccion"}
        for password in ("abcdefghijk", "x" * 257, None):
            with self.subTest(length=len(password) if password is not None else None):
                with self.assertRaisesRegex(ValueError, "entre 12 y 256"):
                    handler.create_workflow_user({**payload, "password": password})
        handler.create_workflow_user({**payload, "password": "abcdefghijkl"})
        self.assertEqual(handler.response[0], 201)
        self.assertIsNotNone(auth.authenticate("password-boundary", "abcdefghijkl"))
        self.assertIsNone(auth.authenticate("password-boundary", "abcdefghijk"))
        handler.create_workflow_user({**payload, "username": "password-maximum", "password": "x" * 256})
        self.assertEqual(handler.response[0], 201)

    def test_local_passwords_are_hashed_and_verified(self):
        password_hash = hash_password("password-user-test")
        self.assertNotEqual(password_hash, "password-user-test")
        self.assertTrue(auth.verify_password("password-user-test", password_hash))
        self.assertFalse(auth.verify_password("incorrect-password", password_hash))

    def test_session_authentication_and_revocation(self):
        connection = connect_db()
        try:
            cursor = connection.execute(
                "INSERT INTO workflow_users "
                "(username, display_name, password_hash, role, active, created_at) "
                "VALUES ('session-user', 'Usuario de sesión', ?, 'user', 1, ?)",
                (hash_password("password-session"), self.now()),
            )
            user_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(auth.authenticate("session-user", "wrong-password"))
        user = auth.authenticate("session-user", "password-session")
        self.assertEqual(user["id"], user_id)
        token = auth.create_session(user_id)
        self.assertEqual(auth.get_session_user(token)["username"], "session-user")
        auth.revoke_session(token)
        self.assertIsNone(auth.get_session_user(token))
