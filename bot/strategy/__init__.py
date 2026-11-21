from ..config import Settings
from .base import EntrySignal, Strategy
from .momentum import MomentumStrategy
from .pullback import PullbackStrategy


def make_strategy(settings: Settings, name: str | None = None, params=None) -> Strategy:
    name = name or settings.active_strategy
    if name == "momentum":
        params = params or settings.momentum
        if params.hold_rank < settings.risk.max_open_positions:
            # Aksi halde her ay sıralaması hold_rank..max arasındaki hisseler satılıp geri alınır.
            raise ValueError("momentum.hold_rank, max_open_positions'tan küçük olamaz")
        return MomentumStrategy(params)
    if name == "pullback":
        return PullbackStrategy(params or settings.pullback)
    raise ValueError(f"Bilinmeyen strateji: {name}")
