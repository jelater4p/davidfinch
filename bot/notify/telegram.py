"""Telegram Bot API istemcisi (ek kütüphane yok, doğrudan HTTPS).

Güvenlik: bot SADECE TELEGRAM_CHAT_ID'deki sohbetten gelen komutlara cevap verir.
Long-polling kullanılır; VPS'te dışarıya açık port gerekmez.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000   # Telegram sınırı 4096; pay bırakıyoruz


def chunk(text: str, size: int = MAX_LEN) -> list[str]:
    """Uzun mesajı satır sınırlarından bölerek parçalara ayırır."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts or [""]


@dataclass
class Update:
    update_id: int
    chat_id: int
    text: str


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID .env içinde tanımlı olmalı")
        self.token = token
        self.chat_id = str(chat_id)
        self.session = session or requests.Session()
        self.offset = 0

    def _call(self, method: str, http_timeout: float = 30, **params):
        r = self.session.post(API.format(token=self.token, method=method), json=params, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} hatası: {data.get('description')}")
        return data["result"]

    def send(self, text: str) -> None:
        for part in chunk(text):
            try:
                self._call("sendMessage", chat_id=self.chat_id, text=part, disable_web_page_preview=True)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                log.error("Telegram mesajı gönderilemedi: %s", exc)

    def poll(self, timeout: int = 25) -> list[Update]:
        """Yeni komutları getirir. Yetkisiz sohbetlerden gelenler sessizce atlanır."""
        try:
            raw = self._call("getUpdates", http_timeout=timeout + 10, offset=self.offset,
                             timeout=timeout, allowed_updates=["message"])
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.warning("Telegram getUpdates hatası: %s", exc)
            return []
        out = []
        for u in raw:
            self.offset = max(self.offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != self.chat_id:
                log.warning("Yetkisiz sohbetten mesaj atlandı: %s", chat)
                continue
            if msg.get("text"):
                out.append(Update(u["update_id"], int(chat), msg["text"]))
        return out


class ConsoleNotifier:
    """Telegram yapılandırılmamışsa mesajları konsola/loga yazar (geliştirme ve test için)."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
        print(text, flush=True)

    def poll(self, timeout: int = 25) -> list[Update]:
        import time
        time.sleep(min(timeout, 5))   # döngü boşa dönüp işlemciyi yakmasın
        return []
