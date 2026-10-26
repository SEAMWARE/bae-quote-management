"""Charging backend notification API access."""
import logging

import httpx

log = logging.getLogger(__name__)


class NotificationClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base_url = base_url

    async def send(self, sender, recipient, subject, message):
        if not self.base_url or not sender or not recipient:
            return
        try:
            response = await self.client.post(self.base_url + "/charging/api/orderManagement/notify",
                                              json=dict(sender=sender, recipient=recipient, subject=subject, message=message))
            response.raise_for_status()
        except httpx.HTTPError:
            log.exception("Notification delivery failed")
