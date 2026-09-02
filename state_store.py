from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime
from typing import List, Optional, Set, Tuple

DEFAULT_DB_PATH = "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_state (
    op_id TEXT NOT NULL,
    day TEXT NOT NULL,
    absent_today INTEGER NOT NULL DEFAULT 0,
    absent_by TEXT NOT NULL DEFAULT '',
    absent_at TEXT NOT NULL DEFAULT '',
    wa_count INTEGER NOT NULL DEFAULT 0,
    wa_ack_active INTEGER NOT NULL DEFAULT 0,
    wa_ack_at TEXT,
    first_call_locked TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    last_call_time TEXT,
    sent_thresholds TEXT NOT NULL DEFAULT '',
    alert_count_15 INTEGER NOT NULL DEFAULT 0,
    alert_count_30 INTEGER NOT NULL DEFAULT 0,
    alert_count_60 INTEGER NOT NULL DEFAULT 0,
    last_message_ids TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (op_id, day)
);

CREATE TABLE IF NOT EXISTS alert_msg_map (
    msg_id INTEGER PRIMARY KEY,
    op_id TEXT NOT NULL,
    threshold INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_report (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sent_day TEXT
);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_int_set(s: str) -> Set[int]:
    out: Set[int] = set()
    for part in (s or "").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except Exception:
                pass
    return out


def _dump_int_set(xs: Set[int]) -> str:
    return ",".join(str(x) for x in sorted(xs))


def _parse_int_list(s: str) -> List[int]:
    out: List[int] = []
    for part in (s or "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except Exception:
                pass
    return out


def _dump_int_list(xs: List[int]) -> str:
    return ",".join(str(x) for x in xs)


class StateStore:
    """Персистентное (SQLite) состояние дня по оператору: алерты, WA, absent.
    Переживает рестарт процесса (важно на Railway, где ФС эфемерная — путь к
    файлу стоит вынести на подключённый volume через STATE_DB_PATH).

    Семантика WhatsApp (mark_wa_cancel_alert) — НЕ заморозка неактивности:
    кнопка откатывает счётчик конкретного алерта (15/30/60), увеличивает
    wa_count и ничего не делает с расчётом текущей/суммарной неактивности —
    те продолжают считаться по реальным звонкам как обычно.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._lock = threading.Lock()
        self.db_path = self._open_path(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

        self.did_print_start = False

    @staticmethod
    def _open_path(db_path: str) -> str:
        """Готовит путь к файлу БД и НЕ даёт боту упасть из-за настроек хранилища.

        Если STATE_DB_PATH указывает на volume, который не подключён (каталога
        нет), sqlite падает с 'unable to open database file' и бот уходит в
        бесконечный крэш-луп — мониторинг молчит. Пробуем создать каталог, а
        если нельзя — работаем на локальном файле с явным предупреждением.
        """
        if db_path == ":memory:":
            return db_path

        directory = os.path.dirname(os.path.abspath(db_path))
        try:
            os.makedirs(directory, exist_ok=True)
            probe = sqlite3.connect(db_path)
            probe.close()
            return db_path
        except Exception as e:
            print(
                f"[STATE] ВНИМАНИЕ: не удалось открыть {db_path!r} ({e}). "
                f"Похоже, volume не подключён или примонтирован по другому пути. "
                f"Работаю на {DEFAULT_DB_PATH!r} — состояние сбросится при передеплое."
            )
            return DEFAULT_DB_PATH

    def _migrate(self):
        """Добавляет недостающие колонки в уже существующую БД.

        CREATE TABLE IF NOT EXISTS не меняет схему у файла, созданного старой
        версией, поэтому без этого после обновления кода запросы к новым
        колонкам падали бы с 'no such column'.
        """
        cur = self._conn.execute("PRAGMA table_info(operator_state)")
        existing = {r[1] for r in cur.fetchall()}
        for col, ddl in (
            ("wa_ack_active", "INTEGER NOT NULL DEFAULT 0"),
            ("wa_ack_at", "TEXT"),
            ("first_call_locked", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in existing:
                self._conn.execute(f"ALTER TABLE operator_state ADD COLUMN {col} {ddl}")
                print(f"[STATE] миграция: добавлена колонка {col}")

    def _row(self, op_id: str, day: date) -> sqlite3.Row:
        op_id = str(op_id)
        day_s = day.isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM operator_state WHERE op_id = ? AND day = ?", (op_id, day_s)
            )
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO operator_state (op_id, day) VALUES (?, ?)", (op_id, day_s)
                )
                self._conn.commit()
                cur = self._conn.execute(
                    "SELECT * FROM operator_state WHERE op_id = ? AND day = ?", (op_id, day_s)
                )
                row = cur.fetchone()
            return row

    def _update(self, op_id: str, day: date, **fields):
        if not fields:
            return
        op_id = str(op_id)
        day_s = day.isoformat()
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [op_id, day_s]
        with self._lock:
            self._conn.execute(
                f"UPDATE operator_state SET {cols} WHERE op_id = ? AND day = ?", values
            )
            self._conn.commit()

    # =========================
    # FIRST CALL (фиксируется один раз за день)
    # =========================
    def set_first_call_if_empty(self, op_id: str, day: date, hm: str):
        row = self._row(op_id, day)
        if not row["first_call_locked"] and hm:
            self._update(op_id, day, first_call_locked=str(hm))

    def get_first_call_locked(self, op_id: str, day: date) -> str:
        return self._row(op_id, day)["first_call_locked"] or ""

    # =========================
    # WA = отмена алерта + ЗАМОРОЗКА неактивности до реального звонка
    # =========================
    def is_wa_ack_active(self, op_id: str, now: datetime) -> bool:
        return bool(self._row(op_id, now.date())["wa_ack_active"])

    def get_wa_ack_at(self, op_id: str, now: datetime) -> Optional[datetime]:
        raw = self._row(op_id, now.date())["wa_ack_at"]
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    def clear_wa(self, op_id: str, now: datetime):
        self._update(op_id, now.date(), wa_ack_active=0, wa_ack_at=None)

    def mark_wa_cancel_alert(self, op_id: str, now: datetime, message_id: int) -> bool:
        """Нажали WA на конкретном алерте: откатывает счётчик этого алерта
        (15/30/60), увеличивает wa_count и включает режим WA — оператор
        считается активным, а накопленная неактивность замораживается на
        момент нажатия. Режим снимается только реальным новым звонком
        (это проверяет monitor.py), а не следующим тиком мониторинга.
        Возвращает True, если это действительно был алерт данного оператора."""
        day = now.date()

        try:
            msg_id = int(message_id)
        except Exception:
            return False

        with self._lock:
            cur = self._conn.execute(
                "SELECT op_id, threshold FROM alert_msg_map WHERE msg_id = ?", (msg_id,)
            )
            info = cur.fetchone()

        if not info:
            return False

        mapped_op_id, thr = str(info["op_id"]), int(info["threshold"])
        if mapped_op_id != str(op_id):
            return False

        row = self._row(op_id, day)
        updates = {
            "wa_count": int(row["wa_count"]) + 1,
            "wa_ack_active": 1,
            "wa_ack_at": _iso(now),
            "status": "ACTIVE",
        }

        if thr in (15, 30, 60):
            col = f"alert_count_{thr}"
            updates[col] = max(0, int(row[col]) - 1)

        msg_ids = _parse_int_list(row["last_message_ids"])
        if msg_id in msg_ids:
            msg_ids.remove(msg_id)
            updates["last_message_ids"] = _dump_int_list(msg_ids)

        self._update(op_id, day, **updates)

        with self._lock:
            self._conn.execute("DELETE FROM alert_msg_map WHERE msg_id = ?", (msg_id,))
            self._conn.commit()

        return True

    def get_wa_count(self, op_id: str, now: datetime) -> int:
        return int(self._row(op_id, now.date())["wa_count"])

    # =========================
    # ABSENT
    # =========================
    def mark_absent_today(self, op_id: str, now: datetime, by: str = ""):
        day = now.date()
        self._row(op_id, day)
        self._update(
            op_id, day,
            absent_today=1,
            absent_by=by or "",
            absent_at=now.strftime("%H:%M"),
            status="ACTIVE",
            last_call_time=None,
            sent_thresholds="",
        )

    def is_absent_today(self, op_id: str, now: datetime) -> bool:
        return bool(self._row(op_id, now.date())["absent_today"])

    # =========================
    # ACTIVITY
    # =========================
    def on_operator_active(self, op_id: str, now: datetime, last_call_time: Optional[datetime]):
        day = now.date()
        row = self._row(op_id, day)
        if row["absent_today"]:
            return
        self._update(
            op_id, day,
            status="ACTIVE",
            last_call_time=_iso(last_call_time) or row["last_call_time"],
            sent_thresholds="",
        )

    def on_operator_inactive(self, op_id: str, now: datetime, last_call_time: Optional[datetime]):
        day = now.date()
        row = self._row(op_id, day)
        if row["absent_today"]:
            return
        if row["wa_ack_active"]:
            # пока включён WA — оператор считается активным, алерты не идут
            self._update(op_id, day, status="ACTIVE")
            return
        self._update(
            op_id, day,
            status="INACTIVE",
            last_call_time=_iso(last_call_time) or row["last_call_time"],
        )

    # =========================
    # THRESHOLDS
    # =========================
    def get_due_thresholds(
        self, op_id: str, now: datetime, current_inactive_seconds: int, thresholds_minutes: List[int]
    ) -> List[int]:
        row = self._row(op_id, now.date())
        if row["absent_today"] or row["wa_ack_active"] or row["status"] != "INACTIVE":
            return []
        sent = _parse_int_set(row["sent_thresholds"])
        mins = int((current_inactive_seconds or 0) // 60)
        return [t for t in sorted(thresholds_minutes) if mins >= t and t not in sent]

    def register_alert_sent(self, op_id: str, now: datetime, threshold_min: int, msg_id: Optional[int]):
        day = now.date()
        row = self._row(op_id, day)
        if row["absent_today"]:
            return

        t = int(threshold_min)
        sent = _parse_int_set(row["sent_thresholds"])
        sent.add(t)

        updates = {"sent_thresholds": _dump_int_set(sent)}
        if t in (15, 30, 60):
            col = f"alert_count_{t}"
            updates[col] = int(row[col]) + 1

        if msg_id:
            msg_ids = _parse_int_list(row["last_message_ids"])
            msg_ids.append(int(msg_id))
            updates["last_message_ids"] = _dump_int_list(msg_ids)

        self._update(op_id, day, **updates)

        if msg_id:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO alert_msg_map (msg_id, op_id, threshold) VALUES (?, ?, ?)",
                    (int(msg_id), str(op_id), t),
                )
                self._conn.commit()

    def get_alert_count(self, op_id: str, threshold_min: int, now: datetime) -> int:
        t = int(threshold_min)
        if t not in (15, 30, 60):
            return 0
        return int(self._row(op_id, now.date())[f"alert_count_{t}"])

    # =========================
    # DAILY
    # =========================
    def can_send_daily_report(self, now: datetime) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT sent_day FROM daily_report WHERE id = 1")
            row = cur.fetchone()
        sent_day = row["sent_day"] if row else None
        return sent_day != now.date().isoformat()

    def mark_daily_report_sent(self, now: datetime):
        with self._lock:
            self._conn.execute(
                "INSERT INTO daily_report (id, sent_day) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET sent_day = excluded.sent_day",
                (now.date().isoformat(),),
            )
            self._conn.commit()
