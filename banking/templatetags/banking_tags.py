from django import template

from ..checknumbers import is_to_print

register = template.Library()


@register.filter
def to_print(doc) -> bool:
    """True for an open check payment or refund that has no number yet."""
    return is_to_print(doc)
