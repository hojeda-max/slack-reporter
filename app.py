"""
app.py — Slack Reporter
"""

import hashlib
import hmac
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

from reporter import process_me, process_user_request
from scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

_ADMIN_TOKEN        = os.environ.get("ADMIN_TOKEN", "")
_SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

if not _ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN no configurado")


# ── Verificación de requests de Slack ────────────────────────────────────────

def _verify_slack(req) -> bool:
    """Verifica la firma de Slack para asegurar que el request es legítimo."""
    if not _SLACK_SIGNING_SECRET:
        log.warning("SLACK_SIGNING_SECRET no configurado — verificación desactivada")
        return True
    ts        = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    if abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        _SLACK_SIGNING_SECRET.encode(),
        base.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ── Trigger manual (admin) ────────────────────────────────────────────────────

@app.post("/trigger")
def trigger():
    token    = request.args.get("token", "")
    if not hmac.compare_digest(token, _ADMIN_TOKEN):
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    lookback = max(1, min(30, int(request.args.get("days", 1))))

    threading.Thread(target=process_me, args=(lookback,), daemon=True).start()
    return jsonify({"ok": True, "message": f"Generando reporte de los últimos {lookback} días…"})


# ── Slack Events API ──────────────────────────────────────────────────────────

@app.post("/slack/events")
def slack_events():
    if not _verify_slack(request):
        return jsonify({"error": "invalid signature"}), 403

    data = request.get_json(force=True)

    # Verificación inicial de Slack
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})

    # Solo procesar DMs al bot
    if event.get("type") == "message" and event.get("channel_type") == "im":
        # Ignorar mensajes del propio bot
        if event.get("bot_id") or event.get("subtype"):
            return jsonify({"ok": True})

        user_id = event.get("user", "")
        text    = (event.get("text") or "").strip().lower()

        if not user_id:
            return jsonify({"ok": True})

        lookback = _parse_days(text)

        if lookback is None:
            # Mensaje de ayuda
            from slack_client import send_dm
            bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
            if bot_token:
                send_dm(bot_token, user_id,
                        "Hola! Para pedir tu reporte escribí:\n"
                        "• *reporte* — resumen del último día\n"
                        "• *reporte 7* — resumen de los últimos 7 días\n"
                        "• *reporte 30* — resumen del último mes")
            return jsonify({"ok": True})

        def _run():
            try:
                process_user_request(user_id, lookback_days=lookback)
            except Exception as e:
                log.error("slack event error: %s", e)

        threading.Thread(target=_run, daemon=True).start()

    return jsonify({"ok": True})


def _parse_days(text: str) -> int | None:
    """
    Extrae la cantidad de días del mensaje.
    Ejemplos: "reporte", "reporte 7", "dame el reporte de 3 días", "últimos 14 días"
    Devuelve None si el mensaje no es un pedido de reporte.
    """
    import re
    keywords = ("reporte", "resumen", "informe", "report")
    if not any(k in text for k in keywords):
        return None

    # Buscar número en el mensaje
    match = re.search(r"\b(\d+)\b", text)
    if match:
        days = int(match.group(1))
        return max(1, min(30, days))
    return 1  # default: 1 día


# ── Init ──────────────────────────────────────────────────────────────────────

start_scheduler()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
