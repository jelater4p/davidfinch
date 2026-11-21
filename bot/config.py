"""Ayarların yüklenmesi ve doğrulanması.

settings.yaml -> sabit kurallar ve başlangıç parametreleri
.env          -> gizli bilgiler (TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY ...)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RiskConfig:
    initial_capital: float = 100_000.0
    min_cash_ratio: float = 0.30
    max_position_ratio: float = 0.28
    risk_per_trade: float = 0.01
    max_open_positions: int = 6

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital pozitif olmalı")
        if not 0 <= self.min_cash_ratio < 1:
            raise ValueError("min_cash_ratio 0 ile 1 arasında olmalı")
        if not 0 < self.max_position_ratio <= 1 - self.min_cash_ratio:
            raise ValueError("max_position_ratio, nakit kuralı ile çelişiyor")
        # %5 üstü işlem başı risk, birkaç ardışık stopta portföyü ciddi eritir.
        if not 0 < self.risk_per_trade <= 0.05:
            raise ValueError("risk_per_trade 0 ile 0.05 arasında olmalı")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions en az 1 olmalı")


@dataclass(frozen=True)
class CostConfig:
    commission_rate: float = 0.002
    bsmv_rate: float = 0.05
    slippage_rate: float = 0.001

    def fee(self, notional: float) -> float:
        """Komisyon + komisyon üzerinden BSMV."""
        commission = notional * self.commission_rate
        return commission * (1 + self.bsmv_rate)


@dataclass(frozen=True)
class StrategyConfig:
    trend_sma: int = 50
    rsi_entry: float = 35
    atr_stop_mult: float = 2.5
    volume_mult: float = 1.2
    atr_period: int = 14
    rsi_period: int = 14


@dataclass(frozen=True)
class Settings:
    timezone: str = "Europe/Istanbul"
    db_path: Path = ROOT / "data" / "davidfinch.db"
    universe_file: Path = ROOT / "data" / "bist100.txt"
    data_source: str = "yahoo"
    history_days: int = 400
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    def universe(self) -> list[str]:
        symbols = []
        for line in self.universe_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().upper()
            if line:
                symbols.append(line)
        return sorted(set(symbols))


def _load_dotenv(path: Path) -> None:
    """Basit .env okuyucu; ortamda zaten tanımlı değişkenleri ezmez."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def load_settings(path: str | Path | None = None) -> Settings:
    _load_dotenv(ROOT / ".env")
    path = _resolve(path or os.environ.get("DAVIDFINCH_SETTINGS", "settings.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    data = raw.get("data", {})
    settings = Settings(
        timezone=raw.get("timezone", "Europe/Istanbul"),
        db_path=_resolve(os.environ.get("DAVIDFINCH_DB", raw.get("db_path", "data/davidfinch.db"))),
        universe_file=_resolve(raw.get("universe_file", "data/bist100.txt")),
        data_source=data.get("source", "yahoo"),
        history_days=int(data.get("history_days", 400)),
        risk=RiskConfig(**raw.get("risk", {})),
        costs=CostConfig(**raw.get("costs", {})),
        strategy=StrategyConfig(**raw.get("strategy", {})),
    )
    settings.risk.validate()
    return settings
