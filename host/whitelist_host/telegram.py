"""Telegram notification transport (§6).

Telegram is notification, never authority. Delivery is at-least-once —
an ambiguous network timeout after the API accepted the message can produce
a duplicate ping on retry, and that is the accepted tradeoff: a duplicate is
far better than a silent miss.
"""

from __future__ import annotations

import logging

from .httpjson import TransportError, request_json

logger = logging.getLogger(__name__)


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str, transport=request_json):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.transport = transport

    def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            status, data = self.transport(
                "POST", url, body={"chat_id": self.chat_id, "text": text}
            )
        except TransportError as e:
            logger.warning("telegram send failed: %s", e)
            return False
        if status == 200 and data.get("ok"):
            return True
        logger.warning("telegram rejected message: %s %s", status, data)
        return False


def verification_message(sub: dict) -> str:
    """Enough to recognize the person from a phone and judge whether the trip
    to a LAN machine is worth making now (§6): real name, claimed username,
    platform, resolved UUID, submission id."""
    return (
        "Whitelist request verified\n"
        f"#{sub.get('id')} {sub.get('username')} ({sub.get('platform')})\n"
        f"Real name: {sub.get('real_name') or '—'}\n"
        f"UUID: {sub.get('uuid') or '—'}\n"
        "Review on the LAN admin app when convenient."
    )
