import json
import re
from datetime import date, datetime, time, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from database import connect_db
from routes.entities import _entity_areas, _entity_name_key

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


FIELD_LABELS = {
    "application_date": "Fecha de solicitud", "case_number": "Número de trámite",
    "company_name": "Institución o empresa", "rut": "RUT", "request_type": "Tipo de solicitud",
    "inspection_type": "Tipo de inspección", "practice": "Práctica",
    "equipment_model": "Modelo de equipo", "contact_details": "Datos de contacto",
    "phone": "Teléfono", "address": "Domicilio", "city": "Ciudad", "email": "Email",
    "inspector": "Inspector líder", "second_inspector": "Segundo inspector",
    "planning_item": "Planificación", "entity_name": "Entidad",
    "scheduled_date": "Fecha de inspección",
    "scheduled_time": "Hora de inspección", "scheduled_place": "Lugar de inspección",
    "pre_checklist": "Checklist previo", "post_checklist": "Checklist posterior",
    "aris_not_applicable": "ARIS no aplica",
    "next_inspection_date": "Próxima inspección",
    "send_mail_saved": "Mail de envío guardado en línea de tiempo",
    "requirement": "Requerimiento",
    "locomotion": "Locomoción",
    "driver_status": "Chofer", "driver_name": "Chofer asignado",
    "departure_time": "Hora de salida", "arrival_time": "Hora aprox. de llegada",
}
DRIVER_STATUS = {"pending": "Pendiente de coordinar", "coordinated": "Chofer coordinado"}
LOCOMOTION_OPTIONS = {"auto_miem": "Auto MIEM", "taxi": "Taxi", "propios": "Medios propios"}
PRE_CHECKLIST_ITEMS = ("Coordinar inspección con el usuario", "Locomoción", "Preparar documentación")
PRE_CHECKLIST_SIZE = len(PRE_CHECKLIST_ITEMS)
POST_CHECKLIST_ITEMS = (
    "Escanear documentación y guardarla en formato digital y físico en la carpeta correspondiente",
    "Enviar por mail el acta de inspección a Secretaría",
    "Ingreso en ARIS (solo equipos nuevos)",
)
POST_CHECKLIST_SIZE = len(POST_CHECKLIST_ITEMS)
STAGE_LABELS = {
    "assignment": "Asignación de inspector",
    "pre_inspection": "Checklist previo y agenda",
    "post_inspection": "Checklist posterior",
    "chief_review": "Revisión de Jefe de Inspección",
    "send": "Envío por inspector",
    "completed": "Enviada",
}
ACTION_LABELS = {
    "created": "Creación", "assign": "Inspector asignado",
    "complete_pre": "Checklist previo completado e inspección agendada",
    "complete_post": "Checklist posterior enviado al jefe",
    "return_post": "Devuelto para corrección", "approve": "Revisión aprobada",
    "send": "Envío registrado",
    "set_second": "Segundo inspector actualizado", "set_entity": "Entidad actualizada",
    "save_checklist": "Checklist guardado",
    "coordinate_driver": "Chofer coordinado",
    "reschedule": "Inspección re-coordinada",
    "add_requirement": "Requerimiento agregado",
    "remove_requirement": "Requerimiento eliminado",
    "resolve_requirement": "Requerimiento resuelto",
    "reopen_requirement": "Requerimiento reabierto",
    "extend_requirement": "Plazo de requerimiento extendido",
}
REQUEST_TYPES = {"inicial": "Autorización inicial", "renovacion": "Renovación"}


def _checklist_summary(value) -> str:
    items = json.loads(value) if isinstance(value, str) else value
    if not items:
        return ""
    return f"{sum(1 for item in items if item)} de {len(items)} casillas marcadas"


def _display(key: str, value) -> str:
    if key in ("pre_checklist", "post_checklist"):
        return _checklist_summary(value)
    if key == "request_type":
        return REQUEST_TYPES.get(value, value or "")
    if key in ("aris_not_applicable", "send_mail_saved"):
        return "Sí" if value else "No"
    if key == "locomotion":
        return LOCOMOTION_OPTIONS.get(value, value or "")
    if key == "driver_status":
        return DRIVER_STATUS.get(value, value or "")
    return "" if value is None else str(value)


def details_text(details: str | dict) -> str:
    """Texto legible del detalle de un evento, para el historial y el Excel."""
    values = json.loads(details) if isinstance(details, str) else details
    parts = []
    for key, value in values.items():
        label = FIELD_LABELS.get(key, key)
        if isinstance(value, list) and len(value) == 2:
            before, after = (_display(key, item) for item in value)
            parts.append(f"{label}: {before or '(vacío)'} → {after or '(vacío)'}")
        else:
            parts.append(f"{label}: {_display(key, value)}")
    return "; ".join(parts)


def _can_manage(user):
    return user["role"] == "admin" or user.get("workflow_role") in {
        "administracion", "jefe_inspeccion",
    }


def _is_coordinator(user):
    return user.get("workflow_role") == "coordinacion"


