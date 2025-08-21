from __future__ import annotations
import argparse
import sys
from typing import List
import re

from .config import Settings
from .scraper import fetch_styles_from_url, parse_styles_from_text, read_styles_from_file
from .inventory import InventoryClient
"""CLI orchestrator for discovering styles and fetching inventory."""


def dedupe_preserve_order(items: List[str], normalize: bool = True) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        x = x.strip()
        if normalize:
            x = x.upper()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch SanMar inventory for a set of styles and export to CSV/XLSX."
    )
    src = parser.add_mutually_exclusive_group(required=False)
    src.add_argument("--url", help="CompanyCasuals category/search URL (or SanMar product URL with /p/{slug})")
    src.add_argument(
        "--styles",
        help="Comma/space separated list of style codes (Promo/Standard) or slugs (webjson), e.g. 'K420, PC61' or '60397_InsBlue'",
    )
    src.add_argument("--styles-file", help="Path to a text file containing style codes")

    parser.add_argument("--output", default="out.xlsx", help="Output file path (.xlsx or .csv)")
    parser.add_argument("--format", choices=["xlsx", "csv"], help="Override output format")
    parser.add_argument(
        "--backend",
        choices=["promostandards", "standard", "webjson"],
        help="Override backend (default from env: SANMAR_BACKEND)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not call APIs; only list styles discovered")
    parser.add_argument("--json-file", help="For webjson backend: path to saved checkInventoryJson response to parse offline")

    args = parser.parse_args(argv)

    settings = Settings()
    if args.backend:
        settings.backend = args.backend

    # Discover styles
    styles: List[str] = []
    def extract_slug_from_url(u: str) -> str | None:
        # Accept https://www.sanmar.com/p/{slug}/... and return slug
        try:
            # naive parse to avoid urlparse import
            parts = u.split("/p/")
            if len(parts) < 2:
                return None
            tail = parts[1]
            slug = tail.split("/")[0]
            return slug or None
        except Exception:
            return None

    if args.url:
        if settings.backend == "webjson":
            slug = extract_slug_from_url(args.url)
            if slug:
                styles = [slug]
            else:
                print("Error: For webjson backend, --url must be a product URL like https://www.sanmar.com/p/60397_InsBlue", file=sys.stderr)
                return 2
        else:
            styles = fetch_styles_from_url(args.url)
            if not styles:
                print("Warning: Unable to scrape styles from URL (site may block scripted requests).", file=sys.stderr)
                print("Provide --styles or --styles-file as a fallback.", file=sys.stderr)
    elif args.styles:
        if settings.backend == "webjson":
            # Treat as raw slugs; split on commas/whitespace
            styles = [s for s in re.split(r"[,\s]+", args.styles) if s]
        else:
            styles = parse_styles_from_text(args.styles)
    elif args.styles_file:
        if settings.backend == "webjson":
            try:
                with open(args.styles_file, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                content = ""
            styles = [s for s in re.split(r"[,\s]+", content) if s]
        else:
            styles = read_styles_from_file(args.styles_file)
    else:
        print("Error: provide one of --url, --styles, or --styles-file", file=sys.stderr)
        return 2

    styles = dedupe_preserve_order(styles, normalize=(settings.backend != "webjson"))
    if not styles:
        print("No styles found.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Discovered styles:")
        for s in styles:
            print(f"- {s}")
        return 0

    # Validate credentials
    missing = []
    if settings.backend == "promostandards":
        if not settings.sanmar_username:
            missing.append("SANMAR_USERNAME")
        if not settings.sanmar_password:
            missing.append("SANMAR_PASSWORD")
    elif settings.backend == "standard":
        if not settings.sanmar_customer_number:
            missing.append("SANMAR_CUSTOMER_NUMBER")
        if not settings.sanmar_username:
            missing.append("SANMAR_USERNAME")
        if not settings.sanmar_password:
            missing.append("SANMAR_PASSWORD")
    else:  # webjson
        # no credentials required
        pass

    if missing:
        print("Missing credentials: " + ", ".join(missing), file=sys.stderr)
        print("Set them in environment variables or a .env file (see .env.example).", file=sys.stderr)
        return 3

    client = InventoryClient(settings)
    all_rows = []
    for style in styles:
        try:
            if settings.backend == "promostandards":
                # Query Type 2: by productId only
                res = client.get_promostandards_inventory(product_id=style)
            elif settings.backend == "standard":
                # SanMar standard: by style only
                res = client.get_standard_inventory(style=style)
            else:  # webjson: style is actually a slug
                if args.json_file:
                    import json
                    from .webjson import parse_inventory_json
                    with open(args.json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    res = parse_inventory_json(data, slug=style)
                else:
                    from .webjson import fetch_inventory_json
                    res = fetch_inventory_json(slug=style)
            all_rows.extend(res.get("rows", []))
        except Exception as e:
            print(f"Error fetching {style}: {e}", file=sys.stderr)

    if not all_rows:
        print("No inventory rows returned.", file=sys.stderr)
        return 4

    # Lazy import to avoid requiring pandas for --dry-run
    from .exporter import save_rows
    out_path = save_rows(all_rows, args.output, fmt=args.format)
    print(f"Saved {len(all_rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
