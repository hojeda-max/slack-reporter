"""
app.py — Slack Reporter (single-user, token directo)
"""

import logging
import os
import threading

from flask import Flask, jsonify, render_template_string

from reporter import process_me
from scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
if not _ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN no configurado")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/trigger")
def trigger():
    """Disparo manual del reporte. Requiere ?token=ADMIN_TOKEN"""
    from flask import request
    token = request.args.get("token", "")
    import hmac
    if not hmac.compare_digest(token, _ADMIN_TOKEN):
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    lookback = int(request.args.get("days", 1))
    lookback = max(1, min(30, lookback))

    def _run():
        try:
            process_me(lookback_days=lookback)
        except Exception as e:
            log.error("trigger error: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": f"Generando reporte de los últimos {lookback} días…"})


start_scheduler()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
