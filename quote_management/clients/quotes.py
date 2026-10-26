"""TMForum Quote v4 API access."""
from fastapi import HTTPException

from .transport import TMForumTransport, segment

ENDPOINT = "/quote/v4/quote"


class QuoteClient:
    def __init__(self, transport: TMForumTransport, base_url: str, page_size: int):
        self.transport = transport
        self.base_url = base_url
        self.page_size = page_size

    async def get(self, identifier: str):
        return await self.transport.request("GET", self.base_url, ENDPOINT + "/" + segment(identifier))

    async def create(self, payload: dict):
        return await self.transport.request("POST", self.base_url, ENDPOINT, json=payload)

    async def patch(self, identifier: str, payload: dict):
        return await self.transport.request("PATCH", self.base_url, ENDPOINT + "/" + segment(identifier), json=payload)

    async def delete(self, identifier: str):
        await self.transport.request("DELETE", self.base_url, ENDPOINT + "/" + segment(identifier))

    async def list(self, filters: dict):
        result, seen = [], set()
        offset = 0
        while True:
            page = await self.transport.request("GET", self.base_url, ENDPOINT,
                                                params={**filters, "limit": self.page_size, "offset": offset})
            if not isinstance(page, list):
                raise HTTPException(502, "TMForum quote list must be an array")
            if not page:
                break
            added = 0
            for q in page:
                key = q.get("id")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                result.append(q)
                added += 1
            if not added:
                raise HTTPException(502, "TMForum API did not advance pagination")
            offset += len(page)
            if len(page) < self.page_size:
                break
        return result
