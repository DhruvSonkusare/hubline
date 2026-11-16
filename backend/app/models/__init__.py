"""Import every model here so Base.metadata is complete (Alembic and tests rely on it)."""

from app.models.connector import ConnectorAccount, ConnectorCredential, OAuthState, SyncState
from app.models.message import AgentJob, Draft, Message, Thread
from app.models.reply import OutboundReply

__all__ = [
    "AgentJob",
    "ConnectorAccount",
    "ConnectorCredential",
    "Draft",
    "Message",
    "OAuthState",
    "OutboundReply",
    "SyncState",
    "Thread",
]
