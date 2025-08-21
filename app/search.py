from __future__ import annotations
import os
import json
from typing import Dict, List, Any
import requests
from urllib.parse import quote_plus

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.sanmar.com",
    "Content-Type": "application/json;charset=UTF-8",
}


def _build_headers_for_query(query: str) -> Dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = f"https://www.sanmar.com/search/?text={quote_plus(query)}"
    cookie = os.getenv("SANMAR_WEBJSON_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    extra_headers = os.getenv("SANMAR_WEBJSON_HEADERS", "").strip()
    if extra_headers:
        try:
            headers.update(json.loads(extra_headers))
        except Exception:
            pass
    return headers


def find_products(query: str, page: int = 0, page_size: int = 24, sort: str = "relevance") -> Dict[str, Any]:
    """
    Calls SanMar search endpoint to find products by text query.
    Returns raw JSON payload.
    """
    url = "https://www.sanmar.com/search/findProducts.json"
    body = {
        "text": query,
        "currentPage": page,
        "pageSize": page_size,
        "sort": sort,
        # Keep payload minimal; filters/facets can be added if needed
    }
    headers = _build_headers_for_query(query)
    resp = requests.post(url, headers=headers, json=body, timeout=25)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as e:
        snippet = resp.text[:200].replace("\n", " ") if isinstance(resp.text, str) else ""
        raise ValueError(f"Non-JSON response from search (status {resp.status_code}). First 200 chars: {snippet}") from e


def parse_search_results(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Extract a compact list of products from search response JSON.
    Returns a list of {slug, code, name, priceText}.
    """
    out: List[Dict[str, str]] = []
    results = data.get("results") or data.get("products") or []
    for item in results:
        code = item.get("code") or ""
        name = item.get("name") or ""
        price = item.get("displayPriceText") or item.get("salePriceText") or item.get("originalPriceText") or ""
        style_number = item.get("styleNumber") or item.get("style") or code
        slug = ""
        url = item.get("url") or item.get("pdpUrl") or ""
        if url and "/p/" in url:
            try:
                slug_part = url.split("/p/")[-1]
                # Remove path segments after slug as well as query/hash fragments
                slug_part = slug_part.split("?")[0].split("#")[0]
                slug = slug_part.split("/")[0].strip("/")
            except Exception:
                slug = code
        else:
            slug = code
        out.append({
            "slug": slug,
            "code": code,
            "styleNumber": style_number,
            "name": name,
            "priceText": price,
        })
    return out
