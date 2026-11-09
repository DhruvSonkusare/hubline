"""Import every model here so Base.metadata is complete (Alembic and tests rely on it)."""

from app.models.connector import ConnectorAccount, SyncState
from app.models.message import Message, Thread

__all__ = ["ConnectorAccount", "Message", "SyncState", "Thread"]
