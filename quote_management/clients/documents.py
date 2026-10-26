"""TMForum Document v4 API access and document payload encoding."""
import base64

from fastapi import HTTPException

from .transport import TMForumTransport, segment

ENDPOINT = "/document/v4/documentSpecification"


class DocumentClient:
    def __init__(self, transport: TMForumTransport, base_url: str):
        self.transport = transport
        self.base_url = base_url

    async def create(self, filename: str, content: bytes, mime_type: str, description: str):
        payload = {
            "name": filename, "description": description, "version": "1.0", "lifecycleStatus": "active",
            "attachment": [{"name": filename, "mimeType": mime_type,
                            "content": base64.b64encode(content).decode()}],
        }
        document = await self.transport.request("POST", self.base_url, ENDPOINT, json=payload)
        if not document or not document.get("id"):
            raise HTTPException(502, "Document API did not return a valid document id")
        return document

    async def delete(self, identifier: str):
        await self.transport.request("DELETE", self.base_url, ENDPOINT + "/" + segment(identifier))
