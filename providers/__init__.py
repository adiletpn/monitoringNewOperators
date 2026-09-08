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

    # Один и тот же человек может звонить то через Kcell, то через Sipuni,
    # поэтому каждому провайдеру отдаём ВЕСЬ список: Kcell найдёт своих по
    # логину, Sipuni — своих по внутреннему номеру. У кого нужного блока в
    # конфиге нет, тот в этом источнике просто не совпадёт ни с чем.
    built = [(n, _BUILDERS[n](cfg, operators)) for n in names]
    for n, _ in built:
        who = [m["id"] for m in operators.values()
               if (m.get("kcell", {}).get("login") if n == "kcell" else (m.get("sipuni", {}) or {}).get("ext"))]
        print(f"[PROVIDERS] {n}: опознаваемых операторов {len(who)} ({', '.join(sorted(who))})")
    return CompositeProvider(built)
