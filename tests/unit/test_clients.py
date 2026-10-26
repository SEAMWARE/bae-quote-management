"""API clients tested independently of the application and other clients."""
import base64
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from quote_management.clients.documents import DocumentClient
from quote_management.clients.notifications import NotificationClient
from quote_management.clients.parties import PartyClient
from quote_management.clients.quotes import QuoteClient
from quote_management.clients.transport import TMForumTransport


class TransportTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=httpx.AsyncClient)
        self.transport = TMForumTransport(self.client, token="secret")

    async def test_authentication_and_json_response(self):
        self.client.request.return_value = httpx.Response(200, json={"id": "q1"}, request=httpx.Request("GET", "http://tmf/quote"))
        result = await self.transport.request("GET", "http://tmf", "/quote", headers={"Accept": "application/json"})
        self.assertEqual(result, {"id": "q1"})
        self.client.request.assert_awaited_once_with("GET", "http://tmf/quote", headers={"Accept": "application/json", "Authorization": "Bearer secret"})

    async def test_no_token_does_not_add_authorization(self):
        self.transport.token = ""
        self.client.request.return_value = httpx.Response(204, request=httpx.Request("DELETE", "http://tmf/quote"))
        self.assertIsNone(await self.transport.request("DELETE", "http://tmf", "/quote"))
        self.assertEqual(self.client.request.await_args.kwargs["headers"], {})

    async def test_http_error_preserves_status(self):
        self.client.request.return_value = httpx.Response(409, text="Conflict", request=httpx.Request("POST", "http://tmf/quote"))
        with self.assertRaises(HTTPException) as error:
            await self.transport.request("POST", "http://tmf", "/quote")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("Conflict", error.exception.detail)

    async def test_timeout_becomes_gateway_error(self):
        self.client.request.side_effect = httpx.ReadTimeout("timeout")
        with self.assertRaises(HTTPException) as error:
            await self.transport.request("GET", "http://tmf", "/quote")
        self.assertEqual(error.exception.status_code, 502)

    async def test_invalid_json_becomes_gateway_error(self):
        self.client.request.return_value = httpx.Response(200, text="<html>bad</html>", request=httpx.Request("GET", "http://tmf/quote"))
        with self.assertRaises(HTTPException) as error:
            await self.transport.request("GET", "http://tmf", "/quote")
        self.assertEqual(error.exception.status_code, 502)
        self.assertIn("invalid JSON", error.exception.detail)


class QuoteClientTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = Mock(spec=TMForumTransport)
        self.client = QuoteClient(self.transport, "http://quotes", page_size=2)

    async def test_pagination_deduplicates_and_advances_by_received_count(self):
        self.transport.request.side_effect = [
            [{"id": "q1"}, {"id": "q2"}], [{"id": "q2"}, {"id": "q3"}], []]
        filters = {"category": "tender"}
        result = await self.client.list(filters)
        self.assertEqual([q["id"] for q in result], ["q1", "q2", "q3"])
        self.assertEqual([c.kwargs["params"]["offset"] for c in self.transport.request.await_args_list], [0, 2, 4])
        self.assertEqual(filters, {"category": "tender"})
        self.assertTrue(all(c.kwargs["params"]["category"] == "tender" for c in self.transport.request.await_args_list))

    async def test_partial_page_stops_pagination(self):
        self.transport.request.return_value = [{"id": "q1"}]
        self.assertEqual(await self.client.list({}), [{"id": "q1"}])
        self.transport.request.assert_awaited_once()

    async def test_repeated_page_is_rejected(self):
        self.transport.request.return_value = [{"id": "q1"}, {"id": "q2"}]
        with self.assertRaises(HTTPException) as error:
            await self.client.list({})
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(self.transport.request.await_count, 2)

    async def test_non_array_response_is_rejected(self):
        self.transport.request.return_value = {"id": "q1"}
        with self.assertRaises(HTTPException) as error:
            await self.client.list({})
        self.assertEqual(error.exception.status_code, 502)

    async def test_identifier_cannot_add_path_or_query_components(self):
        await self.client.get("id/other?limit=100#fragment")
        self.transport.request.assert_awaited_once_with(
            "GET", "http://quotes", "/quote/v4/quote/id%2Fother%3Flimit%3D100%23fragment")


class DocumentClientTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = Mock(spec=TMForumTransport)
        self.client = DocumentClient(self.transport, "http://documents")

    async def test_binary_document_is_encoded_in_document_api_payload(self):
        self.transport.request.return_value = {"id": "document1"}
        content = b"%PDF-\x00\xff"
        result = await self.client.create("offer.pdf", content, "application/pdf", "Offer")
        args = self.transport.request.await_args
        self.assertEqual(args.args, ("POST", "http://documents", "/document/v4/documentSpecification"))
        payload = args.kwargs["json"]
        self.assertEqual(payload["lifecycleStatus"], "active")
        self.assertEqual(base64.b64decode(payload["attachment"][0]["content"]), content)
        self.assertEqual(result, {"id": "document1"})

    async def test_missing_document_id_is_rejected(self):
        for response in (None, {}, {"id": ""}):
            with self.subTest(response=response):
                self.transport.request.return_value = response
                with self.assertRaises(HTTPException) as error:
                    await self.client.create("offer.pdf", b"pdf", "application/pdf", "Offer")
                self.assertEqual(error.exception.status_code, 502)

    async def test_deletion_uses_document_host_and_escapes_identifier(self):
        await self.client.delete("id/with/slashes")
        self.transport.request.assert_awaited_once_with(
            "DELETE", "http://documents", "/document/v4/documentSpecification/id%2Fwith%2Fslashes")


class PartyClientTests(IsolatedAsyncioTestCase):
    async def test_supported_organization_response_shapes(self):
        transport = Mock(spec=TMForumTransport)
        client = PartyClient(transport, "http://parties")
        organization = {"id": "org1"}
        for response in ([organization], {"organization": [organization]}, organization):
            with self.subTest(response=response):
                transport.request.return_value = response
                self.assertEqual(await client.find_organizations("did:test"), [organization])
        self.assertEqual(transport.request.await_args.kwargs["params"],
                         {"organizationIdentification.identificationId": "did:test"})


class NotificationClientTests(IsolatedAsyncioTestCase):
    async def test_disabled_or_missing_recipient_skips_delivery(self):
        http = Mock(spec=httpx.AsyncClient)
        for base, sender, recipient in [("", "s", "r"), ("http://notify", None, "r"), ("http://notify", "s", None)]:
            await NotificationClient(http, base).send(sender, recipient, "Subject", "Message")
        http.post.assert_not_awaited()

    async def test_delivery_failure_is_logged_and_does_not_fail_workflow(self):
        http = Mock(spec=httpx.AsyncClient)
        http.post.side_effect = httpx.ConnectError("unavailable")
        with self.assertLogs("quote_management.clients.notifications", level="ERROR"):
            await NotificationClient(http, "http://notify").send("s", "r", "Subject", "Message")
        http.post.assert_awaited_once_with("http://notify/charging/api/orderManagement/notify",
                                          json={"sender": "s", "recipient": "r", "subject": "Subject", "message": "Message"})
