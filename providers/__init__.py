from __future__ import annotations

from typing import Dict

from .base import CallRecord, TelephonyProvider
from .composite import CompositeProvider
from .kcell import KcellProvider
from .sipuni import SipuniProvider

__all__ = [
    "CallRecord", "TelephonyProvider", "SipuniProvider", "KcellProvider",
    "CompositeProvider", "get_provider", "split_operators_by_source",
]

_BUILDERS = {
    "kcell": KcellProvider,
    "sipuni": SipuniProvider,
}


def split_operators_by_source(operators: Dict[str, Dict], default_source: str) -> Dict[str, Dict[str, Dict]]:
    """Разбивает операторов по источнику (поле source в operators.yml).

    Каждому провайдеру потом отдаём только его людей: матчинг Sipuni идёт по
    номеру внутри текста схемы, и если подсунуть ему операторов Kcell, он
    может зацепить чужие строки.
    """
    by_source: Dict[str, Dict[str, Dict]] = {}
    for name, meta in operators.items():
        src = str(meta.get("source") or default_source).strip().lower()
        by_source.setdefault(src, {})[name] = meta
    return by_source


def get_provider(cfg, operators: Dict[str, Dict]) -> TelephonyProvider:
    """Создаёт провайдера по TELEPHONY_PROVIDER.

    kcell / sipuni — один источник на всех.
    both (или "kcell,sipuni") — составной: каждый оператор идёт к своей АТС
    согласно полю source в operators.yml.
    """
    raw = (getattr(cfg, "telephony_provider", "") or "kcell").strip().lower()
    names = [n.strip() for n in raw.replace("+", ",").split(",") if n.strip()]

    if names == ["both"]:
        names = ["kcell", "sipuni"]

    unknown = [n for n in names if n not in _BUILDERS]
    if unknown or not names:
        raise RuntimeError(
            f"Unknown TELEPHONY_PROVIDER: {raw!r} (expected 'kcell', 'sipuni', 'both' or 'kcell,sipuni')"
        )

    if len(names) == 1:
        return _BUILDERS[names[0]](cfg, operators)

    by_source = split_operators_by_source(operators, default_source=names[0])
    built = []
    for n in names:
        subset = by_source.get(n, {})
        if not subset:
            print(f"[PROVIDERS] в operators.yml нет операторов с source: {n} — источник не опрашиваю")
            continue
        built.append((n, _BUILDERS[n](cfg, subset)))
        print(f"[PROVIDERS] {n}: {len(subset)} операторов ({', '.join(sorted(m['id'] for m in subset.values()))})")

    if not built:
        raise RuntimeError("Ни одного оператора не удалось привязать к источнику — проверь поле source в operators.yml")
    if len(built) == 1:
        return built[0][1]
    return CompositeProvider(built)
