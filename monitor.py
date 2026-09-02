import re
from dataclasses import dataclass
from datetime import datetime, date, time as dtime
from typing import Dict, List, Optional, Tuple
import pytz

from state_store import StateStore
from providers.base import CallRecord


@dataclass
class OperatorStatus:
    name: str
    op_id: str
    category: str  # ACTIVE / INACTIVE / ABSENT
    last_call_time: Optional[datetime]
    current_inactive_seconds: int
    current_inactive_str: str
    calls_today: int
    total_inactive_seconds: int
    total_inactive_str: str
    total_talk_seconds: int
    total_talk_str: str
    first_call_str: str
    last_call_str: str
    from_number: Optional[str]
    to_number: Optional[str]
    wa_active: bool = False


def fmt_hms(seconds: int) -> str:
    s = max(0, int(seconds or 0))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h}ч {m}м {sec}с"


def _parse_hm(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


class MonitorService:
    def __init__(self, cfg, operators: Dict[str, Dict], state: StateStore, provider, project_rops: Optional[Dict[str, str]] = None):
        self.cfg = cfg
        self.operators = operators
        self.state = state
        self.provider = provider
        self.project_rops = project_rops or {}

        self.tz = pytz.timezone(cfg.tz)

    def _mention_or_name(self, name: str) -> str:
        """Возвращает @username, чтобы Telegram реально пинговал в алертах.
        Если tg в конфиге не похож на username (телефон/пусто) — просто имя."""
        meta = self.operators.get(name) or {}
        tg = str(meta.get("tg") or "").strip()

        if not tg:
            return name

        if not tg.startswith("@"):
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", tg):
                tg = "@" + tg
            else:
                return name

        if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", tg):
            return tg

        return name

    def _display_name(self, name: str) -> str:
        meta = self.operators.get(name) or {}
        project = str(meta.get("project") or "").strip()
        if project:
            return f"{name} | {project}"
        return name

    def _rop_by_project(self, project: str) -> str:
        return str(self.project_rops.get(project) or "").strip()

    def _shift_bounds(self, day: date) -> Optional[Tuple[datetime, datetime]]:
        weekday = day.weekday()
        if weekday not in self.cfg.work_schedule:
            return None
        ws, we = self.cfg.work_schedule[weekday]
        start = self.tz.localize(datetime.combine(day, _parse_hm(ws)))
        end = self.tz.localize(datetime.combine(day, _parse_hm(we)))
        return start, end

    def _break_bounds(self, day: date) -> Optional[Tuple[datetime, datetime]]:
        if not (self.cfg.lunch_start and self.cfg.lunch_end):
            return None
        ls = self.tz.localize(datetime.combine(day, _parse_hm(self.cfg.lunch_start)))
        le = self.tz.localize(datetime.combine(day, _parse_hm(self.cfg.lunch_end)))
        return ls, le

    def _work_segments_excluding_break(self, day: date) -> List[Tuple[datetime, datetime]]:
        sb = self._shift_bounds(day)
        if not sb:
            return []
        start, end = sb

        bb = self._break_bounds(day)
        if not bb:
            return [(start, end)]

        ls, le = bb
        segs: List[Tuple[datetime, datetime]] = []
        if start < ls:
            segs.append((start, min(ls, end)))
        if le < end:
            segs.append((max(le, start), end))
        return [(a, b) for a, b in segs if b > a]

    def is_in_shift(self, dt: datetime) -> bool:
        sb = self._shift_bounds(dt.date())
        if not sb:
            return False
        start, end = sb
        return start <= dt <= end

    def is_in_break(self, dt: datetime) -> bool:
        bb = self._break_bounds(dt.date())
        if not bb:
            return False
        ls, le = bb
        return ls <= dt < le

    def _clip_to_shift(self, day: date, dt: datetime) -> datetime:
        sb = self._shift_bounds(day)
        if not sb:
            return dt
        start, end = sb
        if dt < start:
            return start
        if dt > end:
            return end
        return dt

    def _seconds_between(self, segments: List[Tuple[datetime, datetime]], a: datetime, b: datetime) -> int:
        if b <= a:
            return 0
        total = 0
        for x, y in segments:
            s = max(x, a)
            e = min(y, b)
            if e > s:
                total += int((e - s).total_seconds())
        return total

    def _total_inactive_on_interval(
        self,
        segments: List[Tuple[datetime, datetime]],
        shift_start: datetime,
        interval_end: datetime,
        calls: List[Tuple[datetime, datetime, object]],
    ) -> int:
        """Суммарная неактивность = сумма окон БЕЗ звонков:
        shift_start -> начало первого звонка, конец звонка i -> начало звонка
        i+1, конец последнего звонка -> interval_end. Время самих звонков
        (включая разговор) в неактивность не входит — раньше оно ошибочно
        включалось, потому что суммировались только точки начала звонков."""
        if interval_end <= shift_start:
            return 0

        if not calls:
            return self._seconds_between(segments, shift_start, interval_end)

        trimmed = []
        for st, en, r in calls:
            if st >= interval_end:
                break
            trimmed.append((st, min(en, interval_end), r))

        if not trimmed:
            return self._seconds_between(segments, shift_start, interval_end)

        total = self._seconds_between(segments, shift_start, trimmed[0][0])

        for i in range(len(trimmed) - 1):
            total += self._seconds_between(segments, trimmed[i][1], trimmed[i + 1][0])

        total += self._seconds_between(segments, trimmed[-1][1], interval_end)
        return int(total)

    def build_snapshot(self):
        now = datetime.now(self.tz)
        today = now.date()

        sb = self._shift_bounds(today)
        if not sb:
            return [], now, False, False, "no shift today"

        shift_start, shift_end = sb
        in_shift_now = self.is_in_shift(now)
        break_now = self.is_in_break(now)
        now_clipped = self._clip_to_shift(today, now)
        segments = self._work_segments_excluding_break(today)

        records, err = self.provider.fetch_calls(today)
        if not records and err:
            return [], now, in_shift_now, break_now, err

        by_operator: Dict[str, List[CallRecord]] = {}
        for rec in records:
            by_operator.setdefault(rec.operator_key, []).append(rec)

        snapshot: List[OperatorStatus] = []
        min_thr = min(self.cfg.thresholds_minutes) if self.cfg.thresholds_minutes else 15

        for name, meta in self.operators.items():
            op_id = str(meta["id"])

            calls: List[Tuple[datetime, datetime, CallRecord]] = []
            last_record: Optional[CallRecord] = None
            last_record_start: Optional[datetime] = None

            for rec in by_operator.get(op_id, []):
                dt = rec.started_at
                if not (shift_start <= dt <= shift_end):
                    continue

                end_dt = rec.ended_at
                if end_dt > shift_end:
                    end_dt = shift_end

                calls.append((dt, end_dt, rec))

                if last_record_start is None or dt > last_record_start:
                    last_record_start = dt
                    last_record = rec

            calls.sort(key=lambda x: x[0])

            first_start = calls[0][0] if calls else None
            last_start = calls[-1][0] if calls else None
            last_end = calls[-1][1] if calls else None

            # суммарное время разговора за смену (сумма длительностей звонков)
            talk_total = sum(int(rec.duration_sec or 0) for _, _, rec in calls)

            # первый звонок фиксируется один раз за день и больше не меняется
            if first_start:
                self.state.set_first_call_if_empty(op_id, today, first_start.strftime("%H:%M"))

            first_str = self.state.get_first_call_locked(op_id, today) or (
                first_start.strftime("%H:%M") if first_start else "—"
            )
            last_str = last_start.strftime("%H:%M") if last_start else "—"

            anchor = last_end if last_end else shift_start
            current = self._seconds_between(segments, anchor, now_clipped)

            total = self._total_inactive_on_interval(segments, shift_start, now_clipped, calls)

            # ===== WA "заморозка" =====
            wa_active = self.state.is_wa_ack_active(op_id, now)
            wa_at = self.state.get_wa_ack_at(op_id, now)

            # снимается ТОЛЬКО реальным новым звонком после момента нажатия WA,
            # а не любым тиком мониторинга
            if wa_active and wa_at and last_start and last_start > wa_at:
                self.state.clear_wa(op_id, now)
                wa_active = False
                wa_at = None

            if wa_active and wa_at:
                wa_at_clipped = self._clip_to_shift(today, wa_at)
                if wa_at_clipped > now_clipped:
                    wa_at_clipped = now_clipped

                total = self._total_inactive_on_interval(segments, shift_start, wa_at_clipped, calls)
                current = 0

            if self.state.is_absent_today(op_id, now):
                category = "ABSENT"
                current = 0
                total = 0
            else:
                if wa_active:
                    category = "ACTIVE"
                else:
                    category = "ACTIVE" if (current // 60) < min_thr else "INACTIVE"

            snapshot.append(
                OperatorStatus(
                    name=name,
                    op_id=op_id,
                    category=category,
                    last_call_time=last_start,
                    current_inactive_seconds=current,
                    current_inactive_str=fmt_hms(current),
                    calls_today=len(calls),
                    total_inactive_seconds=total,
                    total_inactive_str=fmt_hms(total),
                    total_talk_seconds=talk_total,
                    total_talk_str=fmt_hms(talk_total),
                    first_call_str=first_str,
                    last_call_str=last_str,
                    from_number=(last_record.from_number if last_record else None),
                    to_number=(last_record.to_number if last_record else None),
                    wa_active=wa_active,
                )
            )

        snapshot.sort(key=lambda x: x.name.lower())
        return snapshot, now, in_shift_now, break_now, None

    def find_by_id(self, snapshot: List[OperatorStatus], op_id: str) -> Optional[OperatorStatus]:
        for s in snapshot:
            if str(s.op_id) == str(op_id):
                return s
        return None

    def format_inactive_alert(self, s: OperatorStatus, threshold_min: int) -> str:
        thr = int(threshold_min)
        head = f"🚫 ОПЕРАТОР ОТСУТСТВУЕТ {thr} МИН" if thr >= 60 else f"⛔ ОПЕРАТОР НЕАКТИВЕН {thr} МИН"

        who = self._display_name(s.name)

        meta = self.operators.get(s.name) or {}
        project = str(meta.get("project") or "").strip()
        rop = self._rop_by_project(project)
        rop_line = f"👨‍💼 РОП: {rop}\n" if rop else ""

        return (
            f"{head}\n\n"
            f"👤 {who}\n"
            f"{rop_line}"
            f"🆔 ID: {s.op_id}\n"
            f"⏱ Не звонит: {s.current_inactive_str}\n"
            f"📞 Активные звонки: {s.calls_today}\n"
            f"🕒 Первый звонок: {s.first_call_str}\n"
            f"🕒 Последняя попытка: {s.last_call_str}\n"
            f"🔴 Суммарная неактивность: {s.total_inactive_str}\n"
            f"🗣 Суммарное время разговора: {s.total_talk_str}\n"
            f"📍 Откуда: {s.from_number or '—'}\n"
            f"📍 Куда: {s.to_number or '—'}"
        )

    def format_status_text(self, snapshot, updated_at, working: bool) -> str:
        title = "🟢 РАБОЧАЯ СМЕНА" if working else "⚪️ ВНЕ СМЕНЫ/ОБЕД"
        lines = [
            f"{title}\n",
            f"📅 Дата: {updated_at.strftime('%d.%m.%Y')}",
            f"⏰ Текущее время: {updated_at.strftime('%H:%M:%S')}",
            "------------------------------",
        ]

        total_calls = 0
        total_inactive_all = 0
        total_talk_all = 0

        for s in snapshot:
            total_calls += int(s.calls_today or 0)
            total_inactive_all += int(s.total_inactive_seconds or 0)
            total_talk_all += int(s.total_talk_seconds or 0)

            lines.append(
                f"👤 {self._display_name(s.name)}\n"
                f"📞 Активные звонки: {s.calls_today}\n"
                f"⛔ Текущая неактивность: {s.current_inactive_str}\n"
                f"⛔ Суммарная неактивность: {s.total_inactive_str}\n"
                f"🗣 Суммарное время разговора: {s.total_talk_str}\n"
                f"🕒 Первый звонок: {s.first_call_str}\n"
                f"🕒 Последняя попытка: {s.last_call_str}\n"
                f"🔘 Статус: {s.category}\n"
                f"------------------------------"
            )

        lines.append(
            f"📊 ИТОГО ПО СМЕНЕ\n"
            f"📞 Всего активных звонков: {total_calls}\n"
            f"⛔ Общая неактивность: {fmt_hms(total_inactive_all)}\n"
            f"🗣 Общее время разговора: {fmt_hms(total_talk_all)}"
        )
        return "\n".join(lines)

    def format_who(self, snapshot, updated_at) -> str:
        items = [s for s in snapshot if s.category == "INACTIVE"]
        lines = [
            "🔴 НЕАКТИВНЫЕ ОПЕРАТОРЫ",
            f"⏰ Проверка: {updated_at.strftime('%H:%M:%S')}",
            "------------------------------",
        ]
        if not items:
            lines.append("✅ Нет неактивных операторов")
            return "\n".join(lines)

        for s in items:
            who = self._display_name(s.name)
            lines.append(
                f"👤 {who}\n"
                f"⛔ Не звонит: {s.current_inactive_str}\n"
                f"📞 Активные звонки: {s.calls_today}\n"
                f"🕒 Последняя попытка: {s.last_call_str}\n"
                f"------------------------------"
            )
        return "\n".join(lines)

    def format_operator_list(self) -> str:
        return "👥 Выберите оператора:"

    def format_operator_card(self, s: OperatorStatus) -> str:
        return (
            f"👤 ОПЕРАТОР: {self._display_name(s.name)}\n"
            f"🆔 ID: {s.op_id}\n\n"
            f"📞 Активные звонки: {s.calls_today}\n"
            f"⛔ Текущая неактивность: {s.current_inactive_str}\n"
            f"⛔ Суммарная неактивность: {s.total_inactive_str}\n"
            f"🗣 Суммарное время разговора: {s.total_talk_str}\n"
            f"🕒 Первый звонок: {s.first_call_str}\n"
            f"🕒 Последняя попытка: {s.last_call_str}\n\n"
            f"🔘 Статус: {s.category}"
        )

    def format_absent_confirm(self, s: OperatorStatus) -> str:
        return (
            "⛔ ПОДТВЕРЖДЕНИЕ\n\n"
            "Отметить оператора как ОТСУТСТВУЮЩЕГО?\n\n"
            f"👤 {self._display_name(s.name)}\n"
            f"🆔 ID: {s.op_id}\n\n"
            "⚠️ Оператор будет исключён из мониторинга\n"
            "⚠️ Алерты будут отключены"
        )

    def format_daily_report(self, snapshot, updated_at) -> str:
        lines = [
            "📅 ДНЕВНОЙ ОТЧЁТ",
            f"Дата: {updated_at.strftime('%d.%m.%Y')}",
            "------------------------------",
        ]
        total_calls = 0
        total_inactive_all = 0
        total_talk_all = 0

        for s in snapshot:
            total_calls += int(s.calls_today or 0)
            total_inactive_all += int(s.total_inactive_seconds or 0)
            total_talk_all += int(s.total_talk_seconds or 0)
            lines.append(
                f"👤 {self._display_name(s.name)}\n"
                f"📞 Звонков за сегодня: {s.calls_today}\n"
                f"⛔ Суммарная неактивность: {s.total_inactive_str}\n"
                f"🗣 Суммарное время разговора: {s.total_talk_str}\n"
                f"🕒 Первый звонок: {s.first_call_str}\n"
                f"🕒 Последняя попытка: {s.last_call_str}\n"
                f"🔘 Статус на конец дня: {s.category}\n"
                f"------------------------------"
            )

        lines.append(
            f"📊 ИТОГО\n"
            f"📞 Всего активных звонков: {total_calls}\n"
            f"⛔ Общая неактивность: {fmt_hms(total_inactive_all)}\n"
            f"🗣 Общее время разговора: {fmt_hms(total_talk_all)}"
        )
        return "\n".join(lines)
