"""Actualización automática: cada cierto tiempo recorre las carpetas
(archivos nuevos, modificados y borrados) y procesa solo lo que cambió."""
import logging
import threading
import time
from datetime import datetime

from documents import start_catalog
from routes.entities import run_entity_import_now
from routes.records import run_certificate_import_now
from settings import INDEX_STATE, INDEX_STATE_LOCK


def _catalog_running() -> bool:
    with INDEX_STATE_LOCK:
        return bool(INDEX_STATE["catalog_running"])


def wait_for_catalog(stop: threading.Event, poll_seconds: float = 2) -> None:
    while _catalog_running() and not stop.is_set():
        stop.wait(poll_seconds)


def run_cycle(stop: threading.Event, poll_seconds: float = 2) -> None:
    """Un ciclo completo. Si alguien ya inició a mano alguna etapa, se espera o se omite."""
    # Si el catálogo ya está corriendo (al iniciar la app o por el botón), se espera.
    if not _catalog_running():
        start_catalog()
        # El hilo del catálogo marca catalog_running al iniciar.
    wait_for_catalog(stop, poll_seconds)
    if stop.is_set():
        return
    run_certificate_import_now()
    if stop.is_set():
        return
    run_entity_import_now(incremental=True)
    with INDEX_STATE_LOCK:
        INDEX_STATE["auto_refresh_last"] = datetime.now().isoformat(timespec="seconds")


def start_auto_refresh(interval_minutes: float, poll_seconds: float = 2) -> threading.Event:
    """Inicia el hilo que repite run_cycle; entre un ciclo y el siguiente espera
    interval_minutes desde que terminó el anterior, así nunca se superponen."""
    stop = threading.Event()
    with INDEX_STATE_LOCK:
        INDEX_STATE["auto_refresh_minutes"] = interval_minutes

    def loop() -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                run_cycle(stop, poll_seconds)
                logging.info(
                    "Actualización automática completada en %.0f s", time.monotonic() - started
                )
            except Exception:
                logging.exception("Falló la actualización automática; se reintentará")
            stop.wait(interval_minutes * 60)

    threading.Thread(target=loop, name="actualizacion-automatica", daemon=True).start()
    return stop
