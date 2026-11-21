"""Bildirim analizi: Claude ile "ciddi olumsuz mu?" sınıflandırması.

Kullanıcı kararı: filtre SADECE ciddi olumsuz haberlerde alımı engeller.
Yanıt JSON şemasıyla sınırlandırılır (structured outputs); model reddederse veya
hata olursa sonuç "bilinmiyor" döner ve filtre alımı engellemez (uyarı verir).
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .base import Disclosure

log = logging.getLogger(__name__)

CATEGORIES = [
    "spk_ceza_veya_islem_yasagi",
    "onemli_dava",
    "konkordato_veya_iflas",
    "buyuk_sozlesme_iptali",
    "beklenmedik_zarar",
    "denetci_olumsuz_gorus",
    "borc_odeyememe",
    "diger_ciddi_olumsuz",
    "yok",
]

SYSTEM_PROMPT = """Sen Borsa İstanbul'da işlem yapan bir paper trade botu için KAP bildirimlerini değerlendiren bir analistsin.
Görevin tek bir soruya cevap vermek: Bu bildirim, hisseyi yeni ALMAMAYI gerektirecek kadar CİDDİ OLUMSUZ mu?

Ciddi olumsuz sayılanlar:
- SPK idari para cezası, işlem yasağı, tedbir; borsada işlem sırasının kapatılması (rutin teknik durdurmalar hariç)
- Şirketin mali durumunu önemli ölçüde etkileyebilecek dava veya soruşturma
- Konkordato, iflas, iflas erteleme başvurusu
- Önemli bir sözleşmenin, ihalenin veya iş ilişkisinin iptali/feshi
- Beklenmedik büyük zarar, kâr uyarısı, önemli değer düşüklüğü
- Bağımsız denetçinin olumsuz görüş vermesi veya görüş bildirmekten kaçınması
- Borç ödeyememe, temerrüt, kredi yapılandırma zorunluluğu

Ciddi olumsuz SAYILMAYANLAR (bunlar için ciddi_olumsuz=false):
- Rutin bildirimler: genel kurul, yönetim kurulu kararları, pay alım/satım bildirimleri, faaliyet raporu, sermaye artırımı duyurusu,
  temettü, yatırımcı sunumu, kredi derecelendirme teyidi, yeni iş/sözleşme duyurusu
- Hafif olumsuz veya belirsiz haberler
- Şirketin taraf olduğu küçük ölçekli davalar

Emin değilsen ciddi_olumsuz=false ver ve guven değerini düşük tut. Gerekçeyi Türkçe, en fazla iki cümle yaz."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ciddi_olumsuz": {"type": "boolean"},
        "kategori": {"type": "string", "enum": CATEGORIES},
        "gerekce": {"type": "string"},
        "guven": {"type": "number"},
    },
    "required": ["ciddi_olumsuz", "kategori", "gerekce", "guven"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Analysis:
    severe: bool | None        # None: analiz yapılamadı (bilinmiyor)
    category: str
    reason: str
    confidence: float
    model: str


class NewsAnalyzer(ABC):
    @abstractmethod
    def analyze(self, d: Disclosure) -> Analysis: ...


def _user_message(d: Disclosure) -> str:
    parts = [
        f"Hisse: {d.symbol}",
        f"Yayın zamanı: {d.published_at:%Y-%m-%d %H:%M}",
        f"Konu: {d.subject or '-'}",
        f"Başlık: {d.title or '-'}",
        f"Özet: {d.summary or '-'}",
    ]
    if d.body:
        parts.append(f"Metin:\n{d.body}")
    return "\n".join(parts)


class ClaudeAnalyzer(NewsAnalyzer):
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()   # ANTHROPIC_API_KEY ortam değişkeninden (.env)
        self.client = client
        self.model = model
        self.effort = effort

    def analyze(self, d: Disclosure) -> Analysis:
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_message(d)}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                # Güvenlik sınıflandırıcısı reddederse istek aynı çağrı içinde yedek modelde tekrar çalışır.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            log.warning("Claude analizi başarısız (%s): %s", d.key, exc)
            return Analysis(None, "yok", f"Analiz yapılamadı: {type(exc).__name__}", 0.0, self.model)

        if response.stop_reason == "refusal":
            return Analysis(None, "yok", "Model bu bildirimi değerlendirmeyi reddetti", 0.0, response.model)
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return Analysis(None, "yok", "Model yanıtı okunamadı", 0.0, response.model)
        return Analysis(
            severe=bool(data["ciddi_olumsuz"]),
            category=data["kategori"],
            reason=data["gerekce"],
            confidence=max(0.0, min(1.0, float(data["guven"]))),
            model=response.model,
        )
