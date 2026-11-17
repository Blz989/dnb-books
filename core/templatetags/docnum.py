from django import template

from core.urlnumbers import encode

register = template.Library()
register.filter("docnum", encode)
