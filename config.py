import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def _req(name: str) -> str:
    v = _clean(os.getenv(name))
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def _parse_thresholds(s: str) -> list[int]:
    s = _clean(s)
    if not s:
        return [15, 30, 60]
    out: list[int] = []
    for x in s.split(","):
        try:
            out.append(int(x.strip()))
        except Exception:
            pass
    out = sorted(set([x for x in out if x > 0]))
    return out or [15, 30, 60]


@dataclass(frozen=True)
class Config:
    telephony_provider: str = _clean(os.getenv("TELEPHONY_PROVIDER", "kcell")) or "kcell"

    kcell_base_url: str = _req("KCELL_BASE_URL")
    kcell_api_key: str = _req("KCELL_API_KEY")
    kcell_tz: str = _clean(os.getenv("KCELL_TZ", "Asia/Almaty"))
    kcell_count_directions: str = _clean(os.getenv("KCELL_COUNT_DIRECTIONS", "out")) or "out"
    kcell_operators_yml: str = _clean(os.getenv("KCELL_OPERATORS_YML", "operators.yml")) or "operators.yml"

    tz: str = _clean(os.getenv("TZ", "Asia/Almaty"))

    check_every_seconds: int = int(_clean(os.getenv("CHECK_EVERY_SECONDS", "300")) or "300")

    thresholds_minutes: list[int] = field(
        default_factory=lambda: _parse_thresholds(os.getenv("THRESHOLDS_MINUTES", "15,30,60"))
    )

    # ПН–ПТ
    work_start: str = _clean(os.getenv("WORK_START", "10:00"))
    work_end: str = _clean(os.getenv("WORK_END", "19:00"))

    # СБ — отдельный график (по умолчанию совпадает с будним, если не задан)
    sat_work_start: str = _clean(os.getenv("SAT_WORK_START", "")) or _clean(os.getenv("WORK_START", "10:00"))
    sat_work_end: str = _clean(os.getenv("SAT_WORK_END", "")) or _clean(os.getenv("WORK_END", "19:00"))

    # График: 0=ПН ... 5=СБ. Воскресенья в словаре нет -> выходной.
    work_schedule: dict[int, tuple[str, str]] = field(
        default_factory=lambda: {
            0: (_clean(os.getenv("WORK_START", "10:00")), _clean(os.getenv("WORK_END", "19:00"))),
            1: (_clean(os.getenv("WORK_START", "10:00")), _clean(os.getenv("WORK_END", "19:00"))),
            2: (_clean(os.getenv("WORK_START", "10:00")), _clean(os.getenv("WORK_END", "19:00"))),
            3: (_clean(os.getenv("WORK_START", "10:00")), _clean(os.getenv("WORK_END", "19:00"))),
            4: (_clean(os.getenv("WORK_START", "10:00")), _clean(os.getenv("WORK_END", "19:00"))),
            5: (
                _clean(os.getenv("SAT_WORK_START", "")) or _clean(os.getenv("WORK_START", "10:00")),
                _clean(os.getenv("SAT_WORK_END", "")) or _clean(os.getenv("WORK_END", "19:00")),
            ),
        }
    )

    lunch_start: str = _clean(os.getenv("LUNCH_START", "13:00"))
    lunch_end: str = _clean(os.getenv("LUNCH_END", "14:00"))

    tg_token: str = _req("TELEGRAM_BOT_TOKEN")

    # группа (supergroup) id -100...
    tg_chat_id: str = _req("TELEGRAM_CHAT_ID")

    # дефолтный топик "Мониторинг" (message_thread_id), чтобы не писать в General
    tg_thread_id: int = int(_clean(os.getenv("TELEGRAM_THREAD_ID", "0")) or 0)

    # алерты — по умолчанию тот же чат/топик, что и основной
    tg_alert_chat_id: str = _clean(os.getenv("TELEGRAM_ALERT_CHAT_ID", "")) or tg_chat_id
    tg_alert_thread_id: int = int(_clean(os.getenv("TELEGRAM_ALERT_THREAD_ID", "0")) or 0)

    state_db_path: str = _clean(os.getenv("STATE_DB_PATH", "state.db")) or "state.db"

    def __post_init__(self):
        if self.telephony_provider != "kcell":
            raise RuntimeError(f"Invalid TELEPHONY_PROVIDER: {self.telephony_provider!r} (expected 'kcell')")