def _travel_time(data, key, label):
    value = data.get(key, "")
    if value == "":
        return ""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError
        time.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Ingresa una {label} válida.") from None
    return value


def _schedule(data):
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
    return {"scheduled_date": scheduled_date, "scheduled_time": scheduled_time,
            "scheduled_place": place.strip()}


def _driver_updates(inspection, locomotion, data):
    """Auto MIEM genera la tarea de coordinar chofer; otro medio la quita. No frena la inspección."""
    if locomotion == "auto_miem":
        updates = {
            "departure_time": _travel_time(data, "departure_time", "hora de salida"),
            "arrival_time": _travel_time(data, "arrival_time", "hora aproximada de llegada"),
        }
        if not inspection["driver_status"]:
            updates["driver_status"] = "pending"
        return updates
    return {"driver_status": "", "driver_name": "", "departure_time": "", "arrival_time": ""}


def _is_chief(user):
    return user["role"] == "admin" or user.get("workflow_role") == "jefe_inspeccion"


def _active_inspector(connection, value, label: str) -> int:
    if type(value) is not int or value <= 0 or not connection.execute(
        "SELECT 1 FROM workflow_users WHERE id = ? AND active = 1 "
        "AND workflow_role = 'inspeccion'", (value,),
    ).fetchone():
        raise ValueError(f"Selecciona un {label} activo.")
    return value


def _optional_second_inspector(connection, value, lead_id):
    if value in (None, "", 0):
        return None
    second_id = _active_inspector(connection, value, "segundo inspector")
    if second_id == lead_id:
        raise ValueError("El segundo inspector debe ser distinto del inspector líder.")
    return second_id


