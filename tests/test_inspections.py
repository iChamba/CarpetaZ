import os
import tempfile
import unittest
from pathlib import Path

_ENV_ROOT = tempfile.TemporaryDirectory(prefix="carpetaz-inspections-env-")
os.environ.setdefault("DOCUMENT_ROOT", _ENV_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(_ENV_ROOT.name) / "appdata"))

import database
from routes.inspections import InspectionRoutes
from routes.workflows import WorkflowRoutes


class Handler(InspectionRoutes, WorkflowRoutes):
    def __init__(self, user):
        self.current_user = user
        self.response = None

    def send_json(self, payload, status=200):
        self.response = status, payload

    def send_error_json(self, message, status=400):
        self.response = status, {"error": message}


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.root.name) / "test.sqlite3"
        database.initialize_db()
        connection = database.connect_db()
        self.users = {}
        for name, role in (
            ("secretary", "administracion"), ("chief", "jefe_inspeccion"),
            ("inspector", "inspeccion"), ("other", "inspeccion"),
            ("lawyer", "abogado"), ("inactive", "inspeccion"), ("admin", ""),
        ):
            account_role = "admin" if name == "admin" else "user"
            cursor = connection.execute(
                "INSERT INTO workflow_users "
                "(username, display_name, password_hash, role, workflow_role, active, created_at) "
                "VALUES (?, ?, 'hash', ?, ?, ?, '2026-01-01')",
                (name, name, account_role, role, 0 if name == "inactive" else 1),
            )
            self.users[name] = {
                "id": cursor.lastrowid, "username": name, "display_name": name,
                "role": account_role, "workflow_role": role,
            }
        connection.commit()
        connection.close()

    def tearDown(self):
        database.DATABASE = self.original_database
        self.root.cleanup()

    def create(self):
        handler = Handler(self.users["secretary"])
        handler.create_inspection({"case_number": "00012", "company_name": "Institución"})
        self.assertEqual(handler.response[0], 201)
        return handler.response[1]["id"]

    def act(self, user, inspection_id, version, action, **kwargs):
        handler = Handler(self.users[user])
        handler.act_on_inspection({
            "inspection_id": inspection_id, "version": version, "action": action, **kwargs,
        })
        return handler.response

    def data(self, user):
        handler = Handler(self.users[user])
        handler.get_inspections()
        return handler.response[1]

    def test_complete_workflow_return_and_calendar_visibility(self):
        inspection_id = self.create()
        self.assertEqual(self.data("secretary")["inspections"][0]["stage"], "assignment")
        self.assertEqual(self.data("inspector")["inspections"], [])
        self.assertEqual(self.act("secretary", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])[0], 403)
        self.assertEqual(self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])[0], 200)
        self.assertEqual(self.act("other", inspection_id, 1, "complete_pre")[0], 403)
        pre = [True, False, True, False, True, False]
        self.assertEqual(self.act("inspector", inspection_id, 1, "complete_pre", checklist=pre,
                                  scheduled_date="2026-11-24", scheduled_time="09:30",
                                  scheduled_place="Centro")[0], 200)
        for name in ("secretary", "chief", "inspector", "admin"):
            result = self.data(name)["inspections"][0]
            self.assertEqual((result["scheduled_date"], result["scheduled_time"], result["scheduled_place"]),
                             ("2026-11-24", "09:30", "Centro"))
            self.assertEqual(result["pre_checklist"], pre)
        self.assertEqual(self.data("other")["inspections"], [])
        self.assertEqual(self.data("lawyer")["inspections"], [])
        post = [False, True, False, True, False, True]
        self.assertEqual(self.act("inspector", inspection_id, 2, "complete_post", checklist=post)[0], 200)
        with self.assertRaisesRegex(ValueError, "motivo"):
            self.act("chief", inspection_id, 3, "return_post", comment="  ")
        self.assertEqual(self.act("chief", inspection_id, 3, "return_post", comment="Corregir ítem")[0], 200)
        self.assertEqual(self.data("inspector")["inspections"][0]["post_checklist"], post)
        self.assertEqual(self.act("inspector", inspection_id, 4, "complete_post", checklist=[True] * 6)[0], 200)
        self.assertEqual(self.act("chief", inspection_id, 5, "approve", comment="Revisado")[0], 200)
        self.assertEqual(self.act("chief", inspection_id, 6, "send")[0], 403)
        self.assertEqual(self.act("inspector", inspection_id, 6, "send")[0], 200)
        database.initialize_db()
        result = self.data("chief")["inspections"][0]
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(result["post_checklist"], [True] * 6)
        self.assertEqual([event["action"] for event in result["events"]],
                         ["created", "assign", "complete_pre", "complete_post",
                          "return_post", "complete_post", "approve", "send"])
        self.assertEqual(result["events"][4]["comment"], "Corregir ítem")
        self.assertEqual(result["events"][-1]["actor_name"], "inspector")

    def test_invalid_inputs_stale_actions_and_permissions(self):
        inspection_id = self.create()
        for invalid in (None, True, "1", self.users["inactive"]["id"], self.users["lawyer"]["id"]):
            with self.subTest(inspector=invalid), self.assertRaises(ValueError):
                self.act("chief", inspection_id, 0, "assign", inspector_id=invalid)
        self.assertEqual(self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])[0], 200)
        self.assertEqual(self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["other"]["id"])[0], 409)
        self.assertEqual(self.act("inspector", inspection_id, 1, "complete_post")[0], 409)
        valid = dict(checklist=[False] * 6, scheduled_date="2026-11-24",
                     scheduled_time="09:30", scheduled_place="Centro")
        for field, value in (("checklist", [True] * 5), ("checklist", [1] * 6),
                             ("scheduled_date", "2026-02-30"), ("scheduled_time", "24:00"),
                             ("scheduled_place", " ")):
            invalid = {**valid, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.act("inspector", inspection_id, 1, "complete_pre", **invalid)
        result = self.data("inspector")["inspections"][0]
        self.assertEqual((result["stage"], result["version"], result["scheduled_date"]), ("pre_inspection", 1, ""))
        for name in ("inspector", "other", "lawyer"):
            handler = Handler(self.users[name])
            handler.create_inspection({})
            self.assertEqual(handler.response[0], 403)
        for field, value in (("application_date", "2026-02-30"), ("email", "invalid"),
                             ("request_type", "otro"), ("city", 42), ("rut", "x" * 101)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                Handler(self.users["secretary"]).create_inspection({field: value})

    def test_migration_preserves_existing_assigned_inspections(self):
        connection = database.connect_db()
        connection.execute("DROP TABLE inspection_events")
        connection.execute("DROP TABLE inspections")
        connection.execute(
            "CREATE TABLE inspections (id INTEGER PRIMARY KEY, inspector_id INTEGER NOT NULL, "
            "created_by INTEGER NOT NULL, created_at TEXT NOT NULL, application_date TEXT NOT NULL DEFAULT '', "
            "case_number TEXT NOT NULL DEFAULT '', company_name TEXT NOT NULL DEFAULT '', rut TEXT NOT NULL DEFAULT '', "
            "request_type TEXT NOT NULL DEFAULT '', inspection_type TEXT NOT NULL DEFAULT '', practice TEXT NOT NULL DEFAULT '', "
            "equipment_model TEXT NOT NULL DEFAULT '', contact_details TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', "
            "address TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "INSERT INTO inspections (id, inspector_id, created_by, created_at, case_number) VALUES (9, ?, ?, '2026-01-01', 'OLD')",
            (self.users["inspector"]["id"], self.users["secretary"]["id"]),
        )
        connection.commit()
        connection.close()
        database.initialize_db()
        database.initialize_db()
        item = self.data("inspector")["inspections"][0]
        self.assertEqual((item["id"], item["case_number"], item["stage"], item["version"]),
                         (9, "OLD", "pre_inspection", 0))
        self.assertEqual(self.create(), 10)

    def test_admin_can_create_inspection_chief(self):
        handler = Handler(self.users["admin"])
        handler.create_workflow_user({
            "username": "chief-new", "display_name": "Jefe",
            "password": "password-test", "role": "jefe_inspeccion",
        })
        self.assertEqual(handler.response[0], 201)
        self.assertEqual(handler.response[1]["workflow_role"], "jefe_inspeccion")


if __name__ == "__main__":
    unittest.main()
