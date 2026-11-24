"""Planificación de inspecciones a partir de las planillas de la carpeta 5.

Lee las planillas Excel de "5 Registro de tramites y planillas" (todas menos las
de seguimiento de observaciones), detecta en cada hoja las columnas de
institución, práctica, última y próxima inspección, y agrupa por institución y
práctica. Solo lee: nunca modifica las planillas.
"""

import re
import threading
import unicodedata
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
HEADER_SCAN_ROWS = 10
MAX_COLUMNS = 40
MAX_EMPTY_ROWS = 200
SOON_DAYS = 90
# Cambia cuando cambia la forma de leer las planillas: obliga a volver a leerlas.
PARSER_VERSION = 2

MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
    "ago": 8, "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}
INSTITUTION_KEYS = ("institucion", "usuario", "instalacion", "lugar")
PRACTICE_KEYS = ("tipo de equipo", "practica", "equipos", "isotopos")
LAST_KEYS = ("ultima insp", "ult. insp", "fecha de inspeccion", "insp", "fecha")
LAST_EXCLUDE = (
    "proxima", "venc", "calibracion", "solicitud", "fabric", "reacond", "instalacion",
    "anterior", "actualizado", "hoy", "ingresada", "previas", "faltan", "vencida",
)
NEXT_KEYS = ("proxima", "venc")
NEXT_EXCLUDE = ("faltan", "vencida hace", "mes", "ano")
DEPARTMENT_KEYS = ("depto", "departamento")
GENERIC_SHEETS = {"instalaciones"}
BAJA = re.compile(r"(^baja$|de baja|baja de equipo|^no corresponde$)")

_cache_lock = threading.Lock()
_cache: dict = {}


def normalize(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()


def clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_date(value):
    """Devuelve una fecha o None. Acepta fechas Excel y textos como 'set-22'."""
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    elif isinstance(value, str):
        text = normalize(value)
        match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", text)
        if match:
            day, month, year = (int(part) for part in match.groups())
            year += 2000 if year < 100 else 0
            try:
                result = date(year, month, day)
            except ValueError:
                return None
        else:
            match = re.fullmatch(r"([a-z]{3})[a-z]*[\s./-]*(\d{2}|\d{4})", text)
            if not match or match.group(1) not in MONTHS:
                return None
            year = int(match.group(2))
            result = date(year + 2000 if year < 100 else year, MONTHS[match.group(1)], 1)
    else:
        return None
    # Descarta fechas de relleno de Excel (1900/1904) y valores imposibles.
    return result if 1950 <= result.year <= 2100 else None


def month_year_date(month, year, last):
    """Arma la fecha a partir de columnas sueltas de mes y año (año de 2 o 4 cifras)."""
    try:
        month, year = int(month), int(year)
    except (TypeError, ValueError):
        return None
    year += 2000 if 0 <= year < 100 else 0
    if not 1 <= month <= 12 or not 1950 <= year <= 2100:
        return None
    day = last.day if last else 1
    while day > 28:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, day)


def is_excluded(path: Path) -> bool:
    name = normalize(path.name)
    return name.startswith("~$") or "observacion" in name


def planning_files(root: Path) -> list:
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in EXCEL_SUFFIXES and not is_excluded(path)
    )


def category_name(path: Path) -> str:
    """'PL-11-15 Planilla de instalaciones de MEDICINA NUCLEAR' -> 'Medicina nuclear'."""
    name = re.sub(r"^PL-\d+-\d+\s*", "", path.stem, flags=re.IGNORECASE)
    name = re.sub(r"^Planilla\s+de\s+(instalaciones\s+(de\s+)?|equipos\s+)?",
                  "", name, flags=re.IGNORECASE).strip()
    words = [word for word in name.split() if len(word) > 2]
    if words and all(word.isupper() for word in words):
        name = name.lower()
    return name[:1].upper() + name[1:]


def _find(labels: dict, keys, exclude=()) -> list:
    found = []
    for key in keys:
        for column, label in labels.items():
            if key in label and not any(word in label for word in exclude) and column not in found:
                found.append(column)
    return found


