"""Shared template context: a small cart summary for the sidebar on every page."""

from .models import Cart

ANONYMOUS = {}


def cart_summary(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return ANONYMOUS
    key = request.session.session_key
    if not key:
        return ANONYMOUS
    cart = Cart.objects.filter(session_key=key).first()
    if cart is None:
        return ANONYMOUS
    return {
        "nav_cart": cart,
        "nav_cart_count": cart.item_count,
        "nav_cart_total": cart.subtotal,
        "nav_cart_tax": cart.tax_amount,
        "nav_cart_grand_total": cart.total,
    }
