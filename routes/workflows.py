import re
import sqlite3
from datetime import date, datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from auth import hash_password, validate_new_password
from database import connect_db
from routes.entities import _entity_areas, _entity_name_key

STAGE_ROLES = {
    "carga_datos": "administracion",
    "asignacion_inspector": "jefe_licenciamiento",
    "evaluacion_inspector": "inspeccion",
    "jefe_licenciamiento": "jefe_licenciamiento",
    "abogado": "abogado",
    "director": "director",
}
WORKFLOW_ROLES = set(STAGE_ROLES.values()) | {"jefe_inspeccion"}
DELIVERY_METHODS = {"presencial": "Presencial", "apia": "APIA", "mail": "Mail"}
APPLICATION_FIELDS = {
    "application_date": ("La fecha de solicitud", 10),
    "form_number": ("El número de formulario", 100),
    "short_code": ("El código corto", 100),
    "first_name": ("El nombre", 160),
    "last_name": ("El apellido", 160),
    "identity_number": ("La CI", 100),
    "profession": ("La profesión", 160),
    "practice": ("La práctica", 160),
    "workplace": ("El lugar de trabajo", 200),
    "email": ("El correo", 254),
    "assigned_code": ("El código asignado", 100),
}


def _stage_role(connection, stage_id: int) -> str:
    stage = connection.execute(
        "SELECT stages.stage_key, workflows.name, entity.workflow_id AS entity_workflow "
        "FROM workflow_stages AS stages "
        "JOIN workflow_definitions AS workflows ON workflows.id = stages.workflow_id "
        "LEFT JOIN workflow_entity_types AS entity ON entity.workflow_id = workflows.id "
        "WHERE stages.id = ?", (stage_id,),
    ).fetchone()
    if stage and (stage["name"] == "Autorización Individual" or stage["entity_workflow"] is not None):
        return STAGE_ROLES.get(stage["stage_key"], "")
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_admin(handler) -> bool:
    if handler.current_user["role"] != "admin":
        handler.send_error_json("Esta acción requiere permisos de administrador.", 403)
        return False
    return True


def _valid_name(value: object, label: str, limit: int = 100) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} es obligatorio.")
    value = value.strip()
    if not value or len(value) > limit:
        raise ValueError(f"{label} debe tener entre 1 y {limit} caracteres.")
    return value


def _validate_application_date_and_signature(fields: dict, value: dict) -> None:
    try:
        parsed_date = date.fromisoformat(fields["application_date"])
        if parsed_date.isoformat() != fields["application_date"]:
            raise ValueError
    except ValueError:
        raise ValueError("La fecha de solicitud no es válida.") from None
    if type(value.get("digital_signature")) is not bool:
        raise ValueError("Indica si tiene firma digital.")
    fields["digital_signature"] = value["digital_signature"]


