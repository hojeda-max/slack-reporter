"""
scheduler.py — Hilo de fondo que dispara reportes según el schedule de cada usuario
"""

import logging
import threading
import time
from datetime import datetime

import pytz

from database import get_users_due_now
from reporter import process_user

log = logging.getLogger(__name__)

_TZ = pytz.timezone("America/Argentina/Buenos_Aires")


def run_daily_reports():
    now   = datetime.now(_TZ)
    wday  = now.weekday()   # 0=lun … 6=dom
    hour  = now.hour

    log.info("Scheduler tick: %s (wday=%d hour=%d)", now.strftime("%H:%M"), wday, hour)

    users = get_users_due_now(wday, hour)
    log.info("  → %d usuario(s) programados para ahora", len(users))

    for user in users:
        try:
            process_user(user)
        except Exception as e:
            log.error("Error procesando %s: %s", user.get("slack_user_id"), e)


def _loop():
    while True:
        try:
            run_daily_reports()
        except Exception as e:
            log.error("Scheduler error: %s", e)
        # Dormir hasta el próximo minuto redondo (sincronizado a la hora en punto)
        now = datetime.now(_TZ)
        sleep_secs = 3600 - (now.minute * 60 + now.second)
        log.debug("Próximo tick en %ds", sleep_secs)
        time.sleep(sleep_secs)


def start_scheduler():
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    t.start()
    log.info("Scheduler iniciado")
