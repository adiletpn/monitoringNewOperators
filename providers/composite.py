from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from .base import CallRecord


class CompositeProvider:
    """Несколько АТС как один источник.

    Часть операторов звонит через Kcell, часть — через Sipuni. Каждому
    провайдеру отдаём только его операторов (иначе матчинг Sipuni по номеру
    в тексте схемы может зацепить чужого), а наружу выдаём общий список
    CallRecord — monitor.py по-прежнему не знает, откуда пришли звонки.

    Отказ одного источника не должен ронять весь мониторинг: собираем
    записи с тех, кто ответил, и возвращаем ошибку только если не ответил
    никто. Иначе половина команды молча стала бы "неактивной" из-за чужой
    недоступной АТС.
    """

    def __init__(self, providers: List[Tuple[str, object]]):
        # [(имя источника, провайдер), ...]
        self.providers = providers
        self.unattributed_calls = 0

    def fetch_calls(self, day: date) -> Tuple[List[CallRecord], Optional[str]]:
        records: List[CallRecord] = []
        errors: List[str] = []
        alive = 0

        for name, provider in self.providers:
            try:
                recs, err = provider.fetch_calls(day)
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue

            if err and not recs:
                errors.append(f"{name}: {err}")
                continue

            alive += 1
            records.extend(recs)
            self.unattributed_calls += int(getattr(provider, "unattributed_calls", 0) or 0)

        # дедуп на случай, если один и тот же звонок пришёл из двух источников
        seen = set()
        unique: List[CallRecord] = []
        for r in records:
            key = (r.source, r.call_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)

        unique.sort(key=lambda r: r.started_at)

        if not alive and errors:
            return [], "; ".join(errors)
        if errors:
            print(f"[COMPOSITE] источник недоступен, считаю по остальным: {'; '.join(errors)}")
        return unique, None

    def fetch_employees(self) -> Tuple[List[dict], Optional[str]]:
        items: List[dict] = []
        errors: List[str] = []
        for name, provider in self.providers:
            try:
                emps, err = provider.fetch_employees()
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            if err:
                errors.append(f"{name}: {err}")
                continue
            for e in emps:
                e = dict(e)
                e["_source"] = name
                items.append(e)
        return items, ("; ".join(errors) if errors and not items else None)

    def healthcheck(self) -> Tuple[bool, str]:
        parts = []
        ok_all = True
        for name, provider in self.providers:
            try:
                ok, msg = provider.healthcheck()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            ok_all = ok_all and ok
            parts.append(f"{name}: {'ok' if ok else msg}")
        return ok_all, " | ".join(parts)
