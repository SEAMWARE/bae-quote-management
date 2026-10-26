"""Quote workflows tested with isolated, mocked API dependencies."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from quote_management.clients.catalog import CatalogClient
from quote_management.clients.documents import DocumentClient
from quote_management.clients.notifications import NotificationClient
from quote_management.clients.parties import PartyClient
from quote_management.clients.quotes import QuoteClient
from quote_management.config import Settings
from quote_management.service import MAX_FILE, QuoteService


OLD_DOCUMENT = "urn:ngsi-ld:document-specification:old"
NEW_DOCUMENT = "urn:ngsi-ld:document-specification:new"


class QuoteServiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.quotes = Mock(spec=QuoteClient)
        self.documents = Mock(spec=DocumentClient)
        self.parties = Mock(spec=PartyClient)
        self.catalog = Mock(spec=CatalogClient)
        self.notifications = Mock(spec=NotificationClient)
        self.settings = Settings(did="did:test", verify_attempts=2, verify_delay=0)
        self.service = QuoteService(self.settings, quotes=self.quotes, documents=self.documents,
                                    parties=self.parties, catalog=self.catalog, notifications=self.notifications)
        self.current = {
            "id": "q1",
            "quoteItem": [{"id": "item1", "state": "pending", "product": {"id": "p1"},
                           "attachment": [{"content": OLD_DOCUMENT}]}],
            "relatedParty": [{"id": "buyer", "role": "Buyer"}, {"id": "seller", "role": "Seller"}],
            "note": [{"id": "n1", "text": "Existing note"}],
        }
        self.quotes.get.return_value = self.current
        self.quotes.patch.return_value = {"id": "q1", "updated": True}
        self.quotes.create.return_value = {"id": "created"}
        self.documents.create.return_value = {"id": NEW_DOCUMENT}
        self.parties.get_organization.return_value = {
            "externalReference": [{"name": "Buyer Ltd"}], "tradingName": "Trading name"}
        self.parties.find_organizations.return_value = [
            {"id": "operator", "organizationIdentification": [{"identificationId": "did:test"}]}]
        self.catalog.get_offering.return_value = {"name": "Cloud", "relatedParty": []}
        self.request = SimpleNamespace(customerIdRef="buyer", providerIdRef="seller", customerMessage="Request",
                                       productOfferingId=None, externalId="process1")
        self.file = SimpleNamespace(filename="offer.pdf", content_type="application/pdf",
                                    read=AsyncMock(return_value=b"%PDF-test"))

    async def test_self_quote_rejected_before_any_external_call(self):
        self.request.providerIdRef = "buyer"
        with self.assertRaises(HTTPException) as error:
            await self.service.create(self.request, "tailored")
        self.assertEqual(error.exception.status_code, 409)
        self.parties.get_organization.assert_not_awaited()
        self.quotes.create.assert_not_awaited()
        self.notifications.send.assert_not_awaited()

    async def test_missing_buyer_name_prevents_creation(self):
        self.parties.get_organization.return_value = {"externalReference": [{"name": " "}]}
        with self.assertRaises(HTTPException) as error:
            await self.service.create(self.request, "tailored")
        self.assertEqual(error.exception.status_code, 404)
        self.quotes.create.assert_not_awaited()

    async def test_operator_must_match_identification(self):
        self.parties.find_organizations.return_value = [
            {"id": "wrong", "organizationIdentification": [{"identificationId": "other"}]}]
        with self.assertRaises(HTTPException) as error:
            await self.service.create(self.request, "tailored")
        self.assertEqual(error.exception.status_code, 404)
        self.quotes.create.assert_not_awaited()

    async def test_catalog_failure_falls_back_to_configured_operator(self):
        self.request.productOfferingId = "offering"
        self.catalog.get_offering.side_effect = HTTPException(503, "Unavailable")
        result = await self.service.create(self.request, "tailored")
        payload = self.quotes.create.await_args.args[0]
        operator = next(p for p in payload["relatedParty"] if p["role"] == "SellerOperator")
        self.assertEqual(operator["id"], "did:test")
        self.assertEqual(payload["quoteItem"][0]["productOffering"]["id"], "offering")
        self.assertEqual(result, {"id": "created"})

    async def test_incomplete_catalog_operator_does_not_override_fallback(self):
        self.request.productOfferingId = "offering"
        self.catalog.get_offering.return_value = {"relatedParty": [{"role": "SellerOperator", "id": "partial"}]}
        await self.service.create(self.request, "tailored")
        payload = self.quotes.create.await_args.args[0]
        self.assertEqual(next(p["id"] for p in payload["relatedParty"] if p["role"] == "SellerOperator"), "did:test")

    async def test_coordinator_does_not_create_seller_or_send_notification(self):
        request = SimpleNamespace(customerIdRef="buyer", customerMessage="Tender")
        await self.service.create(request, "coordinator")
        payload = self.quotes.create.await_args.args[0]
        self.assertEqual([p["role"] for p in payload["relatedParty"]], ["Buyer", "BuyerOperator"])
        self.assertNotIn("externalId", payload)
        self.catalog.get_offering.assert_not_awaited()
        self.notifications.send.assert_not_awaited()

    async def test_failed_creation_does_not_notify(self):
        self.quotes.create.side_effect = HTTPException(503, "Unavailable")
        with self.assertRaises(HTTPException):
            await self.service.create(self.request, "tender")
        self.notifications.send.assert_not_awaited()

    async def test_role_filter_is_checked_locally(self):
        self.quotes.list.return_value = [
            {"id": "yes", "category": "tender", "externalId": "process1", "relatedParty": [{"id": "buyer", "role": "Buyer"}]},
            {"id": "wrong-role", "category": "tender", "externalId": "process1", "relatedParty": [{"id": "buyer", "role": "Seller"}]},
            {"id": "wrong-process", "category": "tender", "externalId": "other", "relatedParty": [{"id": "buyer", "role": "Buyer"}]},
            {"id": "wrong-category", "category": "tailored", "externalId": "process1", "relatedParty": [{"id": "buyer", "role": "Buyer"}]},
        ]
        result = await self.service.list("buyer", " Customer ", "tender", "process1")
        self.assertEqual([q["id"] for q in result], ["yes"])
        self.quotes.list.assert_awaited_once_with({"category": "tender", "relatedParty.id": "buyer", "externalId": "process1"})

    async def test_invalid_role_does_not_query_upstream(self):
        self.assertEqual(await self.service.list("buyer", "unknown"), [])
        self.quotes.list.assert_not_awaited()

    async def test_status_preserves_all_items_without_mutating_input(self):
        self.current["quoteItem"].append({"id": "item2", "state": "approved", "custom": {"keep": True}})
        original = deepcopy(self.current)
        await self.service.status("q1", "accepted")
        payload = self.quotes.patch.await_args.args[1]
        self.assertEqual([i["state"] for i in payload["quoteItem"]], ["accepted", "accepted"])
        self.assertEqual(payload["quoteItem"][1]["custom"], {"keep": True})
        self.assertEqual(self.current, original)

    async def test_note_retains_history_and_uses_fixed_clock(self):
        original = deepcopy(self.current)
        with patch("quote_management.service.now_iso", return_value="2026-01-01T12:00:00Z"):
            await self.service.note("q1", "buyer", "Hello")
        notes = self.quotes.patch.await_args.args[1]["note"]
        self.assertEqual(notes[0], original["note"][0])
        self.assertEqual(notes[1], {"@type": "Note", "author": "buyer", "text": "Hello", "date": "2026-01-01T12:00:00Z"})
        self.assertEqual(self.current, original)
        self.assertEqual(self.notifications.send.await_args.args[:2], ("buyer", "seller"))

    async def test_note_from_unrelated_user_does_not_notify(self):
        await self.service.note("q1", "outsider", "Hello")
        self.quotes.patch.assert_awaited_once()
        self.parties.get_organization.assert_not_awaited()
        self.notifications.send.assert_not_awaited()

    async def test_notification_name_lookup_failure_uses_sender_id(self):
        self.parties.get_organization.side_effect = HTTPException(404, "Not found")
        await self.service.note("q1", "seller", "Hello")
        args = self.notifications.send.await_args.args
        self.assertEqual(args[:2], ("seller", "buyer"))
        self.assertIn("from seller", args[3])

    async def test_upload_validation_precedes_external_calls(self):
        for content, mime_type, status in [(b"", "application/pdf", 400), (b"x", "text/plain", 400),
                                            (b"x" * (MAX_FILE + 1), "application/pdf", 413)]:
            with self.subTest(status=status, mime_type=mime_type):
                self.file.read.return_value = content
                self.file.content_type = mime_type
                with self.assertRaises(HTTPException) as error:
                    await self.service.attachment("q1", self.file, "")
                self.assertEqual(error.exception.status_code, status)
        self.quotes.get.assert_not_awaited()
        self.documents.create.assert_not_awaited()

    async def test_replacement_deletes_old_document_only_after_verification(self):
        persisted = deepcopy(self.current)
        persisted["quoteItem"][0]["attachment"] = [{"content": NEW_DOCUMENT}]
        events = []
        async def get(identifier):
            events.append("get")
            return self.current if len(events) == 1 else persisted
        async def upload(*args):
            events.append("upload")
            return {"id": NEW_DOCUMENT}
        async def update(*args):
            events.append("patch")
            return {"id": "updated"}
        async def delete(identifier):
            events.append("delete")
        self.quotes.get.side_effect = get
        self.documents.create.side_effect = upload
        self.quotes.patch.side_effect = update
        self.documents.delete.side_effect = delete
        result = await self.service.attachment("q1", self.file, " ")
        self.assertEqual(events, ["get", "upload", "patch", "get", "delete"])
        self.documents.create.assert_awaited_once_with("offer.pdf", b"%PDF-test", "application/pdf", "PDF document: offer.pdf")
        self.documents.delete.assert_awaited_once_with(OLD_DOCUMENT)
        self.assertEqual(result, {"id": "updated"})

    async def test_verification_retries_and_preserves_old_document_on_timeout(self):
        with patch("quote_management.service.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(HTTPException) as error:
                await self.service.attachment("q1", self.file, "Offer")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(self.quotes.get.await_count, 3)
        sleep.assert_awaited_once_with(0)
        self.documents.delete.assert_not_awaited()

    async def test_failed_attachment_patch_does_not_delete_documents(self):
        self.quotes.patch.side_effect = HTTPException(503, "Unavailable")
        with self.assertRaises(HTTPException):
            await self.service.attachment("q1", self.file, "Offer")
        self.documents.delete.assert_not_awaited()
        self.assertEqual(self.quotes.get.await_count, 1)

    async def test_replaced_document_cleanup_failure_keeps_successful_result(self):
        self.settings.verify_attachment = False
        self.documents.delete.side_effect = HTTPException(503, "Unavailable")
        result = await self.service.attachment("q1", self.file, "Offer")
        self.assertEqual(result, self.quotes.patch.return_value)

    async def test_remove_without_attachment_does_not_patch(self):
        self.current["quoteItem"][0]["attachment"] = []
        with self.assertRaises(HTTPException) as error:
            await self.service.remove_attachment("q1")
        self.assertEqual(error.exception.status_code, 400)
        self.quotes.patch.assert_not_awaited()
        self.documents.delete.assert_not_awaited()

    async def test_failed_removal_patch_does_not_delete_document(self):
        self.quotes.patch.side_effect = HTTPException(503, "Unavailable")
        with self.assertRaises(HTTPException):
            await self.service.remove_attachment("q1")
        self.documents.delete.assert_not_awaited()
