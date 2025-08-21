import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # Auth (placeholders; actual values loaded in __post_init__)
    sanmar_username: str = ""
    sanmar_password: str = ""
    sanmar_customer_number: str = ""

    # Endpoints / flags
    use_test: bool = False
    backend: str = "promostandards"  # promostandards | standard

    # Networking
    timeout_seconds: int = 25

    # Output
    default_format: str = "xlsx"  # xlsx | csv

    def __post_init__(self):
        # Load from environment at instantiation time so UI updates via set_env_temp take effect
        self.sanmar_username = os.getenv("SANMAR_USERNAME", self.sanmar_username or "").strip()
        self.sanmar_password = os.getenv("SANMAR_PASSWORD", self.sanmar_password or "").strip()
        self.sanmar_customer_number = os.getenv("SANMAR_CUSTOMER_NUMBER", self.sanmar_customer_number or "").strip()
        self.use_test = os.getenv("SANMAR_USE_TEST", "false").lower() in {"1", "true", "yes"}
        self.backend = os.getenv("SANMAR_BACKEND", self.backend or "promostandards").lower().strip()
        try:
            self.timeout_seconds = int(os.getenv("HTTP_TIMEOUT_SECONDS", str(self.timeout_seconds)))
        except Exception:
            self.timeout_seconds = 25
        self.default_format = os.getenv("OUTPUT_FORMAT", self.default_format or "xlsx").lower().strip()


def get_endpoints(use_test: bool):
    endpoints = {
        "promostandards_inventory_wsdl": (
            "https://ws.sanmar.com:8080/promostandards/InventoryServiceBindingV2final?WSDL"
            if use_test
            else "https://ws.sanmar.com:8080/promostandards/InventoryServiceBindingV2final?WSDL"
        ),
        "standard_inventory_wsdl": (
            "https://ws.sanmar.com:8080/SanMarWebService/SanMarWebServicePort?wsdl"
            if use_test
            else "https://ws.sanmar.com:8080/SanMarWebService/SanMarWebServicePort?wsdl"
        ),
    }
    return endpoints
