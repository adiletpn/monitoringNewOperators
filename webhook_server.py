"""Приём событий о звонках от ВАТС в реальном времени.

Зачем: история звонков отдаёт ТОЛЬКО завершённые разговоры. Пока человек
говорит, в API его звонка нет вообще — поэтому разговор на 40 минут выглядел
как 40 минут молчания, и на 15-й минуте улетал ложный алерт.

Kcell умеет слать события сама (`cmd=event`: INCOMING/OUTGOING/ACCEPTED/
COMPLETED/CANCELLED/TRANSFERRED, POST form-urlencoded с полем `crm_token`).
Здесь мы их принимаем и держим список «кто прямо сейчас на линии».

Точных имён полей в спеке нет, поэтому парсер намеренно терпимый: понимает и
form-urlencoded, и JSON, ищет звонок и оператора по нескольким вероятным
именам полей, а всё нераспознанное пишет в лог целиком — по первому реальному
событию подстроимся точно, не выкатывая вслепую.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

# Названия событий, приходящих от ВАТС.
START_EVENTS = {"INCOMING", "OUTGOING", "ACCEPTED", "TRANSFERRED", "RINGING", "APPEARED"}
END_EVENTS = {"COMPLETED", "CANCELLED", "CANCELED", "HANGUP", "FINISHED", "DISCONNECTED"}

# По каким ключам искать значения — у разных кабинетов они называются по-разному.
_CALL_ID_KEYS = ("call_id", "callid", "uid", "id", "pbx_call_id", "call_uid", "session_id")
_EVENT_KEYS = ("type", "event", "status", "state", "call_state", "event_type")
_OPERATOR_KEYS = (
    "user", "login", "ext", "extension", "employee", "operator",
    "user_login", "user_ext", "internal",
    # Sipuni: short_* — внутренний номер (97, 240), src_num/dst_num — полный
    "short_src_num", "short_dst_num", "src_num", "dst_num", "from", "to",
)

# Sipuni нумерует события: 1 — вызов инициирован, 2 — завершение,
# 3 — на вызов ответили, 4 — промежуточное завершение при переводе.
# «На линии» считаем только с момента ОТВЕТА: событие 1 приходит и на
# входящий на весь отдел, там оператор ещё никем не занят.
_SIPUNI_KIND = {"2": "end", "3": "start", "4": "end"}


def _flatten(payload: dict) -> Dict[str, str]:
    """Складывает вложенные структуры в плоский словарь строк: события могут
    прийти как JSON с вложенным объектом звонка."""
    flat: Dict[str, str] = {}

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, prefix)
        elif obj is not None and prefix:
            key = str(prefix).strip().lower()
            if key not in flat:
                flat[key] = str(obj).strip()

    walk(payload)
    return flat


def _pick(flat: Dict[str, str], keys) -> str:
    for k in keys:
        v = flat.get(k)
        if v:
            return v
    return ""


def classify_event(payload: dict) -> Optional[dict]:
    """payload -> {'call_id', 'event', 'kind': 'start'|'end', 'operator', 'source'}
    или None, если это не событие о звонке (такие тихо игнорируем).

    Понимает два разных формата, потому что обе АТС шлют на один адрес.

    Kcell: cmd=event, callid, status=ACCEPTED|CANCELLED, from=<внутренний
    номер>, to=<клиент>, duration=<секунды> только в финальном событии.
    Разговор закончился не по названию статуса, а по появлению duration —
    финальное событие тоже ACCEPTED, и по статусу человек навсегда остался бы
    «на линии».

    Sipuni: event=1|2|3|4 числом, call_id, short_src_num / short_dst_num —
    внутренние номера. Началом разговора считаем только событие 3 (ответ).
    """
    flat = _flatten(payload)

    cmd = (flat.get("cmd") or "").strip().lower()
    if cmd and cmd != "event":
        return None

    raw_event = _pick(flat, _EVENT_KEYS).upper()
    source = ""

    # ---- Sipuni: номер события вместо названия ----
    sip_event = str(flat.get("event", "")).strip()
    if sip_event in ("1", "2", "3", "4"):
        # 1 — только инициирование вызова: известное событие, но действовать
        # по нему нельзя. Возвращаем как "ignore", чтобы не считать мусором и
        # не сыпать в лог — Sipuni шлёт его на каждый звонок аккаунта.
        kind = _SIPUNI_KIND.get(sip_event, "ignore")
        source = "sipuni"
        raw_event = f"SIPUNI-{sip_event}"
    else:
        # ---- Kcell: словесный статус + duration ----
        has_duration = str(flat.get("duration", "")).strip() != ""
        if has_duration:
            kind = "end"                  # итог звонка окончателен
        elif raw_event in END_EVENTS:
            kind = "end"
        elif raw_event in START_EVENTS:
            kind = "start"                # трубку сняли, разговор идёт
        else:
            return None
        source = "kcell"

    return {
        "call_id": _pick(flat, _CALL_ID_KEYS),
        "event": raw_event,
        "kind": kind,
        "operator": _pick(flat, _OPERATOR_KEYS),
        "source": source,
        "flat": flat,
    }


def build_operator_index(operators: Dict[str, Dict]) -> Dict[str, str]:
    """Логин/внутренний номер -> op_id. Событие приходит про телефон, а нам
    нужен наш оператор."""
    index: Dict[str, str] = {}
    for meta in operators.values():
        op_id = str(meta.get("id") or "").strip()
        if not op_id:
            continue
        kcell = meta.get("kcell") or {}
        sipuni = meta.get("sipuni") or {}
        # В operators.yml внутренний номер Kcell лежит под ключом "extension",
        # а событие приходит именно про номер (from=702), не про логин.
        for key in (op_id, kcell.get("login"), kcell.get("extension"),
                    kcell.get("ext"), sipuni.get("ext")):
            key = str(key or "").strip().lower()
            if key:
                index.setdefault(key, op_id)
    return index


class LiveCallTracker:
    """Держит «кто сейчас на линии», опираясь на события ВАТС."""

    def __init__(self, state, operators: Dict[str, Dict], tz, source: str = "kcell"):
        self.state = state
        self.tz = tz
        self.source = source
        self.index = build_operator_index(operators)
        self.unknown_payloads = 0
        self.unknown_operators = 0
        self.handled = 0

    def resolve_operator(self, raw: str) -> str:
        return self.index.get(str(raw or "").strip().lower(), "")

    def handle(self, payload: dict) -> str:
        from datetime import datetime

        info = classify_event(payload)
        if not info:
            self.unknown_payloads += 1
            # Незнакомый формат стоит увидеть, но не тысячу раз подряд.
            if self.unknown_payloads <= 5 or self.unknown_payloads % 500 == 0:
                print(
                    f"[WEBHOOK] не событие о звонке (#{self.unknown_payloads}): "
                    f"{json.dumps(payload, ensure_ascii=False)[:400]}"
                )
            return "ignored"

        if info["kind"] == "ignore":
            return "ignored"

        op_id = self.resolve_operator(info["operator"])
        if not op_id:
            # пробуем любой ключ payload — вдруг оператор лежит под другим именем
            for value in info["flat"].values():
                op_id = self.resolve_operator(value)
                if op_id:
                    break

        if not op_id:
            # Sipuni шлёт события по всему аккаунту — чужих операторов там
            # десятки, и это норма, а не ошибка. Логируем изредка, чтобы
            # заметить настоящую проблему сопоставления и не залить лог.
            self.unknown_operators += 1
            if self.unknown_operators <= 5 or self.unknown_operators % 500 == 0:
                print(
                    f"[WEBHOOK] {info['event']}: звонок не нашего оператора "
                    f"(#{self.unknown_operators}, искал по {info['operator']!r})"
                )
            return "unknown-operator"

        now = datetime.now(self.tz)
        call_id = info["call_id"] or f"{op_id}-{now.strftime('%Y%m%d%H%M%S')}"
        self.handled += 1

        source = info.get("source") or self.source
        if info["kind"] == "start":
            self.state.start_live_call(op_id, call_id, now, info["event"], source)
            print(f"[WEBHOOK] {info['event']}: {op_id} на линии (звонок {call_id})")
            return "start"

        self.state.end_live_call(call_id, op_id)
        print(f"[WEBHOOK] {info['event']}: {op_id} освободился (звонок {call_id})")
        return "end"


def _parse_body(raw: bytes, content_type: str) -> dict:
    text = raw.decode("utf-8", "replace")
    ctype = (content_type or "").lower()

    if "json" in ctype:
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            pass

    parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
    if parsed:
        return {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"raw": text}


def make_handler(tracker: LiveCallTracker, crm_token: str, ttl_seconds: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OperatorMonitor"

        def log_message(self, fmt, *args):  # шум Railway-логов не нужен
            pass

        def _reply(self, code: int, body: str = "ok", ctype: str = "text/plain"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _token_ok(self, payload: dict, query: dict) -> bool:
            # Без настроенного ключа события не принимаем вообще: открытый
            # приёмник позволил бы кому угодно объявить оператора занятым.
            if not crm_token:
                return False
            supplied = (
                str(payload.get("crm_token") or "")
                or (query.get("token", [""])[0])
                or (query.get("crm_token", [""])[0])
                or self.headers.get("X-CRM-TOKEN", "")
                or self.headers.get("Authorization", "").replace("Bearer ", "")
            )
            return str(supplied).strip() == crm_token

        def do_GET(self):
            from datetime import datetime

            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            path = parsed.path.rstrip("/") or "/"

            # Railway пингует "/" без параметров. Событие, пришедшее сюда же
            # методом GET, отличаем по наличию ключа — иначе оно потерялось бы,
            # молча приняв вид healthcheck.
            has_token = bool(query.get("token") or query.get("crm_token"))
            if path in ("/", "/health") and not has_token:
                return self._reply(200, "ok")

            # «Кто сейчас на линии» — чтобы второй бот мог забрать то же
            # состояние, если кабинет разрешает только один адрес CRM.
            if path == "/live":
                if not self._token_ok({}, query):
                    return self._reply(403, "forbidden")
                live = tracker.state.get_live_calls(datetime.now(tracker.tz), ttl_seconds)
                body = json.dumps(
                    {op: dt.isoformat() for op, dt in live.items()}, ensure_ascii=False
                )
                return self._reply(200, body, "application/json")

            # Sipuni умеет слать события и через GET — всё, что не health и
            # не live, разбираем как событие о звонке.
            if not self._token_ok({}, query):
                return self._reply(404, "not found")
            try:
                tracker.handle({k: v[0] if len(v) == 1 else v for k, v in query.items()})
            except Exception as e:
                print(f"[WEBHOOK] ошибка обработки GET-события: {e}")
            return self._reply(200, '{"success": true, "ok": true}', "application/json")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""

            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            payload = _parse_body(raw, self.headers.get("Content-Type", ""))

            if not self._token_ok(payload, query):
                print("[WEBHOOK] отклонено: неверный crm_token")
                return self._reply(403, "forbidden")

            try:
                tracker.handle(payload)
            except Exception as e:
                # событие не должно ронять приёмник — мониторинг важнее
                print(f"[WEBHOOK] ошибка обработки: {e}")

            # Sipuni считает доставку успешной только при JSON с success,
            # Kcell устраивает любой 200 — отвечаем так, чтобы годилось обоим.
            return self._reply(200, '{"success": true, "ok": true}', "application/json")

    return Handler


def start_webhook_server(cfg, tracker: LiveCallTracker) -> Optional[ThreadingHTTPServer]:
    """Поднимает приёмник в фоновом потоке. Возвращает None, если выключен или
    порт занять не удалось — бот при этом продолжает работать на истории."""
    if not getattr(cfg, "webhook_enabled", False):
        print("[WEBHOOK] выключен через WEBHOOK_ENABLED=0")
        return None

    if not getattr(cfg, "crm_token", ""):
        print(
            "[WEBHOOK] KCELL_CRM_TOKEN не задан — порт слушаю (healthcheck), "
            "но события отклоняю. Впиши ключ в кабинете Kcell и в переменные."
        )

    port = int(getattr(cfg, "webhook_port", 8080) or 8080)
    ttl = int(getattr(cfg, "live_call_ttl_minutes", 120)) * 60
    handler = make_handler(tracker, getattr(cfg, "crm_token", ""), ttl)

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    except Exception as e:
        print(f"[WEBHOOK] не удалось занять порт {port}: {e}. Работаю только на истории.")
        return None

    threading.Thread(target=httpd.serve_forever, daemon=True, name="webhook").start()
    print(f"[WEBHOOK] слушаю порт {port}; события звонков принимаются в реальном времени")
    return httpd


def pull_peer_live_calls(cfg, state, tz) -> bool:
    """Забирает «кто сейчас на линии» у соседнего бота.

    Адрес CRM в кабинете один, а ботов два — второй берёт то же состояние по
    HTTP, чтобы алерты в обоих чатах совпадали. Молча ничего не делает, если
    LIVE_CALLS_URL не задан; ошибку сети не считаем фатальной — просто в этот
    тик работаем на истории.
    """
    url = getattr(cfg, "live_calls_url", "")
    if not url:
        return False

    from datetime import datetime

    import requests

    token = getattr(cfg, "live_calls_token", "") or getattr(cfg, "crm_token", "")
    try:
        r = requests.get(url, params={"token": token} if token else None, timeout=10)
        if r.status_code != 200:
            print(f"[LIVE] сосед ответил {r.status_code}, работаю на истории")
            return False
        data = r.json()
    except Exception as e:
        print(f"[LIVE] не забрал состояние у соседа: {e}")
        return False

    mapping = {}
    for op_id, iso in (data or {}).items():
        try:
            mapping[str(op_id)] = datetime.fromisoformat(str(iso))
        except Exception:
            continue

    state.replace_live_calls(mapping, datetime.now(tz), source="peer")
    return True
