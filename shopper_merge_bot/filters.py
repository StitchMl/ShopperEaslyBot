from __future__ import annotations

from .dedupe import DedupeStore


def category_matches_filter(category: str, selected: str) -> bool:
    if category == selected:
        return True
    return "/" not in selected and category.startswith(f"{selected}/")


def passes_filters(store: DedupeStore, category: str, price: object | None) -> bool:
    categories = tuple(item for item in store.get_filter_categories() if item != "altro")
    if categories and not any(category_matches_filter(category, item) for item in categories):
        return False

    min_price = store.get_filter_price("filter_min_price")
    max_price = store.get_filter_price("filter_max_price")
    if min_price is None and max_price is None:
        return True
    if price is None:
        return False
    if min_price is not None and price < min_price:
        return False
    if max_price is not None and price > max_price:
        return False
    return True
