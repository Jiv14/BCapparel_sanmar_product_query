import re
from typing import List, Set
import requests
from bs4 import BeautifulSoup

# Heuristics to extract SanMar style codes (e.g., K420, PC61, L223, JST81, LOG105)
STYLE_RE = re.compile(r"\b[A-Z]{1,5}\d{2,5}\b")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_styles_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    seen: Set[str] = set()

    # 1) Look for obvious data attributes
    for attr in ["data-style", "data-sku", "data-productid", "data-style-id"]:
        for tag in soup.find_all(attrs={attr: True}):
            val = str(tag.get(attr, "")).strip().upper()
            if STYLE_RE.search(val):
                for m in STYLE_RE.findall(val):
                    seen.add(m)

    # 2) Look for style codes in text nodes and anchor text
    for tag in soup.find_all(["a", "div", "span", "p"]):
        text = tag.get_text(" ", strip=True).upper()
        if not text:
            continue
        for m in STYLE_RE.findall(text):
            seen.add(m)

    # 3) Look for codes inside hrefs
    for a in soup.find_all("a", href=True):
        href = a["href"].upper()
        for m in STYLE_RE.findall(href):
            seen.add(m)

    return sorted(seen)


def fetch_styles_from_url(url: str, timeout: int = 20) -> List[str]:
    """
    Attempts to fetch the category/search page and extract style codes using heuristics.
    Some CompanyCasuals endpoints block scripted requests; this function tries common
    headers and returns an empty list if blocked.
    """
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    except Exception:
        return []

    if resp.status_code != 200 or "Request Rejected" in resp.text:
        return []

    return _extract_styles_from_html(resp.text)


def parse_styles_from_text(text: str) -> List[str]:
    return sorted(set([s.upper() for s in STYLE_RE.findall(text.upper())]))


def read_styles_from_file(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return []
    return parse_styles_from_text(content)
