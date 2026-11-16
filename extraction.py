import re
import unicodedata
from datetime import date
from pathlib import Path

import pymupdf


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _value_after_label(lines: list[str], labels: list[str]) -> str:
    patterns = [re.compile(label, re.IGNORECASE) for label in labels]
    for index, line in enumerate(lines):
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            value = line[match.end():].strip(" \t:;|.-")
            value = re.sub(
                r"^(?:nro\.?|n[uú]mero|c\.?\s*i\.?)\s*[:.-]?\s*",
                "",
                value,
                flags=re.IGNORECASE,
            )
            if value:
                return value[:500]
            for next_line in lines[index + 1:index + 3]:
                candidate = next_line.strip(" \t:;|.-")
                if candidate and not re.search(
                    r"^(?:nombres?|apellidos?|nro\.?\s*documento|fecha)\b",
                    candidate,
                    re.IGNORECASE,
                ):
                    return candidate[:500]
    return ""


def extract_candidate_fields(text: str) -> dict[str, str]:
    lines = [normalized(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    first_name = _value_after_label(lines, [
        r"\bnombres?\b",
        r"\bprimer\s+nombre\b",
    ])
    last_name = _value_after_label(lines, [
        r"\bapellidos?\b",
        r"\bprimer\s+apellido\b",
    ])
    identity_number = _value_after_label(lines, [
        r"\bnro\.?\s*(?:de\s*)?documento\b",
        r"\bn[uú]mero\s+(?:de\s+)?documento\b",
        r"\bdocumento\s+de\s+identidad\b",
        r"\bc[eé]dula\s+de\s+identidad\b",
        r"\bC\.?\s*I\.?\b",
        r"\bC\.?\s*I\.?\s*(?::|Nro\.?)",
    ])
    identity_match = re.search(r"\b\d{1,2}(?:\.\d{3}){2}-\d\b", identity_number)
    if not identity_match:
        identity_match = re.search(r"\b\d{6,9}\b", identity_number)
    if identity_match:
        identity_number = identity_match.group(0)

    authorization_number = _value_after_label(lines, [
        r"\bn(?:ro|[uú]m(?:ero)?)\.?\s*(?:de\s*)?autorizaci[oó]n\b",
        r"\bn[°º]\s*(?:de\s*)?autorizaci[oó]n\b",
        r"\bno\.?\s*(?:de\s*)?autorizaci[oó]n\b",
        r"\bautorizaci[oó]n\s+(?:nro\.?|n[uú]mero)\b",
    ])
    auth_match = re.search(
        r"\b[A-Z0-9]{1,8}\s*/\s*[A-Z0-9]{1,8}\s*/\s*[A-Z0-9]{1,8}\b",
        authorization_number,
        re.IGNORECASE,
    )
    if auth_match:
        authorization_number = re.sub(r"\s+", "", auth_match.group(0))
    else:
        authorization_number = ""
        all_text = "\n".join(lines)
        auth_context = re.search(
            r"autorizaci[oó]n.{0,100}?\b([A-Z0-9]{1,8}\s*/\s*[A-Z0-9]{1,8}\s*/\s*[A-Z0-9]{1,8})\b",
            all_text,
            re.IGNORECASE | re.DOTALL,
        )
        if auth_context:
            authorization_number = re.sub(r"\s+", "", auth_context.group(1))

    equipment_lines: list[str] = []
    equipment_start = None
    for index, line in enumerate(lines):
        if re.search(r"\bequip\w*\b", line, re.IGNORECASE) and re.search(
            r"\b(habilitad\w*|autorizad\w*)\b",
            line,
            re.IGNORECASE,
        ):
            equipment_start = index + 1
            break
    if equipment_start is not None:
        stop = re.compile(
            r"\b(firma|observaciones|declaraci[oó]n|notificaciones|resoluci[oó]n)\b",
            re.IGNORECASE,
        )
        for line in lines[equipment_start:]:
            if stop.search(line):
                break
            if len(line) > 1:
                equipment_lines.append(line[:300])
            if len(equipment_lines) >= 30:
                break

    return {
        "first_name": first_name,
        "last_name": last_name,
        "identity_number": identity_number,
        "authorization_number": authorization_number,
        "equipment": "\n".join(equipment_lines),
        "practice": "",
        "issue_date": "",
        "expiry_date": "",
    }


MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "setiembre": 9,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def _numeric_date(day: str, month: str, year: str) -> str:
    try:
        return date(int(year), int(month), int(day)).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def _certificate_issue_date(text: str) -> str:
    match = re.search(
        r"\b(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóú]+)\s+de\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        month = MONTHS.get(normalized(match.group(2)).casefold())
        if month:
            return _numeric_date(match.group(1), str(month), match.group(3))

    match = re.search(
        r"\b(?:expedici[oó]n|emisi[oó]n)\s*:?\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return _numeric_date(match.group(1), match.group(2), match.group(3))
    return ""


def _certificate_expiry_date(text: str) -> str:
    match = re.search(
        r"\b(?:vencimiento|vence\s+el|vigente\s+hasta)\s*:?\s*"
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return _numeric_date(match.group(1), match.group(2), match.group(3))
    return ""


def _certificate_practice(text: str) -> str:
    match = re.search(
        r"\bHa\s+sido\s+autorizad[oa](?:/a)?\s+para\s+la\s+pr[aá]ctica\s+de\s*:?\s*"
        r"(.*?)\s*(?=\bde\s+acuerdo\b|\bobservaciones\s*:|\bvencimiento\s*:)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return " ".join(match.group(1).split()).strip(" :;")


def _fold_header(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(value.casefold().split())


def _equipment_from_table(rows: list[list[str | None]]) -> list[str]:
    headers: list[str] | None = None
    header_index = -1
    for index, row in enumerate(rows):
        candidate = [_fold_header(value or "") for value in row]
        if any("tipo de equipo" in value for value in candidate) and any(
            "fabricante" in value for value in candidate
        ):
            headers = candidate
            header_index = index
            break
    if headers is None:
        return []

    column_labels = {
        "Tipo de equipo": ("tipo de equipo",),
        "Fabricante": ("fabricante",),
        "Modelo": ("modelo",),
        "Radiación": ("radiacion",),
        "Práctica": ("practica",),
    }
    columns = {
        label: next(
            (index for index, header in enumerate(headers) if any(key in header for key in keys)),
            None,
        )
        for label, keys in column_labels.items()
    }
    equipment: list[str] = []
    for row in rows[header_index + 1:]:
        values = []
        for label, index in columns.items():
            if index is None or index >= len(row) or not row[index]:
                continue
            value = " ".join(row[index].split())
            value = re.sub(r"(?<=\w)-\s+(?=\w)", "-", value)
            values.append(f"{label}: {value}")
        if values:
            equipment.append(" | ".join(values))
    return equipment


def extract_certificate_fields(
    text: str,
    equipment_rows: list[list[str | None]],
) -> dict[str, str]:
    fields = extract_candidate_fields(text)
    fields["practice"] = _certificate_practice(text)
    fields["issue_date"] = _certificate_issue_date(text)
    fields["expiry_date"] = _certificate_expiry_date(text)
    equipment = _equipment_from_table(equipment_rows)
    if equipment:
        fields["equipment"] = "\n".join(equipment)
    return fields


def is_authorization_certificate(path: Path) -> bool:
    name = unicodedata.normalize("NFKD", path.stem).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-z0-9]", "", name.casefold())
    if "certificadoautorizacionindividualarnr" in name:
        return True
    if "renov" not in name or "autorizacionindividual" not in name:
        return False
    return not any(term in name for term in ("solicitud", "formulario", "tramite"))


def is_authorization_individual(text: str) -> bool:
    if not text.strip():
        return False
    fields = extract_candidate_fields(text)
    if not fields["authorization_number"]:
        return False

    plain_text = unicodedata.normalize("NFKD", text)
    plain_text = plain_text.encode("ascii", "ignore").decode("ascii").casefold()
    granted = re.search(
        r"\bha\s+sido\s+autorizad[oa](?:/a)?\b|\bse\s+autoriz[oa]\b",
        plain_text,
    )
    if granted:
        return True

    request_terms = re.search(
        r"\b(solicitud|formulario|solicita|solicitar|tramite|dictamen)\b",
        plain_text,
    )
    has_heading = re.search(r"\bautorizacion\s+individual\b", plain_text)
    has_certificate_data = any((
        fields["practice"],
        fields["issue_date"],
        fields["expiry_date"],
        fields["equipment"],
    ))
    return bool(has_heading and has_certificate_data and not request_terms)


def extract_authorization_certificate(path: Path) -> dict[str, str]:
    with pymupdf.open(path) as document:
        text = "\n".join(page.get_text("text", sort=True) for page in document)
        equipment_rows: list[list[str | None]] = []
        for page in document:
            for table in page.find_tables().tables:
                equipment_rows.extend(table.extract())
    return extract_certificate_fields(text, equipment_rows)
