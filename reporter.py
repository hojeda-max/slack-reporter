"""
reporter.py — Pipeline principal: fetch → triage → análisis → envío
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic

from database import (get_effective_schedule, log_report, update_last_report)
from slack_client import (
    fetch_channel_messages,
    list_user_channels,
    resolve_user_names,
    send_dm_report,
)

log = logging.getLogger(__name__)

_AI = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
_HAIKU  = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"

_TZ = "America/Argentina/Buenos_Aires"


# ── Triage rápido (Haiku) ─────────────────────────────────────────────────────

def triage_messages(messages: list[dict], channel_names: dict) -> dict:
    """
    Clasifica cada mensaje en: importante / informativo / ruido
    Devuelve {msg_id: {"label": ..., "topic": ...}}
    """
    if not messages:
        return {}

    lines = []
    for m in messages[:300]:  # límite para no sobrepasar contexto
        ch = channel_names.get(m["channel_id"], m["channel_id"])
        lines.append(f'[{m["id"]}] #{ch} | {m["text"][:200]}')

    prompt = (
        "Tenés que clasificar mensajes de Slack de un equipo de trabajo.\n"
        "Para cada mensaje, devolvé JSON:\n"
        '{"msg_id": {"label": "importante|informativo|ruido", "topic": "tema breve"}}\n\n'
        "- importante: requiere acción, decisión o seguimiento\n"
        "- informativo: actualización útil, no requiere acción\n"
        "- ruido: saludos, reacciones sueltas, mensajes triviales\n\n"
        "Mensajes:\n" + "\n".join(lines) + "\n\nRespondé SOLO con el JSON."
    )

    try:
        resp = _AI.messages.create(
            model=_HAIKU,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        import json, re
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning("triage error: %s", e)
        return {}


# ── Análisis profundo (Sonnet) ────────────────────────────────────────────────

def generate_report(messages: list[dict], triage: dict,
                    channel_names: dict, user_names: dict,
                    user_display: str, since: datetime, until: datetime) -> str:
    """
    Genera el reporte HTML a partir de los mensajes y su triage.
    """
    important = [m for m in messages if triage.get(m["id"], {}).get("label") == "importante"]
    informative = [m for m in messages if triage.get(m["id"], {}).get("label") == "informativo"]

    def fmt(msgs):
        parts = []
        for m in msgs[:50]:
            ch = channel_names.get(m["channel_id"], m["channel_id"])
            who = user_names.get(m["user_id"], m["user_id"])
            topic = triage.get(m["id"], {}).get("topic", "")
            parts.append(f"[#{ch}] {who}: {m['text'][:300]}" + (f" ({topic})" if topic else ""))
        return "\n".join(parts)

    period = f"{since.strftime('%d/%m %H:%M')} → {until.strftime('%d/%m %H:%M')}"

    prompt = f"""Sos un asistente que genera reportes diarios de actividad de Slack para {user_display}.

Período: {period}
Total mensajes: {len(messages)} ({len(important)} importantes, {len(informative)} informativos)

MENSAJES IMPORTANTES (requieren acción/decisión):
{fmt(important) or "(ninguno)"}

MENSAJES INFORMATIVOS:
{fmt(informative) or "(ninguno)"}

Generá un reporte HTML conciso con:
1. **Resumen ejecutivo** (2-3 oraciones del día)
2. **Acciones pendientes** (bullets con qué, en qué canal, quién lo pidió)
3. **Decisiones tomadas** (bullets)
4. **Actualizaciones relevantes** (bullets breves)
5. **Temas del día** (lista de etiquetas de temas de negocio)

Estilo: HTML limpio con <h2>, <ul>, <li>, <strong>. Sin <html>/<body>/<head>.
Idioma: español. Tono: profesional y directo. Máximo 600 palabras."""

    resp = _AI.messages.create(
        model=_SONNET,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ── Proceso por usuario ───────────────────────────────────────────────────────

def process_user(user: dict, lookback_days: int = None):
    uid   = user["slack_user_id"]
    token = user["access_token"]
    name  = user.get("display_name") or uid

    log.info("Procesando %s (%s)", name, uid)

    # Período
    now = datetime.now(timezone.utc)
    if lookback_days:
        since = now - timedelta(days=lookback_days)
    else:
        since = now - timedelta(days=1)  # últimas 24h por defecto

    since_ts = since.timestamp()
    until_ts = now.timestamp()

    # Canales
    try:
        channels = list_user_channels(token)
    except Exception as e:
        log.error("  → error listando canales: %s", e)
        return

    if not channels:
        log.info("  → sin canales")
        return

    channel_names = {ch["id"]: ch["name"] for ch in channels}

    # Fetch mensajes de todos los canales
    all_messages = []
    for ch in channels:
        try:
            msgs = fetch_channel_messages(token, ch["id"], since_ts, until_ts)
            all_messages.extend(msgs)
            log.info("  → #%s: %d mensajes", ch["name"], len(msgs))
        except Exception as e:
            log.warning("  → error en #%s: %s", ch["name"], e)

    if not all_messages:
        log.info("  → sin mensajes en el período")
        return

    log.info("  → total: %d mensajes en %d canales", len(all_messages), len(channels))

    # Resolver nombres de usuarios
    user_ids = {m["user_id"] for m in all_messages if m.get("user_id")}
    user_names = resolve_user_names(token, user_ids)

    # Triage
    triage = triage_messages(all_messages, channel_names)

    # Reporte
    html = generate_report(
        all_messages, triage, channel_names, user_names,
        name, since, now
    )

    # Pie con link de configuración
    html += _footer(uid)

    # Enviar DM
    send_dm_report(token, uid, html)

    # Actualizar DB
    update_last_report(uid)
    log_report(
        uid,
        since, now,
        len(all_messages),
        len(set(m["channel_id"] for m in all_messages)),
    )

    log.info("  → reporte enviado a %s", name)


def _footer(slack_user_id: str) -> str:
    app_url = os.environ.get("APP_URL", "")
    if not app_url:
        return ""
    return (
        f'\n\n<p style="font-size:12px;color:#888;">'
        f'<a href="{app_url}/mis-reportes?uid={slack_user_id}">Configurar horario</a> · '
        f'<a href="{app_url}/unsubscribe?uid={slack_user_id}">Cancelar suscripción</a>'
        f"</p>"
    )
