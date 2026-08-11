from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Protocol


@dataclass(frozen=True)
class CallRecord:
    call_id: str                  # уникальный ID звонка в АТС (для дедупликации)
    started_at: datetime          # tz-aware, уже сконвертирован в cfg.tz
    duration_sec: int             # разговор; 0 если недозвон
    ring_sec: int                 # «Ожидание»
    direction: str                # "out" | "in" | "internal"
    answered: bool                # False для «НЕ СОСТОЯЛСЯ» / «НЕ ДОЖДАЛСЯ»
    operator_key: str             # ключ матчинга на оператора (login/id провайдера)
    operator_name: str            # как АТС называет сотрудника
    from_number: str
    to_number: str
    raw: dict = field(default_factory=dict, compare=False)  # исходный объект — для отладки

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=max(self.duration_sec, 0))


class TelephonyProvider(Protocol):
    def fetch_calls(self, day: date) -> tuple[list[CallRecord], Optional[str]]:
        """Возвращает звонки за день, которые должны учитываться в мониторинге
        активности (провайдер сам решает, что считается: направления,
        отвеченность, отсев записей без привязки к конкретному сотруднику)."""
        ...

    def fetch_employees(self) -> tuple[list[dict], Optional[str]]:
        """Список сотрудников АТС для стартовой валидации против operators.yml."""
        ...

    def healthcheck(self) -> tuple[bool, str]:
        """Быстрая проверка доступности источника (без выгрузки звонков)."""
        ...
