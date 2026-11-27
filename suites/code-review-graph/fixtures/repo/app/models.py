"""Domain models."""
from dataclasses import dataclass


@dataclass
class Order:
    id: int
    customer_id: int
    status: str
    discount_percent: float
    total_cents: int


@dataclass
class Customer:
    id: int
    name: str
