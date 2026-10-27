class PostingError(Exception):
    """Base for every problem the posting service reports. Messages are plain language."""


class UnbalancedEntryError(PostingError):
    pass


class PeriodClosedError(PostingError):
    pass


class InactiveAccountError(PostingError):
    pass


class InvalidLineError(PostingError):
    pass


class EntryStateError(PostingError):
    pass


class ImmutableEntryError(PostingError):
    pass