def _validate_application_data(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Completa los datos de la solicitud.")
    fields = {
        key: _valid_name(value.get(key), label, limit)
        for key, (label, limit) in APPLICATION_FIELDS.items()
    }
    _validate_application_date_and_signature(fields, value)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", fields["email"]):
        raise ValueError("El correo no es válido.")
    return fields


def _validate_entity_application_data(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Completa los datos de la solicitud.")
    fields = {
        key: _valid_name(value.get(key), label, limit)
        for key, label, limit in (
            ("application_date", "La fecha de solicitud", 10),
            ("form_number", "El número de formulario", 100),
            ("company_name", "La empresa o institución", 255),
            ("practice", "La práctica", 160),
            ("code", "El código", 100),
        )
    }
    _validate_application_date_and_signature(fields, value)
    return fields


def _case_access(connection, case, user: dict) -> bool:
    if user["role"] == "admin":
        return True
    required_role = _stage_role(connection, case["current_stage_id"])
    if required_role and user.get("workflow_role", "") != required_role:
        return False
    if case["assigned_user_id"] == user["id"]:
        return True
    if case["assigned_group_id"] is None:
        return False
    membership = connection.execute(
        "SELECT 1 FROM workflow_group_members "
        "WHERE group_id = ? AND user_id = ?",
        (case["assigned_group_id"], user["id"]),
    ).fetchone()
    if not membership:
        return False
    return case["claimed_by_user_id"] == user["id"]


def _validate_stage_assignee(connection, stage_id: int, user_id, group_id) -> None:
    required_role = _stage_role(connection, stage_id)
    if user_id is not None:
        user = connection.execute(
            "SELECT role, workflow_role FROM workflow_users WHERE id = ? AND active = 1",
            (user_id,),
        ).fetchone()
        if not user:
            raise ValueError("La persona asignada no existe o está inactiva.")
        if required_role and user["role"] != "admin" and user["workflow_role"] != required_role:
            raise ValueError("La persona asignada no tiene el rol de esta etapa.")
    if group_id is not None:
        if not connection.execute(
            "SELECT 1 FROM workflow_groups WHERE id = ?", (group_id,)
        ).fetchone():
            raise ValueError("El grupo asignado no existe.")
        if required_role and not connection.execute(
            "SELECT 1 FROM workflow_group_members AS members "
            "JOIN workflow_users AS users ON users.id = members.user_id "
            "WHERE members.group_id = ? AND users.active = 1 AND users.workflow_role = ?",
            (group_id, required_role),
        ).fetchone():
            raise ValueError("El grupo no tiene integrantes activos con el rol de esta etapa.")


def _validate_inspector(connection, value) -> int:
    try:
        inspector_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("Selecciona un funcionario de Inspección.") from None
    groups = connection.execute(
        "SELECT groups.name FROM workflow_groups AS groups "
        "JOIN workflow_group_members AS members ON members.group_id = groups.id "
        "JOIN workflow_users AS users ON users.id = members.user_id "
        "WHERE users.id = ? AND users.active = 1 "
        "AND (users.workflow_role = 'inspeccion' OR users.role = 'admin')",
        (inspector_id,),
    ).fetchall()
    if not any(group["name"].casefold() == "inspección" for group in groups):
        raise ValueError("El funcionario debe ser un integrante activo del grupo Inspección.")
    return inspector_id


def _case_inspector_id(connection, case_id: int, workflow_id: int):
    row = connection.execute(
        "SELECT assignments.user_id FROM workflow_case_stage_assignments AS assignments "
        "JOIN workflow_stages AS stages ON stages.id = assignments.stage_id "
        "WHERE assignments.case_id = ? AND stages.workflow_id = ? "
        "AND stages.stage_key = 'evaluacion_inspector'",
        (case_id, workflow_id),
    ).fetchone()
    return row["user_id"] if row else None


def _apply_deadline_history(connection, cases: list[dict]) -> None:
    by_id = {case["id"]: case for case in cases}
    for case in cases:
        case["deadline_started_at"] = case["created_at"]
        case["deadline_paused_at"] = None
    transitions = connection.execute(
        "SELECT events.case_id, events.created_at, source.stage_key AS source_key, "
        "target.stage_key AS target_key FROM workflow_events AS events "
        "JOIN workflow_cases AS cases ON cases.id = events.case_id "
        "JOIN workflow_definitions AS workflows ON workflows.id = cases.workflow_id "
        "LEFT JOIN workflow_entity_types AS entity ON entity.workflow_id = cases.workflow_id "
        "JOIN workflow_stages AS source ON source.id = events.from_stage_id "
        "JOIN workflow_stages AS target ON target.id = events.to_stage_id "
        "WHERE events.event_type = 'transitioned' "
        "AND (workflows.name = 'Autorización Individual' OR entity.workflow_id IS NOT NULL) "
        "AND ((source.stage_key = 'evaluacion_inspector' AND target.stage_key = 'carga_datos') "
        "OR target.stage_key = 'evaluacion_inspector') "
        "ORDER BY events.case_id, events.id"
    )
    for event in transitions:
        case = by_id[event["case_id"]]
        if event["source_key"] == "evaluacion_inspector" and event["target_key"] == "carga_datos":
            case["deadline_paused_at"] = event["created_at"]
        elif case["deadline_paused_at"] is not None and event["target_key"] == "evaluacion_inspector":
            case["deadline_started_at"] = event["created_at"]
            case["deadline_paused_at"] = None


def _insert_event(
    connection,
    case_id: int,
    actor_id: int,
    event_type: str,
    created_at: str,
    from_stage_id: int | None = None,
    to_stage_id: int | None = None,
    from_user_id: int | None = None,
    from_group_id: int | None = None,
    to_user_id: int | None = None,
    to_group_id: int | None = None,
    comment: str = "",
    duration_seconds: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO workflow_events "
        "(case_id, actor_id, event_type, from_stage_id, to_stage_id, "
        "from_assigned_user_id, from_assigned_group_id, to_assigned_user_id, "
        "to_assigned_group_id, comment, duration_seconds, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case_id,
            actor_id,
            event_type,
            from_stage_id,
            to_stage_id,
            from_user_id,
            from_group_id,
            to_user_id,
            to_group_id,
            comment,
            duration_seconds,
            created_at,
        ),
    )


def _move_case(connection, case, to_stage_id: int, stage_key: str, actor_id: int,
               now: str, comment: str = "") -> None:
    assignment = connection.execute(
        "SELECT user_id, group_id FROM workflow_case_stage_assignments "
        "WHERE case_id = ? AND stage_id = ?",
        (case["id"], to_stage_id),
    ).fetchone()
    to_user_id = assignment["user_id"] if assignment else None
    to_group_id = assignment["group_id"] if assignment else None
    started = datetime.fromisoformat(case["entered_stage_at"])
    duration = max(0, int((datetime.fromisoformat(now) - started).total_seconds()))
    has_next = connection.execute(
        "SELECT 1 FROM workflow_transitions WHERE from_stage_id = ? LIMIT 1",
        (to_stage_id,),
    ).fetchone()
    completed = stage_key in {"finalizado", "rechazado"} or not has_next
    connection.execute(
        "UPDATE workflow_cases SET current_stage_id = ?, assigned_user_id = ?, "
        "assigned_group_id = ?, claimed_by_user_id = NULL, status = ?, "
        "updated_at = ?, entered_stage_at = ?, completed_at = ? WHERE id = ?",
        (to_stage_id, to_user_id, to_group_id, "completed" if completed else "active",
         now, now, now if completed else None, case["id"]),
    )
    _insert_event(
        connection, case["id"], actor_id, "transitioned", now,
        from_stage_id=case["current_stage_id"], to_stage_id=to_stage_id,
        from_user_id=case["assigned_user_id"], from_group_id=case["assigned_group_id"],
        to_user_id=to_user_id, to_group_id=to_group_id, comment=comment.strip(),
        duration_seconds=duration,
    )


class WorkflowRoutes:
    def get_workflow_data(self, params: dict[str, list[str]]) -> None:
        connection = connect_db()
        try:
            connection.execute("BEGIN")
            workflows = []
            for workflow in connection.execute(
                "SELECT definitions.id, definitions.name, definitions.description, definitions.version, "
                "definitions.active, CASE WHEN entity.workflow_id IS NULL THEN 0 ELSE 1 END AS is_entity_workflow "
                "FROM workflow_definitions AS definitions LEFT JOIN workflow_entity_types AS entity "
                "ON entity.workflow_id = definitions.id WHERE definitions.active = 1 ORDER BY definitions.id"
            ).fetchall():
                stages = connection.execute(
                    "SELECT stages.id, stages.stage_key, stages.name, stages.position, "
                    "stages.default_assignee_user_id, stages.default_assignee_group_id, "
                    "users.display_name AS default_user_name, "
                    "groups.name AS default_group_name "
                    "FROM workflow_stages AS stages "
                    "LEFT JOIN workflow_users AS users "
                    "ON users.id = stages.default_assignee_user_id "
                    "LEFT JOIN workflow_groups AS groups "
                    "ON groups.id = stages.default_assignee_group_id "
                    "WHERE stages.workflow_id = ? ORDER BY stages.position",
                    (workflow["id"],),
                ).fetchall()
                transitions = connection.execute(
                    "SELECT transitions.id, transitions.from_stage_id, "
                    "transitions.to_stage_id, transitions.label "
                    "FROM workflow_transitions AS transitions "
                    "WHERE transitions.workflow_id = ? ORDER BY transitions.id",
                    (workflow["id"],),
                ).fetchall()
                workflows.append({
                    **dict(workflow),
                    "requires_inspector": any(
                        row["stage_key"] == "evaluacion_inspector" for row in stages
                    ),
                    "stages": [
                        {**dict(row), "required_role": _stage_role(connection, row["id"])}
                        for row in stages
                    ],
                    "transitions": [dict(row) for row in transitions],
                })

            users = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, username, display_name, role, workflow_role "
                    "FROM workflow_users WHERE active = 1 ORDER BY display_name COLLATE NOCASE"
                ).fetchall()
            ]
            groups = []
            for group in connection.execute(
                "SELECT id, name FROM workflow_groups ORDER BY name COLLATE NOCASE"
            ).fetchall():
                members = connection.execute(
                    "SELECT users.id, users.display_name "
                    "FROM workflow_group_members AS members "
                    "JOIN workflow_users AS users ON users.id = members.user_id "
                    "WHERE members.group_id = ? AND users.active = 1 "
                    "ORDER BY users.display_name COLLATE NOCASE",
                    (group["id"],),
                ).fetchall()
                groups.append({
                    "id": group["id"],
                    "name": group["name"],
                    "members": [dict(row) for row in members],
                })

            cases = [
                dict(row)
                for row in connection.execute(
                    "SELECT cases.id, cases.workflow_id, workflows.name AS workflow_name, "
                    "cases.workflow_version, cases.title, cases.reference, cases.description, cases.request_type, "
                    "application.first_name AS applicant_first_name, application.last_name AS applicant_last_name, "
                    "application.identity_number, application.short_code, application.assigned_code, "
                    "COALESCE(application.form_number, entity_data.form_number) AS form_number, "
                    "entity_data.code AS entity_code, "
                    "entity_data.company_name AS applicant_company_name, entity_data.linked_entity_name, "
                    "CASE WHEN entity_type.workflow_id IS NULL THEN 0 ELSE 1 END AS is_entity_workflow, "
                    "cases.linked_user_name, cases.linked_document_path, cases.current_stage_id, "
                    "stages.name AS stage_name, stages.stage_key, cases.status, cases.assigned_user_id, "
                    "users.display_name AS assigned_user_name, cases.assigned_group_id, "
                    "groups.name AS assigned_group_name, cases.claimed_by_user_id, "
                    "claimant.display_name AS claimed_by_name, cases.created_by, "
                    "creator.display_name AS created_by_name, inspector.display_name AS inspector_name, "
                    "inspection_assignment.user_id AS inspector_id, "
                    "cases.created_at, cases.updated_at, "
                    "cases.entered_stage_at, cases.completed_at, cases.delivered_at, cases.delivered_by, "
                    "cases.delivery_method, "
                    "CASE WHEN (workflows.name = 'Autorización Individual' OR entity_type.workflow_id IS NOT NULL) "
                    "AND cases.status = 'active' "
                    "AND stages.stage_key = 'carga_datos' AND last_transition.to_stage_id = cases.current_stage_id "
                    "AND previous_stage.stage_key = 'evaluacion_inspector' THEN 1 ELSE 0 END AS is_observed, "
                    "deliverer.display_name AS delivered_by_name "
                    "FROM workflow_cases AS cases "
                    "JOIN workflow_definitions AS workflows ON workflows.id = cases.workflow_id "
                    "JOIN workflow_stages AS stages ON stages.id = cases.current_stage_id "
                    "LEFT JOIN workflow_case_application_data AS application ON application.case_id = cases.id "
                    "LEFT JOIN workflow_entity_types AS entity_type ON entity_type.workflow_id = workflows.id "
                    "LEFT JOIN workflow_case_entity_data AS entity_data ON entity_data.case_id = cases.id "
                    "LEFT JOIN workflow_events AS last_transition ON last_transition.id = "
                    "(SELECT event.id FROM workflow_events AS event WHERE event.case_id = cases.id "
                    "AND event.event_type = 'transitioned' ORDER BY event.id DESC LIMIT 1) "
                    "LEFT JOIN workflow_stages AS previous_stage ON previous_stage.id = last_transition.from_stage_id "
                    "LEFT JOIN workflow_users AS users ON users.id = cases.assigned_user_id "
                    "LEFT JOIN workflow_groups AS groups ON groups.id = cases.assigned_group_id "
                    "LEFT JOIN workflow_users AS claimant ON claimant.id = cases.claimed_by_user_id "
                    "LEFT JOIN workflow_users AS creator ON creator.id = cases.created_by "
                    "LEFT JOIN workflow_users AS deliverer ON deliverer.id = cases.delivered_by "
                    "LEFT JOIN workflow_stages AS inspection_stage "
                    "ON inspection_stage.workflow_id = cases.workflow_id "
                    "AND inspection_stage.stage_key = 'evaluacion_inspector' "
                    "LEFT JOIN workflow_case_stage_assignments AS inspection_assignment "
                    "ON inspection_assignment.case_id = cases.id "
                    "AND inspection_assignment.stage_id = inspection_stage.id "
                    "LEFT JOIN workflow_users AS inspector ON inspector.id = inspection_assignment.user_id "
                    "ORDER BY CASE cases.status WHEN 'active' THEN 0 ELSE 1 END, "
                    "cases.updated_at DESC, cases.id DESC"
                ).fetchall()
            ]
            for case in cases:
                case["required_role"] = _stage_role(connection, case["current_stage_id"])
            _apply_deadline_history(connection, cases)
            selected_id = params.get("case_id", [""])[0]
            detail = None
            if selected_id:
                try:
                    case_id = int(selected_id)
                except ValueError:
                    raise ValueError("El trámite indicado no es válido.") from None
                if not any(case["id"] == case_id for case in cases):
                    raise FileNotFoundError
                selected_case = next(case for case in cases if case["id"] == case_id)
                application_data = connection.execute(
                    "SELECT application_date, form_number, short_code, first_name, last_name, "
                    "identity_number, profession, practice, workplace, email, assigned_code, "
                    "digital_signature FROM workflow_case_application_data WHERE case_id = ?",
                    (case_id,),
                ).fetchone()
                entity_application_data = connection.execute(
                    "SELECT application_date, form_number, company_name, practice, code, digital_signature, "
                    "linked_entity_name FROM workflow_case_entity_data WHERE case_id = ?", (case_id,),
                ).fetchone()
                transitions = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT transitions.id, transitions.label, "
                        "transitions.to_stage_id, stages.name AS to_stage_name, "
                        "stages.stage_key AS to_stage_key "
                        "FROM workflow_transitions AS transitions "
                        "JOIN workflow_stages AS stages ON stages.id = transitions.to_stage_id "
                        "JOIN workflow_cases AS cases ON cases.workflow_id = transitions.workflow_id "
                        "AND cases.current_stage_id = transitions.from_stage_id "
                        "WHERE cases.id = ? ORDER BY transitions.id",
                        (case_id,),
                    ).fetchall()
                ]
                if ((selected_case["workflow_name"] == "Autorización Individual"
                        or selected_case["is_entity_workflow"])
                        and selected_case["stage_key"] == "carga_datos"):
                    destination = "evaluacion_inspector" if selected_case["inspector_id"] else "asignacion_inspector"
                    transitions = [
                        transition for transition in transitions
                        if transition["to_stage_key"] not in {"evaluacion_inspector", "asignacion_inspector"}
                        or transition["to_stage_key"] == destination
                    ]
                detail = {
                    "application_data": (
                        dict(application_data) if application_data else None
                    ),
                    "entity_application_data": (
                        dict(entity_application_data) if entity_application_data else None
                    ),
                    "transitions": transitions,
                    "events": [
                        dict(row)
                        for row in connection.execute(
                            "SELECT events.id, events.event_type, events.comment, "
                            "events.duration_seconds, events.created_at, "
                            "actor.display_name AS actor_name, "
                            "from_stage.name AS from_stage_name, to_stage.name AS to_stage_name, "
                            "to_stage.stage_key AS to_stage_key, "
                            "from_user.display_name AS from_user_name, from_group.name AS from_group_name, "
                            "to_user.display_name AS to_user_name, to_group.name AS to_group_name "
                            "FROM workflow_events AS events "
                            "JOIN workflow_users AS actor ON actor.id = events.actor_id "
                            "LEFT JOIN workflow_stages AS from_stage ON from_stage.id = events.from_stage_id "
                            "LEFT JOIN workflow_stages AS to_stage ON to_stage.id = events.to_stage_id "
                            "LEFT JOIN workflow_users AS from_user "
                            "ON from_user.id = events.from_assigned_user_id "
                            "LEFT JOIN workflow_groups AS from_group "
                            "ON from_group.id = events.from_assigned_group_id "
                            "LEFT JOIN workflow_users AS to_user ON to_user.id = events.to_assigned_user_id "
                            "LEFT JOIN workflow_groups AS to_group ON to_group.id = events.to_assigned_group_id "
                            "WHERE events.case_id = ? ORDER BY events.id",
                            (case_id,),
                        ).fetchall()
                    ],
                }
        finally:
            connection.close()

        self.send_json({
            "supports_application_data": True,
            "supports_application_autofill": True,
            "supports_identity_search": True,
            "supports_observed_status": True,
            "supports_observed_inspector_reassignment": True,
            "supports_workflow_export": True,
            "supports_workflow_case_search": True,
            "supports_inspection_rejection": True,
            "supports_entity_workflows": True,
            "supports_inspector_folder_link": True,
            "supports_observation_deadline": True,
            "current_user": self.current_user,
            "workflows": workflows,
            "users": users,
            "groups": groups,
            "cases": cases,
            "detail": detail,
        })

    def export_workflows(self) -> None:
        connection = connect_db()
        try:
            connection.execute("BEGIN")
            cases = connection.execute(
                "SELECT cases.*, workflows.name AS workflow_name, stages.name AS stage_name, "
                "stages.stage_key, assigned_user.display_name AS assigned_user_name, "
                "assigned_group.name AS assigned_group_name, claimant.display_name AS claimed_by_name, "
                "creator.display_name AS created_by_name, deliverer.display_name AS delivered_by_name, "
                "inspector.display_name AS inspector_name, inspection_assignment.user_id AS inspector_id, "
                "COALESCE(application.application_date, entity_data.application_date) AS application_date, "
                "COALESCE(application.form_number, entity_data.form_number) AS form_number, "
                "application.short_code, entity_data.company_name, entity_data.code AS entity_code, "
                "entity_data.linked_entity_name, "
                "application.first_name, application.last_name, application.identity_number, "
                "application.profession, COALESCE(application.practice, entity_data.practice) AS practice, "
                "application.workplace, application.email, application.assigned_code, "
                "COALESCE(application.digital_signature, entity_data.digital_signature) AS digital_signature, "
                "CASE WHEN entity_type.workflow_id IS NULL THEN 0 ELSE 1 END AS is_entity_workflow, "
                "CASE WHEN (workflows.name = 'Autorización Individual' OR entity_type.workflow_id IS NOT NULL) "
                "AND cases.status = 'active' "
                "AND stages.stage_key = 'carga_datos' AND last_transition.to_stage_id = cases.current_stage_id "
                "AND previous_stage.stage_key = 'evaluacion_inspector' THEN 1 ELSE 0 END AS is_observed "
                "FROM workflow_cases AS cases "
                "LEFT JOIN workflow_definitions AS workflows ON workflows.id = cases.workflow_id "
                "LEFT JOIN workflow_stages AS stages ON stages.id = cases.current_stage_id "
                "LEFT JOIN workflow_case_application_data AS application ON application.case_id = cases.id "
                "LEFT JOIN workflow_entity_types AS entity_type ON entity_type.workflow_id = workflows.id "
                "LEFT JOIN workflow_case_entity_data AS entity_data ON entity_data.case_id = cases.id "
                "LEFT JOIN workflow_users AS assigned_user ON assigned_user.id = cases.assigned_user_id "
                "LEFT JOIN workflow_groups AS assigned_group ON assigned_group.id = cases.assigned_group_id "
                "LEFT JOIN workflow_users AS claimant ON claimant.id = cases.claimed_by_user_id "
                "LEFT JOIN workflow_users AS creator ON creator.id = cases.created_by "
                "LEFT JOIN workflow_users AS deliverer ON deliverer.id = cases.delivered_by "
                "LEFT JOIN workflow_stages AS inspection_stage ON inspection_stage.workflow_id = cases.workflow_id "
                "AND inspection_stage.stage_key = 'evaluacion_inspector' "
                "LEFT JOIN workflow_case_stage_assignments AS inspection_assignment "
                "ON inspection_assignment.case_id = cases.id AND inspection_assignment.stage_id = inspection_stage.id "
                "LEFT JOIN workflow_users AS inspector ON inspector.id = inspection_assignment.user_id "
                "LEFT JOIN workflow_events AS last_transition ON last_transition.id = "
                "(SELECT event.id FROM workflow_events AS event WHERE event.case_id = cases.id "
                "AND event.event_type = 'transitioned' ORDER BY event.id DESC LIMIT 1) "
                "LEFT JOIN workflow_stages AS previous_stage ON previous_stage.id = last_transition.from_stage_id "
                "ORDER BY cases.id"
            ).fetchall()
            events = connection.execute(
                "SELECT events.*, actor.display_name AS actor_name, "
                "from_stage.name AS from_stage_name, to_stage.name AS to_stage_name, "
                "from_user.display_name AS from_user_name, from_group.name AS from_group_name, "
                "to_user.display_name AS to_user_name, to_group.name AS to_group_name "
                "FROM workflow_events AS events "
                "LEFT JOIN workflow_users AS actor ON actor.id = events.actor_id "
                "LEFT JOIN workflow_stages AS from_stage ON from_stage.id = events.from_stage_id "
                "LEFT JOIN workflow_stages AS to_stage ON to_stage.id = events.to_stage_id "
                "LEFT JOIN workflow_users AS from_user ON from_user.id = events.from_assigned_user_id "
                "LEFT JOIN workflow_groups AS from_group ON from_group.id = events.from_assigned_group_id "
                "LEFT JOIN workflow_users AS to_user ON to_user.id = events.to_assigned_user_id "
                "LEFT JOIN workflow_groups AS to_group ON to_group.id = events.to_assigned_group_id "
                "ORDER BY events.case_id, events.id"
            ).fetchall()
        finally:
            connection.close()

        if len(cases) >= 1048576 or len(events) >= 1048576:
            raise ValueError("Hay demasiados trámites o eventos para exportarlos en un solo Excel.")

        case_columns = [
            ("ID trámite", "id"), ("Tipo de trámite", "workflow_name"),
            ("ID flujo", "workflow_id"), ("Versión del flujo", "workflow_version"),
            ("Solicitud", "request_type"), ("Título", "title"), ("Referencia", "reference"),
            ("Descripción", "description"), ("Carpeta vinculada", "linked_user_name"),
            ("Carpeta de entidad", "linked_entity_name"), ("Empresa/institución", "company_name"),
            ("Código de entidad", "entity_code"),
            ("Ruta vinculada", "linked_document_path"), ("Fecha de solicitud", "application_date"),
            ("Número de formulario", "form_number"), ("Código corto", "short_code"),
            ("Nombre", "first_name"), ("Apellido", "last_name"), ("CI", "identity_number"),
            ("Profesión", "profession"), ("Práctica", "practice"), ("Lugar de trabajo", "workplace"),
            ("Correo", "email"), ("Código asignado", "assigned_code"),
            ("Firma digital", "digital_signature"), ("Estado", "display_status"),
            ("Estado interno", "status"), ("ID etapa actual", "current_stage_id"),
            ("Etapa actual", "stage_name"), ("Clave de etapa", "stage_key"),
            ("ID responsable", "assigned_user_id"), ("Responsable", "assigned_user_name"),
            ("ID grupo responsable", "assigned_group_id"), ("Grupo responsable", "assigned_group_name"),
            ("ID quien tomó", "claimed_by_user_id"), ("Quien tomó", "claimed_by_name"),
            ("ID inspector", "inspector_id"), ("Inspector", "inspector_name"),
            ("ID creador", "created_by"), ("Creador", "created_by_name"),
            ("Creado (UTC)", "created_at"), ("Actualizado (UTC)", "updated_at"),
            ("Ingreso a etapa (UTC)", "entered_stage_at"), ("Finalizado (UTC)", "completed_at"),
            ("Entregado (UTC)", "delivered_at"), ("ID quien entregó", "delivered_by"),
            ("Quien entregó", "delivered_by_name"), ("Modalidad de entrega", "delivery_method"),
        ]
        event_columns = [
            ("ID trámite", "case_id"), ("ID evento", "id"), ("Fecha (UTC)", "created_at"),
            ("Acción", "action_label"), ("Código de acción", "event_type"),
            ("ID funcionario", "actor_id"), ("Funcionario", "actor_name"),
            ("ID etapa origen", "from_stage_id"), ("Etapa origen", "from_stage_name"),
            ("ID etapa destino", "to_stage_id"), ("Etapa destino", "to_stage_name"),
            ("ID responsable anterior", "from_assigned_user_id"), ("Responsable anterior", "from_user_name"),
            ("ID grupo anterior", "from_assigned_group_id"), ("Grupo anterior", "from_group_name"),
            ("ID responsable destino", "to_assigned_user_id"), ("Responsable destino", "to_user_name"),
            ("ID grupo destino", "to_assigned_group_id"), ("Grupo destino", "to_group_name"),
            ("Tiempo hasta la entrega (segundos)", "delivery_duration_seconds"), ("Comentario", "comment"),
        ]
        event_labels = {
            "created": "Inicio del trámite",
            "transitioned": "Cambio de etapa",
            "assigned": "Cambio de asignación",
            "claimed": "Trámite tomado",
            "delivered": "Entrega",
            "folder_linked": "Carpeta vinculada",
        }
        workbook = Workbook()
        for sheet, title, columns, rows in (
            (workbook.active, "Trámites", case_columns, cases),
            (workbook.create_sheet(), "Historial", event_columns, events),
        ):
            sheet.title = title
            sheet.append([heading for heading, _ in columns])
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1D4ED8")
                cell.alignment = Alignment(vertical="center")
            for index, (heading, _) in enumerate(columns, start=1):
                sheet.column_dimensions[get_column_letter(index)].width = min(45, max(18, len(heading) + 2))
            for row_index, row in enumerate(rows, start=2):
                values = dict(row)
                if title == "Trámites":
                    values["display_status"] = ("Entregado" if values["delivered_at"] else
                        "Rechazado" if (values["workflow_name"] == "Autorización Individual"
                        or values["is_entity_workflow"])
                        and values["stage_key"] == "rechazado" and values["status"] == "completed" else
                        "Finalizado" if values["status"] == "completed" else
                        "Observado" if values["is_observed"] else "En curso")
                    signature = values["digital_signature"]
                    values["digital_signature"] = None if signature is None else "Sí" if signature else "No"
                else:
                    values["action_label"] = event_labels.get(values["event_type"], values["event_type"])
                    values["delivery_duration_seconds"] = (
                        values["duration_seconds"] if values["event_type"] == "delivered" else None
                    )
                sheet.append([values[key] for _, key in columns])
                for cell in next(sheet.iter_rows(min_row=row_index, max_row=row_index)):
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions

        output = BytesIO()
        workbook.save(output)
        content = output.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", 'attachment; filename="tramites_historial.xlsx"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def create_workflow_user(self, data: dict) -> None:
        if not _is_admin(self):
            return
        username = _valid_name(data.get("username"), "El usuario", 40)
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", username):
            raise ValueError("El usuario solo puede incluir letras, números, punto, guion y guion bajo.")
        display_name = _valid_name(data.get("display_name"), "El nombre", 100)
        password = validate_new_password(data.get("password"))
        selected_role = data.get("role", "user")
        if not isinstance(selected_role, str) or selected_role not in WORKFLOW_ROLES | {"user", "admin"}:
            raise ValueError("El rol indicado no es válido.")
        role = "admin" if selected_role == "admin" else "user"
        workflow_role = selected_role if selected_role in WORKFLOW_ROLES else ""
        now = _now()
        connection = connect_db()
        try:
            cursor = connection.execute(
                "INSERT INTO workflow_users "
                "(username, display_name, password_hash, role, workflow_role, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (username, display_name, hash_password(password), role, workflow_role, now),
            )
            if workflow_role == "inspeccion":
                group = connection.execute(
                    "SELECT id FROM workflow_groups WHERE name = ? COLLATE NOCASE",
                    ("Inspección",),
                ).fetchone()
                if group:
                    group_id = group["id"]
                else:
                    group_id = connection.execute(
                        "INSERT INTO workflow_groups (name, created_at) VALUES (?, ?)",
                        ("Inspección", now),
                    ).lastrowid
                connection.execute(
                    "INSERT INTO workflow_group_members (group_id, user_id) VALUES (?, ?)",
                    (group_id, cursor.lastrowid),
                )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise ValueError("Ya existe un usuario con ese nombre.") from None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({
            "id": cursor.lastrowid,
            "username": username,
            "display_name": display_name,
            "role": role,
            "workflow_role": workflow_role,
        }, 201)

    def create_workflow_group(self, data: dict) -> None:
        if not _is_admin(self):
            return
        name = _valid_name(data.get("name"), "El nombre del grupo", 80)
        members = data.get("member_ids", [])
        if not isinstance(members, list) or not members:
            raise ValueError("Selecciona al menos un integrante para el grupo.")
        try:
            member_ids = sorted({int(member_id) for member_id in members})
        except (TypeError, ValueError):
            raise ValueError("La lista de integrantes no es válida.") from None
        connection = connect_db()
        try:
            active_ids = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM workflow_users WHERE active = 1"
                ).fetchall()
            }
            if not set(member_ids).issubset(active_ids):
                raise ValueError("Uno o más integrantes no existen o están inactivos.")
            cursor = connection.execute(
                "INSERT INTO workflow_groups (name, created_at) VALUES (?, ?)",
                (name, _now()),
            )
            group_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO workflow_group_members (group_id, user_id) VALUES (?, ?)",
                [(group_id, member_id) for member_id in member_ids],
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise ValueError("Ya existe un grupo con ese nombre.") from None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"id": group_id, "name": name}, 201)

    def create_workflow(self, data: dict) -> None:
        if not _is_admin(self):
            return
        name = _valid_name(data.get("name"), "El nombre del flujo", 100)
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 500:
            raise ValueError("La descripción no puede superar los 500 caracteres.")
        raw_stages = data.get("stages")
        if not isinstance(raw_stages, list) or not 2 <= len(raw_stages) <= 30:
            raise ValueError("El flujo debe tener entre 2 y 30 etapas.")
        stages = [_valid_name(stage, "Cada etapa", 120) for stage in raw_stages]
        if len({stage.casefold() for stage in stages}) != len(stages):
            raise ValueError("No puede haber etapas con nombres repetidos.")
        now = _now()
        connection = connect_db()
        try:
            cursor = connection.execute(
                "INSERT INTO workflow_definitions "
                "(name, description, version, active, created_by, created_at) "
                "VALUES (?, ?, 1, 1, ?, ?)",
                (name, description.strip(), self.current_user["id"], now),
            )
            workflow_id = cursor.lastrowid
            stage_ids = []
            for position, stage_name in enumerate(stages, start=1):
                stage_cursor = connection.execute(
                    "INSERT INTO workflow_stages "
                    "(workflow_id, stage_key, name, position) VALUES (?, ?, ?, ?)",
                    (workflow_id, f"etapa_{position}", stage_name, position),
                )
                stage_ids.append(stage_cursor.lastrowid)
            for index in range(len(stage_ids) - 1):
                connection.execute(
                    "INSERT INTO workflow_transitions "
                    "(workflow_id, from_stage_id, to_stage_id, label) VALUES (?, ?, ?, ?)",
                    (workflow_id, stage_ids[index], stage_ids[index + 1], f"Continuar a {stages[index + 1]}"),
                )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise ValueError("Ya existe un flujo con ese nombre.") from None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"id": workflow_id, "name": name, "version": 1}, 201)

    def set_workflow_transition(self, data: dict) -> None:
        if not _is_admin(self):
            return
        try:
            workflow_id = int(data.get("workflow_id"))
            from_stage_id = int(data.get("from_stage_id"))
            to_stage_id = int(data.get("to_stage_id"))
        except (TypeError, ValueError):
            raise ValueError("El flujo o las etapas indicadas no son válidos.") from None
        label = _valid_name(data.get("label"), "La acción", 120)
        connection = connect_db()
        try:
            if connection.execute(
                "SELECT 1 FROM workflow_cases WHERE workflow_id = ? LIMIT 1",
                (workflow_id,),
            ).fetchone():
                raise ValueError("No se pueden cambiar las acciones de un flujo que ya tiene trámites.")
            stage_ids = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM workflow_stages WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchall()
            }
            if from_stage_id not in stage_ids or to_stage_id not in stage_ids:
                raise ValueError("Las etapas deben pertenecer al mismo flujo.")
            if (_stage_role(connection, from_stage_id) == "abogado"
                    and _stage_role(connection, to_stage_id) == "inspeccion"):
                raise ValueError("Abogado debe devolver el trámite al jefe, no a Inspección.")
            connection.execute(
                "INSERT INTO workflow_transitions "
                "(workflow_id, from_stage_id, to_stage_id, label) VALUES (?, ?, ?, ?)",
                (workflow_id, from_stage_id, to_stage_id, label),
            )
            connection.execute(
                "UPDATE workflow_definitions SET version = version + 1 WHERE id = ?",
                (workflow_id,),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise ValueError("Esa acción ya está configurada.") from None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})

    def set_stage_assignment(self, data: dict) -> None:
        if not _is_admin(self):
            return
        try:
            stage_id = int(data.get("stage_id"))
            user_id = int(data["user_id"]) if data.get("user_id") else None
            group_id = int(data["group_id"]) if data.get("group_id") else None
        except (TypeError, ValueError):
            raise ValueError("La asignación indicada no es válida.") from None
        if user_id is not None and group_id is not None:
            raise ValueError("Asigna la etapa a una persona o a un grupo, no a ambos.")
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            stage = connection.execute(
                "SELECT workflow_id FROM workflow_stages WHERE id = ?", (stage_id,)
            ).fetchone()
            if not stage:
                raise FileNotFoundError
            _validate_stage_assignee(connection, stage_id, user_id, group_id)
            connection.execute(
                "UPDATE workflow_stages SET default_assignee_user_id = ?, "
                "default_assignee_group_id = ? WHERE id = ?",
                (user_id, group_id, stage_id),
            )
            connection.execute(
                "UPDATE workflow_definitions SET version = version + 1 WHERE id = ?",
                (stage["workflow_id"],),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})

    def create_workflow_case(self, data: dict) -> None:
        if self.current_user["role"] != "admin" and self.current_user.get("workflow_role") != "administracion":
            self.send_error_json("Solo Secretaría o un administrador pueden iniciar trámites.", 403)
            return
        try:
            workflow_id = int(data.get("workflow_id"))
        except (TypeError, ValueError):
            raise ValueError("Selecciona un flujo válido.") from None
        reference = data.get("reference", "")
        description = data.get("description", "")
        linked_user_name = data.get("linked_user_name", "")
        linked_entity_name = data.get("linked_entity_name", "")
        request_type = data.get("request_type", "")
        if not all(isinstance(value, str) for value in (
            reference, description, linked_user_name, linked_entity_name, request_type,
        )):
            raise ValueError("Los datos del trámite no son válidos.")
        if (len(reference) > 100 or len(description) > 2000
                or len(linked_user_name) > 160 or len(linked_entity_name) > 255):
            raise ValueError("Uno o más datos superan el máximo permitido.")
        linked_user_name = linked_user_name.strip()
        linked_entity_name = linked_entity_name.strip()
        if linked_entity_name:
            areas = _entity_areas()[0]
            if not any(_entity_name_key(area["institution"]) == _entity_name_key(linked_entity_name)
                       for area in areas):
                raise ValueError("La entidad vinculada no existe o no está disponible.")
        now = _now()
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT definitions.id, definitions.name, definitions.version, "
                "entity.workflow_id AS entity_workflow FROM workflow_definitions AS definitions "
                "LEFT JOIN workflow_entity_types AS entity ON entity.workflow_id = definitions.id "
                "WHERE definitions.id = ? AND definitions.active = 1",
                (workflow_id,),
            ).fetchone()
            if not workflow:
                raise ValueError("El flujo seleccionado no está disponible.")
            is_entity = workflow["entity_workflow"] is not None
            if linked_entity_name and not is_entity:
                raise ValueError("La carpeta de entidad no aplica a este flujo.")
            if is_entity and linked_user_name:
                raise ValueError("Selecciona una carpeta de entidad, no de usuario.")
            title = (
                _valid_name(data["title"], "El asunto", 160)
                if "title" in data else workflow["name"]
            )
            stages = connection.execute(
                "SELECT id, position, stage_key, default_assignee_user_id, "
                "default_assignee_group_id FROM workflow_stages "
                "WHERE workflow_id = ? ORDER BY position",
                (workflow_id,),
            ).fetchall()
            if not stages:
                raise ValueError("El flujo no tiene etapas.")
            inspection_stage = next(
                (stage for stage in stages if stage["stage_key"] == "evaluacion_inspector"),
                None,
            )
            if inspection_stage:
                if request_type not in {"nueva", "renovacion", "modificacion"}:
                    raise ValueError("Selecciona nueva, renovación o modificación.")
                if not is_entity and request_type == "nueva" and linked_user_name:
                    raise ValueError("Un trámite nuevo no puede vincularse a una carpeta de usuario.")
                if request_type in {"renovacion", "modificacion"}:
                    if is_entity and not linked_entity_name:
                        raise ValueError("Selecciona la carpeta de entidad para la renovación o modificación.")
                    if not is_entity and not linked_user_name:
                        raise ValueError("Selecciona la carpeta de usuario para la renovación o modificación.")
            elif request_type:
                raise ValueError("El tipo de solicitud no aplica a este flujo.")
            if data.get("inspector_id") and not is_entity:
                raise ValueError("El jefe asigna el funcionario después de crear el trámite.")
            first_stage = stages[0]
            auto_send = workflow["name"] == "Autorización Individual"
            application_data = _validate_application_data(data.get("application_data")) if auto_send else None
            entity_application_data = _validate_entity_application_data(data.get("application_data")) if is_entity else None
            inspector_id = _validate_inspector(connection, data.get("inspector_id")) if is_entity else None
            assignment_stage = None
            if auto_send:
                assignment_stage = next(
                    (stage for stage in stages if stage["stage_key"] == "asignacion_inspector"),
                    None,
                )
                if not inspection_stage or first_stage["stage_key"] != "carga_datos" or not assignment_stage:
                    raise ValueError("El flujo de Autorización Individual no tiene las etapas esperadas.")
                if not connection.execute(
                    "SELECT 1 FROM workflow_transitions WHERE workflow_id = ? "
                    "AND from_stage_id = ? AND to_stage_id = ?",
                    (workflow_id, first_stage["id"], assignment_stage["id"]),
                ).fetchone():
                    raise ValueError("El flujo no tiene configurado el envío al jefe.")
                if not connection.execute(
                    "SELECT 1 FROM workflow_transitions WHERE workflow_id = ? "
                    "AND from_stage_id = ? AND to_stage_id = ?",
                    (workflow_id, assignment_stage["id"], inspection_stage["id"]),
                ).fetchone():
                    raise ValueError("El flujo no tiene configurado el envío a inspección.")
                if not assignment_stage["default_assignee_user_id"] and not assignment_stage["default_assignee_group_id"]:
                    raise ValueError("Configura un responsable para la asignación del inspector antes de crear el trámite.")
                _validate_stage_assignee(
                    connection, assignment_stage["id"],
                    assignment_stage["default_assignee_user_id"],
                    assignment_stage["default_assignee_group_id"],
                )
            if is_entity:
                if not inspection_stage or first_stage["stage_key"] != "carga_datos":
                    raise ValueError("El flujo de entidad no tiene las etapas esperadas.")
                if not connection.execute(
                    "SELECT 1 FROM workflow_transitions WHERE workflow_id = ? "
                    "AND from_stage_id = ? AND to_stage_id = ?",
                    (workflow_id, first_stage["id"], inspection_stage["id"]),
                ).fetchone():
                    raise ValueError("El flujo no tiene configurado el envío a inspección.")
                _validate_stage_assignee(connection, inspection_stage["id"], inspector_id, None)
            first_user_id = first_stage["default_assignee_user_id"]
            first_group_id = first_stage["default_assignee_group_id"]
            if inspection_stage and self.current_user.get("workflow_role") == "administracion":
                first_user_id = self.current_user["id"]
                first_group_id = None
            inspection_stage_id = inspection_stage["id"] if inspection_stage else None
            if linked_user_name and not connection.execute(
                "SELECT 1 FROM file_catalog WHERE root = 'documents' AND owner = ? LIMIT 1",
                (linked_user_name,),
            ).fetchone():
                raise ValueError("La carpeta de usuario vinculada no existe en el catálogo.")
            cursor = connection.execute(
                "INSERT INTO workflow_cases "
                "(workflow_id, workflow_version, title, reference, description, request_type, "
                "linked_user_name, current_stage_id, assigned_user_id, assigned_group_id, "
                "status, created_by, created_at, updated_at, entered_stage_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    workflow_id,
                    workflow["version"],
                    title,
                    reference.strip(),
                    description.strip(),
                    request_type,
                    linked_user_name,
                    first_stage["id"],
                    first_user_id,
                    first_group_id,
                    self.current_user["id"],
                    now,
                    now,
                    now,
                ),
            )
            case_id = cursor.lastrowid
            if application_data is not None:
                connection.execute(
                    "INSERT INTO workflow_case_application_data "
                    "(case_id, application_date, form_number, short_code, first_name, last_name, "
                    "identity_number, profession, practice, workplace, email, assigned_code, digital_signature) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (case_id, *application_data.values()),
                )
            if entity_application_data is not None:
                connection.execute(
                    "INSERT INTO workflow_case_entity_data "
                    "(case_id, application_date, form_number, company_name, practice, code, "
                    "digital_signature, linked_entity_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (case_id, *entity_application_data.values(), linked_entity_name),
                )
            connection.executemany(
                "INSERT INTO workflow_case_stage_assignments "
                "(case_id, stage_id, user_id, group_id) VALUES (?, ?, ?, ?)",
                [
                    (
                        case_id,
                        stage["id"],
                        first_user_id if stage["id"] == first_stage["id"]
                        else inspector_id if stage["id"] == inspection_stage_id
                        else stage["default_assignee_user_id"],
                        first_group_id if stage["id"] == first_stage["id"]
                        else None if stage["id"] == inspection_stage_id
                        else stage["default_assignee_group_id"],
                    )
                    for stage in stages
                ],
            )
            _insert_event(
                connection,
                case_id,
                self.current_user["id"],
                "created",
                now,
                to_stage_id=first_stage["id"],
                to_user_id=first_user_id,
                to_group_id=first_group_id,
            )
            if assignment_stage:
                created_case = connection.execute(
                    "SELECT * FROM workflow_cases WHERE id = ?", (case_id,),
                ).fetchone()
                _move_case(
                    connection, created_case, assignment_stage["id"],
                    assignment_stage["stage_key"], self.current_user["id"], now,
                )
            elif is_entity:
                created_case = connection.execute(
                    "SELECT * FROM workflow_cases WHERE id = ?", (case_id,),
                ).fetchone()
                _move_case(
                    connection, created_case, inspection_stage["id"], inspection_stage["stage_key"],
                    self.current_user["id"], now,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"id": case_id, "sent_to_chief": auto_send, "sent_to_inspection": is_entity}, 201)

    def link_workflow_folder(self, data: dict) -> None:
        try:
            case_id = int(data.get("case_id"))
        except (TypeError, ValueError):
            raise ValueError("El trámite indicado no es válido.") from None
        kind = data.get("kind")
        if kind not in ("user", "entity"):
            raise ValueError("El tipo de carpeta no es válido.")
        name = _valid_name(data.get("name"), "La carpeta", 160 if kind == "user" else 255)
        if kind == "entity":
            areas = _entity_areas()[0]
            if not any(_entity_name_key(area["institution"]) == _entity_name_key(name) for area in areas):
                raise ValueError("La entidad vinculada no existe o no está disponible.")

        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            case = connection.execute(
                "SELECT cases.*, stages.stage_key, workflows.name AS workflow_name, "
                "entity_type.workflow_id AS entity_workflow, "
                "entity_data.case_id AS entity_data_case_id, entity_data.linked_entity_name "
                "FROM workflow_cases AS cases "
                "JOIN workflow_stages AS stages ON stages.id = cases.current_stage_id "
                "JOIN workflow_definitions AS workflows ON workflows.id = cases.workflow_id "
                "LEFT JOIN workflow_entity_types AS entity_type ON entity_type.workflow_id = cases.workflow_id "
                "LEFT JOIN workflow_case_entity_data AS entity_data ON entity_data.case_id = cases.id "
                "WHERE cases.id = ?", (case_id,),
            ).fetchone()
            if not case:
                raise FileNotFoundError
            if case["status"] != "active" or case["stage_key"] != "evaluacion_inspector":
                raise ValueError("Solo se puede vincular una carpeta durante la evaluación de Inspección.")
            if not _case_access(connection, case, self.current_user):
                raise PermissionError("No tienes asignado este trámite.")
            is_entity = case["entity_workflow"] is not None
            if not is_entity and case["workflow_name"] != "Autorización Individual":
                raise ValueError("Este tipo de trámite no admite vincular una carpeta en Inspección.")
            if kind != ("entity" if is_entity else "user"):
                raise ValueError("El tipo de carpeta no corresponde al trámite.")
            if is_entity:
                if case["entity_data_case_id"] is None:
                    raise ValueError("El trámite no tiene datos de entidad para guardar la carpeta.")
                if case["linked_entity_name"]:
                    raise ValueError("El trámite ya tiene una carpeta vinculada.")
                connection.execute(
                    "UPDATE workflow_case_entity_data SET linked_entity_name = ? WHERE case_id = ?",
                    (name, case_id),
                )
            else:
                if case["linked_user_name"]:
                    raise ValueError("El trámite ya tiene una carpeta vinculada.")
                if not connection.execute(
                    "SELECT 1 FROM file_catalog WHERE root = 'documents' AND owner = ? LIMIT 1",
                    (name,),
                ).fetchone():
                    raise ValueError("La carpeta de usuario vinculada no existe en el catálogo.")
                connection.execute(
                    "UPDATE workflow_cases SET linked_user_name = ? WHERE id = ?", (name, case_id),
                )
            now = _now()
            connection.execute("UPDATE workflow_cases SET updated_at = ? WHERE id = ?", (now, case_id))
            _insert_event(
                connection, case_id, self.current_user["id"], "folder_linked", now,
                from_stage_id=case["current_stage_id"], to_stage_id=case["current_stage_id"],
                comment=f"{'Entidad' if is_entity else 'Carpeta de usuario'}: {name}",
            )
            connection.commit()
        except PermissionError as exc:
            connection.rollback()
            self.send_error_json(str(exc), 403)
            return
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})

    def transition_workflow_case(self, data: dict) -> None:
        try:
            case_id = int(data.get("case_id"))
            transition_id = int(data.get("transition_id"))
        except (TypeError, ValueError):
            raise ValueError("El trámite o la acción indicados no son válidos.") from None
        comment = data.get("comment", "")
        if not isinstance(comment, str) or len(comment) > 1000:
            raise ValueError("El comentario no puede superar los 1000 caracteres.")
        now = _now()
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            case = connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if not case:
                raise FileNotFoundError
            if case["status"] != "active":
                raise ValueError("El trámite ya está finalizado.")
            if not _case_access(connection, case, self.current_user):
                raise PermissionError("No tienes asignado este trámite.")
            transition = connection.execute(
                "SELECT transitions.to_stage_id, stages.stage_key, "
                "stages.name AS stage_name, stages.workflow_id, source.stage_key AS source_key, "
                "workflows.name AS workflow_name, entity.workflow_id AS entity_workflow "
                "FROM workflow_transitions AS transitions "
                "JOIN workflow_stages AS stages ON stages.id = transitions.to_stage_id "
                "JOIN workflow_stages AS source ON source.id = transitions.from_stage_id "
                "JOIN workflow_definitions AS workflows ON workflows.id = transitions.workflow_id "
                "LEFT JOIN workflow_entity_types AS entity ON entity.workflow_id = workflows.id "
                "WHERE transitions.id = ? AND transitions.workflow_id = ? "
                "AND transitions.from_stage_id = ?",
                (transition_id, case["workflow_id"], case["current_stage_id"]),
            ).fetchone()
            if not transition:
                raise ValueError("Esa acción no está disponible para la etapa actual.")
            is_inspection_workflow = (transition["workflow_name"] == "Autorización Individual"
                                      or transition["entity_workflow"] is not None)
            if (is_inspection_workflow
                    and transition["stage_key"] == "rechazado"):
                if transition["source_key"] != "evaluacion_inspector":
                    raise ValueError("Solo se puede rechazar desde la evaluación de Inspección.")
                if not comment.strip():
                    raise ValueError("Ingresa el motivo del rechazo.")
                connection.execute(
                    "INSERT INTO workflow_case_stage_assignments (case_id, stage_id, user_id, group_id) "
                    "VALUES (?, ?, NULL, NULL) ON CONFLICT(case_id, stage_id) DO UPDATE SET "
                    "user_id = NULL, group_id = NULL",
                    (case_id, transition["to_stage_id"]),
                )
            if (is_inspection_workflow
                    and transition["source_key"] == "abogado"
                    and transition["stage_key"] == "evaluacion_inspector"):
                raise ValueError("Abogado debe devolver el trámite al jefe, no a Inspección.")
            if (is_inspection_workflow
                    and transition["source_key"] == "carga_datos"
                    and transition["stage_key"] in {"asignacion_inspector", "evaluacion_inspector"}):
                inspector_id = _case_inspector_id(connection, case_id, case["workflow_id"])
                if transition["stage_key"] == "asignacion_inspector" and inspector_id:
                    raise ValueError("El trámite ya tiene inspector; envíalo directamente a Inspección.")
                if transition["stage_key"] == "evaluacion_inspector":
                    if not inspector_id:
                        raise ValueError("El trámite no tiene inspector asignado; envíalo al jefe.")
                    if data.get("inspector_id") is not None:
                        last_transition = connection.execute(
                            "SELECT source.stage_key, events.to_stage_id FROM workflow_events AS events "
                            "JOIN workflow_stages AS source ON source.id = events.from_stage_id "
                            "WHERE events.case_id = ? AND events.event_type = 'transitioned' "
                            "ORDER BY events.id DESC LIMIT 1",
                            (case_id,),
                        ).fetchone()
                        if (not last_transition or last_transition["stage_key"] != "evaluacion_inspector"
                                or last_transition["to_stage_id"] != case["current_stage_id"]):
                            raise ValueError("Solo se puede cambiar el inspector al devolver un trámite observado.")
                        inspector_id = _validate_inspector(connection, data["inspector_id"])
                        connection.execute(
                            "UPDATE workflow_case_stage_assignments SET user_id = ?, group_id = NULL "
                            "WHERE case_id = ? AND stage_id = ?",
                            (inspector_id, case_id, transition["to_stage_id"]),
                        )
                    _validate_stage_assignee(connection, transition["to_stage_id"], inspector_id, None)
            if (transition["workflow_name"] == "Autorización Individual"
                    and transition["source_key"] == "asignacion_inspector"
                    and transition["stage_key"] == "evaluacion_inspector"):
                inspector_id = _validate_inspector(connection, data.get("inspector_id"))
                connection.execute(
                    "INSERT INTO workflow_case_stage_assignments (case_id, stage_id, user_id, group_id) "
                    "VALUES (?, ?, ?, NULL) ON CONFLICT(case_id, stage_id) DO UPDATE SET "
                    "user_id = excluded.user_id, group_id = NULL",
                    (case_id, transition["to_stage_id"], inspector_id),
                )
            if (is_inspection_workflow
                    and transition["source_key"] == "evaluacion_inspector"
                    and transition["stage_key"] == "carga_datos"
                    and case["assigned_group_id"] and case["claimed_by_user_id"]):
                connection.execute(
                    "INSERT INTO workflow_case_stage_assignments (case_id, stage_id, user_id, group_id) "
                    "VALUES (?, ?, ?, NULL) ON CONFLICT(case_id, stage_id) DO UPDATE SET "
                    "user_id = excluded.user_id, group_id = NULL",
                    (case_id, case["current_stage_id"], case["claimed_by_user_id"]),
                )
            if (is_inspection_workflow
                    and transition["source_key"] == "director"
                    and transition["stage_key"] == "finalizado"):
                connection.execute(
                    "INSERT INTO workflow_case_stage_assignments (case_id, stage_id, user_id, group_id) "
                    "VALUES (?, ?, NULL, NULL) ON CONFLICT(case_id, stage_id) DO UPDATE SET "
                    "user_id = NULL, group_id = NULL",
                    (case_id, transition["to_stage_id"]),
                )
            _move_case(
                connection, case, transition["to_stage_id"], transition["stage_key"],
                self.current_user["id"], now, comment,
            )
            connection.commit()
        except PermissionError as exc:
            connection.rollback()
            self.send_error_json(str(exc), 403)
            return
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})

    def deliver_workflow_case(self, data: dict) -> None:
        if self.current_user["role"] != "admin" and self.current_user.get("workflow_role") != "administracion":
            self.send_error_json("Solo Secretaría o un administrador pueden registrar la entrega.", 403)
            return
        try:
            case_id = int(data.get("case_id"))
        except (TypeError, ValueError):
            raise ValueError("El trámite indicado no es válido.") from None
        method = data.get("delivery_method")
        if not isinstance(method, str) or method not in DELIVERY_METHODS:
            raise ValueError("Selecciona una modalidad de entrega válida.")
        now = _now()
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            case = connection.execute(
                "SELECT cases.*, workflows.name AS workflow_name, stages.stage_key, "
                "entity.workflow_id AS entity_workflow "
                "FROM workflow_cases AS cases "
                "JOIN workflow_definitions AS workflows ON workflows.id = cases.workflow_id "
                "JOIN workflow_stages AS stages ON stages.id = cases.current_stage_id "
                "LEFT JOIN workflow_entity_types AS entity ON entity.workflow_id = workflows.id "
                "WHERE cases.id = ?", (case_id,),
            ).fetchone()
            if not case:
                raise FileNotFoundError
            if ((case["workflow_name"] != "Autorización Individual" and case["entity_workflow"] is None)
                    or case["stage_key"] != "finalizado" or case["status"] != "completed"):
                raise ValueError("Solo se pueden entregar autorizaciones finalizadas por el director.")
            if case["delivered_at"]:
                raise ValueError("El trámite ya fue marcado como entregado.")
            duration = max(0, int((datetime.fromisoformat(now)
                                   - datetime.fromisoformat(case["entered_stage_at"])).total_seconds()))
            connection.execute(
                "UPDATE workflow_cases SET delivered_at = ?, delivered_by = ?, delivery_method = ?, "
                "updated_at = ? WHERE id = ?",
                (now, self.current_user["id"], method, now, case_id),
            )
            _insert_event(
                connection, case_id, self.current_user["id"], "delivered", now,
                from_stage_id=case["current_stage_id"], duration_seconds=duration,
                comment=f"Modalidad: {DELIVERY_METHODS[method]}",
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})

    def assign_workflow_case(self, data: dict) -> None:
        try:
            case_id = int(data.get("case_id"))
            user_id = int(data["user_id"]) if data.get("user_id") else None
            group_id = int(data["group_id"]) if data.get("group_id") else None
        except (TypeError, ValueError):
            raise ValueError("El trámite o la asignación indicada no son válidos.") from None
        if (user_id is None) == (group_id is None):
            raise ValueError("Selecciona una persona o un grupo.")
        now = _now()
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            case = connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if not case:
                raise FileNotFoundError
            if case["status"] != "active":
                raise ValueError("No se puede reasignar un trámite finalizado.")
            if self.current_user["role"] != "admin" and (
                self.current_user.get("workflow_role") != "administracion"
                or not _case_access(connection, case, self.current_user)
            ):
                raise PermissionError("Solo Secretaría o un administrador pueden reasignar este trámite.")
            _validate_stage_assignee(connection, case["current_stage_id"], user_id, group_id)
            connection.execute(
                "UPDATE workflow_cases SET assigned_user_id = ?, assigned_group_id = ?, "
                "claimed_by_user_id = NULL, updated_at = ? WHERE id = ?",
                (user_id, group_id, now, case_id),
            )
            connection.execute(
                "INSERT INTO workflow_case_stage_assignments (case_id, stage_id, user_id, group_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(case_id, stage_id) DO UPDATE SET "
                "user_id = excluded.user_id, group_id = excluded.group_id",
                (case_id, case["current_stage_id"], user_id, group_id),
            )
            _insert_event(
                connection,
                case_id,
                self.current_user["id"],
                "assigned",
                now,
                from_stage_id=case["current_stage_id"],
                to_stage_id=case["current_stage_id"],
                from_user_id=case["assigned_user_id"],
                from_group_id=case["assigned_group_id"],
                to_user_id=user_id,
                to_group_id=group_id,
            )
            connection.commit()
        except PermissionError as exc:
            connection.rollback()
            self.send_error_json(str(exc), 403)
            return
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})

    def claim_workflow_case(self, data: dict) -> None:
        try:
            case_id = int(data.get("case_id"))
        except (TypeError, ValueError):
            raise ValueError("El trámite indicado no es válido.") from None
        now = _now()
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            case = connection.execute(
                "SELECT * FROM workflow_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if not case:
                raise FileNotFoundError
            if case["status"] != "active" or not case["assigned_group_id"]:
                raise ValueError("Este trámite no está disponible para tomarlo del grupo.")
            required_role = _stage_role(connection, case["current_stage_id"])
            if required_role and self.current_user["role"] != "admin" and self.current_user.get("workflow_role") != required_role:
                raise PermissionError("No tienes el rol requerido para esta etapa.")
            member = connection.execute(
                "SELECT 1 FROM workflow_group_members "
                "WHERE group_id = ? AND user_id = ?",
                (case["assigned_group_id"], self.current_user["id"]),
            ).fetchone()
            if not member:
                raise PermissionError("No perteneces al grupo asignado a este trámite.")
            if case["claimed_by_user_id"] not in (None, self.current_user["id"]):
                raise ValueError("Otro integrante ya tomó este trámite.")
            connection.execute(
                "UPDATE workflow_cases SET claimed_by_user_id = ?, updated_at = ? WHERE id = ?",
                (self.current_user["id"], now, case_id),
            )
            _insert_event(
                connection,
                case_id,
                self.current_user["id"],
                "claimed",
                now,
                from_stage_id=case["current_stage_id"],
                to_stage_id=case["current_stage_id"],
                from_user_id=case["assigned_user_id"],
                from_group_id=case["assigned_group_id"],
                to_user_id=self.current_user["id"],
                to_group_id=case["assigned_group_id"],
            )
            connection.commit()
        except PermissionError as exc:
            connection.rollback()
            self.send_error_json(str(exc), 403)
            return
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.send_json({"saved": True})
