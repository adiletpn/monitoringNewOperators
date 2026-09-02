import os
import sys
from datetime import date, datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor import MonitorService
from state_store import StateStore
from providers.base import CallRecord

TZ = pytz.timezone("Asia/Almaty")
DAY = date(2026, 8, 10)  # Monday


class FakeCfg:
    tz = "Asia/Almaty"
    work_schedule = {i: ("11:00", "20:00") for i in range(6)}  # ПН-СБ
    lunch_start = "13:00"
    lunch_end = "14:00"
    thresholds_minutes = [15, 30, 60]


class FakeProvider:
    def __init__(self, records=None):
        self.records = records or []

    def fetch_calls(self, day):
        return [r for r in self.records if r.started_at.date() == day], None


def make_service(records=None, operators=None):
    return MonitorService(
        FakeCfg(),
        operators or {"Test": {"id": "1"}},
        StateStore(":memory:"),
        FakeProvider(records),
        project_rops={"Яндекс такси КЗ": "@rop_handle"},
    )


def dt(h, m=0, s=0):
    return TZ.localize(datetime(DAY.year, DAY.month, DAY.day, h, m, s))


def make_call(op_id, start_h, start_m, duration_sec, name="Test"):
    return CallRecord(
        call_id=f"{op_id}-{start_h}:{start_m}",
        started_at=dt(start_h, start_m),
        duration_sec=duration_sec,
        ring_sec=0,
        direction="out",
        answered=duration_sec > 0,
        operator_key=op_id,
        operator_name=name,
        from_number="",
        to_number="",
    )


# ---------------- _seconds_between ----------------

def test_seconds_between_single_segment():
    svc = make_service()
    segments = [(dt(11, 0), dt(20, 0))]
    assert svc._seconds_between(segments, dt(11, 0), dt(12, 0)) == 3600


def test_seconds_between_excludes_break():
    svc = make_service()
    segments = svc._work_segments_excluding_break(DAY)
    # 12:30 -> 14:30 should only count 12:30-13:00 (30m) + 14:00-14:30 (30m)
    assert svc._seconds_between(segments, dt(12, 30), dt(14, 30)) == 3600


def test_seconds_between_reversed_interval_is_zero():
    svc = make_service()
    segments = [(dt(11, 0), dt(20, 0))]
    assert svc._seconds_between(segments, dt(15, 0), dt(14, 0)) == 0


def test_seconds_between_shift_boundary():
    svc = make_service()
    segments = svc._work_segments_excluding_break(DAY)
    assert svc._seconds_between(segments, dt(19, 59), dt(20, 0)) == 60
    assert svc._seconds_between(segments, dt(10, 0), dt(11, 0)) == 0  # entirely before shift


# ---------------- _total_inactive_on_interval ----------------

def test_total_inactive_zero_calls_equals_full_segments():
    svc = make_service()
    segments = svc._work_segments_excluding_break(DAY)
    shift_start, _ = svc._shift_bounds(DAY)
    total = svc._total_inactive_on_interval(segments, shift_start, dt(15, 0), [])
    # 11:00-15:00 minus lunch (13:00-14:00) = 3h
    assert total == 3 * 3600


def test_total_inactive_call_spanning_lunch_creates_no_extra_gap():
    svc = make_service()
    segments = svc._work_segments_excluding_break(DAY)
    shift_start, _ = svc._shift_bounds(DAY)
    # call starts 12:50, talks 20 min -> ends 13:10 (spans the break)
    calls = [(dt(12, 50), dt(13, 10), None)]
    total = svc._total_inactive_on_interval(segments, shift_start, dt(15, 0), calls)
    gap_before = int((dt(12, 50) - dt(11, 0)).total_seconds())
    gap_after = 3600  # 14:00-15:00, since 13:10 falls inside the excluded break
    assert total == gap_before + gap_after


def test_total_inactive_unanswered_call_zero_duration_splits_gap_evenly():
    svc = make_service()
    segments = svc._work_segments_excluding_break(DAY)
    shift_start, _ = svc._shift_bounds(DAY)
    # недозвон в 11:30 (duration=0) -> "конец" == "начало"
    calls = [(dt(11, 30), dt(11, 30), None)]
    total = svc._total_inactive_on_interval(segments, shift_start, dt(12, 0), calls)
    assert total == 3600  # 30m before + 30m after == same as if there were no calls at all


def test_total_inactive_interval_end_before_shift_start_is_zero():
    svc = make_service()
    segments = svc._work_segments_excluding_break(DAY)
    shift_start, _ = svc._shift_bounds(DAY)
    total = svc._total_inactive_on_interval(segments, shift_start, dt(10, 0), [])
    assert total == 0


def test_total_inactive_does_not_double_count_call_duration_as_inactivity():
    # регрессия: раньше total_inactive суммировал только точки НАЧАЛА звонков,
    # из-за чего сумма промежутков всегда равнялась всей прошедшей смене, вне
    # зависимости от того, сколько оператор реально разговаривал.
    calls = [make_call("1", 11, 0, 300)]  # 5 минут разговора: 11:00-11:05
    svc = make_service(calls)
    segments = svc._work_segments_excluding_break(DAY)
    shift_start, _ = svc._shift_bounds(DAY)
    call_tuples = [(dt(11, 0), dt(11, 5), None)]
    total = svc._total_inactive_on_interval(segments, shift_start, dt(11, 30), call_tuples)
    # 11:00-11:05 разговор не считается неактивностью
    assert total == 25 * 60


# ---------------- build_snapshot: current/total на основе CallRecord ----------------

