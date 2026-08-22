from .base import CountedStatus


class VidaraStatus(CountedStatus):
    """Status line for a Vidara folder task -- counted in videos, not bytes."""

    tool = "vidara"
