import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

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
        self.wfile = BytesIO()

    def send_response(self, status):
        self.response = status, None

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

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
            ("coordinator", "coordinacion"),
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

    def requirement(self, user, requirement_id, action, **kwargs):
        handler = Handler(self.users[user])
        handler.act_on_requirement({"requirement_id": requirement_id, "action": action, **kwargs})
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
        pre = [True, True, True]
        self.assertEqual(self.act("inspector", inspection_id, 1, "complete_pre", checklist=pre, locomotion="taxi",
                                  scheduled_date="2026-11-24", scheduled_time="09:30",
                                  scheduled_place="Centro")[0], 200)
        for name in ("secretary", "chief", "inspector", "admin"):
            result = self.data(name)["inspections"][0]
            self.assertEqual((result["scheduled_date"], result["scheduled_time"], result["scheduled_place"]),
                             ("2026-11-24", "09:30", "Centro"))
            self.assertEqual(result["pre_checklist"], pre)
        self.assertEqual(self.data("other")["inspections"], [])
        self.assertEqual(self.data("lawyer")["inspections"], [])
        post = [True, True, False]
        self.assertEqual(self.act("inspector", inspection_id, 2, "complete_post", checklist=post,
                                  aris_not_applicable=True)[0], 200)
        with self.assertRaisesRegex(ValueError, "motivo"):
            self.act("chief", inspection_id, 3, "return_post", comment="  ")
        self.assertEqual(self.act("chief", inspection_id, 3, "return_post", comment="Corregir ítem")[0], 200)
        self.assertEqual(self.data("inspector")["inspections"][0]["post_checklist"], post)
        self.assertEqual(self.act("inspector", inspection_id, 4, "complete_post", checklist=[True] * 3)[0], 200)
        self.assertEqual(self.act("chief", inspection_id, 5, "approve", comment="Revisado")[0], 200)
        self.assertEqual(self.act("chief", inspection_id, 6, "send", send_mail_saved=True, next_inspection_date="2027-12-01")[0], 403)
        self.assertEqual(self.act("inspector", inspection_id, 6, "send", send_mail_saved=True, next_inspection_date="2027-12-01")[0], 200)
        database.initialize_db()
        result = self.data("chief")["inspections"][0]
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(result["post_checklist"], [True] * 3)
        self.assertEqual([event["action"] for event in result["events"]],
                         ["created", "assign", "complete_pre", "complete_post",
                          "return_post", "complete_post", "approve", "send"])
        self.assertEqual(result["events"][4]["comment"], "Corregir ítem")
        self.assertEqual(result["events"][-1]["actor_name"], "inspector")

    def test_checklist_items_are_saved_without_changing_stage(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        partial = [True, False, True]
        self.assertEqual(self.act("other", inspection_id, 1, "save_checklist", checklist=partial)[0], 403)
        status, payload = self.act("inspector", inspection_id, 1, "save_checklist", checklist=partial)
        self.assertEqual((status, payload["version"]), (200, 2))
        saved = self.data("inspector")["inspections"][0]
        self.assertEqual((saved["stage"], saved["pre_checklist"], saved["version"]),
                         ("pre_inspection", partial, 2))
        # Guardar lo mismo no genera una versión nueva.
        self.assertEqual(self.act("inspector", inspection_id, 2, "save_checklist", checklist=partial)[1]["version"], 2)
        self.assertEqual(self.act("inspector", inspection_id, 1, "save_checklist", checklist=[True] * 3)[0], 409)
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 2, "save_checklist", checklist=partial, locomotion="avion")
        self.assertEqual(self.act("inspector", inspection_id, 2, "save_checklist", checklist=partial,
                                  locomotion="taxi")[0], 200)
        self.assertEqual(self.data("inspector")["inspections"][0]["locomotion"], "taxi")
        # Con casillas sin marcar no deja agendar.
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 3, "complete_pre", checklist=partial, locomotion="taxi",
                     scheduled_date="2026-11-24", scheduled_time="09:30", scheduled_place="Centro")
        self.act("inspector", inspection_id, 3, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-11-24", scheduled_time="09:30", scheduled_place="Centro")
        self.assertEqual(self.act("inspector", inspection_id, 4, "save_checklist", checklist=[True] * 3)[0], 200)
        saved = self.data("inspector")["inspections"][0]
        self.assertEqual((saved["pre_checklist"], saved["post_checklist"]), ([True] * 3, [True] * 3))

    def test_auto_miem_creates_driver_task_without_blocking(self):
        inspection_id = self.create()
        self.create()  # otra inspección de otro inspector: Coordinación también la ve
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.assertEqual(len(self.data("coordinator")["inspections"]), 2)
        self.assertFalse(self.data("coordinator")["can_create"])
        with self.assertRaises(ValueError):
            self.act("coordinator", inspection_id, 1, "coordinate_driver", driver_name="Juan")
        self.act("inspector", inspection_id, 1, "save_checklist", checklist=[False] * 3, locomotion="auto_miem")
        seen = next(i for i in self.data("coordinator")["inspections"] if i["id"] == inspection_id)
        self.assertEqual(seen["driver_status"], "pending")
        # La inspección sigue avanzando aunque el chofer no esté coordinado.
        self.assertEqual(self.act("inspector", inspection_id, 2, "complete_pre", checklist=[True] * 3,
                                  locomotion="auto_miem", scheduled_date="2026-12-01",
                                  scheduled_time="10:00", scheduled_place="Sala 1")[0], 200)
        self.assertEqual(self.act("inspector", inspection_id, 3, "coordinate_driver", driver_name="Juan")[0], 403)
        with self.assertRaises(ValueError):
            self.act("coordinator", inspection_id, 3, "coordinate_driver", driver_name=" ")
        self.assertEqual(self.act("coordinator", inspection_id, 3, "coordinate_driver",
                                  driver_name=" Juan Pérez ")[0], 200)
        mine = self.data("inspector")["inspections"][0]
        self.assertEqual((mine["stage"], mine["driver_status"], mine["driver_name"]),
                         ("post_inspection", "coordinated", "Juan Pérez"))
        self.assertIn("Chofer: Pendiente de coordinar → Chofer coordinado", mine["events"][-1]["details_text"])
        # Coordinación no puede mover las etapas de la inspección.
        self.assertEqual(self.act("coordinator", inspection_id, 4, "complete_post", checklist=[True] * 3)[0], 403)

    def test_auto_miem_travel_times_are_saved_and_cleared(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 1, "save_checklist", checklist=[False] * 3,
                     locomotion="auto_miem", departure_time="25:00")
        self.act("inspector", inspection_id, 1, "save_checklist", checklist=[False] * 3,
                 locomotion="auto_miem", departure_time="08:15", arrival_time="09:00")
        seen = self.data("coordinator")["inspections"][0]
        self.assertEqual((seen["driver_status"], seen["departure_time"], seen["arrival_time"]),
                         ("pending", "08:15", "09:00"))
        self.act("inspector", inspection_id, 2, "save_checklist", checklist=[False] * 3, locomotion="propios")
        seen = self.data("coordinator")["inspections"][0]
        self.assertEqual((seen["departure_time"], seen["arrival_time"]), ("", ""))

    def test_every_inspector_sees_full_calendar_but_only_own_list(self):
        mine = self.create()
        theirs = self.create()
        self.create()  # sin agendar: no aparece en el calendario
        for inspection_id, name in ((mine, "inspector"), (theirs, "other")):
            self.act("chief", inspection_id, 0, "assign", inspector_id=self.users[name]["id"])
            self.act(name, inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                     scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        seen = self.data("inspector")
        self.assertEqual([item["id"] for item in seen["inspections"]], [mine])
        self.assertEqual(sorted(item["id"] for item in seen["calendar"]), sorted([mine, theirs]))
        other = next(item for item in seen["calendar"] if item["id"] == theirs)
        self.assertEqual(other["inspector_name"], "other")
        self.assertNotIn("events", other)

    def test_finished_inspections_are_visible_to_everyone(self):
        inspection_id = self.run_full_flow()
        in_progress = self.create()
        self.act("chief", in_progress, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.assertEqual(self.data("other")["inspections"], [])
        self.act("chief", inspection_id, 5, "approve")
        self.act("inspector", inspection_id, 6, "send", send_mail_saved=True, next_inspection_date="2027-12-01")
        seen = self.data("other")["inspections"]
        self.assertEqual([(item["id"], item["stage"]) for item in seen], [(inspection_id, "completed")])
        self.assertTrue(seen[0]["events"])
        self.assertEqual(self.act("other", inspection_id, 7, "save_checklist", checklist=[True] * 3)[0], 403)

    def test_inspector_reschedules_and_driver_is_coordinated_again(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        with self.assertRaises(ValueError):  # todavía no está agendada
            self.act("inspector", inspection_id, 1, "reschedule", scheduled_date="2026-12-05",
                     scheduled_time="09:00", scheduled_place="Sala 2")
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3,
                 locomotion="auto_miem", departure_time="08:00", scheduled_date="2026-12-01",
                 scheduled_time="10:00", scheduled_place="Sala 1")
        self.act("coordinator", inspection_id, 2, "coordinate_driver", driver_name="Juan")
        new = dict(scheduled_date="2026-12-05", scheduled_time="09:00", scheduled_place="Sala 1")
        self.assertEqual(self.act("other", inspection_id, 3, "reschedule", **new)[0], 403)
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 3, "reschedule", scheduled_date="2026-12-01",
                     scheduled_time="10:00", scheduled_place="Sala 1", departure_time="08:00")
        self.assertEqual(self.act("inspector", inspection_id, 3, "reschedule", departure_time="07:30",
                                  comment="Pidió otro día", **new)[0], 200)
        mine = self.data("inspector")["inspections"][0]
        self.assertEqual((mine["stage"], mine["scheduled_date"], mine["departure_time"], mine["driver_status"]),
                         ("post_inspection", "2026-12-05", "07:30", "pending"))
        last = mine["events"][-1]
        self.assertEqual((last["action"], last["comment"]), ("reschedule", "Pidió otro día"))
        self.assertIn("Fecha de inspección: 2026-12-01 → 2026-12-05", last["details_text"])
        self.assertIn("Chofer coordinado → Pendiente de coordinar", last["details_text"])

    def test_post_checklist_must_be_complete_or_aris_not_applicable(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        self.assertEqual(self.act("inspector", inspection_id, 2, "save_checklist", checklist=[True, False, False],
                                  aris_not_applicable=True)[0], 200)
        saved = self.data("inspector")["inspections"][0]
        self.assertEqual((saved["post_checklist"], saved["aris_not_applicable"]), ([True, False, False], 1))
        with self.assertRaisesRegex(ValueError, "Enviar por mail"):
            self.act("inspector", inspection_id, 3, "complete_post", checklist=[True, False, False],
                     aris_not_applicable=True)
        with self.assertRaisesRegex(ValueError, "ARIS"):
            self.act("inspector", inspection_id, 3, "complete_post", checklist=[True, True, False])
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 3, "complete_post", checklist=[True] * 3, aris_not_applicable="si")
        self.assertEqual(self.act("inspector", inspection_id, 3, "complete_post", checklist=[True, True, False],
                                  aris_not_applicable=True)[0], 200)
        self.assertEqual(self.data("inspector")["inspections"][0]["stage"], "chief_review")

    def test_requirements_are_added_one_by_one_and_visible_by_role(self):
        mine = self.create()
        theirs = self.create()
        for inspection_id, name in ((mine, "inspector"), (theirs, "other")):
            self.act("chief", inspection_id, 0, "assign", inspector_id=self.users[name]["id"])
        # Solo se agregan en la etapa del checklist posterior.
        self.assertEqual(self.act("inspector", mine, 1, "add_requirement", requirement_text="Blindaje",
                                  due_date="2026-12-10")[0], 409)
        for inspection_id, name in ((mine, "inspector"), (theirs, "other")):
            self.act(name, inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                     scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        for invalid in (dict(requirement_text=" ", due_date="2026-12-10"),
                        dict(requirement_text="Blindaje", due_date="2026-02-30")):
            with self.subTest(**invalid), self.assertRaises(ValueError):
                self.act("inspector", mine, 2, "add_requirement", **invalid)
        self.assertEqual(self.act("other", mine, 2, "add_requirement", requirement_text="x",
                                  due_date="2026-12-10")[0], 403)
        self.assertEqual(self.act("inspector", mine, 2, "add_requirement", requirement_text=" Blindaje ",
                                  due_date="2026-12-10")[0], 200)
        self.assertEqual(self.act("inspector", mine, 3, "add_requirement", requirement_text="Dosímetros",
                                  due_date="2026-12-20")[0], 200)
        self.act("other", theirs, 2, "add_requirement", requirement_text="Señalización", due_date="2026-12-15")
        seen = self.data("inspector")
        self.assertEqual([item["text"] for item in seen["requirements"]], ["Blindaje", "Dosímetros"])
        self.assertEqual(len(seen["inspections"][0]["requirements"]), 2)
        self.assertEqual(seen["inspections"][0]["stage"], "post_inspection")
        self.assertEqual([item["text"] for item in self.data("chief")["requirements"]],
                         ["Blindaje", "Señalización", "Dosímetros"])
        first = seen["requirements"][0]["id"]
        self.assertEqual(self.act("inspector", mine, 4, "remove_requirement", requirement_id=first)[0], 200)
        events = self.data("inspector")["inspections"][0]["events"]
        self.assertIn("Requerimiento: (vacío) → Blindaje (plazo 2026-12-10)", events[3]["details_text"])
        self.assertEqual(events[-1]["action"], "remove_requirement")
        self.assertEqual([item["text"] for item in self.data("inspector")["requirements"]], ["Dosímetros"])

    def test_reschedule_is_closed_once_post_checklist_starts(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        new = dict(scheduled_date="2026-12-05", scheduled_time="09:00", scheduled_place="Sala")
        self.assertEqual(self.act("inspector", inspection_id, 2, "reschedule", **new)[0], 200)
        self.act("inspector", inspection_id, 3, "save_checklist", checklist=[True, False, False])
        with self.assertRaisesRegex(ValueError, "checklist posterior"):
            self.act("inspector", inspection_id, 4, "reschedule", scheduled_date="2026-12-06",
                     scheduled_time="09:00", scheduled_place="Sala")
        self.act("inspector", inspection_id, 4, "save_checklist", checklist=[False] * 3)
        self.act("inspector", inspection_id, 5, "add_requirement", requirement_text="Blindaje", due_date="2026-12-10")
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 6, "reschedule", scheduled_date="2026-12-06",
                     scheduled_time="09:00", scheduled_place="Sala")

    def test_requirement_is_resolved_or_extended_from_calendar(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        self.act("inspector", inspection_id, 2, "add_requirement", requirement_text="Blindaje", due_date="2026-12-10")
        req = self.data("inspector")["requirements"][0]["id"]
        self.assertEqual(self.requirement("other", req, "resolve")[0], 403)
        self.assertEqual(self.requirement("coordinator", req, "resolve")[0], 403)
        for due in ("2026-12-09", "2026-12-10", "mañana"):
            with self.subTest(due=due), self.assertRaises(ValueError):
                self.requirement("inspector", req, "extend", due_date=due)
        self.assertEqual(self.requirement("inspector", req, "extend", due_date="2026-12-20")[0], 200)
        self.assertEqual(self.data("chief")["requirements"][0]["due_date"], "2026-12-20")
        self.assertEqual(self.requirement("chief", req, "resolve")[0], 200)
        resolved = self.data("inspector")["requirements"][0]
        self.assertTrue(resolved["resolved_at"])
        with self.assertRaises(ValueError):
            self.requirement("inspector", req, "extend", due_date="2026-12-30")
        self.assertEqual(self.requirement("inspector", req, "reopen")[0], 200)
        self.assertEqual(self.data("inspector")["requirements"][0]["resolved_at"], "")
        actions = [event["action"] for event in self.data("inspector")["inspections"][0]["events"]]
        self.assertEqual(actions[-3:], ["extend_requirement", "resolve_requirement", "reopen_requirement"])
        self.assertEqual(self.requirement("inspector", 9999, "resolve")[0], 404)

    def test_send_checklist_is_required_and_updates_planning_row(self):
        connection = database.connect_db()
        item_id = connection.execute(
            "INSERT INTO planning_items (item_key, sheet_institution, sheet_next_inspection) "
            "VALUES ('k1', 'Hospital', '2027-03-01')"
        ).lastrowid
        connection.commit()
        connection.close()
        handler = Handler(self.users["chief"])
        handler.create_inspection({"company_name": "Hospital", "planning_item_id": item_id,
                                   "inspector_id": self.users["inspector"]["id"]})
        self.assertEqual(handler.response[0], 201, handler.response)
        inspection_id = handler.response[1]["id"]
        v = self.data("inspector")["inspections"][0]["version"]
        self.act("inspector", inspection_id, v, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        self.act("inspector", inspection_id, v + 1, "complete_post", checklist=[True] * 3)
        self.act("chief", inspection_id, v + 2, "approve")
        v += 3
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, v, "save_checklist", next_inspection_date="2027-02-30")
        self.act("inspector", inspection_id, v, "save_checklist", next_inspection_date="2027-11-15")
        with self.assertRaisesRegex(ValueError, "Mail de envío"):
            self.act("inspector", inspection_id, v + 1, "send", next_inspection_date="2027-11-15")
        with self.assertRaisesRegex(ValueError, "próxima inspección"):
            self.act("inspector", inspection_id, v + 1, "send", send_mail_saved=True)

        def planning():
            connection = database.connect_db()
            try:
                row = connection.execute("SELECT manual_next_inspection FROM planning_items WHERE id = ?",
                                         (item_id,)).fetchone()
                event = connection.execute("SELECT action, details FROM planning_events WHERE item_id = ? "
                                           "ORDER BY id DESC", (item_id,)).fetchone()
                return row["manual_next_inspection"], event
            finally:
                connection.close()

        # El autoguardado no toca la planificación: recién se actualiza al registrar el envío.
        manual, event = planning()
        self.assertIsNone(manual)
        self.assertNotEqual(event and event["action"], "inspection_next")
        self.assertEqual(self.act("inspector", inspection_id, v + 1, "send", send_mail_saved=True,
                                  next_inspection_date="2027-11-15")[0], 200)
        manual, event = planning()
        self.assertEqual((manual, event["action"]), ("2027-11-15", "inspection_next"))
        self.assertIn("15/11/2027", event["details"])
        done = self.data("inspector")["inspections"][0]
        self.assertEqual((done["stage"], done["send_mail_saved"]), ("completed", 1))

    def test_requirements_visibility_and_removal_rules(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"],
                 second_inspector_id=self.users["other"]["id"])
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        self.act("inspector", inspection_id, 2, "add_requirement", requirement_text="Blindaje", due_date="2026-12-10")
        # El segundo inspector y Secretaría también ven los requerimientos.
        self.assertEqual(len(self.data("other")["inspections"][0]["requirements"]), 1)
        self.assertEqual(len(self.data("secretary")["requirements"]), 1)
        self.assertEqual(self.data("coordinator")["requirements"], [])
        with self.assertRaises(ValueError):
            self.act("inspector", inspection_id, 3, "remove_requirement", requirement_id=[1])
        req = self.data("inspector")["requirements"][0]["id"]
        self.requirement("chief", req, "resolve")
        with self.assertRaisesRegex(ValueError, "resuelto"):
            self.act("inspector", inspection_id, 3, "remove_requirement", requirement_id=req)

    def test_legacy_six_box_checklists_are_cleared_while_in_progress(self):
        pre_id, post_id, done_id = self.create(), self.create(), self.create()
        connection = database.connect_db()
        legacy = "[true, false, false, false, false, false]"
        for inspection_id, stage in ((pre_id, "pre_inspection"), (post_id, "post_inspection"),
                                     (done_id, "completed")):
            connection.execute("UPDATE inspections SET stage = ?, pre_checklist = ?, post_checklist = ? "
                               "WHERE id = ?", (stage, legacy, legacy, inspection_id))
        connection.commit()
        connection.close()
        database.initialize_db()
        connection = database.connect_db()
        rows = {row["id"]: (row["pre_checklist"], row["post_checklist"]) for row in connection.execute(
            "SELECT id, pre_checklist, post_checklist FROM inspections")}
        connection.close()
        self.assertEqual(rows[pre_id], ("[]", legacy))
        self.assertEqual(rows[post_id], (legacy, "[]"))
        self.assertEqual(rows[done_id], (legacy, legacy))

    def test_pending_requirements_are_counted_for_finished_inspections(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3, locomotion="taxi",
                 scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala")
        self.act("inspector", inspection_id, 2, "add_requirement", requirement_text="Blindaje", due_date="2026-12-10")
        self.act("inspector", inspection_id, 3, "complete_post", next_inspection_date="2027-12-01",
                 checklist=[True] * 3)
        self.act("chief", inspection_id, 4, "approve")
        self.act("inspector", inspection_id, 5, "send", send_mail_saved=True, next_inspection_date="2027-12-01")
        # Otro inspector no ve el requerimiento, pero sí que la finalizada tiene uno pendiente.
        seen = self.data("other")["inspections"][0]
        self.assertEqual((seen["stage"], seen["requirements"], seen["pending_requirements"]), ("completed", [], 1))
        req = self.data("inspector")["requirements"][0]["id"]
        self.requirement("inspector", req, "resolve")
        self.assertEqual(self.data("other")["inspections"][0]["pending_requirements"], 0)

    def test_other_locomotion_clears_driver_task(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.act("inspector", inspection_id, 1, "save_checklist", checklist=[False] * 3, locomotion="auto_miem")
        self.act("inspector", inspection_id, 2, "save_checklist", checklist=[False] * 3, locomotion="taxi")
        self.assertEqual(self.data("inspector")["inspections"][0]["driver_status"], "")

    def test_invalid_inputs_stale_actions_and_permissions(self):
        inspection_id = self.create()
        for invalid in (None, True, "1", self.users["inactive"]["id"], self.users["lawyer"]["id"]):
            with self.subTest(inspector=invalid), self.assertRaises(ValueError):
                self.act("chief", inspection_id, 0, "assign", inspector_id=invalid)
        self.assertEqual(self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])[0], 200)
        self.assertEqual(self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["other"]["id"])[0], 409)
        self.assertEqual(self.act("inspector", inspection_id, 1, "complete_post")[0], 409)
        valid = dict(checklist=[True] * 3, locomotion="taxi", scheduled_date="2026-11-24",
                     scheduled_time="09:30", scheduled_place="Centro")
        for field, value in (("checklist", [True] * 6), ("checklist", [1] * 3), ("locomotion", "avion"),
                             ("checklist", [True, False, True]), ("locomotion", ""),
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

    def run_full_flow(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3,
                 locomotion="auto_miem", scheduled_date="2026-12-01", scheduled_time="10:00", scheduled_place="Sala 1")
        self.act("inspector", inspection_id, 2, "complete_post", checklist=[True, True, False],
                 aris_not_applicable=True)
        self.act("chief", inspection_id, 3, "return_post", comment="Revisar")
        self.act("inspector", inspection_id, 4, "complete_post", checklist=[True] * 3)
        return inspection_id

    def test_history_records_what_changed_and_stages(self):
        inspection_id = self.run_full_flow()
        events = self.data("chief")["inspections"][0]["events"]
        self.assertIn("Número de trámite: 00012", events[0]["details_text"])
        self.assertEqual(events[1]["details_text"], "Inspector líder: (vacío) → inspector")
        self.assertIn("Fecha de inspección: (vacío) → 2026-12-01", events[2]["details_text"])
        self.assertIn("Checklist previo: (vacío) → 3 de 3 casillas marcadas", events[2]["details_text"])
        self.assertIn("2 de 3 casillas marcadas → 3 de 3 casillas marcadas", events[5]["details_text"])
        connection = database.connect_db()
        try:
            rows = connection.execute(
                "SELECT action, from_stage, to_stage FROM inspection_events "
                "WHERE inspection_id = ? ORDER BY id", (inspection_id,),
            ).fetchall()
            updated_at = connection.execute(
                "SELECT updated_at FROM inspections WHERE id = ?", (inspection_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(tuple(rows[4]), ("return_post", "chief_review", "post_inspection"))
        self.assertTrue(updated_at)

    def test_existing_history_is_kept_when_upgrading(self):
        legacy = Path(self.root.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        try:
            connection.execute(
                "CREATE TABLE inspection_events (id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL, "
                "actor_id INTEGER NOT NULL, action TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO inspection_events (inspection_id, actor_id, action, comment, created_at) "
                "VALUES (1, 1, 'approve', 'Antiguo', '2026-01-01')"
            )
            connection.commit()
        finally:
            connection.close()
        database.DATABASE = legacy
        database.initialize_db()
        connection = database.connect_db()
        try:
            row = connection.execute("SELECT action, comment, details FROM inspection_events").fetchone()
        finally:
            connection.close()
        self.assertEqual(tuple(row), ("approve", "Antiguo", "{}"))

    def export(self, user):
        handler = Handler(self.users[user])
        handler.export_inspections()
        return load_workbook(BytesIO(handler.wfile.getvalue()))

    def test_export_contains_inspections_and_history_as_text(self):
        handler = Handler(self.users["secretary"])
        handler.create_inspection({"case_number": "=1+1", "company_name": '=HYPERLINK("x")'})
        self.run_full_flow()
        workbook = self.export("chief")
        try:
            sheet = workbook["Inspecciones"]
            headings = [cell.value for cell in sheet[1]]
            self.assertIn("Número de trámite", headings)
            self.assertEqual(sheet.max_row, 3)
            case_column = headings.index("Número de trámite") + 1
            formula_cell = sheet.cell(row=2, column=case_column)
            self.assertEqual(formula_cell.value, "=1+1")
            self.assertEqual(formula_cell.data_type, "s")
            second = {heading: sheet.cell(row=3, column=index).value
                      for index, heading in enumerate(headings, start=1)}
            self.assertEqual(second["Inspector líder"], "inspector")
            self.assertEqual(second["Etapa"], "Revisión de Jefe de Inspección")
            self.assertEqual(second["Checklist posterior"], "3 de 3 casillas marcadas")
            history = workbook["Historial"]
            self.assertEqual(history.max_row, 1 + 1 + 6)
            for row in history.iter_rows():
                for cell in row:
                    self.assertNotEqual(cell.data_type, "f")
            actions = [history.cell(row=index, column=4).value for index in range(2, history.max_row + 1)]
            self.assertIn("Devuelto para corrección", actions)
        finally:
            workbook.close()

    def test_inspector_exports_only_assigned_inspections(self):
        self.create()
        self.run_full_flow()
        workbook = self.export("inspector")
        try:
            self.assertEqual(workbook["Inspecciones"].max_row, 2)
        finally:
            workbook.close()
        workbook = self.export("other")
        try:
            self.assertEqual(workbook["Inspecciones"].max_row, 1)
            self.assertEqual(workbook["Historial"].max_row, 1)
        finally:
            workbook.close()

    def test_chief_assigns_lead_and_second_inspector_when_creating(self):
        handler = Handler(self.users["chief"])
        with self.assertRaisesRegex(ValueError, "inspector líder"):
            handler.create_inspection({"case_number": "1"})
        with self.assertRaisesRegex(ValueError, "distinto"):
            handler.create_inspection({
                "inspector_id": self.users["inspector"]["id"],
                "second_inspector_id": self.users["inspector"]["id"],
            })
        with self.assertRaisesRegex(ValueError, "segundo inspector activo"):
            handler.create_inspection({
                "inspector_id": self.users["inspector"]["id"],
                "second_inspector_id": self.users["lawyer"]["id"],
            })
        handler.create_inspection({
            "case_number": "77", "inspector_id": self.users["inspector"]["id"],
            "second_inspector_id": self.users["other"]["id"],
        })
        self.assertEqual(handler.response[0], 201)
        self.assertEqual(handler.response[1]["stage"], "pre_inspection")
        inspection = self.data("chief")["inspections"][0]
        self.assertEqual(inspection["inspector_name"], "inspector")
        self.assertEqual(inspection["second_inspector_name"], "other")
        self.assertEqual(inspection["version"], 1)
        self.assertEqual([event["action"] for event in inspection["events"]], ["created", "assign"])
        self.assertIn("Segundo inspector: (vacío) → other", inspection["events"][1]["details_text"])
        # El líder continúa el flujo normal con la versión que recibió.
        self.assertEqual(self.act("inspector", inspection["id"], 1, "complete_pre",
                                  checklist=[True] * 3, locomotion="propios", scheduled_date="2026-12-02",
                                  scheduled_time="11:00", scheduled_place="Hospital")[0], 200)

    def test_only_chief_assigns_when_creating(self):
        handler = Handler(self.users["secretary"])
        handler.create_inspection({"inspector_id": self.users["inspector"]["id"]})
        self.assertEqual(handler.response[0], 403)
        handler.create_inspection({"case_number": "sin asignar"})
        self.assertEqual(handler.response[1]["stage"], "assignment")

    def test_second_inspector_sees_schedule_but_cannot_act(self):
        inspection_id = self.create()
        self.assertEqual(self.act("chief", inspection_id, 0, "assign",
                                  inspector_id=self.users["inspector"]["id"],
                                  second_inspector_id=self.users["other"]["id"])[0], 200)
        self.act("inspector", inspection_id, 1, "complete_pre", checklist=[True] * 3,
                 locomotion="taxi", scheduled_date="2026-12-03", scheduled_time="08:00", scheduled_place="Clínica")
        seen = self.data("other")["inspections"]
        self.assertEqual([item["id"] for item in seen], [inspection_id])
        self.assertEqual(seen[0]["scheduled_date"], "2026-12-03")
        self.assertTrue(seen[0]["events"])
        self.assertEqual(self.act("other", inspection_id, 2, "complete_post", checklist=[True] * 3)[0], 403)
        self.assertEqual(self.act("other", inspection_id, 2, "set_second",
                                  second_inspector_id=None)[0], 403)
        workbook = self.export("other")
        try:
            sheet = workbook["Inspecciones"]
            headings = [cell.value for cell in sheet[1]]
            self.assertEqual(sheet.max_row, 2)
            self.assertEqual(sheet.cell(row=2, column=headings.index("Segundo inspector") + 1).value, "other")
        finally:
            workbook.close()

    def test_chief_changes_or_removes_second_inspector_without_moving_stage(self):
        inspection_id = self.create()
        self.act("chief", inspection_id, 0, "assign", inspector_id=self.users["inspector"]["id"])
        self.assertEqual(self.act("chief", inspection_id, 1, "set_second",
                                  second_inspector_id=self.users["other"]["id"])[0], 200)
        with self.assertRaisesRegex(ValueError, "distinto"):
            self.act("chief", inspection_id, 2, "set_second",
                     second_inspector_id=self.users["inspector"]["id"])
        self.assertEqual(self.act("chief", inspection_id, 2, "set_second", second_inspector_id=None)[0], 200)
        inspection = self.data("chief")["inspections"][0]
        self.assertEqual(inspection["stage"], "pre_inspection")
        self.assertIsNone(inspection["second_inspector_id"])
        self.assertEqual(self.data("other")["inspections"], [])
        self.assertEqual(inspection["events"][-1]["details_text"], "Segundo inspector: other → (vacío)")


if __name__ == "__main__":
    unittest.main()


class CoordinatorAccessTests(unittest.TestCase):
    def test_coordinator_only_reaches_inspections(self):
        import server

        handler = server.RequestHandler.__new__(server.RequestHandler)
        handler.current_user = {"role": "user", "workflow_role": "coordinacion"}
        blocked = lambda path, method="GET": handler.coordinator_blocked(
            path, server.COORDINATOR_GET_PATHS if method == "GET" else server.COORDINATOR_POST_PATHS)
        self.assertFalse(blocked("/api/inspections"))
        self.assertFalse(blocked("/api/inspections/action", "POST"))
        self.assertFalse(blocked("/api/account/password", "POST"))
        for path in ("/api/workflow", "/api/users", "/api/entities", "/api/records",
                     "/api/inspections/export", "/api/inspections/planning"):
            self.assertTrue(blocked(path), path)
        self.assertTrue(blocked("/api/inspections", "POST"))
        handler.current_user = {"role": "user", "workflow_role": "inspeccion"}
        self.assertFalse(blocked("/api/workflow"))
