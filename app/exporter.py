from __future__ import annotations
from typing import List, Dict, Optional
import pandas as pd


def rows_to_dataframe(rows: List[Dict]) -> pd.DataFrame:
    cols = [
        "style",
        "partId",
        "color",
        "size",
        "description",
        "warehouseId",
        "warehouse",
        "qty",
        "totalAvailable",
        "price",
    ]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols]


def save_rows(rows: List[Dict], path: str, fmt: Optional[str] = None) -> str:
    fmt = fmt or ("xlsx" if path.lower().endswith(".xlsx") else "csv")
    df = rows_to_dataframe(rows)
    if fmt == "xlsx":
        if not path.lower().endswith(".xlsx"):
            path = path + ".xlsx"
        df.to_excel(path, index=False)
    else:
        if not path.lower().endswith(".csv"):
            path = path + ".csv"
        df.to_csv(path, index=False)
    return path