def _header(rows: list):
    """Busca la fila de encabezado y arma una etiqueta por columna."""
    for index, row in enumerate(rows):
        texts = {column: normalize(value) for column, value in enumerate(row)
                 if isinstance(value, str) and value.strip()}
        if not any(key in text for text in texts.values() for key in INSTITUTION_KEYS[:3]) and \
                not any(text.startswith("lugar") for text in texts.values()):
            continue
        if not any("insp" in text or "venc" in text or "fecha" in text for text in texts.values()):
            continue
        labels = dict(texts)
        # Sub-encabezados (mes/año, FECHA:, INSPECTOR:) en las filas siguientes.
        for extra in rows[index + 1:index + 3]:
            if any(parse_date(value) for value in extra):
                break
            for column, value in enumerate(extra):
                if isinstance(value, str) and value.strip():
                    labels[column] = (labels.get(column, "") + " " + normalize(value)).strip()
        return index, labels
    return None, {}


def _date_count(rows, column) -> int:
    return sum(1 for row in rows if column < len(row) and parse_date(row[column]))


def parse_sheet(sheet, source: str, category: str) -> list:
    rows = []
    empty = 0
    for row in sheet.iter_rows(max_col=MAX_COLUMNS, values_only=True):
        if any(value not in (None, "") for value in row):
            empty = 0
            rows.append(list(row))
        else:
            empty += 1
            if empty > MAX_EMPTY_ROWS:
                break
            rows.append([])
    header_index, labels = _header(rows[:HEADER_SCAN_ROWS])
    if header_index is None:
        return []
    data = rows[header_index + 1:]
    institution_cols = _find(labels, INSTITUTION_KEYS, exclude=("de instalacion", "direccion"))
    if not institution_cols:
        return []
    institution_col = institution_cols[0]
    practice_cols = _find(labels, PRACTICE_KEYS, exclude=("fecha",))
    department_cols = _find(labels, DEPARTMENT_KEYS)
    last_cols = sorted(
        _find(labels, LAST_KEYS, exclude=LAST_EXCLUDE),
        key=lambda column: (-_date_count(data, column), column),
    )
    last_col = last_cols[0] if last_cols and _date_count(data, last_cols[0]) else None
    # Si la hoja tiene "nuevo vencimiento 2023" (Médicos y Odontológicos), esa es la
    # única columna válida: la próxima inspección anterior y el mes/año se ignoran.
    renewal_cols = _find(labels, ("nuevo vencimiento",))
    next_cols = renewal_cols[:1] if renewal_cols else sorted(
        (column for column in _find(labels, NEXT_KEYS, exclude=NEXT_EXCLUDE)
         if _date_count(data, column)),
        key=lambda column: (-_date_count(data, column), column),
    )
    if last_col is None and not next_cols:
        return []

    # En algunas planillas la próxima inspección solo figura como mes y año.
    month_col = next((column for column, label in labels.items() if label.endswith(" mes") or label == "mes"), None)
    year_col = next((column for column, label in labels.items() if label == "ano" or label.endswith(" ano")), None)
    generic_sheet = normalize(sheet.title).startswith("hoja") or normalize(sheet.title) in GENERIC_SHEETS

    def cell(row, column):
        return row[column] if column is not None and column < len(row) else None

    records = []
    current = {"institution": "", "department": ""}
    for row in data:
        if not row:
            continue
        own_institution = clean(cell(row, institution_col))
        if own_institution and not own_institution.endswith(":"):
            current = {"institution": own_institution, "department": ""}
        department = clean(cell(row, department_cols[0])) if department_cols else ""
        if department:
            current["department"] = department
        last = parse_date(cell(row, last_col))
        upcoming = next((parse_date(cell(row, column)) for column in next_cols
                         if parse_date(cell(row, column))), None)
        if not upcoming and not renewal_cols and month_col is not None and year_col is not None:
            upcoming = month_year_date(cell(row, month_col), cell(row, year_col), last)
        if not current["institution"] or not (last or upcoming):
            continue
        if any(isinstance(value, str) and BAJA.search(normalize(value)) for value in row):
            continue
        practice_value = cell(row, practice_cols[0]) if practice_cols else None
        practice = clean(practice_value) if isinstance(practice_value, str) else ""
        if normalize(practice) == "mn":
            practice = "Medicina nuclear"
        elif not practice:
            practice = category if generic_sheet else sheet.title.strip().capitalize()
        records.append({
            "institution": current["institution"],
            "practice": practice,
            "department": current["department"],
            "last_inspection": last.isoformat() if last else "",
            "next_inspection": upcoming.isoformat() if upcoming else "",
            "source": source,
            "category": category,
        })
    return records


