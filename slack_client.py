"""
slack_client.py — Wrapper sobre slack-sdk para leer mensajes del usuario
"""

import logging
import time
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

_PAGE_SIZE = 200  # mensajes por request


def _client(token: str) -> WebClient:
    return WebClient(token=token)


# ── Info del usuario ──────────────────────────────────────────────────────────

def get_user_info(token: str) -> dict:
    """Devuelve {id, email, display_name, team_id}."""
    c = _client(token)
    identity = c.openid_connect_userInfo()
    return {
        "id":           identity["sub"],
        "email":        identity.get("email", ""),
        "display_name": identity.get("name", ""),
        "team_id":      identity.get("https://slack.com/team_id", ""),
    }


# ── Canales ───────────────────────────────────────────────────────────────────

def list_user_channels(token: str) -> list[dict]:
    """
    Devuelve todos los canales (públicos y privados) en los que el usuario participa.
    Cada item: {id, name, is_private, is_im, is_mpim}
    """
    c = _client(token)
    channels = []

    # Canales públicos y privados
    cursor = None
    while True:
        try:
            kwargs = {"types": "public_channel,private_channel", "limit": 200,
                      "exclude_archived": True}
            if cursor:
                kwargs["cursor"] = cursor
            resp = c.users_conversations(**kwargs)
        except SlackApiError as e:
            log.warning("list_channels error: %s", e)
            break
        for ch in resp.get("channels", []):
            channels.append({
                "id":         ch["id"],
                "name":       ch.get("name", ch["id"]),
                "is_private": ch.get("is_private", False),
                "is_im":      False,
                "is_mpim":    False,
            })
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # DMs (im) y grupos (mpim)
    cursor = None
    while True:
        try:
            kwargs = {"types": "im,mpim", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = c.users_conversations(**kwargs)
        except SlackApiError as e:
            log.warning("list_im error: %s", e)
            break
        for ch in resp.get("channels", []):
            is_im   = ch.get("is_im", False)
            is_mpim = ch.get("is_mpim", False)
            name = ch.get("name") or ("DM" if is_im else "Grupo")
            channels.append({
                "id":         ch["id"],
                "name":       name,
                "is_private": True,
                "is_im":      is_im,
                "is_mpim":    is_mpim,
            })
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return channels


# ── Mensajes ──────────────────────────────────────────────────────────────────

def fetch_channel_messages(token: str, channel_id: str,
                           since_ts: float, until_ts: float) -> list[dict]:
    """
    Devuelve mensajes del canal en el rango [since_ts, until_ts] (unix timestamp).
    Incluye texto de threads (respuestas) si los hay.
    """
    c = _client(token)
    messages = []
    cursor = None

    while True:
        try:
            kwargs = {
                "channel": channel_id,
                "oldest":  str(since_ts),
                "latest":  str(until_ts),
                "limit":   _PAGE_SIZE,
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = c.conversations_history(**kwargs)
        except SlackApiError as e:
            if e.response["error"] == "ratelimited":
                retry = int(e.response.headers.get("Retry-After", 5))
                log.info("Rate limited, esperando %ds", retry)
                time.sleep(retry)
                continue
            log.warning("history error ch=%s: %s", channel_id, e)
            break

        for msg in resp.get("messages", []):
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                continue
            parsed = _parse_message(msg, channel_id)
            messages.append(parsed)

            # Si tiene replies, los traemos
            if msg.get("reply_count", 0) > 0:
                replies = fetch_thread_replies(token, channel_id, msg["ts"])
                # El primer mensaje del hilo ya lo tenemos, salteamos
                for reply in replies[1:]:
                    messages.append(reply)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break

    return messages


def fetch_thread_replies(token: str, channel_id: str, thread_ts: str) -> list[dict]:
    c = _client(token)
    replies = []
    cursor = None
    while True:
        try:
            kwargs = {"channel": channel_id, "ts": thread_ts, "limit": _PAGE_SIZE}
            if cursor:
                kwargs["cursor"] = cursor
            resp = c.conversations_replies(**kwargs)
        except SlackApiError as e:
            log.warning("replies error: %s", e)
            break
        for msg in resp.get("messages", []):
            replies.append(_parse_message(msg, channel_id))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break
    return replies


def _parse_message(msg: dict, channel_id: str) -> dict:
    ts = float(msg.get("ts", 0))
    return {
        "id":         msg.get("ts", ""),
        "channel_id": channel_id,
        "user_id":    msg.get("user", ""),
        "text":       msg.get("text", ""),
        "ts":         ts,
        "thread_ts":  msg.get("thread_ts"),
        "reply_count": msg.get("reply_count", 0),
        "reactions":  [r["name"] for r in msg.get("reactions", [])],
        "dt":         datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
    }


# ── Resolución de nombres ─────────────────────────────────────────────────────

def resolve_user_names(token: str, user_ids: set) -> dict:
    """Devuelve {user_id: display_name}."""
    c = _client(token)
    names = {}
    for uid in user_ids:
        if not uid:
            continue
        try:
            resp = c.users_info(user=uid)
            profile = resp["user"].get("profile", {})
            names[uid] = (
                profile.get("display_name")
                or profile.get("real_name")
                or uid
            )
        except SlackApiError:
            names[uid] = uid
    return names


# ── Envío del reporte ─────────────────────────────────────────────────────────

def send_dm_report(token: str, slack_user_id: str, html_report: str):
    """Envía el reporte como mensaje al DM del propio usuario (slackbot)."""
    c = _client(token)
    # Abrir DM con uno mismo
    try:
        resp = c.conversations_open(users=slack_user_id)
        dm_channel = resp["channel"]["id"]
    except SlackApiError as e:
        log.error("No se pudo abrir DM: %s", e)
        return

    # Slack no soporta HTML — convertimos a mrkdwn básico
    text = _html_to_mrkdwn(html_report)

    try:
        c.chat_postMessage(
            channel=dm_channel,
            text=text,
            mrkdwn=True,
        )
        log.info("Reporte enviado por DM a %s", slack_user_id)
    except SlackApiError as e:
        log.error("Error enviando DM: %s", e)


def _html_to_mrkdwn(html: str) -> str:
    """Conversión mínima de HTML a Slack mrkdwn."""
    import re
    text = re.sub(r"<h[1-3][^>]*>(.*?)</h[1-3]>", r"*\1*\n", html, flags=re.S)
    text = re.sub(r"<strong>(.*?)</strong>", r"*\1*", text, flags=re.S)
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text, flags=re.S)
    text = re.sub(r"<em>(.*?)</em>", r"_\1_", text, flags=re.S)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"• \1\n", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
