"""
reporter.py — Pipeline principal para un solo usuario
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic

from slack_client import (
    fetch_channel_messages,
    get_my_user_id,
    list_user_channels,
    resolve_user_names,
    send_dm_to_me,
)

log = logging.getLogger(__name__)

_AI     = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
_HAIKU  = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"
_TOKEN  = os.environ.get("SLACK_USER_TOKEN", "")


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


def generate_report(messages, triage, channel_names, user_names,
                    since: datetime, until: datetime) -> str:
    important   = [m for m in messages if triage.get(m["id"], {}).get("label") == "importante"]
    informative = [m for m in messages if triage.get(m["id"], {}).get("label") == "informativo"]

    def fmt(msgs):
        parts = []
        for m in msgs[:50]:
            ch    = channel_names.get(m["channel_id"], m["channel_id"])
            who   = user_names.get(m["user_id"], m["user_id"])
            topic = triage.get(m["id"], {}).get("topic", "")
            parts.append(f"[#{ch}] {who}: {m['text'][:300]}" + (f" ({topic})" if topic else ""))
        return "\n".join(parts)

    period = f"{since.strftime('%d/%m %H:%M')} → {until.strftime('%d/%m %H:%M')}"

    prompt = f"""Generá un resumen diario de actividad de Slack.

Período: {period}
Total mensajes: {len(messages)} ({len(important)} importantes, {len(informative)} informativos)

MENSAJES IMPORTANTES:
{fmt(important) or "(ninguno)"}

MENSAJES INFORMATIVOS:
{fmt(informative) or "(ninguno)"}

Generá un reporte en texto plano con formato Slack (mrkdwn):
*Resumen del día* — 2-3 oraciones
*Acciones pendientes* — bullets
*Decisiones tomadas* — bullets
*Temas del día* — lista de etiquetas

Usá *negrita*, bullet points con •. Idioma español. Conciso."""

    resp = _AI.messages.create(
        model=_SONNET,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def process_me(lookback_days: int = 1):
    if not _TOKEN:
        log.error("SLACK_USER_TOKEN no configurado.")
        return
    log.info("Generando reporte (últimos %d días)…", lookback_days)

    now      = datetime.now(timezone.utc)
    since    = now - timedelta(days=lookback_days)
    since_ts = since.timestamp()
    until_ts = now.timestamp()

    channels     = list_user_channels(_TOKEN)
    channel_names = {ch["id"]: ch["name"] for ch in channels}

    all_messages = []
    for ch in channels:
        try:
            msgs = fetch_channel_messages(_TOKEN, ch["id"], since_ts, until_ts)
            if msgs:
                log.info("  #%s: %d mensajes", ch["name"], len(msgs))
            all_messages.extend(msgs)
        except Exception as e:
            log.warning("  error en #%s: %s", ch["name"], e)

    if not all_messages:
        log.info("Sin mensajes en el período.")
        return

    log.info("Total: %d mensajes en %d canales", len(all_messages),
             len(set(m["channel_id"] for m in all_messages)))

    user_ids   = {m["user_id"] for m in all_messages if m.get("user_id")}
    user_names = resolve_user_names(_TOKEN, user_ids)

    triage  = triage_messages(all_messages, channel_names)
    report  = generate_report(all_messages, triage, channel_names, user_names, since, now)

    my_id = get_my_user_id(_TOKEN)
    send_dm_to_me(_TOKEN, my_id, report)
    log.info("Reporte enviado.")
