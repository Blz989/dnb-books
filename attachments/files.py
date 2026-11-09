"""What can be attached: the type is read from the file's first bytes, never from its name or label."""
from .models import Kind

MAX_BYTES = 10 * 1024 * 1024
MAX_PER_DOCUMENT = 10
EXT = {Kind.PDF: "pdf", Kind.JPEG: "jpg", Kind.PNG: "png"}
CONTENT_TYPE = {Kind.PDF: "application/pdf", Kind.JPEG: "image/jpeg", Kind.PNG: "image/png"}

_SIGNATURES = (
    (b"%PDF", Kind.PDF),
    (b"\xff\xd8\xff", Kind.JPEG),
    (b"\x89PNG\r\n\x1a\n", Kind.PNG),
)


def sniff(head: bytes) -> Kind | None:
    for signature, kind in _SIGNATURES:
        if head.startswith(signature):
            return kind
    return None


def human_size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / (1024 * 1024):.1f} MB"
