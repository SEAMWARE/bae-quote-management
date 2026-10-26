"""TMForum Party v4 organization API access."""
from .transport import TMForumTransport, segment

ENDPOINT = "/party/v4/organization"


class PartyClient:
    def __init__(self, transport: TMForumTransport, base_url: str):
        self.transport = transport
        self.base_url = base_url

    async def get_organization(self, identifier: str):
        return await self.transport.request("GET", self.base_url, ENDPOINT + "/" + segment(identifier))

    async def find_organizations(self, identification_id: str):
        result = await self.transport.request("GET", self.base_url, ENDPOINT,
                                              params={"organizationIdentification.identificationId": identification_id})
        if isinstance(result, dict):
            return result.get("organization", [result])
        return result
