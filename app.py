"""
app.py — Flask app para slack-reporter
"""

import hashlib
import hmac
import logging
import os
import threading
import urllib.parse

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from database import (
    deactivate_user,
    get_active_users,
    get_all_users,
    get_effective_schedule,
    get_user,
    init_db,
    update_report_schedule,
    upsert_user,
)
from reporter import process_user
from scheduler import start_scheduler

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY no configurada")

_SLACK_CLIENT_ID     = os.environ["SLACK_CLIENT_ID"]
_SLACK_CLIENT_SECRET = os.environ["SLACK_CLIENT_SECRET"]
_SLACK_REDIRECT_URI  = os.environ.get("SLACK_REDIRECT_URI", "")
_APP_URL             = os.environ.get("APP_URL", "")
_ADMIN_TOKEN         = os.environ.get("ADMIN_TOKEN", "")
if not _ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN no configurado")

_SCOPES = ",".join([
    "openid", "email", "profile",
    "channels:history", "channels:read",
    "groups:history", "groups:read",
    "im:history", "im:read",
    "mpim:history", "mpim:read",
    "chat:write",
    "users:read",
])

# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_admin(token: str) -> bool:
    return bool(_ADMIN_TOKEN) and hmac.compare_digest(token, _ADMIN_TOKEN)


def _admin_token_from_request() -> str:
    return (
        request.args.get("token")
        or request.form.get("token")
        or request.headers.get("X-Admin-Token", "")
    )


# ── OAuth ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html", app_url=_APP_URL)


@app.get("/login")
def login():
    state = os.urandom(16).hex()
    session["oauth_state"] = state
    params = {
        "client_id":     _SLACK_CLIENT_ID,
        "scope":         _SCOPES,
        "redirect_uri":  _SLACK_REDIRECT_URI,
        "state":         state,
        "response_type": "code",
    }
    url = "https://slack.com/openid/connect/authorize?" + urllib.parse.urlencode(params)
    return redirect(url)


@app.get("/oauth/callback")
def oauth_callback():
    error = request.args.get("error")
    if error:
        return render_template("error.html", message=f"Error de autorización: {error}")

    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if state != session.pop("oauth_state", None):
        return render_template("error.html", message="Estado OAuth inválido.")

    # Exchange code por token
    resp = requests.post("https://slack.com/api/openid.connect.token", data={
        "client_id":     _SLACK_CLIENT_ID,
        "client_secret": _SLACK_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  _SLACK_REDIRECT_URI,
        "grant_type":    "authorization_code",
    })
    data = resp.json()
    if not data.get("ok"):
        return render_template("error.html", message=f"Error al obtener token: {data.get('error')}")

    access_token = data["access_token"]

    # Info del usuario
    user_resp = requests.get("https://slack.com/api/openid.connect.userInfo", headers={
        "Authorization": f"Bearer {access_token}"
    })
    uinfo = user_resp.json()

    slack_user_id = uinfo["sub"]
    team_id       = uinfo.get("https://slack.com/team_id", "")
    email         = uinfo.get("email", "")
    display_name  = uinfo.get("name", "")

    existing = get_user(slack_user_id)
    upsert_user(slack_user_id, team_id, email, display_name, access_token)

    return render_template(
        "success.html",
        display_name=display_name,
        is_new_user=existing is None,
        slack_user_id=slack_user_id,
    )


# ── Self-service ──────────────────────────────────────────────────────────────

@app.get("/mis-reportes")
def mis_reportes():
    uid  = request.args.get("uid", "")
    user = get_user(uid) if uid else None
    if not user:
        return render_template("error.html", message="Usuario no encontrado.")
    sched = get_effective_schedule(user)
    return render_template("mis_reportes.html", user=user, current_schedule=sched)


@app.post("/mis-reportes/guardar")
def mis_reportes_guardar():
    import json
    uid  = request.form.get("uid", "")
    user = get_user(uid) if uid else None
    if not user:
        return jsonify({"ok": False, "error": "Usuario no encontrado"}), 404
    raw  = request.form.get("schedule", "{}")
    try:
        sched = json.loads(raw)
        sched = {str(k): int(v) for k, v in sched.items()}
    except Exception:
        return jsonify({"ok": False, "error": "Schedule inválido"}), 400
    update_report_schedule(uid, sched)
    return jsonify({"ok": True})


@app.post("/set-schedule")
def set_schedule():
    import json
    uid  = request.form.get("uid") or request.json.get("uid", "") if request.is_json else request.form.get("uid", "")
    if request.is_json:
        data  = request.get_json()
        uid   = data.get("uid", "")
        sched = data.get("schedule", {})
    else:
        uid   = request.form.get("uid", "")
        sched = json.loads(request.form.get("schedule", "{}"))

    user = get_user(uid) if uid else None
    if not user:
        return jsonify({"ok": False, "error": "Usuario no encontrado"}), 404

    sched = {str(k): int(v) for k, v in sched.items()}
    update_report_schedule(uid, sched)
    return jsonify({"ok": True})


@app.get("/unsubscribe")
def unsubscribe():
    uid  = request.args.get("uid", "")
    user = get_user(uid) if uid else None
    if not user:
        return render_template("error.html", message="Usuario no encontrado.")
    deactivate_user(uid)
    return render_template("unsubscribed.html", display_name=user.get("display_name", uid))


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.get("/admin")
def admin_dashboard():
    token = _admin_token_from_request()
    if not _check_admin(token):
        return "No autorizado", 403
    users = get_all_users()
    for u in users:
        u["schedule"] = get_effective_schedule(u)
    return render_template("admin.html", users=users, admin_token=token)


@app.post("/admin/send-report")
def admin_send_report():
    token = _admin_token_from_request()
    if not _check_admin(token):
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    uid          = request.form.get("uid", "")
    lookback_days = int(request.form.get("lookback_days", 1))
    lookback_days = max(1, min(30, lookback_days))

    user = get_user(uid)
    if not user:
        return jsonify({"ok": False, "error": "Usuario no encontrado"}), 404

    def _run():
        try:
            process_user(user, lookback_days=lookback_days)
        except Exception as e:
            log.error("admin send-report error: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": f"Generando reporte para {user.get('display_name')}…"})


@app.post("/admin/toggle-user")
def admin_toggle_user():
    token = _admin_token_from_request()
    if not _check_admin(token):
        return jsonify({"ok": False, "error": "No autorizado"}), 403
    import psycopg2
    from database import _conn
    uid    = request.form.get("uid", "")
    active = request.form.get("active", "true") == "true"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET active = %s WHERE slack_user_id = %s", (active, uid))
        conn.commit()
    return jsonify({"ok": True})


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    try:
        init_db()
    except Exception as e:
        log.error("init_db error: %s", e)

start_scheduler()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
