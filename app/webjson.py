from __future__ import annotations
import json
from typing import Dict, List, Optional
import os
import requests

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.sanmar.com",
    # Add typical browser fetch hints to reduce bot detection
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "sec-ch-ua": '"Chromium";v="126", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}


def fetch_inventory_json(slug: str, timeout: int = 20) -> Dict[str, List[Dict]]:
    """
    Fetches inventory and price JSON from https://www.sanmar.com/p/{slug}/checkInventoryJson
    Example slug: "60397_InsBlue" or "PC61_White".
    Returns rows in the same shape used by exporter with an extra "price" column.
    """
    url = f"https://www.sanmar.com/p/{slug}/checkInventoryJson?pantWaistSize="
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = f"https://www.sanmar.com/p/{slug}"

    # Optional: allow overriding headers/cookie via env if needed
    cookie = os.getenv("SANMAR_WEBJSON_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    extra_headers = os.getenv("SANMAR_WEBJSON_HEADERS", "").strip()
    if extra_headers:
        try:
            headers.update(json.loads(extra_headers))
        except Exception:
            pass

    resp = requests.get(url, headers=headers, timeout=timeout)
    attempted_urls = [url]
    try:
        resp.raise_for_status()
        data = resp.json()
        return parse_inventory_json(data, slug)
    except Exception:
        # Retry with base style code (strip color) if slug contains underscore
        base = slug.split("_", 1)[0] if "_" in slug else slug
        if base and base != slug:
            url_base = f"https://www.sanmar.com/p/{base}/checkInventoryJson?pantWaistSize="
            attempted_urls.append(url_base)
            try:
                resp2 = requests.get(url_base, headers=headers, timeout=timeout)
                resp2.raise_for_status()
                data2 = resp2.json()
                return parse_inventory_json(data2, base)
            except Exception:
                # Continue to build error below using the last response
                resp = locals().get("resp2", resp)
        # Return a structured error so UI can surface a helpful message
        snippet = resp.text[:300].replace("\n", " ") if getattr(resp, "text", None) else ""
        ctype = resp.headers.get("Content-Type", "") if getattr(resp, "headers", None) else ""
        return {
            "rows": [],
            "error": True,
            "message": (
                "Non-JSON response. Tried: " + ", ".join(attempted_urls) +
                f". Status {getattr(resp, 'status_code', 'n/a')}, content-type: {ctype}. First 300 chars: {snippet}"
            ),
        }


def fetch_inventory_check(slug: str, timeout: int = 20) -> Dict[str, List[Dict]]:
    """
    Fetches inventory using https://www.sanmar.com/p/{slug}/checkInventory
    Falls back to /checkInventoryJson if the response is not JSON.
    Returns rows in the same shape as fetch_inventory_json.
    """
    url = f"https://www.sanmar.com/p/{slug}/checkInventory"
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = f"https://www.sanmar.com/p/{slug}"

    cookie = os.getenv("SANMAR_WEBJSON_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    extra_headers = os.getenv("SANMAR_WEBJSON_HEADERS", "").strip()
    if extra_headers:
        try:
            headers.update(json.loads(extra_headers))
        except Exception:
            pass

    resp = requests.get(url, headers=headers, timeout=timeout)
    # Try JSON first
    try:
        resp.raise_for_status()
        data = resp.json()
        return parse_inventory_json(data, slug)
    except Exception:
        # Fallback to the JSON endpoint we already support
        return fetch_inventory_json(slug=slug, timeout=timeout)


def parse_inventory_json(data: Dict, slug: str = "") -> Dict[str, List[Dict]]:
    product = data.get("product", {})
    warehouses = {str(w.get("code")): (w.get("shortName") or w.get("name") or str(w.get("code"))) for w in data.get("warehouses", [])}

    rows: List[Dict] = []
    variant_options = product.get("variantOptions", [])

    # Price selection preference: try key "3" then fallback to "UPG" or any value's formattedValue
    def extract_price(price_map: Optional[Dict]) -> Optional[float]:
        if not price_map:
            return None
        for key in ("3", "UPG"):
            if key in price_map and price_map[key].get("formattedValue"):
                try:
                    return float(price_map[key]["formattedValue"])  # formatted is numeric string
                except Exception:
                    pass
        # fallback to first entry
        for v in price_map.values():
            fv = v.get("formattedValue")
            if fv:
                try:
                    return float(fv)
                except Exception:
                    continue
        return None

    # Attempt to derive color from slug: e.g., 60397_InsBlue -> InsBlue
    color_from_slug = ""
    if slug and "_" in slug:
        try:
            color_from_slug = slug.split("_", 1)[1]
        except Exception:
            color_from_slug = ""

    for opt in variant_options:
        size = None
        for q in opt.get("variantOptionQualifiers", []):
            if q.get("qualifier") == "size":
                size = q.get("value")
                break
        price = extract_price(opt.get("priceDataMap"))
        stock_map = opt.get("stockLevelsMap", {}) or opt.get("availableStockMap", {})
        for whse_id, qty in stock_map.items():
            try:
                qty_int = int(qty)
            except Exception:
                continue
            rows.append(
                {
                    "style": slug or product.get("baseProduct") or product.get("code") or "",
                    "partId": "",
                    "color": color_from_slug,
                    "size": size or "",
                    "description": product.get("name") or "",
                    "warehouseId": str(whse_id),
                    "warehouse": warehouses.get(str(whse_id), ""),
                    "qty": qty_int,
                    "totalAvailable": None,
                    "price": price,
                }
            )
    return {"rows": rows}
