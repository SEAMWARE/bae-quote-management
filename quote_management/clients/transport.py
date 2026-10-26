"""HTTP authentication and error handling shared by TMForum clients."""
from urllib.parse import quote

import httpx
from fastapi import HTTPException


def segment(value: str) -> str:
    return quote(value, safe="")


class TMForumTransport:
    def __init__(self, client: httpx.AsyncClient, token: str = ""):
        self.client = client
        self.token = token

    async def request(self, method: str, base_url: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            response = await self.client.request(method, base_url + path, headers=headers, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, "TMForum API Error: " + exc.response.text) from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, "TMForum API unavailable") from exc
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(502, "TMForum API returned invalid JSON") from exc
