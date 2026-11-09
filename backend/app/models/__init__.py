"""Import every model here so Base.metadata is complete (Alembic and tests rely on it)."""

from app.models.connector import ConnectorAccount, SyncState
from app.models.message import AgentJob, Draft, Message, Thread

__all__ = ["AgentJob", "ConnectorAccount", "Draft", "Message", "SyncState", "Thread"]
