from __future__ import annotations
import html
import logging
from typing import Dict, Iterable, List, Optional
import requests
from bs4 import BeautifulSoup

from .config import Settings, get_endpoints

logger = logging.getLogger(__name__)

SOAP_ENVELOPE_START = (
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:ns="{ns}" xmlns:shar="{shared}">\n'
    "<soapenv:Header />\n<soapenv:Body>\n"
)
SOAP_ENVELOPE_END = "</soapenv:Body>\n</soapenv:Envelope>\n"


class InventoryClient:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()
        self.endpoints = get_endpoints(self.settings.use_test)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
                "Content-Type": "text/xml; charset=utf-8",
                "Accept": "text/xml, application/xml, */*;q=0.1",
            }
        )
        self.timeout = self.settings.timeout_seconds
        # Debugging capture
        self.last_standard_request_xml: Optional[str] = None
        self.last_standard_response_xml: Optional[str] = None
        self.last_ps_request_xml: Optional[str] = None
        self.last_ps_response_xml: Optional[str] = None
        self.last_standard_url: Optional[str] = None
        self.last_ps_url: Optional[str] = None

    # -------------------- PromoStandards Inventory v2.0.0 --------------------
    def _build_promostandards_get_inventory_levels_xml(
        self,
        product_id: str,
        label_sizes: Optional[Iterable[str]] = None,
        part_colors: Optional[Iterable[str]] = None,
        part_ids: Optional[Iterable[str]] = None,
    ) -> str:
        ns = "http://www.promostandards.org/WSDL/Inventory/2.0.0/"
        shared = "http://www.promostandards.org/WSDL/Inventory/2.0.0/SharedObjects/"
        start = SOAP_ENVELOPE_START.format(ns=ns, shared=shared)
        end = SOAP_ENVELOPE_END

        def tag(name: str, value: str, prefix: str = "shar") -> str:
            return f"<{prefix}:{name}>{html.escape(value)}</{prefix}:{name}>\n"

        filter_parts: List[str] = []
        if label_sizes:
            filter_parts.append("<shar:LabelSizeArray>\n" + "".join(tag("labelSize", s) for s in label_sizes) + "</shar:LabelSizeArray>\n")
        if part_colors:
            filter_parts.append("<shar:PartColorArray>\n" + "".join(tag("partColor", c) for c in part_colors) + "</shar:PartColorArray>\n")
        if part_ids:
            filter_parts.append("<shar:partIdArray>\n" + "".join(tag("partId", str(pid)) for pid in part_ids) + "</shar:partIdArray>\n")

        filter_xml = f"<shar:Filter>\n{''.join(filter_parts)}</shar:Filter>\n" if filter_parts else ""

        # Trim potential whitespace from credentials and product id
        pid = (product_id or "").strip()
        sm_user = (self.settings.sanmar_username or "").strip()
        sm_pass = (self.settings.sanmar_password or "").strip()
        body = (
            "<ns:GetInventoryLevelsRequest>\n"
            + tag("wsVersion", "2.0.0")
            + tag("id", sm_user)
            + tag("password", sm_pass)
            + tag("productId", pid)
            + filter_xml
            + "</ns:GetInventoryLevelsRequest>\n"
        )
        return start + body + end

    def get_promostandards_inventory(
        self,
        product_id: str,
        label_sizes: Optional[Iterable[str]] = None,
        part_colors: Optional[Iterable[str]] = None,
        part_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, List[Dict]]:
        url = self.endpoints["promostandards_inventory_wsdl"].split("?")[0]
        # The binding endpoint is the base without ?WSDL
        xml = self._build_promostandards_get_inventory_levels_xml(
            product_id, label_sizes=label_sizes, part_colors=part_colors, part_ids=part_ids
        )
        # Save last request/response for debugging
        self.last_ps_request_xml = xml
        self.last_ps_url = url
        resp = self.session.post(url, data=xml.encode("utf-8"), timeout=self.timeout)
        resp.raise_for_status()
        self.last_ps_response_xml = resp.text
        return self._parse_promostandards_inventory_response(resp.text)

    def _parse_promostandards_inventory_response(self, xml_text: str) -> Dict[str, List[Dict]]:
        soup = BeautifulSoup(xml_text, "xml")
        # Detect SOAP Fault and return a structured error
        fault = soup.find("Fault")
        if fault:
            msg = ""
            fs = fault.find("faultstring")
            if fs and fs.text:
                msg = fs.text.strip()
            return {"rows": [], "error": True, "message": msg}
        rows: List[Dict] = []
        # Capture productId at envelope level if present
        prod_node = soup.find("productId")
        root_product_id = prod_node.text.strip() if prod_node and prod_node.text else ""
        for part in soup.find_all("PartInventory"):
            part_id_node = part.find("partId")
            color_node = part.find("partColor")
            size_node = part.find("labelSize")
            desc_node = part.find("partDescription")
            part_id = part_id_node.text.strip() if part_id_node and part_id_node.text else ""
            color = color_node.text.strip() if color_node and color_node.text else ""
            size = size_node.text.strip() if size_node and size_node.text else ""
            desc = desc_node.text.strip() if desc_node and desc_node.text else ""
            # Total available
            total_val = part.find("quantityAvailable")
            total_available = None
            if total_val and total_val.find("value"):
                try:
                    total_available = int(total_val.find("value").text)
                except Exception:
                    total_available = None

            # Per location
            for loc in part.find_all("InventoryLocation"):
                whse_id_node = loc.find("inventoryLocationId")
                whse_name_node = loc.find("inventoryLocationName")
                whse_id = whse_id_node.text.strip() if whse_id_node and whse_id_node.text else ""
                whse_name = whse_name_node.text.strip() if whse_name_node and whse_name_node.text else ""
                qty = None
                qnode = loc.find("inventoryLocationQuantity")
                if qnode and qnode.find("value"):
                    try:
                        qty = int(qnode.find("value").text)
                    except Exception:
                        qty = None
                rows.append(
                    {
                        "style": root_product_id,
                        "partId": part_id,
                        "color": color,
                        "size": size,
                        "description": desc,
                        "warehouseId": whse_id,
                        "warehouse": whse_name,
                        "qty": qty,
                        "totalAvailable": total_available,
                    }
                )
        return {"rows": rows}

    # -------------------- SanMar Standard Inventory --------------------
    def _build_standard_get_inventory_xml(
        self,
        style: str,
        color: Optional[str] = None,
        size: Optional[str] = None,
        by_whse: bool = False,
        whse_no: Optional[str] = None,
    ) -> str:
        # Note namespace differs from examples in guide; service path is http://webservice.integration.sanmar.com/
        ns = "http://webservice.integration.sanmar.com/"
        start = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:web="{ns}">\n<soapenv:Header />\n<soapenv:Body>\n'
        )
        end = SOAP_ENVELOPE_END
        method = "getInventoryQtyForStyleColorSizeByWhse" if by_whse else "getInventoryQtyForStyleColorSize"
        # Trim potential whitespace from credentials and parameters
        cust_no = (self.settings.sanmar_customer_number or "").strip()
        sm_user = (self.settings.sanmar_username or "").strip()
        sm_pass = (self.settings.sanmar_password or "").strip()
        style_code = (style or "").strip()
        color_val = (color or "").strip() if color else None
        size_val = (size or "").strip() if size else None
        parts = [
            f"<web:{method}>\n",
            f"<arg0>{html.escape(cust_no)}</arg0>\n",
            f"<arg1>{html.escape(sm_user)}</arg1>\n",
            f"<arg2>{html.escape(sm_pass)}</arg2>\n",
            f"<arg3>{html.escape(style_code)}</arg3>\n",
        ]
        if color_val:
            parts.append(f"<arg4>{html.escape(color_val)}</arg4>\n")
        if size_val:
            parts.append(f"<arg5>{html.escape(size_val)}</arg5>\n")
        if by_whse and whse_no is not None:
            parts.append(f"<arg6>{html.escape(str(whse_no))}</arg6>\n")
        parts.append(f"</web:{method}>\n")
        return start + "".join(parts) + end

    def get_standard_inventory(
        self,
        style: str,
        color: Optional[str] = None,
        size: Optional[str] = None,
        by_whse: bool = False,
        whse_no: Optional[str] = None,
    ) -> Dict[str, List[Dict]]:
        url = self.endpoints["standard_inventory_wsdl"].split("?")[0]
        xml = self._build_standard_get_inventory_xml(style, color=color, size=size, by_whse=by_whse, whse_no=whse_no)
        # Save last request/response for debugging
        self.last_standard_request_xml = xml
        # Some SOAP servers expect SOAPAction header set to method name
        method = "getInventoryQtyForStyleColorSizeByWhse" if by_whse else "getInventoryQtyForStyleColorSize"
        # Some servers expect SOAPAction to be the fully-qualified action (namespace + method)
        action = f"http://webservice.integration.sanmar.com/{method}"
        headers = {"SOAPAction": f'"{action}"'}
        self.last_standard_url = url
        resp = self.session.post(url, headers=headers, data=xml.encode("utf-8"), timeout=self.timeout)
        resp.raise_for_status()
        self.last_standard_response_xml = resp.text
        return self._parse_standard_inventory_response(resp.text)

    def _parse_standard_inventory_response(self, xml_text: str) -> Dict[str, List[Dict]]:
        soup = BeautifulSoup(xml_text, "xml")
        rows: List[Dict] = []
        # Capture error/message if present to help diagnose empty responses
        error_flag = None
        message_text = None
        err_node = soup.find("errorOccurred")
        if err_node and err_node.text:
            error_flag = err_node.text.strip().lower() in {"true", "1", "yes"}
        msg_node = soup.find("message")
        if msg_node and msg_node.text:
            message_text = msg_node.text.strip()
            if error_flag:
                logger.warning("Standard SOAP response error: %s", message_text)
        # Style at root <style>
        style_node = soup.find("style")
        style = style_node.text.strip() if style_node and style_node.text else ""
        for sku in soup.find_all("sku"):
            color_node = sku.find("color")
            size_node = sku.find("size")
            color = color_node.text.strip() if color_node and color_node.text else ""
            size = size_node.text.strip() if size_node and size_node.text else ""
            for whse in sku.find_all("whse"):
                whse_id_node = whse.find("whseID")
                whse_name_node = whse.find("whseName")
                qty_node = whse.find("qty")
                whse_id = whse_id_node.text.strip() if whse_id_node and whse_id_node.text else ""
                whse_name = whse_name_node.text.strip() if whse_name_node and whse_name_node.text else ""
                qty_text = qty_node.text.strip() if qty_node and qty_node.text else ""
                qty = None
                try:
                    qty = int(qty_text)
                except Exception:
                    pass
                rows.append(
                    {
                        "style": style,
                        "partId": "",
                        "color": color,
                        "size": size,
                        "description": "",
                        "warehouseId": whse_id,
                        "warehouse": whse_name,
                        "qty": qty,
                        "totalAvailable": None,
                    }
                )
        # Fallback older response may be listResponse ints only; map order is warehouse IDs 31,12,7,6,5,4,3,2,1 per guide
        if not rows:
            ints = [int(node.text) for node in soup.find_all("listResponse") if node.text and node.text.isdigit()]
            if ints:
                ordering = [31, 12, 7, 6, 5, 4, 3, 2, 1]
                for whse_id, qty in zip(ordering, ints):
                    rows.append(
                        {
                            "style": style,
                            "partId": "",
                            "color": "",
                            "size": "",
                            "description": "",
                            "warehouseId": str(whse_id),
                            "warehouse": "",
                            "qty": qty,
                            "totalAvailable": None,
                        }
                    )
        # Include message to aid UI debugging/logging
        out: Dict[str, List[Dict] | str | bool] = {"rows": rows}
        if message_text:
            out["message"] = message_text
        # Expose error flag (True/False) so UI can surface server-reported errors
        if error_flag is not None:
            out["error"] = bool(error_flag)
        return out
