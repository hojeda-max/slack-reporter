"""
scheduler.py — Dispara el reporte según REPORT_SCHEDULE
Formato: "1:9,2:9,3:9,4:9,5:9"  (weekday ISO : hora)
"""

import logging
import os
import threading
import time
from datetime import datetime

import pytz

from reporter import process_me

log = logging.getLogger(__name__)

_TZ = pytz.timezone("America/Argentina/Buenos_Aires")


def _parse_schedule() -> dict:
    """Lee REPORT_SCHEDULE → {weekday: hour}. Default: lun-vie a las 9."""
    raw = os.environ.get("REPORT_SCHEDULE", "0:9,1:9,2:9,3:9,4:9")
    sched = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            d, h = part.split(":", 1)
            sched[int(d.strip())] = int(h.strip())
    return sched


def _loop():
    while True:
        now   = datetime.now(_TZ)
        sched = _parse_schedule()
        if sched.get(now.weekday()) == now.hour:
            log.info("Scheduler: disparando reporte (%s)", now.strftime("%a %H:%M"))
            try:
                process_me()
            except Exception as e:
                log.error("Scheduler error: %s", e)

        # Dormir hasta el próximo minuto en punto + buffer
        secs_to_next_hour = 3600 - (now.minute * 60 + now.second)
        time.sleep(secs_to_next_hour + 5)


def start_scheduler():
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    t.start()
    log.info("Scheduler iniciado")
