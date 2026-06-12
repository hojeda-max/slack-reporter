"""
slack_client.py — Wrapper Slack SDK (single-user)
"""

import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

_PAGE_SIZE = 200


def _c(token: str) -> WebClient:
    return WebClient(token=token)


def get_my_user_id(token: str) -> str:
    resp = _c(token).auth_test()
    return resp["user_id"]


def list_user_channels(token: str) -> list[dict]:
    c        = _c(token)
    channels = []

    for types in ("public_channel,private_channel", "im,mpim"):
        cursor = None
        while True:
            try:
                kwargs = {"types": types, "limit": 200, "exclude_archived": True}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = c.users_conversations(**kwargs)
            except SlackApiError as e:
                log.warning("list_channels (%s): %s", types, e)
                break

            for ch in resp.get("channels", []):
                is_im   = ch.get("is_im", False)
                is_mpim = ch.get("is_mpim", False)
                name    = ch.get("name") or ("DM" if is_im else "Grupo")
                channels.append({
                    "id":      ch["id"],
                    "name":    name,
                    "is_im":   is_im,
                    "is_mpim": is_mpim,
                })

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    return channels


def fetch_channel_messages(token: str, channel_id: str,
                            since_ts: float, until_ts: float) -> list[dict]:
    c        = _c(token)
    messages = []
    cursor   = None

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
                time.sleep(int(e.response.headers.get("Retry-After", 5)))
                continue
            # missing_scope, not_in_channel, etc → saltar silenciosamente
            return messages

        for msg in resp.get("messages", []):
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                continue
            parsed = _parse(msg, channel_id)
            messages.append(parsed)
            if msg.get("reply_count", 0) > 0:
                for reply in _fetch_replies(c, channel_id, msg["ts"])[1:]:
                    messages.append(reply)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break

    return messages


def _fetch_replies(c: WebClient, channel_id: str, thread_ts: str) -> list[dict]:
    replies = []
    cursor  = None
    while True:
        try:
            kwargs = {"channel": channel_id, "ts": thread_ts, "limit": _PAGE_SIZE}
            if cursor:
                kwargs["cursor"] = cursor
            resp = c.conversations_replies(**kwargs)
        except SlackApiError:
            break
        for msg in resp.get("messages", []):
            replies.append(_parse(msg, channel_id))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break
    return replies


def _parse(msg: dict, channel_id: str) -> dict:
    from datetime import datetime, timezone
    ts = float(msg.get("ts", 0))
    return {
        "id":         msg.get("ts", ""),
        "channel_id": channel_id,
        "user_id":    msg.get("user", ""),
        "text":       msg.get("text", ""),
        "ts":         ts,
        "thread_ts":  msg.get("thread_ts"),
        "dt":         datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
    }


def resolve_user_names(token: str, user_ids: set) -> dict:
    c     = _c(token)
    names = {}
    for uid in user_ids:
        if not uid:
            continue
        try:
            resp    = c.users_info(user=uid)
            profile = resp["user"].get("profile", {})
            names[uid] = (
                profile.get("display_name")
                or profile.get("real_name")
                or uid
            )
        except SlackApiError:
            names[uid] = uid
    return names


def send_dm_to_me(token: str, user_id: str, text: str):
    c = _c(token)
    try:
        resp       = c.conversations_open(users=user_id)
        dm_channel = resp["channel"]["id"]
        c.chat_postMessage(channel=dm_channel, text=text, mrkdwn=True)
        log.info("DM enviado a %s", user_id)
    except SlackApiError as e:
        log.error("Error enviando DM: %s", e)
