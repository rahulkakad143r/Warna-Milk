"""Template helpers for the bakery storefront."""

from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# Glyph + tile tint per category, so every product card has artwork offline.
CATEGORY_ART = {
    "bread": ("\U0001F35E", "wheat"),
    "cake": ("\U0001F382", "cocoa"),
    "cakes": ("\U0001F382", "cocoa"),
    "pastries": ("\U0001F950", "caramel"),
    "cookies": ("\U0001F36A", "biscuit"),
    "dairy": ("\U0001F95B", "milk"),
    "beverages": ("\U00002615", "coffee"),
    "sweets": ("\U0001F36E", "caramel"),
    "snacks": ("\U0001F35C", "wheat"),
}


@register.filter
def category_slug(value):
    return str(value or "").strip().lower()


@register.filter
def product_glyph(product):
    entry = CATEGORY_ART.get(category_slug(getattr(product, "category", None)))
    if entry is None:
        cat = getattr(product, "category", None)
        entry = CATEGORY_ART.get(category_slug(cat), ("\U0001F36E", "caramel"))
    return mark_safe(entry[0])


@register.filter
def product_tint(product):
    cat = getattr(product, "category", None)
    entry = CATEGORY_ART.get(category_slug(cat))
    if entry is None:
        entry = CATEGORY_ART.get(category_slug(cat), ("\U0001F36E", "caramel"))
    return entry[1]


@register.filter
def stock_percent(source, cap=200):
    """Stock level as a 0-100 percentage for the meter bar.

    Accepts an Inventory instance or a bare quantity number.
    """
    if source is None:
        return 0
    if hasattr(source, "reorder_level"):
        quantity, reorder = source.quantity, source.reorder_level
    else:
        try:
            quantity = int(source)
        except (TypeError, ValueError):
            return 0
        reorder = 0
    ceiling = max(cap, reorder * 3, 1)
    return min(100, int(quantity / ceiling * 100))


@register.filter
def multiply(value, factor):
    try:
        return float(value) * float(factor)
    except (TypeError, ValueError):
        return 0
