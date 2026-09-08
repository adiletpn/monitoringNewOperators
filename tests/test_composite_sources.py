import os
import sys
from datetime import date, datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.base import CallRecord
from providers.composite import CompositeProvider
from providers.sipuni import SipuniProvider

TZ = pytz.timezone("Asia/Almaty")
DAY = date(2026, 9, 8)


def rec(op, source, h, m, dur=60):
    return CallRecord(
        call_id=f"{source}-{op}-{h}{m}",
        started_at=TZ.localize(datetime(DAY.year, DAY.month, DAY.day, h, m)),
        duration_sec=dur,
        ring_sec=0,
        direction="out",
        answered=dur > 0,
        operator_key=op,
        operator_name=op,
        from_number="",
        to_number="",
        source=source,
    )


class FakeProvider:
    def __init__(self, records=None, err=None, employees=None, boom=False):
        self.records = records or []
        self.err = err
        self.employees = employees or []
        self.boom = boom
        self.unattributed_calls = 0

    def fetch_calls(self, day):
        if self.boom:
            raise RuntimeError("АТС недоступна")
        return list(self.records), self.err

    def fetch_employees(self):
        return list(self.employees), None

    def healthcheck(self):
        return (self.err is None and not self.boom), (self.err or "ok")


# ---------------- склейка источников ----------------

def test_merges_records_from_both_sources_sorted_by_time():
    c = CompositeProvider([
        ("kcell", FakeProvider([rec("dina", "kcell", 11, 30)])),
        ("sipuni", FakeProvider([rec("balnur", "sipuni", 10, 5)])),
    ])
    records, err = c.fetch_calls(DAY)
    assert err is None
    assert [r.operator_key for r in records] == ["balnur", "dina"]  # по времени
    assert {r.source for r in records} == {"kcell", "sipuni"}


def test_one_dead_source_does_not_hide_the_other():
    """Если одна АТС легла, вторая половина команды не должна молча
    превратиться в «неактивных»."""
    c = CompositeProvider([
        ("kcell", FakeProvider([rec("dina", "kcell", 11, 0)])),
        ("sipuni", FakeProvider(err="500 Internal Server Error")),
    ])
    records, err = c.fetch_calls(DAY)
    assert err is None                       # мониторинг продолжается
    assert [r.operator_key for r in records] == ["dina"]


def test_exception_in_one_source_is_contained():
    c = CompositeProvider([
        ("kcell", FakeProvider([rec("dina", "kcell", 11, 0)])),
        ("sipuni", FakeProvider(boom=True)),
    ])
    records, err = c.fetch_calls(DAY)
    assert err is None
    assert len(records) == 1


def test_error_only_when_every_source_is_down():
    c = CompositeProvider([
        ("kcell", FakeProvider(err="timeout")),
        ("sipuni", FakeProvider(err="500")),
    ])
    records, err = c.fetch_calls(DAY)
    assert records == []
    assert err and "kcell" in err and "sipuni" in err


def test_same_call_id_within_one_source_is_deduplicated():
    dup = rec("dina", "kcell", 11, 0)
    c = CompositeProvider([("kcell", FakeProvider([dup, dup]))])
    records, _ = c.fetch_calls(DAY)
    assert len(records) == 1


def test_healthcheck_reports_each_source():
    c = CompositeProvider([
        ("kcell", FakeProvider()),
        ("sipuni", FakeProvider(err="ключ протух")),
    ])
    ok, msg = c.healthcheck()
    assert ok is False
    assert "kcell: ok" in msg and "sipuni: ключ протух" in msg


# ---------------- матчинг Sipuni по внутреннему номеру ----------------

class Cfg:
    tz = "Asia/Almaty"
    sipuni_csv_tz = ""
    sipuni_user = "u"
    sipuni_secret = "s"


def match(scheme, operators):
    p = SipuniProvider(Cfg(), operators)
    got = p._match_operator({"Схема": scheme, "Кто ответил": ""})
    return got[1] if got else None


def test_sipuni_matches_by_ext_when_op_id_is_a_login():
    """op_id теперь логин (balnur), а номер живёт в sipuni.ext — раньше на
    таком конфиге человек не находился вообще."""
    ops = {"Балнур": {"id": "balnur", "sipuni": {"ext": "97"}, "match": ["97"]}}
    assert match("Входящая 7475567651 Балнур 97", ops) == "Балнур"
    assert match("исход +77078273726 nom 97", ops) == "Балнур"


def test_sipuni_ext_does_not_match_inside_a_longer_number():
    ops = {"Балнур": {"id": "balnur", "sipuni": {"ext": "97"}, "match": ["97"]}}
    assert match("исход +77078273726 nom 970", ops) is None
    assert match("исход +77078273726 nom 94", ops) is None


def test_sipuni_falls_back_to_numeric_op_id_for_old_config():
    ops = {"Алина": {"id": "208", "sipuni": {}, "match": ["208", "Алина"]}}
    assert match("Входящая 7079263862 Алина 208 линия", ops) == "Алина"


def test_sipuni_matches_by_name_when_ext_is_unknown():
    ops = {"Карина": {"id": "karina", "sipuni": {"ext": ""}, "match": ["Карина"]}}
    assert match("Входящая 7079263946 Карина 300", ops) == "Карина"
    assert match("исход +77078273726 nom 94", ops) is None
