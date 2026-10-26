"""Construct application services and their external API dependencies."""
import httpx

from .clients.catalog import CatalogClient
from .clients.documents import DocumentClient
from .clients.notifications import NotificationClient
from .clients.parties import PartyClient
from .clients.quotes import QuoteClient
from .clients.transport import TMForumTransport
from .config import Settings
from .service import QuoteService


def create_quote_service(settings: Settings, client: httpx.AsyncClient) -> QuoteService:
    transport = TMForumTransport(client, settings.token)
    return QuoteService(
        settings=settings,
        quotes=QuoteClient(transport, settings.base_url, settings.page_size),
        documents=DocumentClient(transport, settings.document_url),
        parties=PartyClient(transport, settings.party_url),
        catalog=CatalogClient(transport, settings.catalog_url),
        notifications=NotificationClient(client, settings.notification_url),
    )
