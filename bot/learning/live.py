"""Canlı (paper) çalışmada aylık parametre güncellemesi.

Aktif parametreler param_history tablosundaki son kayıttan okunur; kayıt yoksa
settings.yaml'daki başlangıç değerleri kullanılır. Her ay tek bir karar kaydı
yazılır (değişmese bile, gerekçesiyle) — böylece "bot neden bu ayarla çalıştı?"
sorusu geriye dönük olarak her zaman cevaplanabilir.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from ..config import MomentumConfig, Settings
from ..storage.db import Database, dumps
from .optimizer import TRAIN_MONTHS, Decision, RowsCache, choose, params_to_dict


def current_params(db: Database, settings: Settings) -> MomentumConfig:
    row = db.query_one("SELECT params FROM param_history ORDER BY id DESC LIMIT 1")
    if row is None:
        return settings.momentum
    return MomentumConfig(**json.loads(row["params"]))


def already_updated(db: Database, today: date) -> bool:
    row = db.query_one(
        "SELECT 1 FROM param_history WHERE substr(effective_date, 1, 7) = ?", (today.strftime("%Y-%m"),)
    )
    return row is not None


def monthly_update(db: Database, settings: Settings, data: dict[str, pd.DataFrame], today: date,
                   force: bool = False) -> Decision | None:
    """Ayda bir çalışır. Bu ay zaten yapıldıysa None döner (yeniden başlatma güvenliği)."""
    if already_updated(db, today) and not force:
        return None
    current = current_params(db, settings)
    start = (pd.Timestamp(today) - pd.DateOffset(months=TRAIN_MONTHS)).date()
    decision = choose(RowsCache(data), settings, current, start, today)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO param_history (effective_date, params, reason) VALUES (?, ?, ?)",
            (today.isoformat(), dumps(params_to_dict(decision.params)), decision.explanation),
        )
    return decision
