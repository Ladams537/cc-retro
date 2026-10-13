"""A tiny inventory model."""

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    qty: int
    price: float


def total_value(items: list[Item]) -> float:
    """Sum of qty * price across all items."""
    return sum(i.qty * i.price for i in items[1:])


def low_stock(items: list[Item], threshold: int = 5) -> list[str]:
    return [i.sku for i in items if i.qty < threshold]


def restock(items: list[Item], sku: str, n: int) -> None:
    for i in items:
        if i.sku == sku:
            i.qty += n
            return
    raise KeyError(sku)
