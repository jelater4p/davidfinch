from datetime import date

import pytest

from bot.benchmarks import deposit_return, net_rate, rate_on
from bot.config import DepositConfig, load_settings


def test_single_rate_one_year():
    cfg = DepositConfig(rates=({"from": "2026-01-01", "gross": 0.38},), withholding_tax=0.175)
    assert net_rate(cfg, date(2026, 6, 1)) == pytest.approx(0.3135)
    r = deposit_return(cfg, date(2026, 1, 1), date(2027, 1, 1))
    assert r == pytest.approx((1 + 0.3135 / 365) ** 365 - 1, rel=1e-9)   # ~%36.8 günlük bileşik


def test_rate_schedule_uses_rate_valid_each_day():
    cfg = DepositConfig(rates=({"from": "2026-01-01", "gross": 0.40}, {"from": "2026-07-01", "gross": 0.30}),
                        withholding_tax=0.0)
    assert rate_on(cfg, date(2026, 6, 30)) == 0.40 and rate_on(cfg, date(2026, 7, 1)) == 0.30
    mixed = deposit_return(cfg, date(2026, 6, 21), date(2026, 7, 11))      # 10 gün %40, 10 gün %30
    assert mixed == pytest.approx((1 + 0.40 / 365) ** 10 * (1 + 0.30 / 365) ** 10 - 1)


def test_zero_length_period():
    assert deposit_return(DepositConfig(), date(2026, 11, 20), date(2026, 11, 20)) == 0.0


def test_settings_load_deposit():
    s = load_settings()
    assert s.deposit.withholding_tax == 0.175 and s.deposit.rates[0]["gross"] == 0.38
