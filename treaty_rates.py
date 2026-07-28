"""Refreshes the Ministry of Finance's double-taxation treaty rate table.

Source: mof.ge's Georgian and English double-taxation pages (scraping
permitted by the site). Each treaty is one `<tr class="IDResult">` row of
five columns -- country, permanent-establishment period, dividends, interest,
royalties, per the page's own header row -- and only country (td 1) and
interest rate (td 4) are kept. The two languages are scraped and saved
separately: the English page is missing one treaty the Georgian page has
(Sweden, as of this writing), so they are not guaranteed to have the same
country count.

Parsed with the stdlib `html.parser` rather than a new dependency: the table
is simple and stable, and a nested `<span>` (footnote markers, and one row
with an empty `<span></span>` in its rate cell) only needs its text folded
into the enclosing <td>, which HTMLParser does natively.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import requests

import diagnostics as diag

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

URL_KA = "https://www.mof.ge/ka/doubleTaxation"
URL_EN = "https://www.mof.ge/en/doubleTaxation"

OUT_PATH_KA = KNOWLEDGE_DIR / "double_tax_treaty_rates.json"
OUT_PATH_EN = KNOWLEDGE_DIR / "double_tax_treaty_rates_en.json"

ROW_CLASS = "IDResult"
COUNTRY_COL, INTEREST_COL = 0, 3


class _TreatyTableParser(HTMLParser):
    """Collects td[0] (country) and td[3] (interest rate) from every
    <tr class="IDResult"> row into `rows`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: dict[str, str] = {}
        self._in_row = False
        self._in_td = False
        self._td_index = -1
        self._td_texts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            classes = (dict(attrs).get("class") or "").split()
            self._in_row = ROW_CLASS in classes
            self._td_index = -1
            self._td_texts = []
        elif tag == "td" and self._in_row:
            self._td_index += 1
            self._in_td = True
            self._td_texts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._in_td = False
        elif tag == "tr" and self._in_row:
            if len(self._td_texts) > INTEREST_COL:
                country = self._td_texts[COUNTRY_COL].strip()
                rate = self._td_texts[INTEREST_COL].strip()
                if country:
                    self.rows[country] = rate
            self._in_row = False

    def handle_data(self, data: str) -> None:
        if self._in_row and self._in_td:
            self._td_texts[self._td_index] += data


def scrape_treaty_rates(url: str, *, timeout: int = 30) -> dict[str, str]:
    """Fetch `url` and return {country: interest_rate} for every IDResult row."""
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    parser = _TreatyTableParser()
    parser.feed(response.text)
    return parser.rows


def refresh_treaty_rates(url: str, path: Path) -> dict[str, str] | None:
    """Scrape the treaty table at `url` and save it to `path` as JSON.

    Never raises: this runs before every analysis, and a site hiccup or a
    layout change on mof.ge must not stop a contract analysis that does not
    even depend on this table yet. Returns None (and warns) on failure,
    leaving whatever was last saved at `path` in place.
    """
    try:
        rates = scrape_treaty_rates(url)
    except Exception as exc:  # noqa: BLE001 -- network/parse failures must not sink a run
        diag.warn(f"could not refresh double-tax treaty rates ({path.name}): {exc}")
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rates, ensure_ascii=False, indent=2), encoding="utf-8")
    diag.progress(f"Refreshed double-tax treaty rates ({len(rates)} countries) -> {path.name}")
    return rates


def refresh_all_treaty_rates() -> None:
    """Refresh both the Georgian and English treaty tables.

    This is the entry point CLI and web app call before every analysis run;
    each language is independent, so one failing does not skip the other.
    """
    refresh_treaty_rates(URL_KA, OUT_PATH_KA)
    refresh_treaty_rates(URL_EN, OUT_PATH_EN)