def entity_name_or_error(value) -> str:
    """Devuelve el nombre de la entidad tal como figura en sus carpetas, o "" si no hay."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 255:
        raise ValueError("La entidad no es válida.")
    value = value.strip()
    if not value:
        return ""
    key = _entity_name_key(value)
    for area in _entity_areas()[0]:
        if _entity_name_key(area["institution"]) == key:
            return area["institution"]
    raise ValueError("La entidad no existe en «3 Entidades Usuarias» o no está disponible.")


def _link_planning_entity(connection, item_id, entity_name, actor_id, now, inspection_id=None):
    """Asocia la entidad a una fila de planificación sin entidad y sube su versión,
    para que un editor abierto con la fila vieja no la borre al guardar."""
    connection.execute(
        "UPDATE planning_items SET entity_name = ?, version = version + 1, updated_at = ?, "
        "updated_by = ? WHERE id = ?",
        (entity_name, now, actor_id, item_id),
    )
    if inspection_id is not None:
        connection.execute(
            "INSERT INTO planning_events (item_id, actor_id, action, created_at, details) "
            "VALUES (?, ?, 'edit', ?, ?)",
            (item_id, actor_id, now, json.dumps(
                {"entity_name": ["", entity_name], "inspection": f"#{inspection_id}"},
                ensure_ascii=False,
            )),
        )


def _next_inspection_date(data):
    value = data.get("next_inspection_date", "")
    if value == "":
        return ""
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("Ingresa una fecha válida para la próxima inspección.") from None
    return value


def _apply_next_to_planning(connection, item_id, value, actor_id, now, inspection_id):
    """Lleva la fecha de la próxima inspección a su fila de planificación."""
    row = connection.execute(
        "SELECT sheet_next_inspection, manual_next_inspection FROM planning_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not row:
        return
    before = row["sheet_next_inspection"] if row["manual_next_inspection"] is None \
        else row["manual_next_inspection"]
    if before == value:
        return
    # Si coincide con la planilla, deja de ser una corrección manual.
    manual = None if value == row["sheet_next_inspection"] else value
    connection.execute(
        "UPDATE planning_items SET manual_next_inspection = ?, version = version + 1, "
        "updated_at = ?, updated_by = ? WHERE id = ?",
        (manual, now, actor_id, item_id),
    )
    day = lambda iso: f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso else ""
    connection.execute(
        "INSERT INTO planning_events (item_id, actor_id, action, created_at, details) "
        "VALUES (?, ?, 'inspection_next', ?, ?)",
        (item_id, actor_id, now, json.dumps(
            {"next_inspection": [day(before), day(value)], "inspection": f"#{inspection_id}"},
            ensure_ascii=False,
        )),
    )


def _user_names(connection, *ids) -> dict:
    wanted = [value for value in ids if value]
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    return dict(connection.execute(
        f"SELECT id, display_name FROM workflow_users WHERE id IN ({placeholders})", wanted,
    ).fetchall())


def _list_visibility(user):
    """Como _visibility, pero las inspecciones finalizadas (enviadas) las ven todos."""
    where, params = _visibility(user)
    if not where:
        return where, params
    return where[:-1] + " OR inspections.stage = 'completed')", params


def _visibility(user):
    """Filtro SQL: quien gestiona ve todas; cada inspector, las suyas como líder o segundo."""
    if _can_manage(user) or _is_coordinator(user):
        return "", ()
    return (
        " WHERE (inspections.inspector_id = ? OR inspections.second_inspector_id = ?)",
        (user["id"], user["id"]),
    )


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _checklist(data, size):
    value = data.get("checklist")
    if not isinstance(value, list) or len(value) != size or any(type(item) is not bool for item in value):
        raise ValueError(f"El checklist debe contener {size} casillas.")
    return json.dumps(value)


def _send_mail_saved(data):
    value = data.get("send_mail_saved", False)
    if type(value) is not bool:
        raise ValueError("Indica si el mail de envío quedó guardado.")
    return int(value)


def _aris_not_applicable(data):
    value = data.get("aris_not_applicable", False)
    if type(value) is not bool:
        raise ValueError("Indica si el ingreso en ARIS aplica.")
    return int(value)


def _requirement(data):
    text = data.get("requirement_text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
        raise ValueError("Escribe el requerimiento (máximo 1000 caracteres).")
    due = data.get("due_date")
    try:
        if not isinstance(due, str) or date.fromisoformat(due).isoformat() != due:
            raise ValueError
    except ValueError:
        raise ValueError("Ingresa un plazo válido para el requerimiento.") from None
    return text.strip(), due


def _post_checklist_started(connection, inspection):
    """El checklist posterior se empezó si hay alguna casilla, el "No aplica" o un requerimiento."""
    if any(json.loads(inspection["post_checklist"] or "[]")) or inspection["aris_not_applicable"]:
        return True
    return connection.execute(
        "SELECT 1 FROM inspection_requirements WHERE inspection_id = ? LIMIT 1", (inspection["id"],),
    ).fetchone() is not None


def _requirement_visibility(user):
    """Jefe de Inspección, Secretaría y admin ven todos; cada inspector, los que ingresó
    y los de las inspecciones donde es líder o segundo."""
    if _can_manage(user):
        return "", ()
    return (" WHERE (requirements.created_by = ? OR inspections.inspector_id = ? "
            "OR inspections.second_inspector_id = ?)", (user["id"], user["id"], user["id"]))


def _locomotion(data):
    value = data.get("locomotion", "")
    if value not in ("", *LOCOMOTION_OPTIONS):
        raise ValueError("El medio de locomoción no es válido.")
    return value


class InspectionRoutes:
    def get_inspections(self):
        connection = connect_db()
        try:
            managers = _can_manage(self.current_user)
            where, params = _list_visibility(self.current_user)
            inspections = [
                dict(row) for row in connection.execute(
                    "SELECT inspections.*, users.display_name AS inspector_name, "
                    "second.display_name AS second_inspector_name "
                    "FROM inspections LEFT JOIN workflow_users AS users "
                    "ON users.id = inspections.inspector_id "
                    "LEFT JOIN workflow_users AS second ON second.id = inspections.second_inspector_id"
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
                event_where = where
                for event in connection.execute(
                    "SELECT events.inspection_id, events.action, events.comment, events.created_at, "
                    "events.details, users.display_name AS actor_name FROM inspection_events AS events "
                    "JOIN inspections ON inspections.id = events.inspection_id "
                    "JOIN workflow_users AS users ON users.id = events.actor_id"
                    + event_where + " ORDER BY events.id", params,
                ):
                    item = dict(event)
                    item["details_text"] = details_text(item.pop("details") or "{}")
                    by_id[event["inspection_id"]]["events"].append(item)
            # Calendario: todos ven todas las inspecciones agendadas, con datos básicos.
            calendar = [
                dict(row) for row in connection.execute(
                    "SELECT inspections.id, inspections.company_name, inspections.case_number, "
                    "inspections.stage, inspections.scheduled_date, inspections.scheduled_time, "
                    "inspections.scheduled_place, users.display_name AS inspector_name, "
                    "second.display_name AS second_inspector_name "
                    "FROM inspections LEFT JOIN workflow_users AS users "
                    "ON users.id = inspections.inspector_id "
                    "LEFT JOIN workflow_users AS second ON second.id = inspections.second_inspector_id "
                    "WHERE inspections.scheduled_date != '' ORDER BY inspections.scheduled_date, "
                    "inspections.scheduled_time"
                )
            ]
            req_where, req_params = _requirement_visibility(self.current_user)
            requirements = [
                dict(row) for row in connection.execute(
                    "SELECT requirements.id, requirements.inspection_id, requirements.text, "
                    "requirements.due_date, requirements.created_at, requirements.resolved_at, "
                    "requirements.created_by, "
                    "users.display_name AS created_by_name, inspections.company_name, "
                    "inspections.case_number, inspector.display_name AS inspector_name "
                    "FROM inspection_requirements AS requirements "
                    "JOIN inspections ON inspections.id = requirements.inspection_id "
                    "JOIN workflow_users AS users ON users.id = requirements.created_by "
                    "LEFT JOIN workflow_users AS inspector ON inspector.id = inspections.inspector_id"
                    + req_where + " ORDER BY requirements.due_date, requirements.id", req_params,
                )
            ]
            # Requerimientos sin resolver de cada inspección (sin importar quién los ve),
            # para separar "Finalizadas" de "Finalizadas con requerimientos".
            pending = dict(connection.execute(
                "SELECT inspection_id, COUNT(*) FROM inspection_requirements "
                "WHERE resolved_at = '' GROUP BY inspection_id"
            ).fetchall())
            for inspection in inspections:
                inspection["requirements"] = []
                inspection["pending_requirements"] = pending.get(inspection["id"], 0)
            for item in requirements:
                if item["inspection_id"] in by_id:
                    by_id[item["inspection_id"]]["requirements"].append(item)
            self.send_json({
                "current_user": self.current_user,
                "can_create": managers,
                "calendar": calendar,
                "requirements": requirements,
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
        user = self.current_user
        lead_value = data.get("inspector_id")
        second_value = data.get("second_inspector_id")
        if lead_value in ("", 0):
            lead_value = None
        if second_value in ("", 0):
            second_value = None
        if (lead_value is not None or second_value is not None) and not _is_chief(user):
            return self.send_error_json(
                "Solo Jefe de Inspección o un administrador pueden asignar inspectores.", 403
            )
        if user.get("workflow_role") == "jefe_inspeccion" and lead_value is None:
            raise ValueError("Selecciona el inspector líder de la inspección.")
        if second_value is not None and lead_value is None:
            raise ValueError("Selecciona primero el inspector líder.")
        entity_name = entity_name_or_error(data.get("entity_name"))
        planning_value = data.get("planning_item_id")
        if planning_value in ("", 0):
            planning_value = None
        if planning_value is not None:
            if not _is_chief(user):
                return self.send_error_json(
                    "Solo Jefe de Inspección o un administrador agendan desde la planificación.", 403
                )
            if type(planning_value) is not int:
                raise ValueError("La fila de planificación no es válida.")
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lead_id = (
                _active_inspector(connection, lead_value, "inspector líder")
                if lead_value is not None else None
            )
            second_id = _optional_second_inspector(connection, second_value, lead_id)
            planning_row = None
            if planning_value is not None:
                planning_row = connection.execute(
                    "SELECT id, COALESCE(manual_institution, sheet_institution) AS institution, "
                    "COALESCE(manual_practice, sheet_practice) AS practice, "
                    "COALESCE(manual_department, sheet_department) AS department, entity_name "
                    "FROM planning_items WHERE id = ?", (planning_value,),
                ).fetchone()
                if not planning_row:
                    raise ValueError("La fila de planificación ya no existe.")
                if not entity_name:
                    entity_name = planning_row["entity_name"]
            columns = ", ".join(FIELDS)
            placeholders = ", ".join("?" for _ in FIELDS)
            now = _now()
            stage = "pre_inspection" if lead_id else "assignment"
            cursor = connection.execute(
                f"INSERT INTO inspections (created_by, created_at, updated_at, stage, "
                f"inspector_id, second_inspector_id, version, planning_item_id, entity_name, "
                f"{columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {placeholders})",
                (user["id"], now, now, stage, lead_id, second_id, 1 if lead_id else 0,
                 planning_value, entity_name, *values.values()),
            )
            inspection_id = cursor.lastrowid
            created_details = {key: value for key, value in values.items() if value}
            if entity_name:
                created_details["entity_name"] = entity_name
                from routes.entity_profiles import fill_profile_from_inspection
                profile_values = dict(values)
                if planning_row:
                    # El departamento de la planilla va al campo Departamento de la ficha.
                    profile_values["department"] = planning_row["department"]
                fill_profile_from_inspection(
                    connection, entity_name, profile_values, inspection_id, user["id"],
                )
            if planning_row:
                scheduled = {"inspection": f"#{inspection_id}"}
                if entity_name and not planning_row["entity_name"]:
                    # La entidad elegida queda asociada también a la fila de planificación.
                    _link_planning_entity(connection, planning_row["id"], entity_name, user["id"], now)
                    scheduled["entity_name"] = ["", entity_name]
                created_details["planning_item"] = (
                    f"{planning_row['institution']} · {planning_row['practice']}"
                )
                connection.execute(
                    "INSERT INTO planning_events (item_id, actor_id, action, created_at, details) "
                    "VALUES (?, ?, 'scheduled', ?, ?)",
                    (planning_row["id"], user["id"], now,
                     json.dumps(scheduled, ensure_ascii=False)),
                )
            connection.execute(
                "INSERT INTO inspection_events "
                "(inspection_id, actor_id, action, created_at, to_stage, details) "
                "VALUES (?, ?, 'created', ?, 'assignment', ?)",
                (
                    inspection_id, user["id"], now,
                    json.dumps(created_details, ensure_ascii=False),
                ),
            )
            if lead_id:
                # El jefe asigna al crear: queda registrado como la asignación habitual.
                names = _user_names(connection, lead_id, second_id)
                changes = {"inspector": ["", names.get(lead_id, "")]}
                if second_id:
                    changes["second_inspector"] = ["", names.get(second_id, "")]
                connection.execute(
                    "INSERT INTO inspection_events (inspection_id, actor_id, action, "
                    "created_at, from_stage, to_stage, details) "
                    "VALUES (?, ?, 'assign', ?, 'assignment', 'pre_inspection', ?)",
                    (inspection_id, user["id"], now, json.dumps(changes, ensure_ascii=False)),
                )
            connection.commit()
            self.send_json({"id": inspection_id, "stage": stage}, 201)
        finally:
            connection.close()

    def act_on_requirement(self, data):
        """Resolver un requerimiento o extender su plazo, en cualquier etapa de la inspección."""
        requirement_id = data.get("requirement_id")
        action = data.get("action")
        if type(requirement_id) is not int or requirement_id <= 0:
            raise ValueError("El requerimiento no es válido.")
        if action not in ("resolve", "extend", "reopen"):
            raise ValueError("La acción indicada no es válida.")
        user = self.current_user
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT requirements.*, inspections.inspector_id, inspections.stage "
                "FROM inspection_requirements AS requirements "
                "JOIN inspections ON inspections.id = requirements.inspection_id "
                "WHERE requirements.id = ?", (requirement_id,),
            ).fetchone()
            if not row:
                return self.send_error_json("No se encontró el requerimiento.", 404)
            permitted = _is_chief(user) or (
                user.get("workflow_role") == "inspeccion"
                and user["id"] in (row["inspector_id"], row["created_by"])
            )
            if not permitted:
                return self.send_error_json("No tienes permiso para modificar este requerimiento.", 403)
            label = f"{row['text']} (plazo {row['due_date']})"
            now = _now()
            if action == "resolve":
                if row["resolved_at"]:
                    raise ValueError("El requerimiento ya está resuelto.")
                connection.execute(
                    "UPDATE inspection_requirements SET resolved_at = ?, resolved_by = ? WHERE id = ?",
                    (now, user["id"], requirement_id),
                )
                changes = {"requirement": [label, f"{label} · resuelto"]}
                event = "resolve_requirement"
            elif action == "reopen":
                if not row["resolved_at"]:
                    raise ValueError("El requerimiento no está resuelto.")
                connection.execute(
                    "UPDATE inspection_requirements SET resolved_at = '', resolved_by = NULL WHERE id = ?",
                    (requirement_id,),
                )
                changes = {"requirement": [f"{label} · resuelto", label]}
                event = "reopen_requirement"
            else:
                if row["resolved_at"]:
                    raise ValueError("El requerimiento ya está resuelto.")
                due = data.get("due_date")
                try:
                    if not isinstance(due, str) or date.fromisoformat(due).isoformat() != due:
                        raise ValueError
                except ValueError:
                    raise ValueError("Ingresa un plazo válido para el requerimiento.") from None
                if due <= row["due_date"]:
                    raise ValueError("El nuevo plazo debe ser posterior al actual.")
                connection.execute(
                    "UPDATE inspection_requirements SET due_date = ? WHERE id = ?", (due, requirement_id),
                )
                changes = {"requirement": [label, f"{row['text']} (plazo {due})"]}
                event = "extend_requirement"
            comment = data.get("comment", "")
            if not isinstance(comment, str) or len(comment.strip()) > 1000:
                raise ValueError("El comentario debe tener hasta 1000 caracteres.")
            connection.execute(
                "INSERT INTO inspection_events (inspection_id, actor_id, action, comment, "
                "created_at, from_stage, to_stage, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["inspection_id"], user["id"], event, comment.strip(), now, row["stage"], row["stage"],
                 json.dumps(changes, ensure_ascii=False)),
            )
            connection.commit()
            self.send_json({"id": requirement_id, "action": action})
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
            # Cambia solo el segundo inspector; no mueve la etapa.
            "set_second": (None, None, "chief"),
            # Asocia o cambia la entidad usuaria; no mueve la etapa.
            "set_entity": (None, None, "manager"),
            # Guarda las casillas marcadas del checklist en curso; no mueve la etapa.
            "save_checklist": (None, None, "inspector"),
            # Coordinación confirma el chofer del Auto MIEM; no mueve la etapa.
            "coordinate_driver": (None, None, "coordinator"),
            # El inspector cambia fecha, hora o lugar de una inspección ya agendada; no mueve la etapa.
            "reschedule": (None, None, "inspector"),
            # Requerimientos del checklist posterior; no mueven la etapa.
            "add_requirement": ("post_inspection", "post_inspection", "inspector"),
            "remove_requirement": ("post_inspection", "post_inspection", "inspector"),
        }
        if type(inspection_id) is not int or inspection_id <= 0 or type(version) is not int or version < 0:
            raise ValueError("La inspección o su versión no es válida.")
        if not isinstance(action, str) or action not in actions:
            raise ValueError("La acción indicada no es válida.")
        # La entidad se valida antes de bloquear la base: puede recorrer las carpetas.
        new_entity = entity_name_or_error(data.get("entity_name")) if action == "set_entity" else ""
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
            permitted = _is_chief(user) if required_role == "chief" else _can_manage(user) \
                if required_role == "manager" else (
                user["role"] == "admin" or
                (user.get("workflow_role") == "inspeccion" and inspection["inspector_id"] == user["id"])
            )
            if required_role == "coordinator":
                permitted = user["role"] == "admin" or _is_coordinator(user)
            if not permitted:
                return self.send_error_json("No tienes permiso para realizar esta acción.", 403)
            if action == "set_second":
                if inspection["stage"] == "completed":
                    raise ValueError("La inspección ya fue enviada.")
                source = target = inspection["stage"]
            if action == "set_entity":
                source = target = inspection["stage"]
            if action == "reschedule":
                if inspection["stage"] != "post_inspection" or _post_checklist_started(connection, inspection):
                    raise ValueError(
                        "Solo se puede re-coordinar una inspección agendada antes de empezar el checklist posterior."
                    )
                source = target = inspection["stage"]
            if action == "coordinate_driver":
                if inspection["driver_status"] not in ("pending", "coordinated"):
                    raise ValueError("Esta inspección no tiene un chofer por coordinar.")
                source = target = inspection["stage"]
            if action == "save_checklist":
                if inspection["stage"] not in ("pre_inspection", "post_inspection", "send"):
                    raise ValueError("El checklist no se puede modificar en esta etapa.")
                source = target = inspection["stage"]
            if inspection["stage"] != source or inspection["version"] != version:
                return self.send_error_json("La inspección cambió; actualiza e inténtalo nuevamente.", 409)

            updates = {"stage": target}
            comment = ""
            extra_changes = {}
            requirement = None
            removed_requirement = None
            if action == "save_checklist":
                if target == "pre_inspection":
                    updates["pre_checklist"] = _checklist(data, PRE_CHECKLIST_SIZE)
                    updates["locomotion"] = _locomotion(data)
                    updates.update(_driver_updates(inspection, updates["locomotion"], data))
                elif target == "send":
                    updates["send_mail_saved"] = _send_mail_saved(data)
                    updates["next_inspection_date"] = _next_inspection_date(data)
                else:
                    updates["post_checklist"] = _checklist(data, POST_CHECKLIST_SIZE)
                    updates["aris_not_applicable"] = _aris_not_applicable(data)
                if all(inspection[key] == value for key, value in updates.items()):
                    connection.rollback()
                    return self.send_json({"id": inspection_id, "stage": target, "version": version})
            elif action == "add_requirement":
                requirement = _requirement(data)
                extra_changes = {"requirement": ["", f"{requirement[0]} (plazo {requirement[1]})"]}
            elif action == "remove_requirement":
                requirement_id = data.get("requirement_id")
                if type(requirement_id) is not int or requirement_id <= 0:
                    raise ValueError("El requerimiento no es válido.")
                row = connection.execute(
                    "SELECT id, text, due_date, resolved_at FROM inspection_requirements "
                    "WHERE id = ? AND inspection_id = ?",
                    (requirement_id, inspection_id),
                ).fetchone()
                if not row:
                    raise ValueError("No se encontró el requerimiento.")
                if row["resolved_at"]:
                    raise ValueError("Un requerimiento resuelto no se puede quitar; reábrelo primero.")
                removed_requirement = row["id"]
                extra_changes = {"requirement": [f"{row['text']} (plazo {row['due_date']})", ""]}
            elif action == "reschedule":
                updates.update(_schedule(data))
                if inspection["locomotion"] == "auto_miem":
                    updates.update(
                        departure_time=_travel_time(data, "departure_time", "hora de salida"),
                        arrival_time=_travel_time(data, "arrival_time", "hora aproximada de llegada"),
                    )
                if all(inspection[key] == value for key, value in updates.items()):
                    raise ValueError("No cambiaste la fecha, la hora ni el lugar.")
                # Con Auto MIEM, Coordinación tiene que volver a confirmar el chofer.
                if inspection["driver_status"] == "coordinated":
                    updates["driver_status"] = "pending"
                comment = data.get("comment", "")
                if not isinstance(comment, str) or len(comment.strip()) > 1000:
                    raise ValueError("El motivo debe tener hasta 1000 caracteres.")
                comment = comment.strip()
            elif action == "coordinate_driver":
                driver = data.get("driver_name")
                if not isinstance(driver, str) or not 1 <= len(driver.strip()) <= 120:
                    raise ValueError("Indica qué chofer viene (máximo 120 caracteres).")
                updates.update(driver_status="coordinated", driver_name=driver.strip())
            elif action == "assign":
                inspector_id = _active_inspector(connection, data.get("inspector_id"), "inspector")
                updates["inspector_id"] = inspector_id
                updates["second_inspector_id"] = _optional_second_inspector(
                    connection, data.get("second_inspector_id"), inspector_id,
                )
            elif action == "set_second":
                if not inspection["inspector_id"] and data.get("second_inspector_id") not in (None, "", 0):
                    raise ValueError("Selecciona primero el inspector líder.")
                updates["second_inspector_id"] = _optional_second_inspector(
                    connection, data.get("second_inspector_id"), inspection["inspector_id"],
                )
            elif action == "set_entity":
                updates["entity_name"] = new_entity
                if new_entity:
                    from routes.entity_profiles import fill_profile_from_inspection
                    fill_profile_from_inspection(
                        connection, new_entity, inspection, inspection_id, user["id"],
                    )
                if new_entity and inspection["planning_item_id"]:
                    planning_row = connection.execute(
                        "SELECT id FROM planning_items WHERE id = ? AND entity_name = ''",
                        (inspection["planning_item_id"],),
                    ).fetchone()
                    if planning_row:
                        _link_planning_entity(
                            connection, planning_row["id"], new_entity, user["id"], _now(),
                            inspection_id,
                        )
            elif action == "send":
                # Checklist de envío: obligatorio antes de registrar el envío.
                updates["send_mail_saved"] = _send_mail_saved(data)
                updates["next_inspection_date"] = _next_inspection_date(data)
                missing = []
                if not updates["send_mail_saved"]:
                    missing.append("marcar «Mail de envío guardado en línea de tiempo»")
                if not updates["next_inspection_date"]:
                    missing.append("cargar la fecha de la próxima inspección")
                if missing:
                    raise ValueError("Para registrar el envío falta: " + "; ".join(missing) + ".")
            elif action in ("complete_pre", "complete_post"):
                if action == "complete_pre":
                    updates["pre_checklist"] = _checklist(data, PRE_CHECKLIST_SIZE)
                    updates["locomotion"] = _locomotion(data)
                    updates.update(_driver_updates(inspection, updates["locomotion"], data))
                else:
                    updates["post_checklist"] = _checklist(data, POST_CHECKLIST_SIZE)
                    updates["aris_not_applicable"] = _aris_not_applicable(data)
                    done = json.loads(updates["post_checklist"])
                    done[2] = done[2] or bool(updates["aris_not_applicable"])
                    missing = [f"marcar «{name}»" for name, ok in zip(POST_CHECKLIST_ITEMS, done) if not ok]
                    if missing:
                        raise ValueError("Para enviar al jefe falta: " + "; ".join(missing) + ".")
                if action == "complete_pre":
                    missing = [f"marcar «{name}»" for name, done in zip(PRE_CHECKLIST_ITEMS, json.loads(updates["pre_checklist"]))
                               if not done]
                    if not updates["locomotion"]:
                        missing.append("elegir el medio de locomoción")
                    if missing:
                        raise ValueError("Para agendar falta: " + "; ".join(missing) + ".")
                    updates.update(_schedule(data))
            elif action in ("return_post", "approve"):
                comment = data.get("comment", "")
                if not isinstance(comment, str) or len(comment.strip()) > 1000:
                    raise ValueError("Las observaciones deben tener hasta 1000 caracteres.")
                comment = comment.strip()
                if action == "return_post" and not comment:
                    raise ValueError("Indica el motivo de la devolución.")
            # Se guarda el valor anterior y el nuevo de cada dato que cambia.
            changes = {}
            people = {"inspector_id": "inspector", "second_inspector_id": "second_inspector"}
            for key, value in updates.items():
                if key == "stage" or key in people or inspection[key] == value:
                    continue
                changes[key] = [inspection[key], value]
            for key, label in people.items():
                if key in updates and inspection[key] != updates[key]:
                    names = _user_names(connection, inspection[key], updates[key])
                    changes[label] = [names.get(inspection[key], ""), names.get(updates[key], "")]
            changes.update(extra_changes)
            now = _now()
            # La fecha pasa a la planificación recién al registrar el envío (no en cada autoguardado).
            next_date = updates.get("next_inspection_date")
            if action == "send" and next_date and inspection["planning_item_id"]:
                _apply_next_to_planning(connection, inspection["planning_item_id"], next_date,
                                        user["id"], now, inspection_id)
            if requirement:
                connection.execute(
                    "INSERT INTO inspection_requirements (inspection_id, text, due_date, created_by, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (inspection_id, requirement[0], requirement[1], user["id"], now),
                )
            if removed_requirement:
                connection.execute("DELETE FROM inspection_requirements WHERE id = ?", (removed_requirement,))
            assignments = ", ".join(f"{key} = ?" for key in updates)
            connection.execute(
                f"UPDATE inspections SET {assignments}, updated_at = ?, version = version + 1 "
                "WHERE id = ?",
                (*updates.values(), now, inspection_id),
            )
            connection.execute(
                "INSERT INTO inspection_events (inspection_id, actor_id, action, comment, "
                "created_at, from_stage, to_stage, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (inspection_id, user["id"], action, comment, now, source, target,
                 json.dumps(changes, ensure_ascii=False)),
            )
            connection.commit()
            self.send_json({"id": inspection_id, "stage": target, "version": version + 1})
        finally:
            connection.close()

    def export_inspections(self):
        where, params = _visibility(self.current_user)
        connection = connect_db()
        try:
            connection.execute("BEGIN")
            inspections = connection.execute(
                "SELECT inspections.*, inspector.display_name AS inspector_name, "
                "second.display_name AS second_inspector_name, "
                "creator.display_name AS created_by_name FROM inspections "
                "LEFT JOIN workflow_users AS inspector ON inspector.id = inspections.inspector_id "
                "LEFT JOIN workflow_users AS second ON second.id = inspections.second_inspector_id "
                "LEFT JOIN workflow_users AS creator ON creator.id = inspections.created_by"
                + where + " ORDER BY inspections.id", params,
            ).fetchall()
            events = connection.execute(
                "SELECT events.*, actor.display_name AS actor_name FROM inspection_events AS events "
                "JOIN inspections ON inspections.id = events.inspection_id "
                "LEFT JOIN workflow_users AS actor ON actor.id = events.actor_id"
                + where + " ORDER BY events.inspection_id, events.id", params,
            ).fetchall()
        finally:
            connection.close()

        inspection_columns = [
            ("ID inspección", "id"), ("Etapa", "stage_label"), ("Código de etapa", "stage"),
            *((label, field) for field, label in FIELD_LABELS.items()
              if field in FIELDS),
            ("Entidad", "entity_name"),
            ("Inspector líder", "inspector_name"), ("Segundo inspector", "second_inspector_name"),
            ("Fecha de inspección", "scheduled_date"),
            ("Hora de inspección", "scheduled_time"), ("Lugar de inspección", "scheduled_place"),
            ("Checklist previo", "pre_checklist"), ("Locomoción", "locomotion"),
            ("Chofer", "driver_status"), ("Chofer asignado", "driver_name"),
            ("Hora de salida", "departure_time"), ("Hora aprox. de llegada", "arrival_time"),
            ("Checklist posterior", "post_checklist"), ("ARIS no aplica", "aris_not_applicable"),
            ("Próxima inspección", "next_inspection_date"),
            ("Mail de envío guardado", "send_mail_saved"),
            ("Creada por", "created_by_name"), ("Creada (UTC)", "created_at"),
            ("Última modificación (UTC)", "updated_at"),
        ]
        event_columns = [
            ("ID inspección", "inspection_id"), ("ID evento", "id"), ("Fecha (UTC)", "created_at"),
            ("Acción", "action_label"), ("Código de acción", "action"),
            ("Funcionario", "actor_name"), ("Etapa origen", "from_stage_label"),
            ("Etapa destino", "to_stage_label"), ("Comentario", "comment"),
            ("Cambios", "details_text"),
        ]

        def inspection_values(row):
            values = dict(row)
            values["stage_label"] = STAGE_LABELS.get(values["stage"], values["stage"])
            values["request_type"] = REQUEST_TYPES.get(values["request_type"], values["request_type"])
            values["pre_checklist"] = _checklist_summary(values["pre_checklist"])
            values["post_checklist"] = _checklist_summary(values["post_checklist"])
            values["aris_not_applicable"] = "Sí" if values["aris_not_applicable"] else "No"
            values["send_mail_saved"] = "Sí" if values["send_mail_saved"] else "No"
            values["locomotion"] = LOCOMOTION_OPTIONS.get(values["locomotion"], values["locomotion"])
            values["driver_status"] = DRIVER_STATUS.get(values["driver_status"], values["driver_status"])
            return values

        def event_values(row):
            values = dict(row)
            values["action_label"] = ACTION_LABELS.get(values["action"], values["action"])
            values["from_stage_label"] = STAGE_LABELS.get(values["from_stage"], values["from_stage"])
            values["to_stage_label"] = STAGE_LABELS.get(values["to_stage"], values["to_stage"])
            values["details_text"] = details_text(values["details"] or "{}")
            return values

        workbook = Workbook()
        for sheet, title, columns, rows, convert in (
            (workbook.active, "Inspecciones", inspection_columns, inspections, inspection_values),
            (workbook.create_sheet(), "Historial", event_columns, events, event_values),
        ):
            sheet.title = title
            sheet.append([heading for heading, _ in columns])
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1D4ED8")
                cell.alignment = Alignment(vertical="center")
            for index, (heading, _) in enumerate(columns, start=1):
                sheet.column_dimensions[get_column_letter(index)].width = min(
                    45, max(16, len(heading) + 2)
                )
            for row in rows:
                values = convert(row)
                sheet.append([values[key] for _, key in columns])
                # Texto ingresado por usuarios: nunca se interpreta como fórmula.
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions

        output = BytesIO()
        workbook.save(output)
        content = output.getvalue()
        self.send_response(200)
        self.send_header(
            "Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", 'attachment; filename="inspecciones_historial.xlsx"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)
