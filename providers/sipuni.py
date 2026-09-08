from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pytz

from sipuni_api import fetch_calls_csv_export_all, fetch_operators_csv
from utils_csv import parse_csv

from .base import CallRecord


def _safe_int(x) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return 0


def _parse_csv_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except Exception:
            pass
    return None


def _guess_direction(scheme: str) -> str:
    s = (scheme or "").lower()
    if "вход" in s:
        return "in"
    if "исход" in s:
        return "out"
    return "unknown"


class SipuniProvider:
    """Обёртка над старым Sipuni CSV export/all: тянет весь CSV, сопоставляет
    строки операторам по внутреннему номеру/имени, зашитому в "Схема"
    (см. _row_matches_operator), и отдаёт нормализованные CallRecord.

    Матчинг воспроизводит поведение из старого monitor.py один в один —
    это провайдер для сравнения/отката, поведение сознательно не меняется.
    """

    def __init__(self, cfg, operators: Dict[str, Dict]):
        self.cfg = cfg
        self.operators = operators
        self.tz = pytz.timezone(cfg.tz)

        csv_tz = (getattr(cfg, "sipuni_csv_tz", "") or "").strip()
        self.csv_tz = pytz.timezone(csv_tz) if csv_tz else self.tz

    # ======= MATCHING =======
    def _row_matches_operator(self, op_id: str, meta: Dict, row: Dict[str, str]) -> bool:
        """В выгрузке Sipuni нет поля «сотрудник»: человека опознаём по его
        внутреннему номеру внутри текста «Схема» — «Входящая 7475567651
        Балнур 97» или «исход +7707... nom 94».

        Номер берём из sipuni.ext. Если его нет — откатываемся на op_id, как
        было в старом конфиге, где op_id и был номером (208, 210...).
        """
        fields_primary = ["Схема", "Кто ответил"]
        primary_text = " ".join(str(row.get(f, "") or "").lower() for f in fields_primary)

        ext = str((meta.get("sipuni") or {}).get("ext") or "").strip()
        if not ext and str(op_id).isdigit():
            ext = str(op_id)

        # номер как отдельное число: «nom 94» — да, «94» внутри 1940 — нет
        if ext and re.search(rf"(?<!\d){re.escape(ext)}(?!\d)", primary_text):
            return True

        # fallback: слова из match (имя) целиком присутствуют в схеме
        scheme = str(row.get("Схема", "") or "").lower()
        tokens = [str(x).lower() for x in (meta.get("match") or []) if not str(x).isdigit()]
        tokens = [t for t in tokens if t and t not in ("такси", "кз", "kz")]

        if tokens and scheme:
            return all(t in scheme for t in tokens)

        return False

    def _match_operator(self, row: Dict[str, str]) -> Optional[Tuple[str, str]]:
        """Возвращает (op_id, operator_name) первого совпавшего оператора."""
        for name, meta in self.operators.items():
            op_id = str(meta["id"])
            if self._row_matches_operator(op_id, meta, row):
                return op_id, name
        return None

    def fetch_calls(self, day: date) -> Tuple[List[CallRecord], Optional[str]]:
        csv_data, err = fetch_calls_csv_export_all(
            self.cfg.sipuni_user,
            self.cfg.sipuni_secret,
            limit=5000,
            order="desc",
            page=1,
        )
        if not csv_data:
            return [], err

        _, rows = parse_csv(csv_data)

        records: List[CallRecord] = []
        for r in rows:
            dt_naive = _parse_csv_dt(r.get("Время"))
            if not dt_naive:
                continue

            dt = self.csv_tz.localize(dt_naive).astimezone(self.tz)
            if dt.date() != day:
                continue

            matched = self._match_operator(r)
            if not matched:
                continue
            op_id, op_name = matched

            talk_sec = _safe_int(r.get("Длительность разговора, сек"))
            call_sec = _safe_int(r.get("Длительность звонка, сек"))
            dur = talk_sec if talk_sec > 0 else call_sec
            if dur < 0:
                dur = 0

            scheme = r.get("Схема", "") or ""
            call_id = "|".join([
                r.get("Время", "") or "",
                r.get("Откуда", "") or "",
                r.get("Куда", "") or "",
                r.get("ID записи", "") or "",
                op_id,
            ])

            records.append(
                CallRecord(
                    call_id=call_id,
                    started_at=dt,
                    duration_sec=dur,
                    ring_sec=0,
                    direction=_guess_direction(scheme),
                    answered=dur > 0,
                    operator_key=op_id,
                    operator_name=op_name,
                    from_number=r.get("Откуда", "") or "",
                    to_number=r.get("Куда", "") or "",
                    source="sipuni",
                    raw=r,
                )
            )

        return records, None

    def fetch_employees(self) -> Tuple[List[dict], Optional[str]]:
        csv_data, err = fetch_operators_csv(self.cfg.sipuni_user, self.cfg.sipuni_secret)
        if not csv_data:
            return [], err
        _, rows = parse_csv(csv_data)
        return rows, None

    def healthcheck(self) -> Tuple[bool, str]:
        _, err = self.fetch_employees()
        if err:
            return False, err
        return True, "ok"
