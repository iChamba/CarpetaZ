import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from database import connect_db
from settings import APP_PASSWORD, APP_USER

PASSWORD_ITERATIONS = 310_000
SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
# Tras MAX_LOGIN_FAILURES intentos fallidos en LOGIN_WINDOW_SECONDS, se bloquea
# el usuario durante LOGIN_LOCK_SECONDS. La dirección IP tiene un límite más alto
# porque detrás de un proxy todos los equipos pueden compartir la misma.
MAX_LOGIN_FAILURES = 5
MAX_ADDRESS_FAILURES = 30
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iteration_text, salt_hex, digest_hex = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class LoginThrottle:
    """Limita los intentos de inicio de sesión fallidos, en memoria."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def _keys(username: str, address: str) -> tuple[str, str]:
        return f"user:{username.strip().casefold()}", f"ip:{address}"

    def retry_after(self, username: str, address: str) -> int:
        now = time.monotonic()
        with self._lock:
            remaining = max(
                (self._locked_until.get(key, 0) - now for key in self._keys(username, address)),
                default=0,
            )
        return int(remaining) + 1 if remaining > 0 else 0

    def record_failure(self, username: str, address: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in self._keys(username, address):
                recent = [
                    moment for moment in self._failures.get(key, [])
                    if now - moment < LOGIN_WINDOW_SECONDS
                ]
                recent.append(now)
                limit = MAX_LOGIN_FAILURES if key.startswith("user:") else MAX_ADDRESS_FAILURES
                if len(recent) >= limit:
                    self._locked_until[key] = now + LOGIN_LOCK_SECONDS
                    recent = []
                self._failures[key] = recent
            if len(self._failures) > 10_000:
                self._failures = {key: value for key, value in self._failures.items() if value}
                self._locked_until = {
                    key: until for key, until in self._locked_until.items() if until > now
                }

    def record_success(self, username: str, address: str) -> None:
        # Solo se limpia el contador de la cuenta: si se limpiara el de la IP, alguien
        # con cuenta podría entrar cada tanto con la suya y seguir probando contraseñas
        # ajenas desde la misma IP sin que se bloquee.
        user_key, _address_key = self._keys(username, address)
        with self._lock:
            self._failures.pop(user_key, None)
            self._locked_until.pop(user_key, None)


LOGIN_THROTTLE = LoginThrottle()
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def validate_new_password(password: object) -> str:
    if not isinstance(password, str) or not (
        MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
    ):
        raise ValueError(
            f"La contraseña debe tener entre {MIN_PASSWORD_LENGTH} y "
            f"{MAX_PASSWORD_LENGTH} caracteres."
        )
    return password


def bootstrap_admin(connection) -> None:
    """Crea la cuenta administradora inicial con APP_PASSWORD.

    Si la cuenta ya existe, su contraseña solo se reemplaza cuando se inicia
    con APP_RESET_ADMIN=1, para que reiniciar la aplicación no la sobrescriba.
    """
    if not APP_PASSWORD:
        return
    existing = connection.execute(
        "SELECT id FROM workflow_users WHERE username = ? COLLATE NOCASE",
        (APP_USER,),
    ).fetchone()
    if existing and os.environ.get("APP_RESET_ADMIN", "").strip() != "1":
        return
    password_hash = hash_password(APP_PASSWORD)
    connection.execute(
        "INSERT OR IGNORE INTO workflow_users "
        "(username, display_name, password_hash, role, active, created_at) "
        "VALUES (?, ?, ?, 'admin', 1, ?)",
        (
            APP_USER,
            APP_USER,
            password_hash,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    connection.execute(
        "UPDATE workflow_users SET password_hash = ?, role = 'admin', active = 1 "
        "WHERE username = ? COLLATE NOCASE",
        (password_hash, APP_USER),
    )
    admin = connection.execute(
        "SELECT id FROM workflow_users WHERE username = ? COLLATE NOCASE",
        (APP_USER,),
    ).fetchone()
    connection.execute(
        "DELETE FROM workflow_sessions WHERE user_id = ?", (admin["id"],)
    )


def authenticate(username: str, password: str) -> dict | None:
    if len(username) > 40 or len(password) > 256:
        return None
    connection = connect_db()
    try:
        user = connection.execute(
            "SELECT id, username, display_name, password_hash, role, workflow_role, active "
            "FROM workflow_users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    finally:
        connection.close()
    if not user:
        # Se calcula igual un hash para no revelar por el tiempo de respuesta
        # si el usuario existe.
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None
    if not user["active"] or not verify_password(password, user["password_hash"]):
        return None
    return {key: user[key] for key in ("id", "username", "display_name", "role", "workflow_role")}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    created = datetime.now(timezone.utc)
    expires = created + timedelta(hours=SESSION_HOURS)
    connection = connect_db()
    try:
        connection.execute(
            "DELETE FROM workflow_sessions WHERE expires_at <= ?",
            (created.isoformat(timespec="seconds"),),
        )
        connection.execute(
            "INSERT INTO workflow_sessions (token_hash, user_id, expires_at) "
            "VALUES (?, ?, ?)",
            (token_hash, user_id, expires.isoformat(timespec="seconds")),
        )
        connection.commit()
    finally:
        connection.close()
    return token


def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = connect_db()
    try:
        row = connection.execute(
            "SELECT users.id, users.username, users.display_name, users.role, users.workflow_role "
            "FROM workflow_sessions AS sessions "
            "JOIN workflow_users AS users ON users.id = sessions.user_id "
            "WHERE sessions.token_hash = ? AND sessions.expires_at > ? "
            "AND users.active = 1",
            (token_hash, now),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def change_password(user_id: int, current: object, new: object, confirm: object,
                    keep_token: str) -> bool:
    """Cambia la contraseña propia. Devuelve False si la actual no coincide.

    Cierra las demás sesiones del usuario y conserva la que hizo el cambio.
    """
    if not isinstance(current, str) or len(current) > MAX_PASSWORD_LENGTH:
        return False
    password = validate_new_password(new)
    if confirm != password:
        raise ValueError("La nueva contraseña y su confirmación no coinciden.")
    connection = connect_db()
    try:
        row = connection.execute(
            "SELECT password_hash FROM workflow_users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
        if not row or not verify_password(current, row["password_hash"]):
            return False
        if verify_password(password, row["password_hash"]):
            raise ValueError("La nueva contraseña debe ser distinta de la actual.")
        keep_hash = hashlib.sha256(keep_token.encode("ascii", errors="ignore")).hexdigest()
        connection.execute(
            "UPDATE workflow_users SET password_hash = ? WHERE id = ?",
            (hash_password(password), user_id),
        )
        connection.execute(
            "DELETE FROM workflow_sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, keep_hash),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def revoke_session(token: str) -> None:
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    connection = connect_db()
    try:
        connection.execute(
            "DELETE FROM workflow_sessions WHERE token_hash = ?", (token_hash,)
        )
        connection.commit()
    finally:
        connection.close()
