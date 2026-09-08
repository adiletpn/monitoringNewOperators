import csv
import io
from typing import Dict, List, Tuple


def _guess_delimiter(sample: str) -> str:
    # простая, но надежная эвристика
    # считаем разделители по строкам, чтобы не ломаться на тексте с запятыми в полях
    lines = [ln for ln in sample.splitlines() if ln.strip()][:20]
    if not lines:
        return ";"

    semi = sum(ln.count(";") for ln in lines)
    comma = sum(ln.count(",") for ln in lines)
    tab = sum(ln.count("\t") for ln in lines)

    # иногда бывает TSV
    if tab > semi and tab > comma:
        return "\t"
    return ";" if semi >= comma else ","


def parse_csv(text: str) -> Tuple[List[str], List[Dict[str, str]]]:
    raw = (text or "").strip()
    if not raw:
        return [], []

    # нормализуем переносы строк
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    sample = raw[:4000]
    delim = _guess_delimiter(sample)

    f = io.StringIO(raw, newline="")
    reader = csv.reader(
        f,
        delimiter=delim,
        quotechar='"',
        skipinitialspace=True,
    )

    rows = list(reader)
    if not rows:
        return [], []

    headers = [h.strip() for h in rows[0]]
    if headers:
        headers[0] = headers[0].lstrip("\ufeff")  # убираем BOM

    # если все заголовки пустые — выход
    if not any(headers):
        return [], []

    # фикс пустых/дублирующихся заголовков (чтобы словарь не перетирался)
    fixed_headers: List[str] = []
    seen: Dict[str, int] = {}
    for i, h in enumerate(headers):
        base = h if h else f"COL_{i+1}"
        cnt = seen.get(base, 0) + 1
        seen[base] = cnt
        fixed_headers.append(base if cnt == 1 else f"{base}_{cnt}")

    out: List[Dict[str, str]] = []
    for r in rows[1:]:
        if not r or all((c or "").strip() == "" for c in r):
            continue
        d: Dict[str, str] = {}
        for i, h in enumerate(fixed_headers):
            d[h] = (r[i].strip() if i < len(r) else "")
        out.append(d)

    return fixed_headers, out