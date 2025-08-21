from __future__ import annotations
import os
import io
import json
from typing import List, Dict
import base64
import time

import logging
import re

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

from app.config import Settings, get_endpoints
from app.inventory import InventoryClient
from app.scraper import parse_styles_from_text
from app.webjson import fetch_inventory_json, parse_inventory_json
from app.exporter import rows_to_dataframe
from app.search import find_products, parse_search_results, _build_headers_for_query
from app.inventory_formatter import create_inventory_display_table


def set_env_temp(key: str, value: str | None):
    if value is None:
        return
    if value:
        os.environ[key] = value


def as_bytes_xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return buf.read()


def as_bytes_xlsx_sheets(sheets: Dict[str, pd.DataFrame]) -> bytes:
    """Create an XLSX with multiple sheets from a mapping of sheet_name -> DataFrame.
    Sheet names are sanitized to Excel's 31-char limit and made unique.
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        used: set[str] = set()
        for raw_name, df in sheets.items():
            name = (raw_name or "Sheet").strip() or "Sheet"
            name = name[:31]
            base = name
            i = 1
            while name in used:
                suffix = f"_{i}"
                name = (base[: max(0, 31 - len(suffix))] + suffix) or f"Sheet_{i}"
                i += 1
            used.add(name)
            # Keep index for cross tables to preserve the first column header
            df.to_excel(writer, sheet_name=name, index=True)
    buf.seek(0)
    return buf.read()


def _sanitize_xml_for_log(xml_text: str | None) -> str | None:
    """Mask sensitive values in SOAP XML (customer number, username, password).
    Replaces the contents of <arg0>, <arg1>, <arg2> with ***.
    """
    if not xml_text:
        return xml_text
    try:
        # Mask typical credential arg tags
        masked = re.sub(r"(<arg0>)(.*?)(</arg0>)", r"\1***\3", xml_text)
        masked = re.sub(r"(<arg1>)(.*?)(</arg1>)", r"\1***\3", masked)
        masked = re.sub(r"(<arg2>)(.*?)(</arg2>)", r"\1***\3", masked)
        # Mask PromoStandards shared tags and generic id/password
        masked = re.sub(r"(<shar:id>)(.*?)(</shar:id>)", r"\1***\3", masked)
        masked = re.sub(r"(<shar:password>)(.*?)(</shar:password>)", r"\1***\3", masked)
        masked = re.sub(r"(<id>)(.*?)(</id>)", r"\1***\3", masked)
        masked = re.sub(r"(<password>)(.*?)(</password>)", r"\1***\3", masked)
        return masked
    except Exception:
        return "[unavailable]"


def _sanitize_headers_for_log(headers: dict | None) -> dict | None:
    """Mask sensitive headers like Cookie/Authorization for logging."""
    if not headers:
        return headers
    try:
        masked = dict(headers)
        if "Cookie" in masked:
            masked["Cookie"] = "***"
        if "Authorization" in masked:
            masked["Authorization"] = "***"
        return masked
    except Exception:
        return {"[unavailable]": ""}


def render_inventory_table(df: pd.DataFrame, chunk_size: int = 30) -> None:
    """Render the cross-table in chunks to avoid React errors for very wide tables.
    Keeps the index column and splits size columns into groups of chunk_size.
    Includes retry mechanism with smaller chunks if React errors occur.
    """
    if df is None or df.empty:
        st.warning("No inventory data available for this selection.")
        return
    
    cols = list(df.columns)
    if len(cols) <= chunk_size:
        # Try rendering the full table first
        try:
            st.dataframe(df, use_container_width=True, height=None)
            return
        except Exception as e:
            # If full table fails, fall back to chunking even for smaller tables
            st.warning(f"Table rendering failed, using chunked display. Error: {str(e)[:100]}")
    
    # Render in chunks with retry mechanism
    total = len(cols)
    current_chunk_size = chunk_size
    
    for attempt in range(3):  # Try up to 3 times with smaller chunks
        try:
            for i in range(0, total, current_chunk_size):
                sub = df.iloc[:, i : i + current_chunk_size]
                st.caption(f"Columns {i+1}-{min(i+current_chunk_size, total)} of {total}")
                st.dataframe(sub, use_container_width=True, height=None)
            return  # Success, exit function
        except Exception as e:
            if attempt < 2:  # Not the last attempt
                current_chunk_size = max(5, current_chunk_size // 2)  # Halve chunk size, minimum 5
                st.warning(f"Render attempt {attempt + 1} failed, retrying with smaller chunks ({current_chunk_size} columns)")
            else:
                # Last attempt failed, show error and fallback
                st.error(f"Failed to render table after {attempt + 1} attempts. Showing summary instead.")
                st.write(f"**Table Summary:** {len(df)} rows × {len(df.columns)} columns")
                st.write("**Column names:**", ", ".join(df.columns[:20]) + ("..." if len(df.columns) > 20 else ""))
                if not df.empty:
                    st.write("**First few rows:**")
                    st.dataframe(df.head(3), use_container_width=True)


def render_product_inventory(style: str, product_rows: List[Dict], key_prefix: str = "inv") -> None:
    """Render a single product's inventory with a color selector instead of tabs.
    This reduces the number of concurrently-mounted heavy tables and mitigates React crashes.
    """
    st.subheader(f"Inventory for {style}")
    color_values = sorted({(r.get('color') or '').strip() for r in product_rows if (r.get('color') or '').strip()})
    options = ["All"] + color_values if color_values else ["All"]
    choice = st.selectbox(
        "Color filter",
        options,
        key=f"{key_prefix}_{style}_color",
        help="Show all colors or a specific color",
    )
    rows_for_view = (
        product_rows if choice == "All" else [r for r in product_rows if (r.get('color') or '').strip() == choice]
    )
    inventory_table = create_inventory_display_table(rows_for_view, style)
    if not inventory_table.empty and 'Message' not in inventory_table.columns:
        render_inventory_table(inventory_table)
    else:
        st.warning(f"No inventory data available for {style}{'' if choice=='All' else f' — {choice}'}")


st.set_page_config(
    page_title="SanMar Inventory & Pricing",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Lightweight responsive tweaks for iPad widths
st.markdown(
    """
    <style>
    /* Improve table fit on medium screens (iPad landscape ~1024px) */
    .block-container { padding-top: 1rem; padding-bottom: 1.5rem; }
    @media (max-width: 1180px) {
      .stDataFrame { font-size: 0.9rem; }
      .stTextInput > div > div input, .stTextArea textarea { font-size: 0.95rem; }
      .stButton button { padding: 0.4rem 0.9rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("SanMar Inventory & Pricing")

with st.sidebar:
    st.header("Configuration")
    backend = st.selectbox(
        "Backend",
        options=["promostandards", "standard", "webjson"],
        index=["promostandards", "standard", "webjson"].index(os.getenv("SANMAR_BACKEND", "promostandards")),
        help="Choose data source. webjson uses sanmar.com product JSON endpoint.",
    )
    # Keep env in sync for other modules
    set_env_temp("SANMAR_BACKEND", backend)

    use_test = st.toggle("Use test environment", value=os.getenv("SANMAR_USE_TEST", "false").lower() in {"1","true","yes"})
    # Keep env in sync for backend clients
    set_env_temp("SANMAR_USE_TEST", "true" if use_test else "false")

    if backend in ("promostandards", "standard"):
        st.caption("Credentials (stored only in app state, not persisted)")
        sm_user = os.getenv("SANMAR_USERNAME", "")
        sm_pass = os.getenv("SANMAR_PASSWORD", "")
        sm_cust = ""
        if backend == "standard":
            sm_cust = st.text_input("SANMAR_CUSTOMER_NUMBER", os.getenv("SANMAR_CUSTOMER_NUMBER", ""))
        # Reflect credentials to env for InventoryClient
        set_env_temp("SANMAR_USERNAME", sm_user)
        set_env_temp("SANMAR_PASSWORD", sm_pass)
        if backend == "standard":
            set_env_temp("SANMAR_CUSTOMER_NUMBER", sm_cust)
    
    # Optional cookie/headers for sanmar.com fetches (search/pdp), shown for all backends
    with st.expander("Web fetch overrides (Cookie/Headers) — optional", expanded=(backend == "webjson")):
        st.caption("If live fetches are blocked, paste your browser Cookie and any extra headers from DevTools.")
        web_cookie = st.text_input("Cookie", os.getenv("SANMAR_WEBJSON_COOKIE", ""), type="password", key="sidebar_cookie")
        extra_headers = st.text_area("Extra headers (JSON)", os.getenv("SANMAR_WEBJSON_HEADERS", ""), key="sidebar_extra_headers", placeholder='{"sec-ch-ua":"\"Chromium\";v=\"126\""}')
        st.markdown(
            """
            - Open sanmar.com and log in.
            - Open DevTools → Network. Perform a product search or open a product page.
            - Click a JSON/XHR request (e.g. findProducts.json), copy Request Headers.
            - Paste the Cookie value above. If needed, paste extra headers as JSON (e.g. sec-ch-ua, sec-fetch-site).
            - Retry a single product first by selecting just one item below.
            """
        )

    st.divider()
    output_fmt = st.radio("Download format", ["xlsx", "csv"], horizontal=True, index=0)
    debug_log = st.checkbox("Debug: log/show fetched data", value=False)

    # Persistent downloads for last exports (helps when auto-download is blocked)
    if st.session_state.get("last_all_xlsx"):
        st.download_button(
            "Download last ALL results (XLSX)",
            data=st.session_state["last_all_xlsx"],
            file_name="sanmar_inventory_all.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_sidebar_all_xlsx",
        )
    if st.session_state.get("last_selected_xlsx"):
        st.download_button(
            "Download last SELECTED results (XLSX)",
            data=st.session_state["last_selected_xlsx"],
            file_name="sanmar_inventory_selected.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_sidebar_sel_xlsx",
        )

# Input section removed (URL/Slug/Upload + Fetch Inventory)

st.divider()
st.subheader("Search Products")

# Search controls
search_mode = st.radio("Search Mode", ["Live Search", "Upload JSON"], horizontal=True, key="search_mode")

# Persist search results and inventory data across reruns so selections/buttons don't clear the section
if "search_results" not in st.session_state:
    st.session_state["search_results"] = []
if "all_inventory_data" not in st.session_state:
    st.session_state["all_inventory_data"] = None
if "selected_inventory_data" not in st.session_state:
    st.session_state["selected_inventory_data"] = None
if "manual_inventory_data" not in st.session_state:
    st.session_state["manual_inventory_data"] = None

search_results: List[Dict] = st.session_state["search_results"]
run_search = False

if search_mode == "Live Search":
    query = st.text_input("Search query", placeholder="e.g., blue nike polos", key="search_query")
    col_q1, col_q2 = st.columns([3, 1])
    with col_q1:
        pass
    with col_q2:
        page_size = st.number_input("Page size", min_value=12, max_value=96, value=24, step=12, key="search_page_size")
    run_search = st.button("Run Search", key="run_search")
    if run_search and query:
        try:
            # Prefer sidebar overrides if provided; otherwise fall back to env
            set_env_temp("SANMAR_WEBJSON_COOKIE", web_cookie or os.getenv("SANMAR_WEBJSON_COOKIE", ""))
            set_env_temp("SANMAR_WEBJSON_HEADERS", extra_headers or os.getenv("SANMAR_WEBJSON_HEADERS", ""))
            # Prepare request spec for debugging
            req_url = "https://www.sanmar.com/search/findProducts.json"
            req_body = {"text": query, "currentPage": 0, "pageSize": int(page_size), "sort": "relevance"}
            req_headers = _build_headers_for_query(query)

            raw = find_products(query=query, page=0, page_size=int(page_size))
            search_results = parse_search_results(raw)
            st.session_state["search_results"] = search_results

            if debug_log:
                # Build compact response sample
                try:
                    results = raw.get("results") or raw.get("products") or []
                    sample = {
                        "top_level_keys": list(raw.keys()) if isinstance(raw, dict) else [],
                        "result_count": len(results) if isinstance(results, list) else None,
                        "results_head": results[:3] if isinstance(results, list) else None,
                    }
                except Exception:
                    sample = {"note": "unavailable"}
                with st.expander("Live Search logs", expanded=False):
                    st.json(
                        {
                            "request": {
                                "url": req_url,
                                "headers": _sanitize_headers_for_log(req_headers),
                                "body": req_body,
                            },
                            "response_sample": sample,
                        }
                    )
        except Exception as e:
            st.exception(e)
elif search_mode == "Upload JSON":
    uploaded_search = st.file_uploader("findProducts.json response (JSON)", type=["json"], key="search_upload")
    if st.button("Parse Uploaded Search", key="parse_search"):
        if uploaded_search is None:
            st.warning("Please upload a search JSON file.")
        else:
            try:
                data = json.load(uploaded_search)
                search_results = parse_search_results(data)
                st.session_state["search_results"] = search_results
            except Exception as e:
                st.exception(e)

# Manual input (style codes) — always available
with st.expander("Manual input (style codes)", expanded=False):
    manual_styles_input = st.text_area(
        "Enter style codes (one per line or comma-separated)",
        key="manual_styles_input",
        placeholder="K420\nL223",
        help="Works with PromoStandards and Standard backends. Color suffixes will be ignored."
    )

if st.button("Fetch inventory for MANUAL styles", type="primary", key="fetch_manual_styles"):
    rows_manual: List[Dict] = []
    failed_manual: List[str] = []
    first_error_msg_manual: str | None = None
    debug_manual_payloads: List[Dict] = []

    # Prepare client and any overrides
    inv_client = InventoryClient(Settings())
    set_env_temp("SANMAR_WEBJSON_COOKIE", web_cookie or os.getenv("SANMAR_WEBJSON_COOKIE", ""))
    set_env_temp("SANMAR_WEBJSON_HEADERS", extra_headers or os.getenv("SANMAR_WEBJSON_HEADERS", ""))

    manual_codes = [c.strip().upper() for c in re.split(r"[\s,;]+", manual_styles_input or "") if c.strip()]
    if not manual_codes:
        st.warning("Please enter one or more style codes.")
    elif backend == "webjson":
        st.warning("Manual input works only for PromoStandards/Standard backends (style codes). Switch backend to use manual styles.")
    else:
        with st.spinner("Fetching inventory for manual styles..."):
            for m_idx, style_code in enumerate(manual_codes, 1):
                try:
                    style_root = style_code.split("_", 1)[0]
                    if backend == "promostandards":
                        res = inv_client.get_promostandards_inventory(product_id=style_root)
                    elif backend == "standard":
                        # Use SanMar Standard Inventory: getInventoryQtyForStyleColorSize
                        res = inv_client.get_standard_inventory(style=style_root)
                    else:
                        # Should not happen due to earlier guard, but keep safe
                        raise ValueError("Manual input not supported for this backend")
                    rows_manual.extend(res.get("rows", []))
                    st.success(f"✓ Fetched {len(res.get('rows', []))} rows for {style_root} (manual)")
                    if debug_log:
                        item_payload = {
                            "selected": style_root,
                            "backend": backend,
                            "response": res,
                            "source": "manual",
                        }
                        if backend == "promostandards":
                            item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_ps_request_xml)
                            item_payload["last_response_xml"] = inv_client.last_ps_response_xml
                            item_payload["endpoint_url"] = inv_client.last_ps_url
                        elif backend == "standard":
                            item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_standard_request_xml)
                            item_payload["last_response_xml"] = inv_client.last_standard_response_xml
                            item_payload["endpoint_url"] = inv_client.last_standard_url
                        item_payload["use_test"] = use_test
                        debug_manual_payloads.append(item_payload)
                except Exception as e:
                    failed_manual.append(style_code)
                    if first_error_msg_manual is None:
                        first_error_msg_manual = str(e)[:600]

        if rows_manual:
            st.success(f"Fetched inventory data for {len(set([r.get('style','') for r in rows_manual]))} products (manual).")
            
            # Store in session state
            st.session_state["manual_inventory_data"] = rows_manual

            # Group rows by product/style for tabular display
            products_inventory = {}
            for row in rows_manual:
                style_key = row.get('styleNumber') or row.get('style', 'Unknown')
                if style_key not in products_inventory:
                    products_inventory[style_key] = []
                products_inventory[style_key].append(row)

            # Display each product's inventory (single table via selector)
            for style, product_rows in products_inventory.items():
                render_product_inventory(style, product_rows, key_prefix="manual")

            with st.expander("Raw Data View (manual)", expanded=False):
                dfm = rows_to_dataframe(rows_manual)
                st.dataframe(dfm, use_container_width=True, height=300)

            # Download options
            # Build per-style cross tables for XLSX export
            dfm = rows_to_dataframe(rows_manual)
            cross_sheets: Dict[str, pd.DataFrame] = {}
            for style, product_rows in products_inventory.items():
                tbl = create_inventory_display_table(product_rows, style)
                if not tbl.empty and 'Message' not in tbl.columns:
                    cross_sheets[style] = tbl
        if output_fmt == "csv":
            csv_bytes = dfm.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV (manual)", data=csv_bytes, file_name="sanmar_inventory_manual.csv", mime="text/csv")
        else:
            # Prefer cross tables; fallback to flat if unavailable
            if cross_sheets:
                xlsx_bytes = as_bytes_xlsx_sheets(cross_sheets)
            else:
                xlsx_bytes = as_bytes_xlsx(dfm)
            st.session_state["last_selected_xlsx"] = xlsx_bytes
            st.download_button(
                "Download XLSX (manual)",
                data=xlsx_bytes,
                file_name="sanmar_inventory_manual.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    if debug_log and debug_manual_payloads:
        with st.expander("Fetched data (manual)"):
            st.json(debug_manual_payloads)
    if failed_manual:
        st.warning(f"Failed manual styles: {', '.join(failed_manual[:8])}{' ...' if len(failed_manual) > 8 else ''}")
        if first_error_msg_manual:
            with st.expander("First error details (manual)"):
                st.code(first_error_msg_manual)

if search_results:
    df_search = pd.DataFrame(search_results)
    # Display compactly
    st.dataframe(df_search.rename(columns={"priceText": "price"}), use_container_width=True, height=360)

    # Build selection maps: label -> slug/styleNumber
    options = []
    label_to_slug: Dict[str, str] = {}
    label_to_code: Dict[str, str] = {}
    label_to_style_number: Dict[str, str] = {}
    for r in search_results:
        label = f"{(r.get('styleNumber') or r.get('code') or '')} - {r.get('name','')} ({r.get('slug','')})"
        options.append(label)
        label_to_slug[label] = r.get("slug", "")
        label_to_code[label] = r.get("code", "")
        label_to_style_number[label] = r.get("styleNumber", "")

    picked = st.multiselect("Select products to fetch inventory", options, key="search_select")
    # Manual input UI moved above to be always available

    if st.button("Fetch inventory for selected", type="primary", key="fetch_from_search"):
        st.info(f"Starting fetch for {len(picked)} selected products using backend: {backend}")
        rows2: List[Dict] = []
        failed_sel: List[str] = []
        debug_payloads: List[Dict] = []
        first_error_msg_sel: str | None = None
        sel_errors: List[str] = []
        # Prepare clients/overrides per backend
        inv_client = InventoryClient(Settings())
        set_env_temp("SANMAR_WEBJSON_COOKIE", web_cookie or os.getenv("SANMAR_WEBJSON_COOKIE", ""))
        set_env_temp("SANMAR_WEBJSON_HEADERS", extra_headers or os.getenv("SANMAR_WEBJSON_HEADERS", ""))
        # Standard backend uses SanMar Standard SOAP: getInventoryQtyForStyleColorSize
        
        with st.spinner("Fetching inventory for selected products..."):
            for idx, label in enumerate(picked, 1):
                slug_sel = label_to_slug.get(label, "")
                code_sel = label_to_code.get(label, "")
                style_num_sel = label_to_style_number.get(label, "")
                st.write(f"Processing {idx}/{len(picked)}: {label[:50]}...")
                try:
                    if backend == "webjson":
                        if not slug_sel:
                            st.warning(f"Skipping {label[:30]} - no slug")
                            continue
                        # Use the JSON endpoint with pantWaistSize parameter
                        res = fetch_inventory_json(slug_sel)
                    elif backend == "promostandards":
                        # Prefer SanMar web JSON endpoint if slug and Cookie available; else fall back to SOAP
                        used_webjson_ps_sel = False
                        cookie_present = bool(os.getenv("SANMAR_WEBJSON_COOKIE", "").strip())
                        if slug_sel and cookie_present:
                            res = fetch_inventory_json(slug_sel)
                            used_webjson_ps_sel = True
                        else:
                            if not style_num_sel:
                                st.warning(f"Skipping {label[:30]} - no styleNumber")
                                continue
                            # Fallback: PromoStandards expects the style (root) productId, not color-suffixed codes
                            style_root = style_num_sel.split("_", 1)[0]
                            res = inv_client.get_promostandards_inventory(product_id=style_root)
                    else:  # standard (use SanMar Standard SOAP)
                        if not style_num_sel:
                            st.warning(f"Skipping {label[:30]} - no styleNumber")
                            continue
                        # Use style only (root) for Standard inventory
                        style_root = style_num_sel.split("_", 1)[0]
                        res = inv_client.get_standard_inventory(style=style_root)
                    # If structured error returned (e.g., HTML/non-JSON), record and continue
                    if res.get("error"):
                        failed_sel.append(style_num_sel or slug_sel)
                        sel_errors.append(f"{label[:50]}: {res.get('message', 'unknown error')[:200]}")
                        if first_error_msg_sel is None:
                            first_error_msg_sel = res.get("message", "")[:600]
                        continue
                    # Normalize and annotate rows to ensure grouping by styleNumber
                    group_style = (style_num_sel or "").split("_", 1)[0] or (code_sel or "").split("_", 1)[0] or (slug_sel or "").split("_", 1)[0]
                    for r in res.get("rows", []):
                        r2 = dict(r)
                        if group_style:
                            r2["styleNumber"] = group_style
                            r2["style"] = group_style
                        rows2.append(r2)
                    st.success(f"✓ Fetched {len(res.get('rows', []))} rows for {label[:30]}")
                    # Record server-reported errors/messages
                    if res.get("error"):
                        sel_errors.append(f"{style_num_sel or slug_sel}: {res.get('message') or 'Service reported an error.'}")
                    elif res.get("message") and not res.get("rows"):
                        sel_errors.append(f"{style_num_sel or slug_sel}: {res.get('message')}")
                    
                    if debug_log:
                        logging.info("[selected] backend=%s key=%s rows=%s", backend, style_num_sel or slug_sel, len(res.get("rows", [])))
                        item_payload = {
                            "selected": style_num_sel or slug_sel,
                            "backend": backend,
                            "response": res,
                        }
                        # Attach sanitized XML for SOAP backends
                        if backend == "standard":
                            # Using SanMar Standard SOAP under the hood
                            item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_standard_request_xml)
                            item_payload["last_response_xml"] = inv_client.last_standard_response_xml
                            item_payload["endpoint_url"] = inv_client.last_standard_url
                            item_payload["use_test"] = use_test
                        elif backend == "promostandards":
                            if 'used_webjson_ps_sel' in locals() and used_webjson_ps_sel:
                                # We used the web JSON endpoint instead of SOAP
                                item_payload["endpoint_url"] = f"https://www.sanmar.com/p/{slug_sel}/checkInventoryJson?pantWaistSize="
                            else:
                                item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_ps_request_xml)
                                item_payload["last_response_xml"] = inv_client.last_ps_response_xml
                                item_payload["endpoint_url"] = inv_client.last_ps_url
                                item_payload["use_test"] = use_test
                        debug_payloads.append(item_payload)
                except Exception as e:
                    failed_sel.append(style_num_sel or slug_sel)
                    if first_error_msg_sel is None:
                        first_error_msg_sel = str(e)[:600]
                    sel_errors.append(f"{label[:50]}: {str(e)[:200]}")

            # Also process manual style inputs, if any
            manual_codes = [c.strip().upper() for c in re.split(r"[\s,;]+", manual_styles_input or "") if c.strip()]
            manual_warned_webjson = False
            for m_idx, style_code in enumerate(manual_codes, 1):
                try:
                    if backend == "webjson":
                        if not manual_warned_webjson:
                            st.warning("Manual input works only for PromoStandards/Standard backends (style codes). Switch backend to use manual styles.")
                            manual_warned_webjson = True
                        continue
                    # Use style root (strip any color suffix)
                    style_root = style_code.split("_", 1)[0]
                    if backend == "promostandards":
                        res = inv_client.get_promostandards_inventory(product_id=style_root)
                    else:  # standard
                        res = inv_client.get_standard_inventory(style=style_root)
                    for r in res.get("rows", []):
                        r2 = dict(r)
                        r2["styleNumber"] = style_root
                        r2["style"] = style_root
                        rows2.append(r2)
                    st.success(f"✓ Fetched {len(res.get('rows', []))} rows for {style_root} (manual)")
                    if debug_log:
                        item_payload = {
                            "selected": style_root,
                            "backend": backend,
                            "response": res,
                            "source": "manual",
                        }
                        if backend == "promostandards":
                            item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_ps_request_xml)
                            item_payload["last_response_xml"] = inv_client.last_ps_response_xml
                            item_payload["endpoint_url"] = inv_client.last_ps_url
                        else:
                            item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_standard_request_xml)
                            item_payload["last_response_xml"] = inv_client.last_standard_response_xml
                            item_payload["endpoint_url"] = inv_client.last_standard_url
                        item_payload["use_test"] = use_test
                        debug_payloads.append(item_payload)
                except Exception as e:
                    failed_sel.append(style_code)
                    if first_error_msg_sel is None:
                        first_error_msg_sel = str(e)[:600]
                    st.error(f"✗ Failed (manual) {style_code[:30]}: {str(e)[:100]}")

        if rows2:
            # Display inventory in tabular format matching the uploaded image
            st.success(f"Fetched inventory data for {len(picked)} selected products.")
            
            # Store in session state
            st.session_state["selected_inventory_data"] = rows2
            
            # Group rows by product/style for tabular display
            products_inventory = {}
            for row in rows2:
                style_key = row.get('styleNumber') or row.get('style', 'Unknown')
                if style_key not in products_inventory:
                    products_inventory[style_key] = []
                products_inventory[style_key].append(row)
            
            # Display each product's inventory (single table via selector)
            for style, product_rows in products_inventory.items():
                render_product_inventory(style, product_rows, key_prefix="selected")
            
            # Also keep the traditional dataframe view as backup
            with st.expander("Raw Data View", expanded=False):
                df2 = rows_to_dataframe(rows2)
                st.dataframe(df2, use_container_width=True, height=300)

            # Download options
            df2 = rows_to_dataframe(rows2)
            if output_fmt == "csv":
                csv_bytes = df2.to_csv(index=False).encode("utf-8")
                st.download_button("Download CSV (selected)", data=csv_bytes, file_name="sanmar_inventory_selected.csv", mime="text/csv")
            else:
                xlsx_bytes = as_bytes_xlsx(df2)
                st.session_state["last_selected_xlsx"] = xlsx_bytes
                st.download_button(
                    "Download XLSX (selected)",
                    data=xlsx_bytes,
                    file_name="sanmar_inventory_selected.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        if debug_log and debug_payloads:
            with st.expander("Fetched data (selected)"):
                st.json(debug_payloads)
        if failed_sel:
            st.warning(f"Failed to fetch some selected products: {', '.join(failed_sel[:8])}{' ...' if len(failed_sel) > 8 else ''}")
            if first_error_msg_sel:
                with st.expander("First error details (selected)"):
                    st.code(first_error_msg_sel)
        if sel_errors:
            with st.expander("Errors (selected)", expanded=False):
                for msg in sel_errors:
                    st.write(f"- {msg}")

    # Fetch inventory for ALL search results
    if st.button("Fetch inventory for ALL results", type="secondary", key="fetch_all_from_search"):
        # Set fetching flag to prevent persisted data from showing during fetch
        st.session_state["currently_fetching"] = True
        
        rows_all: List[Dict] = []
        failed_all: List[str] = []
        first_error_msg: str | None = None
        debug_all_payloads: List[Dict] = []
        all_errors: List[str] = []
        # Prepare clients/overrides per backend
        inv_client = InventoryClient(Settings())
        set_env_temp("SANMAR_WEBJSON_COOKIE", web_cookie or os.getenv("SANMAR_WEBJSON_COOKIE", ""))
        set_env_temp("SANMAR_WEBJSON_HEADERS", extra_headers or os.getenv("SANMAR_WEBJSON_HEADERS", ""))
        # Standard backend uses SanMar Standard SOAP inventory method

        with st.spinner("Fetching inventory for all results..."):
            total = max(len(search_results), 1)
            prog = st.progress(0)
            for idx, r in enumerate(search_results, start=1):
                slug_all = r.get("slug", "")
                code_all = r.get("code", "")
                style_num_all = r.get("styleNumber", "")
                # Only require slug for webjson; SOAP backends rely on style code
                if backend == "webjson" and not slug_all:
                    prog.progress(min(idx/total, 1.0))
                    continue
                if backend in ("promostandards", "standard") and not style_num_all:
                    prog.progress(min(idx/total, 1.0))
                    continue
                try:
                    if backend == "webjson":
                        if not slug_all:
                            raise ValueError("Missing slug for webjson fetch")
                        res = fetch_inventory_json(slug_all)
                    elif backend == "promostandards":
                        # Prefer JSON endpoint if slug is available; fall back to SOAP
                        if slug_all:
                            res = fetch_inventory_json(slug_all)
                        else:
                            # Use style root (strip color suffix if present)
                            style = (style_num_all or "").split("_", 1)[0]
                            if not style:
                                raise ValueError("Missing styleNumber for PromoStandards fetch")
                            res = inv_client.get_promostandards_inventory(product_id=style)
                    else:  # standard (use SanMar Standard SOAP)
                        style = (style_num_all or "").split("_", 1)[0]
                        if not style:
                            raise ValueError("Missing styleNumber for Standard fetch")
                        res = inv_client.get_standard_inventory(style=style)
                    # Normalize and annotate rows to ensure grouping by styleNumber
                    group_style = (style_num_all or "").split("_", 1)[0] or (code_all or "").split("_", 1)[0] or (slug_all or "").split("_", 1)[0]
                    for r2 in res.get("rows", []):
                        rr = dict(r2)
                        if group_style:
                            rr["styleNumber"] = group_style
                            rr["style"] = group_style
                        rows_all.append(rr)
                    if backend == "standard" and not res.get("rows") and res.get("message"):
                        st.warning(f"Server message for {style_num_all}: {res.get('message')}")
                    # Record server-reported errors/messages
                    if res.get("error"):
                        all_errors.append(f"{style_num_all or slug_all}: {res.get('message') or 'Service reported an error.'}")
                    elif res.get("message") and not res.get("rows"):
                        all_errors.append(f"{style_num_all or slug_all}: {res.get('message')}")
                    if debug_log:
                        logging.info("[all] backend=%s key=%s rows=%s", backend, style_num_all or slug_all, len(res.get("rows", [])))
                        item_payload = {
                            "item": style_num_all or slug_all,
                            "backend": backend,
                            "response": res,
                        }
                        if backend == "standard":
                            # Using SanMar Standard SOAP under the hood for 'standard'
                            item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_standard_request_xml)
                            item_payload["last_response_xml"] = inv_client.last_standard_response_xml
                            item_payload["endpoint_url"] = inv_client.last_standard_url
                            item_payload["use_test"] = use_test
                        elif backend == "promostandards":
                            if slug_all:
                                item_payload["endpoint_url"] = f"https://www.sanmar.com/p/{slug_all}/checkInventoryJson?pantWaistSize="
                            else:
                                item_payload["last_request_xml"] = _sanitize_xml_for_log(inv_client.last_ps_request_xml)
                                item_payload["last_response_xml"] = inv_client.last_ps_response_xml
                                item_payload["endpoint_url"] = inv_client.last_ps_url
                                item_payload["use_test"] = use_test
                        debug_all_payloads.append(item_payload)
                except Exception as e:
                    failed_all.append(r.get("styleNumber") or slug_all)
                    if first_error_msg is None:
                        first_error_msg = str(e)[:600]
                    all_errors.append(f"{(r.get('styleNumber') or slug_all)[:50]}: {str(e)[:200]}")
                prog.progress(min(idx/total, 1.0))
                # small delay to avoid hammering endpoints
                time.sleep(0.05)

        if rows_all:
            # Summarize
            st.success(
                f"Fetched {len(rows_all)} rows from all search results. Success: {len(set([r.get('style','') for r in rows_all]))} products | Failures: {len(failed_all)}"
            )
            
            # Store in session state to persist after button clicks
            st.session_state["all_inventory_data"] = rows_all
            # Clear fetching flag
            st.session_state["currently_fetching"] = False

            # Group rows by product/style for tabular (cross-table) display
            products_inventory: Dict[str, List[Dict]] = {}
            for row in rows_all:
                style = row.get('style', 'Unknown')
                products_inventory.setdefault(style, []).append(row)

            # Display each product's inventory (single table via selector)
            for style, product_rows in products_inventory.items():
                render_product_inventory(style, product_rows, key_prefix="all")

            # Raw flat view as backup
            with st.expander("Raw Data View (ALL)", expanded=False):
                df_all = rows_to_dataframe(rows_all)
                st.dataframe(df_all, use_container_width=True, height=520)

            if debug_log and debug_all_payloads:
                with st.expander("Fetched data (ALL) – sample"):
                    st.json(debug_all_payloads[:5])

            # Download options
            df_all = rows_to_dataframe(rows_all)
            if output_fmt == "csv":
                csv_bytes = df_all.to_csv(index=False).encode("utf-8")
                st.download_button("Download CSV (all results)", data=csv_bytes, file_name="sanmar_inventory_all.csv", mime="text/csv")
            else:
                # Build per-style cross tables for multi-sheet XLSX; fallback to flat
                cross_sheets: Dict[str, pd.DataFrame] = {}
                for style, product_rows in products_inventory.items():
                    tbl = create_inventory_display_table(product_rows, style)
                    if not tbl.empty and 'Message' not in tbl.columns:
                        cross_sheets[style] = tbl
                if cross_sheets:
                    xlsx_bytes = as_bytes_xlsx_sheets(cross_sheets)
                else:
                    xlsx_bytes = as_bytes_xlsx(df_all)
                st.session_state["last_all_xlsx"] = xlsx_bytes
                filename = "sanmar_inventory_all.xlsx"
                # Present only a manual download button (no auto-download)
                st.download_button(
                    "Download XLSX (all results)",
                    data=xlsx_bytes,
                    file_name=filename,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            if failed_all:
                st.warning(f"Failed slugs: {', '.join(failed_all[:8])}{' ...' if len(failed_all) > 8 else ''}")
                if first_error_msg:
                    with st.expander("First error details"):
                        st.code(first_error_msg)
            if all_errors:
                with st.expander("Errors (ALL results)", expanded=False):
                    for msg in all_errors:
                        st.write(f"- {msg}")
        else:
            # Clear fetching flag even if no results
            st.session_state["currently_fetching"] = False
            if failed_all:
                st.warning(f"No inventory rows fetched. All attempted slugs failed. Check Cookie/Headers in the sidebar. Failed count: {len(failed_all)}")
                if first_error_msg:
                    with st.expander("First error details"):
                        st.code(first_error_msg)
            else:
                st.info("No inventory rows fetched for these results.")
            if all_errors:
                with st.expander("Errors (ALL results)", expanded=False):
                    for msg in all_errors:
                        st.write(f"- {msg}")

# Display persisted inventory data if available (only show when not actively fetching)
if not st.session_state.get("currently_fetching", False):
    if st.session_state.get("all_inventory_data"):
        st.divider()
        st.subheader("📊 All Inventory Results (Persisted)")
        rows_all = st.session_state["all_inventory_data"]
        
        # Group rows by product/style for tabular display
        products_inventory: Dict[str, List[Dict]] = {}
        for row in rows_all:
            style = row.get('style', 'Unknown')
            products_inventory.setdefault(style, []).append(row)
        
        # Display each product's inventory (single table via selector)
        for style, product_rows in products_inventory.items():
            render_product_inventory(style, product_rows, key_prefix="persisted_all")
        
        # Add clear button
        if st.button("Clear All Inventory Data", type="secondary"):
            st.session_state["all_inventory_data"] = None
        # (Removed) Selected Inventory Results (Persisted) section

    elif st.session_state.get("manual_inventory_data"):
        st.divider()
        st.subheader("📊 Manual Inventory Results (Persisted)")
        rows_manual = st.session_state["manual_inventory_data"]
        
        # Group rows by product/style for tabular display
        products_inventory = {}
        for row in rows_manual:
            style_key = row.get('styleNumber') or row.get('style', 'Unknown')
            if style_key not in products_inventory:
                products_inventory[style_key] = []
            products_inventory[style_key].append(row)
        
        # Display each product's inventory (single table via selector)
        for style, product_rows in products_inventory.items():
            render_product_inventory(style, product_rows, key_prefix="persisted_manual")
        
        # Add clear button
        if st.button("Clear Manual Inventory Data", type="secondary"):
            st.session_state["manual_inventory_data"] = None

st.caption("Tip: webjson works best if you paste your browser Cookie in the sidebar when fetching live.")
