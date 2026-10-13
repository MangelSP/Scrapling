"""Extracción por selectores CSS / XPath."""


def _is_xpath(sel: str) -> bool:
    return sel.lstrip().startswith(("/", "./", "(", "xpath:"))


def _norm(item) -> str:
    if hasattr(item, "get_all_text"):
        return item.get_all_text(strip=True)
    return str(item).strip()


def _run(root, sel: str):
    sel = sel.strip()
    if sel.startswith("xpath:"):
        return root.xpath(sel[6:].strip())
    return root.xpath(sel) if _is_xpath(sel) else root.css(sel)


def extract_fields(page, selectors: dict[str, str]) -> dict:
    out = {}
    for field, sel in selectors.items():
        try:
            out[field] = _norm(res[0]) if (res := _run(page, sel)) else None
        except Exception:
            out[field] = None
    return out


def extract_many(page, selectors: dict[str, str]) -> list[dict]:
    """Cada campo -> lista; se alinean por posición (zip hasta el más largo)."""
    cols = {}
    for field, sel in selectors.items():
        try:
            cols[field] = [_norm(i) for i in _run(page, sel)]
        except Exception:
            cols[field] = []
    n = max((len(v) for v in cols.values()), default=0)
    return [{f: (v[i] if i < len(v) else None) for f, v in cols.items()} for i in range(n)]
