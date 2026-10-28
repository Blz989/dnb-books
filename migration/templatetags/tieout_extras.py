from django import template

register = template.Library()


@register.filter
def passed_count(checks):
    return sum(1 for c in checks if c.passed)


@register.filter
def differing(diffs):
    return [d for d in diffs if not d.matches]


@register.filter
def dict_items(d):
    return sorted(d.items())
