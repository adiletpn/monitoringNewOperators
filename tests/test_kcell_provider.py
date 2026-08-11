import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.kcell import KcellProvider

DAY = date(2026, 8, 10)


class FakeCfg:
    kcell_base_url = "https://corpsol.vpbx.kcell.kz/crmapi/v1"
    kcell_api_key = "test-key"
    kcell_tz = "Asia/Almaty"
    tz = "Asia/Almaty"
    kcell_count_directions = "out"


def make_provider(count_directions="out"):
    cfg = FakeCfg()
    cfg.kcell_count_directions = count_directions
    return KcellProvider(cfg, {})


# Формы записей взяты 1:1 с реального живого ответа /history/json
# (номера телефонов заменены на фиктивные, структура и набор ключей — настоящие).
ROW_OUT_SUCCESS = {
    "start": "2026-08-10T10:54:42Z", "uid": "UID-SUCCESS-1", "type": "out", "status": "success",
    "client": "77000000001", "diversion": "77780030234", "destination": "",
    "user": "lyuda", "user_name": "Люда", "wait": 9, "duration": 22,
    "record": "https://example/record.mp3",
}
ROW_OUT_NOANSWER = {
    # у "noanswer" ключа duration нет вообще (не 0, а отсутствует)
    "start": "2026-08-10T10:55:41Z", "uid": "UID-NOANSWER-1", "type": "out", "status": "noanswer",
    "client": "77000000002", "diversion": "77780030234", "destination": "",
    "user": "lyuda", "user_name": "Люда", "wait": 6,
}
ROW_IN_MISSED_DEPARTMENT = {
    # пропущенный на отдел: НЕТ ключей user/user_name вообще
    "start": "2026-08-10T10:49:55Z", "uid": "UID-MISSED-1", "type": "in", "status": "missed",
    "client": "77000000003", "destination": "vm", "group_name": "Отдел продаж",
    "diversion": "77780030234", "wait": 6,
}
ROW_IN_SUCCESS = {
    "start": "2026-08-10T09:00:00Z", "uid": "UID-IN-SUCCESS-1", "type": "in", "status": "success",
    "client": "77000000004", "user": "aisha", "user_name": "Аиша", "group_name": "",
    "diversion": "77780030234", "wait": 5, "duration": 40,
}


def _provider_with_rows(rows, count_directions="out"):
    provider = make_provider(count_directions)
    provider._get = lambda path, params=None: (rows, None)
    return provider


def test_out_success_is_counted_and_answered():
    provider = _provider_with_rows([ROW_OUT_SUCCESS])
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert len(records) == 1
    r = records[0]
    assert r.answered is True
    assert r.duration_sec == 22
    assert r.operator_key == "lyuda"
    assert r.call_id == "UID-SUCCESS-1"


def test_out_noanswer_counts_as_unanswered_attempt_with_zero_duration():
    provider = _provider_with_rows([ROW_OUT_NOANSWER])
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert len(records) == 1
    r = records[0]
    assert r.answered is False
    assert r.duration_sec == 0  # ключа duration нет вовсе -> 0, не падаем
    assert r.ended_at == r.started_at  # "недозвон" не растягивает время


def test_missed_to_department_is_skipped_and_counted_unattributed():
    provider = _provider_with_rows([ROW_IN_MISSED_DEPARTMENT])
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert records == []
    assert provider.unattributed_calls == 1


def test_answered_incoming_excluded_by_default_out_only():
    provider = _provider_with_rows([ROW_IN_SUCCESS], count_directions="out")
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert records == []


def test_answered_incoming_included_when_configured():
    provider = _provider_with_rows([ROW_IN_SUCCESS], count_directions="out,in")
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert len(records) == 1
    assert records[0].operator_key == "aisha"


def test_duplicate_uid_is_deduplicated():
    provider = _provider_with_rows([ROW_OUT_SUCCESS, dict(ROW_OUT_SUCCESS)])
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert len(records) == 1


def test_unknown_status_is_treated_as_unanswered_attempt_not_a_crash():
    row = dict(ROW_OUT_NOANSWER)
    row["uid"] = "UID-WEIRD-STATUS"
    row["status"] = "some_new_status_kcell_added_later"
    provider = _provider_with_rows([row])
    records, err = provider.fetch_calls(DAY)
    assert err is None
    assert len(records) == 1
    assert records[0].answered is False


def test_started_at_is_converted_from_utc_to_almaty_and_tz_aware():
    provider = _provider_with_rows([ROW_OUT_SUCCESS])
    records, _ = provider.fetch_calls(DAY)
    r = records[0]
    assert r.started_at.tzinfo is not None
    # 10:54:42Z UTC -> 15:54:42 Asia/Almaty (UTC+5)
    assert r.started_at.hour == 15
    assert r.started_at.minute == 54


def test_records_outside_requested_day_after_tz_conversion_are_filtered():
    # 19:30 UTC 09.08 -> 00:30 Asia/Almaty 10.08 (через границу суток)
    row = dict(ROW_OUT_SUCCESS)
    row["uid"] = "UID-EDGE"
    row["start"] = "2026-08-09T19:30:00Z"
    provider = _provider_with_rows([row])
    records, _ = provider.fetch_calls(date(2026, 8, 9))
    assert records == []  # после конвертации это уже 10.08, не 09.08

    records2, _ = provider.fetch_calls(date(2026, 8, 10))
    assert len(records2) == 1


def test_healthcheck_treats_almaty_and_oral_as_matching_after_2024_merge():
    provider = make_provider()
    provider._get = lambda path, params=None: ({"timezone": {"name": "Asia/Oral", "offset": -300}}, None)
    ok, msg = provider.healthcheck()
    assert ok is True


def test_healthcheck_fails_on_real_mismatch():
    provider = make_provider()
    provider._get = lambda path, params=None: ({"timezone": {"name": "Europe/Moscow", "offset": -180}}, None)
    ok, msg = provider.healthcheck()
    assert ok is False
    assert "mismatch" in msg
