"""Идущий прямо сейчас звонок не должен выглядеть как неактивность.

История АТС отдаёт только завершённые разговоры, поэтому человек на длинном
звонке ловил ложный алерт: «16 минут молчит», хотя он всё это время говорил.
Реалтайм закрывается событиями от ВАТС (вебхук), а придержанный на
перепроверку алерт — страховка на случай, когда события нет.
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor import MonitorService
from providers.base import CallRecord
from state_store import StateStore
from webhook_server import LiveCallTracker, build_operator_index, classify_event

TZ = pytz.timezone("Asia/Almaty")
DAY = date(2026, 8, 10)  # понедельник


class FakeCfg:
    tz = "Asia/Almaty"
    work_schedule = {i: ("11:00", "20:00") for i in range(6)}
    lunch_start = "13:00"
    lunch_end = "14:00"
    thresholds_minutes = [15, 30, 60]
    live_call_ttl_minutes = 120


class FakeProvider:
    def __init__(self, records=None):
        self.records = records or []

    def fetch_calls(self, day):
        return [r for r in self.records if r.started_at.date() == day], None


def dt(h, m=0, s=0):
    return TZ.localize(datetime(DAY.year, DAY.month, DAY.day, h, m, s))


def make_call(op_id, h, m, duration_sec, call_id=None):
    return CallRecord(
        call_id=call_id or f"{op_id}-{h}:{m}",
        started_at=dt(h, m),
        duration_sec=duration_sec,
        ring_sec=0,
        direction="out",
        answered=duration_sec > 0,
        operator_key=op_id,
        operator_name="Test",
        from_number="",
        to_number="",
        source="kcell",
    )


def make_service(records=None, state=None):
    return MonitorService(
        FakeCfg(),
        {"Test": {"id": "1", "kcell": {"login": "test", "ext": "701"}}},
        state or StateStore(":memory:"),
        FakeProvider(records),
    )


def freeze_now(monkeypatch, moment):
    import monitor as monitor_mod

    class FrozenDatetime(monitor_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(monitor_mod, "datetime", FrozenDatetime)


# ---------------- главный баг: длинный разговор ----------------

def test_operator_on_a_long_call_is_not_marked_inactive(monkeypatch):
    """Ровно тот случай, из-за которого всё затевалось: звонок идёт дольше
    порога, в истории его ещё нет — алерта быть не должно."""
    state = StateStore(":memory:")
    svc = make_service([make_call("1", 11, 0, 60)], state)   # прошлый звонок кончился 11:01

    state.start_live_call("1", "live-1", dt(11, 10), "ACCEPTED", "kcell")

    freeze_now(monkeypatch, dt(11, 30))                      # говорит уже 20 минут
    s = svc.find_by_id(svc.build_snapshot()[0], "1")

    assert s.on_call is True
    assert s.category == "ACTIVE"
    assert s.current_inactive_seconds == 0


def test_without_the_live_call_the_same_situation_is_inactive(monkeypatch):
    """Контроль: без события о звонке поведение прежнее — это и был ложный алерт."""
    svc = make_service([make_call("1", 11, 0, 60)])
    freeze_now(monkeypatch, dt(11, 30))
    s = svc.find_by_id(svc.build_snapshot()[0], "1")

    assert s.on_call is False
    assert s.category == "INACTIVE"
    assert s.current_inactive_seconds == 29 * 60


def test_total_inactivity_stops_at_the_start_of_the_live_call(monkeypatch):
    """Пауза ДО звонка — настоящая неактивность и должна остаться в отчёте,
    а сам разговор в неё попадать не должен."""
    state = StateStore(":memory:")
    svc = make_service([make_call("1", 11, 0, 60)], state)
    state.start_live_call("1", "live-1", dt(11, 10), "OUTGOING", "kcell")

    freeze_now(monkeypatch, dt(11, 45))
    s = svc.find_by_id(svc.build_snapshot()[0], "1")

    assert s.total_inactive_seconds == 9 * 60      # 11:01 -> 11:10, дальше говорит


def test_finished_call_in_history_clears_the_live_flag(monkeypatch):
    """Событие COMPLETED могло не дойти. Как только разговор появился в
    истории — пометку «на линии» снимаем сами."""
    state = StateStore(":memory:")
    svc = make_service([make_call("1", 11, 10, 900, call_id="live-1")], state)
    state.start_live_call("1", "live-1", dt(11, 10), "ACCEPTED", "kcell")

    freeze_now(monkeypatch, dt(11, 30))
    s = svc.find_by_id(svc.build_snapshot()[0], "1")

    assert s.on_call is False
    assert state.get_live_calls(dt(11, 30)) == {}
    assert s.current_inactive_seconds == 5 * 60   # 11:25 (конец) -> 11:30


def test_hung_live_call_expires_and_monitoring_resumes():
    """Если COMPLETED потерялся совсем, человек не должен навсегда остаться
    «разговаривающим» и выпасть из мониторинга."""
    state = StateStore(":memory:")
    state.start_live_call("1", "live-1", dt(11, 0), "ACCEPTED", "kcell")

    assert state.get_live_calls(dt(11, 30), ttl_seconds=7200)   # ещё жив
    assert state.get_live_calls(dt(14, 0), ttl_seconds=7200) == {}   # 3 часа — снят


def test_absent_still_wins_over_a_live_call(monkeypatch):
    state = StateStore(":memory:")
    svc = make_service([], state)
    state.mark_absent_today("1", dt(11, 5), by="Руководитель")
    state.start_live_call("1", "live-1", dt(11, 10), "ACCEPTED", "kcell")

    freeze_now(monkeypatch, dt(11, 30))
    s = svc.find_by_id(svc.build_snapshot()[0], "1")
    assert s.category == "ABSENT"


# ---------------- разбор событий от ВАТС ----------------

def test_classify_form_encoded_start_and_end():
    start = classify_event({"cmd": "event", "type": "ACCEPTED", "call_id": "abc", "user": "dina"})
    assert start["kind"] == "start" and start["call_id"] == "abc" and start["operator"] == "dina"

    end = classify_event({"cmd": "event", "type": "COMPLETED", "call_id": "abc", "user": "dina"})
    assert end["kind"] == "end"


def test_classify_reads_nested_json():
    payload = {"cmd": "event", "call": {"uid": "x1", "status": "OUTGOING", "login": "balnur"}}
    info = classify_event(payload)
    assert info["kind"] == "start" and info["call_id"] == "x1" and info["operator"] == "balnur"


def test_classify_ignores_other_commands_and_noise():
    assert classify_event({"cmd": "contact", "phone": "+7700"}) is None
    assert classify_event({"cmd": "rating", "score": "5"}) is None
    assert classify_event({"hello": "world"}) is None


def test_operator_index_maps_login_and_extensions():
    ops = {
        "Дина": {"id": "dina", "kcell": {"login": "dina", "ext": "704"}},
        "Балнур": {"id": "balnur", "kcell": {"login": "balnur", "ext": "702"}, "sipuni": {"ext": "97"}},
    }
    index = build_operator_index(ops)
    assert index["dina"] == "dina"
    assert index["704"] == "dina"
    assert index["97"] == "balnur"
    assert index["702"] == "balnur"


def test_tracker_marks_and_clears_the_line():
    state = StateStore(":memory:")
    tracker = LiveCallTracker(state, {"Дина": {"id": "dina", "kcell": {"login": "dina"}}}, TZ)

    assert tracker.handle({"cmd": "event", "type": "OUTGOING", "call_id": "c1", "user": "dina"}) == "start"
    assert set(state.get_live_calls(datetime.now(TZ))) == {"dina"}

    assert tracker.handle({"cmd": "event", "type": "COMPLETED", "call_id": "c1", "user": "dina"}) == "end"
    assert state.get_live_calls(datetime.now(TZ)) == {}


def test_tracker_keeps_the_original_start_when_accepted_follows_outgoing():
    """OUTGOING и ACCEPTED — один и тот же звонок; начало сдвигать нельзя,
    иначе неактивность посчитается не от той точки."""
    state = StateStore(":memory:")
    tracker = LiveCallTracker(state, {"Дина": {"id": "dina", "kcell": {"login": "dina"}}}, TZ)

    tracker.handle({"cmd": "event", "type": "OUTGOING", "call_id": "c1", "user": "dina"})
    first = state.get_live_calls(datetime.now(TZ))["dina"]
    tracker.handle({"cmd": "event", "type": "ACCEPTED", "call_id": "c1", "user": "dina"})
    assert state.get_live_calls(datetime.now(TZ))["dina"] == first


def test_tracker_survives_an_unknown_operator():
    state = StateStore(":memory:")
    tracker = LiveCallTracker(state, {"Дина": {"id": "dina", "kcell": {"login": "dina"}}}, TZ)
    assert tracker.handle({"cmd": "event", "type": "ACCEPTED", "call_id": "c1", "user": "chuzhoy"}) == "unknown-operator"
    assert state.get_live_calls(datetime.now(TZ)) == {}


# ---------------- зеркало для второго бота ----------------

def test_replace_live_calls_mirrors_peer_state():
    state = StateStore(":memory:")
    state.replace_live_calls({"dina": dt(11, 10)}, dt(11, 12), source="peer")
    assert set(state.get_live_calls(dt(11, 20))) == {"dina"}

    state.replace_live_calls({}, dt(11, 30), source="peer")
    assert state.get_live_calls(dt(11, 30)) == {}


# ---------------- придержанный алерт ----------------

def test_pending_threshold_is_remembered_and_cleared():
    state = StateStore(":memory:")
    state.mark_threshold_pending("1", dt(11, 15), 15)

    pending = state.get_pending_thresholds("1", dt(11, 16))
    assert 15 in pending
    assert (dt(11, 16) - pending[15]) == timedelta(minutes=1)

    state.clear_pending_threshold("1", dt(11, 16), 15)
    assert state.get_pending_thresholds("1", dt(11, 16)) == {}


def test_pending_is_per_day():
    state = StateStore(":memory:")
    state.mark_threshold_pending("1", dt(11, 15), 15)
    tomorrow = dt(11, 15) + timedelta(days=1)
    assert state.get_pending_thresholds("1", tomorrow) == {}


# ---------------- реальный формат событий Kcell ----------------
# Конверт подтверждён работающей интеграцией соседнего проекта на этой же АТС:
#   cmd=event, callid, status=ACCEPTED|CANCELLED, from=<внутренний номер>,
#   to=<клиент>, duration=<секунды> только в финальном событии.

def test_kcell_accepted_without_duration_means_talking_now():
    info = classify_event({"cmd": "event", "callid": "77", "status": "ACCEPTED",
                           "from": "704", "to": "77012345678"})
    assert info["kind"] == "start"
    assert info["call_id"] == "77"


def test_kcell_final_event_ends_the_call_even_though_status_is_accepted():
    """Ловушка: финальное событие тоже ACCEPTED. Отличает его duration —
    иначе человек навсегда завис бы «на линии»."""
    info = classify_event({"cmd": "event", "callid": "77", "status": "ACCEPTED",
                           "from": "704", "to": "77012345678", "duration": "930"})
    assert info["kind"] == "end"


def test_kcell_zero_duration_is_also_final():
    info = classify_event({"cmd": "event", "callid": "78", "status": "CANCELLED",
                           "from": "704", "to": "77012345678", "duration": "0"})
    assert info["kind"] == "end"


def test_kcell_cancelled_without_duration_ends_the_call():
    info = classify_event({"cmd": "event", "callid": "79", "status": "CANCELLED",
                           "from": "704", "to": "77012345678"})
    assert info["kind"] == "end"


def test_operator_is_found_by_kcell_internal_number():
    """Событие приходит про номер (from=702), а не про логин — в operators.yml
    он лежит под ключом extension."""
    ops = {"Балнур": {"id": "balnur", "kcell": {"login": "balnur", "extension": "702"}}}
    assert build_operator_index(ops)["702"] == "balnur"


def test_incoming_call_finds_the_operator_in_the_to_field():
    """У входящего from — это клиент, а менеджер лежит в to."""
    state = StateStore(":memory:")
    ops = {"Дина": {"id": "dina", "kcell": {"login": "dina", "extension": "704"}}}
    tracker = LiveCallTracker(state, ops, TZ)
    assert tracker.handle({"cmd": "event", "callid": "c9", "status": "ACCEPTED",
                           "from": "77012345678", "to": "704"}) == "start"
    assert set(state.get_live_calls(datetime.now(TZ))) == {"dina"}


def test_full_kcell_call_cycle_marks_and_clears_the_line():
    state = StateStore(":memory:")
    ops = {"Дина": {"id": "dina", "kcell": {"login": "dina", "extension": "704"}}}
    tracker = LiveCallTracker(state, ops, TZ)

    tracker.handle({"cmd": "event", "callid": "c1", "status": "ACCEPTED", "from": "704", "to": "7701"})
    assert set(state.get_live_calls(datetime.now(TZ))) == {"dina"}

    tracker.handle({"cmd": "event", "callid": "c1", "status": "ACCEPTED",
                    "from": "704", "to": "7701", "duration": "960"})
    assert state.get_live_calls(datetime.now(TZ)) == {}


def test_history_command_is_not_treated_as_a_call_event():
    assert classify_event({"cmd": "history", "callid": "c1", "status": "ACCEPTED",
                           "duration": "12", "from": "704"}) is None


# ---------------- формат событий Sipuni ----------------
# Настройки → API → События АТС. Событие приходит номером, а не словом:
#   1 — вызов инициирован, 2 — завершение, 3 — на вызов ответили,
#   4 — промежуточное завершение при переводе.

def test_sipuni_answer_starts_the_call():
    info = classify_event({"event": "3", "call_id": "s1", "short_dst_num": "97",
                           "src_num": "77012345678", "dst_num": "77475567651"})
    assert info["kind"] == "start"
    assert info["source"] == "sipuni"
    assert info["call_id"] == "s1"


def test_sipuni_hangup_ends_the_call():
    assert classify_event({"event": "2", "call_id": "s1", "status": "ANSWER",
                           "short_src_num": "97"})["kind"] == "end"
    assert classify_event({"event": "4", "call_id": "s1", "short_src_num": "97"})["kind"] == "end"


def test_sipuni_call_initiation_alone_does_not_mark_anyone_busy():
    """event=1 приходит и на входящий, звонящий во весь отдел — оператор им
    ещё не занят, иначе пометили бы «на линии» всех подряд."""
    assert classify_event({"event": "1", "call_id": "s1", "short_dst_num": "97"})["kind"] == "ignore"


def test_initiation_event_is_quiet_and_changes_nothing():
    state = StateStore(":memory:")
    ops = {"Балнур": {"id": "balnur", "sipuni": {"ext": "97"}}}
    tracker = LiveCallTracker(state, ops, TZ)
    assert tracker.handle({"event": "1", "call_id": "s1", "short_dst_num": "97"}) == "ignored"
    assert state.get_live_calls(datetime.now(TZ)) == {}
    assert tracker.unknown_payloads == 0        # это не мусор, в лог не сыплем


def test_foreign_operators_do_not_pollute_the_state():
    """Sipuni шлёт события по всему аккаунту — чужие звонки просто игнорируем."""
    state = StateStore(":memory:")
    ops = {"Балнур": {"id": "balnur", "sipuni": {"ext": "97"}}}
    tracker = LiveCallTracker(state, ops, TZ)
    assert tracker.handle({"event": "3", "call_id": "x", "short_dst_num": "208"}) == "unknown-operator"
    assert state.get_live_calls(datetime.now(TZ)) == {}


def test_sipuni_operator_matched_by_internal_number_not_the_full_one():
    """У Балнур внутренний 97, а полный номер линии — 77475567651.
    Искать надо именно внутренний."""
    state = StateStore(":memory:")
    ops = {"Балнур": {"id": "balnur", "kcell": {"login": "balnur", "extension": "702"},
                      "sipuni": {"ext": "97"}}}
    tracker = LiveCallTracker(state, ops, TZ)

    assert tracker.handle({"event": "3", "call_id": "s1", "src_num": "77012345678",
                           "dst_num": "77475567651", "short_dst_num": "97"}) == "start"
    assert set(state.get_live_calls(datetime.now(TZ))) == {"balnur"}

    assert tracker.handle({"event": "2", "call_id": "s1", "short_dst_num": "97"}) == "end"
    assert state.get_live_calls(datetime.now(TZ)) == {}


def test_both_pbxs_can_post_to_the_same_receiver():
    """Обе АТС шлют на один адрес — форматы не должны мешать друг другу."""
    state = StateStore(":memory:")
    ops = {
        "Дина": {"id": "dina", "kcell": {"login": "dina", "extension": "704"}},
        "Балнур": {"id": "balnur", "kcell": {"login": "balnur", "extension": "702"},
                   "sipuni": {"ext": "97"}},
    }
    tracker = LiveCallTracker(state, ops, TZ)

    tracker.handle({"cmd": "event", "callid": "k1", "status": "ACCEPTED", "from": "704", "to": "7701"})
    tracker.handle({"event": "3", "call_id": "s1", "short_src_num": "97", "dst_num": "7701"})
    assert set(state.get_live_calls(datetime.now(TZ))) == {"dina", "balnur"}

    tracker.handle({"cmd": "event", "callid": "k1", "status": "ACCEPTED",
                    "from": "704", "to": "7701", "duration": "930"})
    assert set(state.get_live_calls(datetime.now(TZ))) == {"balnur"}

    tracker.handle({"event": "2", "call_id": "s1", "short_src_num": "97"})
    assert state.get_live_calls(datetime.now(TZ)) == {}
