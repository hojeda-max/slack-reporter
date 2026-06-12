"""
reporter.py — Pipeline de reportes (owner vía User Token, equipo vía Bot Token)
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic
import pytz

from slack_client import (
    fetch_channel_messages,
    get_my_user_id,
    list_bot_channels,
    list_user_channels,
    resolve_user_names,
    send_dm,
)

log = logging.getLogger(__name__)

_AI         = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
_HAIKU      = "claude-haiku-4-5-20251001"
_SONNET     = "claude-sonnet-4-6"
_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")
_TZ         = pytz.timezone("America/Argentina/Buenos_Aires")


# ── Triage ────────────────────────────────────────────────────────────────────

def triage_messages(messages: list[dict], channel_names: dict) -> dict:
    if not messages:
        return {}

    lines = []
    for m in messages[:300]:
        ch = channel_names.get(m["channel_id"], m["channel_id"])
        lines.append(f'[{m["id"]}] #{ch} | {m["text"][:200]}')

    prompt = (
        "Clasificá estos mensajes de Slack:\n"
        '{"msg_id": {"label": "importante|informativo|ruido", "topic": "tema breve"}}\n\n'
        "- importante: requiere acción, decisión o seguimiento\n"
        "- informativo: actualización útil, no requiere acción\n"
        "- ruido: saludos, reacciones, triviales\n\n"
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


# ── Generación del reporte ────────────────────────────────────────────────────

def generate_report(messages: list, triage: dict, channel_names: dict,
                    user_names: dict, for_user_id: str,
                    since: datetime, until: datetime) -> str:
    """
    Genera el reporte para `for_user_id`.
    Si for_user_id está definido, filtra solo sus mensajes para las secciones
    de acciones/decisiones, pero muestra el contexto completo del canal.
    """
    # Mensajes del propio usuario (para acciones pendientes)
    mine = [m for m in messages if m.get("user_id") == for_user_id] if for_user_id else messages

    important   = [m for m in messages if triage.get(m["id"], {}).get("label") == "importante"]
    informative = [m for m in messages if triage.get(m["id"], {}).get("label") == "informativo"]

    def fmt(msgs, limit=50):
        parts = []
        for m in msgs[:limit]:
            ch    = channel_names.get(m["channel_id"], m["channel_id"])
            who   = user_names.get(m["user_id"], m["user_id"])
            topic = triage.get(m["id"], {}).get("topic", "")
            parts.append(f"[#{ch}] {who}: {m['text'][:300]}" + (f" ({topic})" if topic else ""))
        return "\n".join(parts)

    period    = f"{since.strftime('%d/%m %H:%M')} → {until.strftime('%d/%m %H:%M')}"
    user_name = user_names.get(for_user_id, "vos") if for_user_id else "vos"

    prompt = f"""Generá un resumen de actividad de Slack para {user_name}.

Período: {period}
Total mensajes en canales compartidos: {len(messages)} ({len(important)} importantes)
Mensajes propios de {user_name}: {len(mine)}

MENSAJES IMPORTANTES (de toda la conversación):
{fmt(important) or "(ninguno)"}

MENSAJES INFORMATIVOS:
{fmt(informative) or "(ninguno)"}

Generá el reporte en formato Slack (mrkdwn):
*📊 Resumen del período* — 2-3 oraciones sobre qué pasó
*⚡ Acciones pendientes* — bullets de lo que requiere atención
*✅ Decisiones tomadas* — bullets
*🏷️ Temas del período* — etiquetas de temas de negocio

Usá *negrita*, bullet • por ítem. Español. Conciso y directo."""

    resp = _AI.messages.create(
        model=_SONNET,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ── Proceso owner (User Token, para vos) ─────────────────────────────────────

def process_me(lookback_days: int = 1):
    if not _USER_TOKEN:
        log.error("SLACK_USER_TOKEN no configurado.")
        return
    log.info("Reporte owner (últimos %d días)…", lookback_days)

    now, since, since_ts, until_ts = _time_range(lookback_days)
    channels      = list_user_channels(_USER_TOKEN)
    channel_names = {ch["id"]: ch["name"] for ch in channels}
    all_messages  = _fetch_all(channels, _USER_TOKEN, since_ts, until_ts)

    if not all_messages:
        log.info("Sin mensajes en el período.")
        return

    my_id      = get_my_user_id(_USER_TOKEN)
    user_names = resolve_user_names(_USER_TOKEN, {m["user_id"] for m in all_messages if m.get("user_id")})
    triage     = triage_messages(all_messages, channel_names)
    report     = generate_report(all_messages, triage, channel_names, user_names, my_id, since, now)

    send_dm(_USER_TOKEN, my_id, report)
    log.info("Reporte owner enviado.")


# ── Proceso por pedido de equipo (Bot Token) ──────────────────────────────────

def process_user_request(requesting_user_id: str, lookback_days: int = 1):
    """
    Genera y envía un reporte a `requesting_user_id` via Bot Token.
    Lee los canales donde el bot está invitado y filtra mensajes del usuario.
    """
    if not _BOT_TOKEN:
        log.error("SLACK_BOT_TOKEN no configurado.")
        return

    log.info("Reporte para %s (últimos %d días)…", requesting_user_id, lookback_days)

    now, since, since_ts, until_ts = _time_range(lookback_days)
    channels      = list_bot_channels(_BOT_TOKEN)
    channel_names = {ch["id"]: ch["name"] for ch in channels}
    all_messages  = _fetch_all(channels, _BOT_TOKEN, since_ts, until_ts)

    if not all_messages:
        send_dm(_BOT_TOKEN, requesting_user_id,
                "No encontré mensajes en los canales compartidos para ese período.")
        return

    user_names = resolve_user_names(_BOT_TOKEN, {m["user_id"] for m in all_messages if m.get("user_id")})
    triage     = triage_messages(all_messages, channel_names)
    report     = generate_report(all_messages, triage, channel_names, user_names,
                                 requesting_user_id, since, now)

    send_dm(_BOT_TOKEN, requesting_user_id, report)
    log.info("Reporte enviado a %s.", requesting_user_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _time_range(lookback_days: int):
    now   = datetime.now(_TZ)
    since = now - timedelta(days=lookback_days)
    return now, since, since.timestamp(), now.timestamp()


def _fetch_all(channels: list, token: str, since_ts: float, until_ts: float) -> list:
    from slack_client import fetch_channel_messages
    messages = []
    for ch in channels:
        try:
            msgs = fetch_channel_messages(token, ch["id"], since_ts, until_ts)
            if msgs:
                log.info("  #%s: %d mensajes", ch["name"], len(msgs))
            messages.extend(msgs)
        except Exception as e:
            log.warning("  error en #%s: %s", ch["name"], e)
    log.info("Total: %d mensajes en %d canales", len(messages),
             len(set(m["channel_id"] for m in messages)))
    return messages
