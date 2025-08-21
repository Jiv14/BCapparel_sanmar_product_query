from __future__ import annotations
import pandas as pd
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# Warehouse names come from response; fallback will be `Warehouse {id}` when name is missing.

def format_inventory_table(inventory_rows: List[Dict], pricing_data: Optional[Dict] = None) -> pd.DataFrame:
    """
    Transform inventory data into tabular format matching the uploaded image.
    
    Args:
        inventory_rows: List of inventory rows from InventoryClient
        pricing_data: Optional pricing information
        
    Returns:
        DataFrame formatted as warehouse x size matrix
    """
    if not inventory_rows:
        return pd.DataFrame()
    
    # Group data by style/product
    products = {}
    for row in inventory_rows:
        style = row.get('style', '')
        if style not in products:
            products[style] = {
                'sizes': set(),
                'warehouses': {},
                'case_size': None,
                'pricing': {}
            }
        
        size = row.get('size', '').upper()
        warehouse_id = row.get('warehouseId', '')
        warehouse_name = (row.get('warehouse') or '').strip() or f"Warehouse {warehouse_id}"
        qty = row.get('qty', 0) or 0
        
        if size:
            products[style]['sizes'].add(size)
        
        if warehouse_name not in products[style]['warehouses']:
            products[style]['warehouses'][warehouse_name] = {}
        
        products[style]['warehouses'][warehouse_name][size] = qty
    
    # For now, handle single product (first one found)
    if not products:
        return pd.DataFrame()
    
    style = list(products.keys())[0]
    product_data = products[style]
    
    # Get all sizes and sort them logically
    all_sizes = sorted(list(product_data['sizes']), key=_size_sort_key)
    
    if not all_sizes:
        return pd.DataFrame({'Message': ['No size data available']})
    
    # Create the table structure
    table_data = []
    
    # Pricing row
    pricing_row = {'': 'Price: $'}
    for size in all_sizes:
        pricing_row[size] = get_size_price(size, pricing_data)
    table_data.append(pricing_row)
    
    # Case Size row
    case_size_row = {'': 'Case Size'}
    case_size_value = product_data.get('case_size', 12)  # Default case size
    for size in all_sizes:
        case_size_row[size] = case_size_value
    table_data.append(case_size_row)
    
    # Warehouse header row
    warehouse_header = {'': 'Warehouse'}
    for size in all_sizes:
        warehouse_header[size] = size
    table_data.append(warehouse_header)
    
    # Warehouse inventory rows - derive from response (prefer known order when present)
    known_order = [
        "Dallas, TX",
        "Cincinnati, OH", 
        "Richmond, VA",
        "Jacksonville, FL",
        "Phoenix, AZ", 
        "Reno, NV",
        "Minneapolis, MN",
        "Robbinsville, NJ",
        "Seattle, WA"
    ]
    present = list(product_data['warehouses'].keys())
    warehouse_order = [w for w in known_order if w in present] + sorted([w for w in present if w not in known_order])
    
    total_inventory = {size: 0 for size in all_sizes}
    
    # Add warehouse rows for warehouses present in response
    for warehouse in warehouse_order:
        warehouse_row = {'': warehouse}
        has_inventory = False
        
        for size in all_sizes:
            qty = 0
            if warehouse in product_data['warehouses']:
                qty = product_data['warehouses'][warehouse].get(size, 0)
            warehouse_row[size] = qty
            total_inventory[size] += qty
            if qty > 0:
                has_inventory = True
        
        table_data.append(warehouse_row)
    
    # Total Inventory row
    total_row = {'': 'Total Inventory'}
    for size in all_sizes:
        total_row[size] = total_inventory[size]
    table_data.append(total_row)
    
    # Convert to DataFrame
    df = pd.DataFrame(table_data)
    
    # Set first column as index for better display
    df.set_index('', inplace=True)
    
    return df

def _size_sort_key(size: str) -> Tuple[int, str]:
    """Sort sizes logically (S, M, L, XL, 2XL, etc.)"""
    size = size.upper().strip()
    
    # Handle numeric prefixes (2XL, 3XL, etc.)
    if size[0].isdigit():
        num = int(size[0])
        base = size[1:]
        return (num + 10, base)  # Put numbered sizes after base sizes
    
    # Standard size ordering
    size_order = {
        'XS': 1, 'S': 2, 'M': 3, 'L': 4, 'XL': 5,
        'LT': 6, 'XLT': 7, '2XLT': 8, '3XLT': 9, '4XLT': 10
    }
    
    return (size_order.get(size, 99), size)

def get_size_price(size: str, pricing_data: Optional[Dict] = None) -> str:
    """Get price for a specific size"""
    if pricing_data and size in pricing_data:
        return f"{pricing_data[size]:.2f}"
    
    # Default pricing structure based on size
    base_prices = {
        'S': 37.26, 'M': 37.26, 'L': 37.26, 'XL': 37.26,
        'LT': 37.26, 'XLT': 37.26, '2XLT': 38.26, '3XLT': 40.26, '4XLT': 41.26
    }
    
    return f"{base_prices.get(size.upper(), 37.26):.2f}"

def create_inventory_display_table(inventory_rows: List[Dict], style_name: str = "") -> pd.DataFrame:
    """
    Create a styled inventory table for display in Streamlit
    """
    df = format_inventory_table(inventory_rows)
    
    if df.empty:
        return pd.DataFrame({'Message': ['No inventory data available']})
    
    return df
