"""Document numbers in URLs. Imported numbers can hold a slash ("GF-Net Credit 6/2025"), which a plain <str:...>
route can't carry or reverse, so the slash travels as a token and is restored when the route is matched."""
from django.urls import register_converter

SLASH = "~s~"


def encode(number: str) -> str:
    return str(number).replace("/", SLASH)


class DocNumberConverter:
    regex = "[^/]+"

    def to_python(self, value: str) -> str:
        return value.replace(SLASH, "/")

    def to_url(self, value) -> str:
        return encode(value)


register_converter(DocNumberConverter, "docnum")
