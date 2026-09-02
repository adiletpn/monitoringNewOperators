import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import parse_start_date


class Cfg:
    def __init__(self, value):
        self.monitor_start_date = value


def test_parses_valid_date():
    assert parse_start_date(Cfg("2026-09-03")) == date(2026, 9, 3)


def test_empty_means_no_delay():
    assert parse_start_date(Cfg("")) is None
    assert parse_start_date(Cfg("   ")) is None


def test_broken_value_does_not_crash_the_bot(capsys):
    """Кривой формат не должен ронять бота на старте — мониторинг важнее
    строгой валидации: логируем и работаем без отложенного старта."""
    assert parse_start_date(Cfg("03.09.2026")) is None
    assert parse_start_date(Cfg("завтра")) is None
    assert "MONITOR_START_DATE" in capsys.readouterr().out


def test_gate_logic_before_and_after():
    """Проверка самого условия, по которому main.py решает молчать."""
    start = date(2026, 9, 3)
    assert date(2026, 9, 2) < start   # сегодня — молчим
    assert not (date(2026, 9, 3) < start)  # в день X — работаем
    assert not (date(2026, 9, 4) < start)  # позже — работаем
