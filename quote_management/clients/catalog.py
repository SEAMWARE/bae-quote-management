"""TMForum Product Catalog v4 API access."""
from .transport import TMForumTransport, segment

ENDPOINT = "/productCatalogManagement/v4/productOffering"


class CatalogClient:
    def __init__(self, transport: TMForumTransport, base_url: str):
        self.transport = transport
        self.base_url = base_url

    async def get_offering(self, identifier: str):
        return await self.transport.request("GET", self.base_url, ENDPOINT + "/" + segment(identifier))
