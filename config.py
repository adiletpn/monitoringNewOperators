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

    # Sipuni — часть операторов звонит через неё
    sipuni_user: str = _clean(os.getenv("SIPUNI_USER", ""))
    sipuni_secret: str = _clean(os.getenv("SIPUNI_SECRET", ""))
    sipuni_csv_tz: str = _clean(os.getenv("SIPUNI_CSV_TZ", ""))

    kcell_base_url: str = _clean(os.getenv("KCELL_BASE_URL", ""))
    kcell_api_key: str = _clean(os.getenv("KCELL_API_KEY", ""))
    kcell_tz: str = _clean(os.getenv("KCELL_TZ", "Asia/Almaty"))
    kcell_count_directions: str = _clean(os.getenv("KCELL_COUNT_DIRECTIONS", "out")) or "out"
    kcell_operators_yml: str = _clean(os.getenv("KCELL_OPERATORS_YML", "operators.yml")) or "operators.yml"

    tz: str = _clean(os.getenv("TZ", "Asia/Almaty"))

    check_every_seconds: int = int(_clean(os.getenv("CHECK_EVERY_SECONDS", "60")) or "60")

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

    # Отложенный старт: до этой даты бот молчит — не шлёт алерты и дневной
    # отчёт. Формат YYYY-MM-DD, пусто = работать сразу. Нужно, когда бота
    # выкатили заранее, а следить надо начать с конкретного дня.
    monitor_start_date: str = _clean(os.getenv("MONITOR_START_DATE", ""))

    # Пауза перед отправкой алерта. АТС не показывает звонок, пока он идёт —
    # запись появляется только после завершения. Поэтому долгий разговор
    # выглядит как молчание, и на 15-й минуте прилетал ложный алерт.
    # Достигнув порога, бот ждёт это время и перепроверяет: если звонок
    # успел завершиться и появился в истории, алерт отменяется.
    alert_confirm_seconds: int = int(_clean(os.getenv("ALERT_CONFIRM_SECONDS", "180")) or "180")

    # ---- события о звонках в реальном времени (вебхук от ВАТС) ----
    # Ключ из кабинета Kcell, поле «Ключ для авторизации». Пока он не задан,
    # порт слушаем (Railway ждёт этого от web-процесса), но события отклоняем:
    # открытый приёмник позволил бы кому угодно объявить оператора занятым.
    crm_token: str = _clean(os.getenv("KCELL_CRM_TOKEN", ""))
    # Railway сам подставляет PORT для web-процесса
    webhook_port: int = int(_clean(os.getenv("PORT", "8080")) or "8080")
    # Страховка от потерянного COMPLETED: через столько минут «разговаривает»
    # снимается само, иначе человек навсегда выпал бы из мониторинга.
    live_call_ttl_minutes: int = int(_clean(os.getenv("LIVE_CALL_TTL_MINUTES", "120")) or "120")

    # Если кабинет разрешает только один адрес CRM, второй бот забирает
    # «кто сейчас на линии» у первого: LIVE_CALLS_URL=https://<бот1>/live
    live_calls_url: str = _clean(os.getenv("LIVE_CALLS_URL", ""))
    live_calls_token: str = _clean(os.getenv("LIVE_CALLS_TOKEN", ""))

    @property
    def webhook_enabled(self) -> bool:
        forced = _clean(os.getenv("WEBHOOK_ENABLED", "")).lower()
        return forced not in ("0", "false", "no")

    @property
    def telephony_sources(self) -> list[str]:
        """TELEPHONY_PROVIDER -> список источников: 'kcell', 'sipuni',
        'both' или 'kcell,sipuni'."""
        raw = (self.telephony_provider or "kcell").strip().lower()
        if raw == "both":
            return ["kcell", "sipuni"]
        return [n.strip() for n in raw.replace("+", ",").split(",") if n.strip()]

    def __post_init__(self):
        sources = self.telephony_sources
        unknown = [s for s in sources if s not in ("sipuni", "kcell")]
        if unknown or not sources:
            raise RuntimeError(
                f"Invalid TELEPHONY_PROVIDER: {self.telephony_provider!r} "
                f"(expected 'kcell', 'sipuni', 'both' or 'kcell,sipuni')"
            )
        if "sipuni" in sources and not (self.sipuni_user and self.sipuni_secret):
            raise RuntimeError("TELEPHONY_PROVIDER включает sipuni — нужны SIPUNI_USER и SIPUNI_SECRET")
        if "kcell" in sources and not (self.kcell_base_url and self.kcell_api_key):
            raise RuntimeError("TELEPHONY_PROVIDER включает kcell — нужны KCELL_BASE_URL и KCELL_API_KEY")