import json
import re
from datetime import date, datetime, time, timezone

from database import connect_db

FIELDS = {
    "application_date": 10,
    "case_number": 100,
    "company_name": 255,
    "rut": 100,
    "request_type": 20,
    "inspection_type": 160,
    "practice": 160,
    "equipment_model": 200,
    "contact_details": 255,
    "phone": 100,
    "address": 255,
    "city": 160,
    "email": 254,
}


def _can_manage(user):
    return user["role"] == "admin" or user.get("workflow_role") in {
        "administracion", "jefe_inspeccion",
    }


def _is_chief(user):
    return user["role"] == "admin" or user.get("workflow_role") == "jefe_inspeccion"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _checklist(data):
    value = data.get("checklist")
    if not isinstance(value, list) or len(value) != 6 or any(type(item) is not bool for item in value):
        raise ValueError("El checklist debe contener seis casillas.")
    return json.dumps(value)


class InspectionRoutes:
    def get_inspections(self):
        connection = connect_db()
        try:
            managers = _can_manage(self.current_user)
            where = "" if managers else " WHERE inspections.inspector_id = ?"
            params = () if managers else (self.current_user["id"],)
            inspections = [
                dict(row) for row in connection.execute(
                    "SELECT inspections.*, users.display_name AS inspector_name "
                    "FROM inspections LEFT JOIN workflow_users AS users "
                    "ON users.id = inspections.inspector_id"
                    + where + " ORDER BY inspections.id DESC", params,
                )
            ]
            inspectors = []
            if _is_chief(self.current_user):
                inspectors = [
                    dict(row) for row in connection.execute(
                        "SELECT id, display_name FROM workflow_users "
                        "WHERE active = 1 AND workflow_role = 'inspeccion' "
                        "ORDER BY display_name COLLATE NOCASE, id"
                    )
                ]
            for inspection in inspections:
                inspection["pre_checklist"] = json.loads(inspection["pre_checklist"])
                inspection["post_checklist"] = json.loads(inspection["post_checklist"])
                inspection["events"] = []
            by_id = {inspection["id"]: inspection for inspection in inspections}
            if by_id:
                event_where = "" if managers else " WHERE inspections.inspector_id = ?"
                for event in connection.execute(
                    "SELECT events.inspection_id, events.action, events.comment, events.created_at, "
                    "users.display_name AS actor_name FROM inspection_events AS events "
                    "JOIN inspections ON inspections.id = events.inspection_id "
                    "JOIN workflow_users AS users ON users.id = events.actor_id"
                    + event_where + " ORDER BY events.id", params,
                ):
                    by_id[event["inspection_id"]]["events"].append(dict(event))
            self.send_json({
                "current_user": self.current_user,
                "can_create": managers,
                "inspectors": inspectors,
                "inspections": inspections,
            })
        finally:
            connection.close()

    def create_inspection(self, data):
        if not _can_manage(self.current_user):
            return self.send_error_json(
                "Solo Secretaría, Jefe de Inspección o un administrador pueden crear inspecciones.", 403
            )
        values = {}
        for field, limit in FIELDS.items():
            value = data.get(field, "")
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise ValueError(f"El campo {field} no es válido.")
            values[field] = value.strip()
        if values["application_date"]:
            try:
                if date.fromisoformat(values["application_date"]).isoformat() != values["application_date"]:
                    raise ValueError
            except ValueError:
                raise ValueError("La fecha de solicitud no es válida.") from None
        if values["request_type"] not in ("", "inicial", "renovacion"):
            raise ValueError("El tipo de solicitud no es válido.")
        if values["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+", values["email"]):
            raise ValueError("El correo no es válido.")
        connection = connect_db()
        try:
            columns = ", ".join(FIELDS)
            placeholders = ", ".join("?" for _ in FIELDS)
            cursor = connection.execute(
                f"INSERT INTO inspections (created_by, created_at, {columns}) "
                f"VALUES (?, ?, {placeholders})",
                (self.current_user["id"], _now(), *values.values()),
            )
            connection.execute(
                "INSERT INTO inspection_events (inspection_id, actor_id, action, created_at) "
                "VALUES (?, ?, 'created', ?)", (cursor.lastrowid, self.current_user["id"], _now()),
            )
            connection.commit()
            self.send_json({"id": cursor.lastrowid}, 201)
        finally:
            connection.close()

    def act_on_inspection(self, data):
        inspection_id = data.get("inspection_id")
        version = data.get("version")
        action = data.get("action")
        actions = {
            "assign": ("assignment", "pre_inspection", "chief"),
            "complete_pre": ("pre_inspection", "post_inspection", "inspector"),
            "complete_post": ("post_inspection", "chief_review", "inspector"),
            "return_post": ("chief_review", "post_inspection", "chief"),
            "approve": ("chief_review", "send", "chief"),
            "send": ("send", "completed", "inspector"),
        }
        if type(inspection_id) is not int or inspection_id <= 0 or type(version) is not int or version < 0:
            raise ValueError("La inspección o su versión no es válida.")
        if not isinstance(action, str) or action not in actions:
            raise ValueError("La acción indicada no es válida.")
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inspection = connection.execute(
                "SELECT * FROM inspections WHERE id = ?", (inspection_id,)
            ).fetchone()
            if not inspection:
                return self.send_error_json("No se encontró la inspección.", 404)
            source, target, required_role = actions[action]
            user = self.current_user
            permitted = _is_chief(user) if required_role == "chief" else (
                user["role"] == "admin" or
                (user.get("workflow_role") == "inspeccion" and inspection["inspector_id"] == user["id"])
            )
            if not permitted:
                return self.send_error_json("No tienes permiso para realizar esta acción.", 403)
            if inspection["stage"] != source or inspection["version"] != version:
                return self.send_error_json("La inspección cambió; actualiza e inténtalo nuevamente.", 409)

            updates = {"stage": target}
            comment = ""
            if action == "assign":
                inspector_id = data.get("inspector_id")
                if type(inspector_id) is not int or inspector_id <= 0 or not connection.execute(
                    "SELECT 1 FROM workflow_users WHERE id = ? AND active = 1 "
                    "AND workflow_role = 'inspeccion'", (inspector_id,),
                ).fetchone():
                    raise ValueError("Selecciona un inspector activo.")
                updates["inspector_id"] = inspector_id
            elif action in ("complete_pre", "complete_post"):
                updates["pre_checklist" if action == "complete_pre" else "post_checklist"] = _checklist(data)
                if action == "complete_pre":
                    scheduled_date = data.get("scheduled_date")
                    scheduled_time = data.get("scheduled_time")
                    place = data.get("scheduled_place")
                    try:
                        if not isinstance(scheduled_date, str) or date.fromisoformat(scheduled_date).isoformat() != scheduled_date:
                            raise ValueError
                        if not isinstance(scheduled_time, str) or not re.fullmatch(r"\d{2}:\d{2}", scheduled_time):
                            raise ValueError
                        time.fromisoformat(scheduled_time)
                    except ValueError:
                        raise ValueError("Ingresa una fecha y hora válidas para la inspección.") from None
                    if not isinstance(place, str) or not 1 <= len(place.strip()) <= 255:
                        raise ValueError("Ingresa el lugar de la inspección (máximo 255 caracteres).")
                    updates.update(scheduled_date=scheduled_date, scheduled_time=scheduled_time, scheduled_place=place.strip())
            elif action in ("return_post", "approve"):
                comment = data.get("comment", "")
                if not isinstance(comment, str) or len(comment.strip()) > 1000:
                    raise ValueError("Las observaciones deben tener hasta 1000 caracteres.")
                comment = comment.strip()
                if action == "return_post" and not comment:
                    raise ValueError("Indica el motivo de la devolución.")
            assignments = ", ".join(f"{key} = ?" for key in updates)
            connection.execute(
                f"UPDATE inspections SET {assignments}, version = version + 1 WHERE id = ?",
                (*updates.values(), inspection_id),
            )
            connection.execute(
                "INSERT INTO inspection_events (inspection_id, actor_id, action, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (inspection_id, user["id"], action, comment, _now()),
            )
            connection.commit()
            self.send_json({"id": inspection_id, "stage": target})
        finally:
            connection.close()
