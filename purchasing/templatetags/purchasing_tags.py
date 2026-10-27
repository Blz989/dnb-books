from django import template

from ..posting.orders import drop_ship_candidates

register = template.Library()


@register.inclusion_tag("purchasing/_drop_ship_button.html")
def drop_ship_po_button(order):
    """Sales order screen: shown when some drop-ship lines aren't on a purchase order yet."""
    return {"order": order, "show": bool(drop_ship_candidates(order))}


@register.inclusion_tag("purchasing/_drop_ship_pos.html")
def drop_ship_po_list(order):
    return {"order": order, "purchase_orders": list(order.purchase_orders.select_related("vendor"))}
