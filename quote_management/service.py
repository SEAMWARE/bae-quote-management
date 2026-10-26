"""Quote workflows coordinating independent external API clients."""
import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import HTTPException

from .config import Settings
from .clients.catalog import CatalogClient
from .clients.documents import DocumentClient
from .clients.notifications import NotificationClient
from .clients.parties import PartyClient
from .clients.quotes import QuoteClient

log = logging.getLogger(__name__)
MAX_FILE = 10 * 1024 * 1024
FOOTER = "THIS IS AN AUTOMATED NOTIFICATION. Please do not reply to this email!"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def party(identifier, role, name=None, href=None):
    return {"id": identifier, "href": href or identifier, "role": role,
            "name": name or identifier, "@referredType": "organization"}


def items(q):
    return deepcopy(q.get("quoteItem") or [{"@type": "QuoteItem", "action": "add", "quantity": 1, "state": "inProgress"}])


def document_id(q):
    return next((a["content"] for i in q.get("quoteItem") or [] for a in i.get("attachment") or []
                 if str(a.get("content", "")).startswith("urn:ngsi-ld:document-specification:")), None)


def party_id(q, role):
    return next((p.get("id") for p in q.get("relatedParty") or [] if p.get("role", "").lower() == role.lower()), None)


class QuoteService:
    def __init__(self, settings: Settings, *, quotes: QuoteClient, documents: DocumentClient,
                 parties: PartyClient, catalog: CatalogClient, notifications: NotificationClient):
        self.settings = settings
        self.quotes = quotes
        self.documents = documents
        self.parties = parties
        self.catalog = catalog
        self.notifications = notifications

    async def get(self, identifier):
        return await self.quotes.get(identifier)

    async def patch(self, identifier, payload):
        return await self.quotes.patch(identifier, payload)

    async def delete(self, identifier):
        await self.quotes.delete(identifier)

    async def list(self, user=None, role=None, category=None, external=None):
        if role is not None:
            role = role.strip().lower()
            role = "buyer" if role == "customer" else role
            if role not in ("buyer", "seller"):
                return []
        params = {}
        if category:
            params["category"] = category
        if user:
            params["relatedParty.id"] = user
        if external and external.strip():
            params["externalId"] = external
        result = await self.quotes.list(params)
        return [q for q in result
                if (not category or q.get("category", "").lower() == category)
                and (not external or not external.strip() or q.get("externalId") == external)
                and (not user or any(p.get("id") == user and (not role or p.get("role", "").lower() == role)
                                     for p in q.get("relatedParty") or []))]

    async def create(self, data, category):
        customer = data.customerIdRef
        provider = getattr(data, "providerIdRef", None)
        if provider == customer:
            raise HTTPException(409, "The user cannot request a quote for its own service!")
        org = await self.parties.get_organization(customer)
        name = next((r["name"].strip() for r in org.get("externalReference", []) if r.get("name", "").strip()), None)
        if not name:
            raise HTTPException(404, "externalReference.name not found in Organization for id: " + customer)
        organizations = await self.parties.find_organizations(self.settings.did)
        operator = next((o["id"] for o in organizations if o.get("id") and any(
            i.get("identificationId", "").strip() == self.settings.did for i in o.get("organizationIdentification", []))), None)
        if not operator:
            raise HTTPException(404, "Organization not found with identificationId: " + self.settings.did)
        buyer = {**party(customer, "Buyer", name), "@type": "RelatedParty"}
        parties = [buyer, party(operator, "BuyerOperator", self.settings.did)]
        offering_id = getattr(data, "productOfferingId", None)
        offering = {}
        if category != "coordinator":
            seller_operator = party(self.settings.did, "SellerOperator")
            if offering_id and offering_id.strip():
                try:
                    offering = await self.catalog.get_offering(offering_id)
                    resolved = next((p for p in offering.get("relatedParty", []) if p.get("role", "").lower() == "selleroperator"
                                     and all(p.get(k) for k in ("id", "href", "name"))), None)
                    if resolved:
                        seller_operator = party(resolved["id"], "SellerOperator", resolved["name"], resolved["href"])
                except HTTPException:
                    log.warning("Product offering lookup failed; using configured operator")
            parties = [party(provider, "Seller"), seller_operator] + parties
        item = {"@type": "QuoteItem", "action": "add", "state": "pending", "quantity": 1, "note": {}}
        if offering_id and offering_id.strip():
            item["productOffering"] = {"id": offering_id, "@type": "ProductOfferingRef"}
        payload = {"category": category, "relatedParty": parties, "quoteItem": [item]}
        if data.customerMessage and data.customerMessage.strip():
            payload["description"] = data.customerMessage
        if category != "coordinator":
            payload["externalId"] = data.externalId if category == "tender" else "0000"
        created = await self.quotes.create(payload)
        if provider:
            await self.notify(customer, provider, "New Quote Request",
                              f"You have received a new quotation request for the product {offering.get('name', 'N/A')} from {org.get('tradingName') or customer}.<br><br>Message: {data.customerMessage or ''}<br><br>" + FOOTER)
        return created

    async def status(self, identifier, status):
        current = await self.get(identifier)
        updated = items(current)
        for item in updated:
            item["state"] = status
        return await self.patch(identifier, {"quoteItem": updated})

    async def note(self, identifier, user, message):
        current = await self.get(identifier)
        result = await self.patch(identifier, {"note": (current.get("note") or []) + [
            {"@type": "Note", "author": user, "text": message, "date": now_iso()}]})
        buyer, seller = party_id(current, "Buyer"), party_id(current, "Seller")
        recipient = seller if user == buyer else buyer if user == seller else None
        if recipient:
            name = user
            try:
                org = await self.parties.get_organization(user)
                name = org.get("tradingName") or user
            except HTTPException:
                pass
            subject = "You Have a New Message"
            body = f"You received a new message related to quotation request from {name}.<br><br>Message: {message}<br><br>"
            if message.startswith("Attachment uploaded: "):
                subject = "New Document Uploaded"
                body = f"A new document {message.removeprefix('Attachment uploaded: ').strip()} has been uploaded to your quote by {name}.<br><br>"
            elif message.startswith("Status changed to: "):
                subject = "Quote Status Update"
                status = message.removeprefix("Status changed to: ").strip()
                details = {
                    "inprogress": "Your quotation request has been accepted by the provider and it's being evaluated. The provider will send a document proposal as soon as possible.",
                    "approved": "The provider has sent you a document proposal that you can evaluate and decide whether to accept or refuse.",
                    "accepted": "The customer has accepted your proposal. Please create a customized offering based on the agreed proposal.",
                    "canceled": "The quotation request has been canceled. No further action can be performed. You can still send a message in the chat.",
                    "cancelled": "The quotation request has been canceled. No further action can be performed. You can still send a message in the chat.",
                }
                if status.lower() in details:
                    body = f"Status changed to: {status}<br><br>Your quotation request has been updated by {name}.<br><br>{details[status.lower()]}<br><br>"
                else:
                    body = f"Quote status has been updated by {name}.<br>Status changed to: {status}<br><br>"
            await self.notify(user, recipient, subject, body + FOOTER)
        return result

    async def notify(self, sender, recipient, subject, message):
        await self.notifications.send(sender, recipient, subject, message)

    async def attachment(self, identifier, file, description):
        content = await file.read(MAX_FILE + 1)
        if len(content) > MAX_FILE:
            raise HTTPException(413, "File size exceeds maximum allowed size of 10MB")
        if not content:
            raise HTTPException(400, "File is empty")
        if file.content_type != "application/pdf":
            raise HTTPException(400, "Only PDF files are allowed. Received: " + str(file.content_type))
        current = await self.get(identifier)
        description = description if description.strip() else "PDF document: " + file.filename
        doc = await self.documents.create(file.filename, content, file.content_type, description)
        updated = items(current)
        for item in updated:
            item["attachment"] = [{"@type": "AttachmentRefOrValue", "name": file.filename,
                                   "description": description, "mimeType": file.content_type, "content": doc["id"]}]
        result = await self.patch(identifier, {"quoteItem": updated})
        if self.settings.verify_attachment:
            for attempt in range(self.settings.verify_attempts):
                persisted = await self.get(identifier)
                if all(any(a.get("content") == doc["id"] for a in i.get("attachment") or [])
                       for i in persisted.get("quoteItem") or []) and persisted.get("quoteItem"):
                    break
                if attempt == self.settings.verify_attempts - 1:
                    raise HTTPException(502, "Document reference persistence could not be confirmed")
                await asyncio.sleep(self.settings.verify_delay)
        previous = document_id(current)
        if previous and previous != doc["id"]:
            try:
                await self.documents.delete(previous)
            except HTTPException:
                log.exception("Could not remove replaced document")
        return result

    async def remove_attachment(self, identifier):
        current = await self.get(identifier)
        doc = document_id(current)
        if not doc:
            raise HTTPException(400, "Quote has no attachment to remove")
        updated = items(current)
        for item in updated:
            item["attachment"] = []
        result = await self.patch(identifier, {"quoteItem": updated})
        await self.documents.delete(doc)
        return result