def test_build_snapshot_computes_current_from_call_end_not_start(monkeypatch):
    calls = [make_call("1", 11, 0, 300)]  # ends 11:05
    svc = make_service(calls)

    fixed_now = dt(11, 20)  # 15 min after call ended

    import monitor as monitor_mod

    class FrozenDatetime(monitor_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(monitor_mod, "datetime", FrozenDatetime)

    snapshot, *_ = svc.build_snapshot()
    s = svc.find_by_id(snapshot, "1")
    assert s.current_inactive_seconds == 15 * 60
    assert s.category == "INACTIVE"  # ровно на пороге в 15 минут уже считается неактивным


def test_build_snapshot_total_inactive_is_not_full_elapsed_shift(monkeypatch):
    calls = [make_call("1", 11, 0, 300)]  # 5 min talk 11:00-11:05
    svc = make_service(calls)

    fixed_now = dt(12, 0)  # час прошёл с начала смены

    import monitor as monitor_mod

    class FrozenDatetime(monitor_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(monitor_mod, "datetime", FrozenDatetime)

    snapshot, *_ = svc.build_snapshot()
    s = svc.find_by_id(snapshot, "1")
    # весь час прошёл с начала смены, но 5 минут был разговор -> неактивность 55 мин, не 60
    assert s.total_inactive_seconds == 55 * 60


# ---------------- WA = отмена конкретного алерта, БЕЗ заморозки ----------------

def test_wa_cancel_alert_freezes_inactivity_until_a_real_call(monkeypatch):
    """WA = оператор работает в мессенджере: алерт отменяется, накопленная
    неактивность замораживается на момент нажатия, оператор считается
    активным. Снимается только реальным новым звонком."""
    calls = [make_call("1", 11, 0, 300)]  # ends 11:05
    provider = FakeProvider(calls)
    svc = MonitorService(
        FakeCfg(), {"Test": {"id": "1"}}, StateStore(":memory:"), provider,
        project_rops={},
    )

    import monitor as monitor_mod

    current_now = {"value": dt(11, 40)}  # 35 min after call ended -> INACTIVE

    class FrozenDatetime(monitor_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            v = current_now["value"]
            return v if tz else v.replace(tzinfo=None)

    monkeypatch.setattr(monitor_mod, "datetime", FrozenDatetime)

    snapshot, *_ = svc.build_snapshot()
    s = svc.find_by_id(snapshot, "1")
    assert s.category == "INACTIVE"

    svc.state.on_operator_inactive("1", current_now["value"], s.last_call_time)
    svc.state.register_alert_sent("1", current_now["value"], 15, msg_id=999)
    assert svc.state.get_alert_count("1", 15, current_now["value"]) == 1

    ok = svc.state.mark_wa_cancel_alert("1", current_now["value"], message_id=999)
    assert ok is True
    assert svc.state.get_alert_count("1", 15, current_now["value"]) == 0
    assert svc.state.get_wa_count("1", current_now["value"]) == 1

    # тик мониторинга без нового звонка: WA держится, неактивность заморожена
    current_now["value"] = dt(11, 50)
    snapshot2, *_ = svc.build_snapshot()
    s2 = svc.find_by_id(snapshot2, "1")
    assert s2.wa_active is True
    assert s2.category == "ACTIVE"
    assert s2.current_inactive_seconds == 0
    assert s2.total_inactive_seconds == 35 * 60  # заморожено на моменте нажатия

    # алерты во время WA не идут
    assert svc.state.get_due_thresholds("1", current_now["value"], 60 * 60, [15, 30, 60]) == []

    # реальный звонок после WA — режим снимается сам
    provider.records.append(make_call("1", 11, 55, 60))
    current_now["value"] = dt(12, 5)
    snapshot3, *_ = svc.build_snapshot()
    s3 = svc.find_by_id(snapshot3, "1")
    assert s3.wa_active is False
    assert svc.state.is_wa_ack_active("1", current_now["value"]) is False
    assert s3.current_inactive_seconds == 9 * 60  # от конца звонка 11:56 до 12:05


def test_wa_cancel_alert_returns_false_for_unknown_message():
    svc = make_service()
    now = dt(11, 0)
    ok = svc.state.mark_wa_cancel_alert("1", now, message_id=12345)
    assert ok is False


def test_wa_cancel_alert_does_not_clear_sent_thresholds_so_15m_wont_resend():
    svc = make_service()
    now = dt(11, 0)
    svc.state.on_operator_inactive("1", now, None)
    svc.state.register_alert_sent("1", now, 15, msg_id=1)
    svc.state.mark_wa_cancel_alert("1", now, message_id=1)
    # 15м порог всё ещё считается "отправленным" -> повторно не выдаётся due
    due = svc.state.get_due_thresholds("1", now, current_inactive_seconds=20 * 60, thresholds_minutes=[15, 30, 60])
    assert 15 not in due


# ---------------- display name / project / ROP ----------------

def test_display_name_includes_project():
    svc = make_service(operators={"Асель": {"id": "1", "project": "Яндекс такси КЗ", "tg": ""}})
    assert svc._display_name("Асель") == "Асель | Яндекс такси КЗ"


def test_display_name_without_project_is_plain():
    svc = make_service(operators={"Люда": {"id": "1", "project": "", "tg": ""}})
    assert svc._display_name("Люда") == "Люда"


def test_rop_by_project_from_injected_dict():
    svc = make_service()
    assert svc._rop_by_project("Яндекс такси КЗ") == "@rop_handle"
    assert svc._rop_by_project("Неизвестный проект") == ""
