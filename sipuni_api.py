import hashlib
import os
import time
import requests
import certifi
from typing import Optional, Tuple

from utils_csv import parse_csv


DEFAULT_BASE_URL = "https://sipuni.com"

DEFAULT_TIMEOUT = 30

DEFAULT_RETRIES = int(os.getenv("SIPUNI_RETRIES", "3"))
DEFAULT_RETRY_SLEEP = float(os.getenv("SIPUNI_RETRY_SLEEP", "1"))

SSL_INSECURE = os.getenv("SIPUNI_SSL_INSECURE", "0").strip() == "1"


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def is_html(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return t.startswith("<!doctype") or t.startswith("<html") or t.startswith("<!--")


def build_export_all_hash(limit: int, order: str, page: int, user: str, secret: str) -> str:
    return md5_hex(f"{limit}+{order}+{page}+{user}+{secret}")


def _verify_value():
    """
    ✅ Самый надежный вариант:
    - либо verify=False (только если SIPUNI_SSL_INSECURE=1)
    - либо verify=certifi.where() (всегда безопасно)
    """
    if SSL_INSECURE:
        return False
    return certifi.where()


def _normalize_base_url(base_url: Optional[str]) -> str:
    b = (base_url or "").strip()
    if not b:
        b = DEFAULT_BASE_URL
    # убираем хвостовой /
    if b.endswith("/"):
        b = b[:-1]
    return b


def _make_urls(base_url: Optional[str]) -> Tuple[str, str]:
    """
    Позволяет переопределять домен (если понадобится),
    но по умолчанию: https://sipuni.com
    """
    b = _normalize_base_url(base_url)
    export_all_url = f"{b}/api/statistic/export/all"
    operators_url = f"{b}/api/statistic/operators"
    return export_all_url, operators_url


def _post(
    url: str,
    data: dict,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    retry_sleep: float = DEFAULT_RETRY_SLEEP,
) -> Tuple[int, str]:
    last_exc_text = ""

    headers = {
        "User-Agent": "SipuniMonitor/1.0",
        "Accept": "*/*",
    }

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                url,
                data=data,
                timeout=timeout,
                verify=_verify_value(),   # ✅ явный CA bundle
                headers=headers,
            )

            # ВАЖНО: успешный ответ не режем, чтобы CSV полностью парсился
            if r.status_code == 200:
                return r.status_code, (r.text or "")

            # А вот ошибочный ответ можно ограничить, чтобы не засорять логи HTML-страницами
            txt = (r.text or "")
            if len(txt) > 2000:
                txt = txt[:2000] + "\n...[truncated]..."
            return r.status_code, txt

        except requests.exceptions.SSLError as e:
            last_exc_text = f"SSL_ERROR: {e}"
        except requests.exceptions.Timeout as e:
            last_exc_text = f"TIMEOUT: {e}"
        except Exception as e:
            last_exc_text = str(e)

        if attempt >= retries:
            break

        time.sleep(retry_sleep)

    return 0, last_exc_text


def fetch_operators_csv(
    user: str,
    secret: str,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: Optional[str] = None,   # ✅ ДОБАВИЛ
) -> Tuple[Optional[str], str]:
    _, operators_url = _make_urls(base_url)

    params = {"user": user, "hash": md5_hex(f"{user}+{secret}")}

    status, text = _post(operators_url, params, timeout=timeout)

    if status == 0:
        return None, f"operators request error: {text}"

    if status != 200:
        return None, f"operators HTTP {status}: {text}"

    if is_html(text):
        return None, "operators returned HTML (not CSV)"

    headers, _ = parse_csv(text)
    if not headers:
        return None, "operators returned empty/invalid CSV (headers missing)"

    return text, ""


def fetch_calls_csv_export_all(
    user: str,
    secret: str,
    limit: int = 5000,
    order: str = "desc",
    page: int = 1,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: Optional[str] = None,   # ✅ ДОБАВИЛ (чтобы не падало)
) -> Tuple[Optional[str], str]:
    export_all_url, _ = _make_urls(base_url)

    params = {
        "user": user,
        "limit": str(limit),
        "order": order,
        "page": str(page),
    }
    params["hash"] = build_export_all_hash(limit, order, page, user, secret)

    status, raw = _post(export_all_url, params, timeout=timeout)

    if status == 0:
        return None, f"export/all request error: {raw}"

    if status != 200:
        return None, f"export/all HTTP {status}: {raw}"

    if is_html(raw):
        return None, "export/all returned HTML (not CSV)"

    headers, _ = parse_csv(raw)
    if not headers:
        return None, "export/all returned empty/invalid CSV (headers missing)"

    return raw, ""