def parse_workbook(path: Path) -> list:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # avisos de formato condicional no soportado
        workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        category = category_name(path)
        records = []
        for sheet in workbook.worksheets:
            if sheet.sheet_state != "visible" or normalize(sheet.title) in ("altbaj",):
                continue
            records.extend(parse_sheet(sheet, f"{path.stem} · {sheet.title.strip()}", category))
        return records
    finally:
        workbook.close()


def _cached_records(path: Path) -> list:
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
    records = parse_workbook(path)
    with _cache_lock:
        _cache[path] = (key, records)
    return records


def status_for(next_inspection: str, today: date) -> str:
    if not next_inspection:
        return "sin_fecha"
    due = date.fromisoformat(next_inspection)
    if due < today:
        return "vencida"
    if due <= today + timedelta(days=SOON_DAYS):
        return "proxima"
    return "al_dia"


def item_key(institution: str, practice: str) -> str:
    return f"{normalize(institution)}|{normalize(practice)}"


def group_records(records: list, today: date) -> list:
    groups = {}
    for record in records:
        key = (normalize(record["institution"]), normalize(record["practice"]))
        group = groups.setdefault(key, {
            "institution": record["institution"], "practice": record["practice"],
            "department": record["department"], "sources": [], "categories": [], "records": [],
        })
        if not group["department"] and record["department"]:
            group["department"] = record["department"]
        if record["category"] not in group["categories"]:
            group["categories"].append(record["category"])
        if record["source"] not in group["sources"]:
            group["sources"].append(record["source"])
        group["records"].append(record)
    result = []
    for group in groups.values():
        items = group.pop("records")
        last = max((item["last_inspection"] for item in items), default="")
        # La próxima inspección sale del registro con la última inspección más
        # reciente; si hay varios equipos ese día, se toma la más cercana.
        candidates = [item["next_inspection"] for item in items
                      if item["next_inspection"] and item["last_inspection"] == last]
        if not candidates:
            candidates = [item["next_inspection"] for item in items
                          if item["next_inspection"] and item["next_inspection"] >= last]
        upcoming = min(candidates, default="")
        group.update({
            "equipment_count": len(items),
            "last_inspection": last,
            "next_inspection": upcoming,
            "status": status_for(upcoming, today),
        })
        result.append(group)
    result.sort(key=lambda item: (item["next_inspection"] or "9999", normalize(item["institution"])))
    return result


STATUS_LABELS = {
    "vencida": "Vencida", "proxima": "Próxima (90 días)",
    "al_dia": "Al día", "sin_fecha": "Sin próxima fecha",
}
SORTS = ("next", "last", "institution", "practice")


def filter_groups(groups: list, query: str = "", status: str = "", category: str = "",
                  sort: str = "next", month: str = "") -> list:
    """Mismos filtros y orden que la pantalla de planificación.

    month ("AAAA-MM") deja solo las que vencen ese mes.
    """
    query = normalize(query)
    result = [
        group for group in groups
        if (not status or group["status"] == status)
        and (not month or (group["next_inspection"] or "").startswith(month + "-"))
        and (not category or category in group["categories"])
        and (not query or query in normalize(
            f"{group['institution']} {group['practice']} {group['department']}"))
    ]
    by_name = lambda group: (normalize(group["institution"]), normalize(group["practice"]))
    if sort == "last":
        result.sort(key=by_name)
        result.sort(key=lambda group: group["last_inspection"] or "", reverse=True)
    elif sort == "institution":
        result.sort(key=by_name)
    elif sort == "practice":
        result.sort(key=lambda group: (normalize(group["practice"]), normalize(group["institution"])))
    else:
        result.sort(key=lambda group: (group["next_inspection"] or "9999", normalize(group["institution"])))
    return result


def load_planning(root: Path, today: date | None = None) -> dict:
    today = today or date.today()
    files, errors, records = [], [], []
    for path in planning_files(root):
        try:
            found = _cached_records(path)
        except Exception as error:  # planilla abierta, dañada o con formato inesperado
            errors.append({"file": path.name, "error": str(error)[:200]})
            continue
        files.append({"file": path.name, "records": len(found)})
        records.extend(found)
    return {
        "root": str(root),
        "root_exists": root.is_dir(),
        "files": files,
        "errors": errors,
        "groups": group_records(records, today),
        "today": today.isoformat(),
    }
