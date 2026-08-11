"""Kcell VATS REST API.

Ключевые эмпирически подтверждённые факты (проверено на живом кабинете
corpsol.vpbx.kcell.kz на первом инстансе — этот второй инстанс работает
в том же кабинете):
  - GET /history/json отдаёт ГОЛЫЙ массив, без обёртки `info` — пагинации у него
    нет (по крайней мере при разумном объёме звонков/день). Дедупликация по
    `uid` обязательна в любом случае, объём проверяй эмпирически.
  - Реальные значения `status`: "success" (разговор состоялся), "noanswer"
    (исходящий недозвон), "missed" (пропущенный входящий). Ключ `duration`
    присутствует ТОЛЬКО при status == "success" — иначе его нет вообще
    (не 0, а отсутствует), поэтому только `.get("duration", 0)`.
  - У пропущенных на отдел `user`/`user_name` отсутствуют как ключи (не пустая
    строка) — тоже только `.get("user", "")`.
  - GET /domain может вернуть неожиданное имя таймзоны (у первого инстанса
    было "Asia/Oral" вместо "Asia/Almaty" — не ошибка, после объединения
    часовых поясов Казахстана в 2024 году обе зоны дают одинаковый UTC+5).
    Сверяй по факту смещения (pytz utcoffset), а не по имени зоны.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import certifi
import pytz
import requests

from .base import CallRecord

DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3
DEFAULT_RETRY_SLEEP = 1.5

_KNOWN_STATUSES = {"success", "noanswer", "missed"}


def _fmt_kcell_dt(dt_utc: datetime) -> str:
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")


def _parse_kcell_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


class KcellProvider:
    def __init__(self, cfg, operators: Dict[str, Dict]):
        self.cfg = cfg
        self.operators = operators

        self.base_url = (getattr(cfg, "kcell_base_url", "") or "").rstrip("/")
        self.api_key = getattr(cfg, "kcell_api_key", "") or ""
        self.tz = pytz.timezone(getattr(cfg, "kcell_tz", "") or getattr(cfg, "tz", "Asia/Almaty"))

        directions = (getattr(cfg, "kcell_count_directions", "") or "out").strip()
        self.count_directions = {d.strip() for d in directions.split(",") if d.strip()} or {"out"}

        self._seen_statuses = set(_KNOWN_STATUSES)
        self.unattributed_calls = 0

    # ---------------- HTTP ----------------
    def _get(self, path: str, params: Optional[dict] = None) -> Tuple[Optional[object], Optional[str]]:
        if not self.base_url or not self.api_key:
            return None, "KCELL_BASE_URL/KCELL_API_KEY is not configured"

        url = f"{self.base_url}{path}"
        headers = {"X-API-KEY": self.api_key, "User-Agent": "OperatorMonitor/1.0"}

        last_err = ""
        for attempt in range(1, DEFAULT_RETRIES + 1):
            try:
                r = requests.get(
                    url, params=params or {}, headers=headers,
                    timeout=DEFAULT_TIMEOUT, verify=certifi.where(),
                )
            except requests.exceptions.Timeout as e:
                last_err = f"TIMEOUT: {e}"
                time.sleep(DEFAULT_RETRY_SLEEP * attempt)
                continue
            except Exception as e:
                last_err = str(e)
                time.sleep(DEFAULT_RETRY_SLEEP * attempt)
                continue

            if r.status_code == 200:
                try:
                    return r.json(), None
                except Exception as e:
                    return None, f"invalid JSON from {path}: {e}"

            # фатальные — не ретраим
            if r.status_code == 401:
                return None, f"FATAL 401 (неверный ключ или интеграция выключена): {r.text[:300]}"
            if r.status_code == 403:
                return None, f"403 Forbidden (нет прав на {path}): {r.text[:300]}"
            if r.status_code in (400, 405):
                return None, f"HTTP {r.status_code} на {path}: {r.text[:300]}"

            # 429 / 5xx — ретраим с backoff
            last_err = f"HTTP {r.status_code} на {path}: {r.text[:300]}"
            time.sleep(DEFAULT_RETRY_SLEEP * attempt)

        return None, last_err or f"request to {path} failed after retries"

    # ---------------- healthcheck ----------------
    def healthcheck(self) -> Tuple[bool, str]:
        data, err = self._get("/domain")
        if err:
            return False, err
        if not isinstance(data, dict):
            return False, "unexpected /domain response shape"

        remote_name = ((data.get("timezone") or {}).get("name")) or ""
        try:
            remote_offset = datetime.now(pytz.timezone(remote_name)).utcoffset() if remote_name else None
        except Exception:
            remote_offset = None
        expected_offset = datetime.now(self.tz).utcoffset()

        if remote_offset is not None and remote_offset != expected_offset:
            return False, (
                f"timezone mismatch: Kcell /domain={remote_name} ({remote_offset}) "
                f"vs KCELL_TZ={self.tz} ({expected_offset})"
            )

        return True, "ok"

    # ---------------- employees ----------------
    def fetch_employees(self) -> Tuple[List[dict], Optional[str]]:
        items: List[dict] = []
        start = 0
        limit = 100

        for _ in range(50):  # защита от зацикливания при неожиданном формате info
            data, err = self._get("/users", {"with": "status", "start": start, "limit": limit})
            if err:
                return items, err
            if not isinstance(data, dict):
                return items, "unexpected /users response shape"

            page = data.get("items") or []
            items.extend(page)

            info = data.get("info") or {}
            nxt = info.get("next")
            total = info.get("total")

            if nxt:
                start = nxt
                continue
            if total is not None and len(items) >= int(total):
                break
            if len(page) < limit:
                break
            start += limit

        return items, None

    # ---------------- calls ----------------
    def fetch_calls(self, day: date) -> Tuple[List[CallRecord], Optional[str]]:
        local_start = self.tz.localize(datetime(day.year, day.month, day.day, 0, 0, 0))
        local_end = self.tz.localize(datetime(day.year, day.month, day.day, 23, 59, 59))

        params = {
            "start": _fmt_kcell_dt(local_start.astimezone(pytz.utc)),
            "end": _fmt_kcell_dt(local_end.astimezone(pytz.utc)),
            "type": "all",
        }

        data, err = self._get("/history/json", params)
        if err:
            return [], err

        rows = data if isinstance(data, list) else ((data or {}).get("items") or [])

        seen_uids = set()
        records: List[CallRecord] = []

        for row in rows:
            uid = row.get("uid")
            if not uid or uid in seen_uids:
                continue
            seen_uids.add(uid)

            started = _parse_kcell_dt(row.get("start"))
            if not started:
                continue
            started_local = started.astimezone(self.tz)
            if started_local.date() != day:
                continue

            status = str(row.get("status") or "").lower()
            if status and status not in self._seen_statuses:
                print(f"[KCELL] неизвестный статус {status!r} (uid={uid}) — считаю попыткой")
                self._seen_statuses.add(status)

            direction = row.get("type") or "unknown"
            answered = status == "success"

            user = row.get("user") or ""
            if not user:
                self.unattributed_calls += 1
                continue  # пропущенный на отдел / без сотрудника — не привязать к человеку

            if direction == "out":
                if "out" not in self.count_directions:
                    continue
            elif direction == "in":
                if "in" not in self.count_directions:
                    continue
                if not answered:
                    continue
            else:
                continue  # внутренние/неизвестные — не учитываем

            duration = int(row.get("duration") or 0)
            client = str(row.get("client") or "")
            diversion = str(row.get("diversion") or "")

            records.append(
                CallRecord(
                    call_id=str(uid),
                    started_at=started_local,
                    duration_sec=max(duration, 0),
                    ring_sec=int(row.get("wait") or 0),
                    direction=direction,
                    answered=answered,
                    operator_key=user,
                    operator_name=str(row.get("user_name") or ""),
                    from_number=client if direction == "in" else diversion,
                    to_number=diversion if direction == "in" else client,
                    raw=row,
                )
            )

        return records, None
