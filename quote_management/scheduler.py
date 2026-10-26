import asyncio
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from .service import items, now_iso, party_id

log = logging.getLogger(__name__)


def passed(value, now):
    if not value:
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return now > parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


async def reconcile(service, now=None):
    now = now or datetime.now(timezone.utc)
    quotes = await service.list()
    overdue = {q["id"] for q in quotes if q.get("category") == "coordinator"
               and passed(q.get("expectedFulfillmentStartDate"), now)}
    for q in quotes:
        try:
            states = {i.get("state") for i in q.get("quoteItem") or []}
            category = q.get("category", "").lower()
            status, message, only = None, None, None
            if category == "tailored" and "inProgress" in states and passed(q.get("requestedQuoteCompletionDate"), now):
                status, message = "cancelled", "Quote automatically cancelled due to expiration of requested completion date."
            elif category == "coordinator":
                if "inProgress" in states and passed(q.get("expectedFulfillmentStartDate"), now):
                    status, message = "approved", "Tender automatically approved - expected fulfillment start date has been reached."
                elif "approved" in states and passed(q.get("effectiveQuoteCompletionDate"), now):
                    status, message = "accepted", "Tender automatically accepted - effective completion date has been reached."
            elif category == "tender" and q.get("externalId") in overdue and "pending" in states:
                status, message, only = "cancelled", "Tender automatically cancelled - coordinator expected fulfillment start date has been reached.", "pending"
            if status:
                current = await service.get(q["id"])
                # Recheck state after fetching to avoid applying an obsolete list snapshot.
                if {i.get("state") for i in current.get("quoteItem") or []} != states:
                    continue
                updated = items(current)
                for item in updated:
                    if not only or item.get("state") == only:
                        item["state"] = status
                await service.patch(q["id"], {"quoteItem": updated, "note": (current.get("note") or []) + [
                    {"@type": "Note", "author": "SYSTEM", "text": message, "date": now_iso()}]})
                await service.notify(party_id(q, "Seller"), party_id(q, "Buyer"),
                                     "Quote Expired" if category == "tailored" else "Tender Status Update", message)
        except (HTTPException, ValueError, KeyError):
            log.exception("Scheduled transition failed for quote %s", q.get("id"))


async def run_scheduler(service):
    while True:
        now = datetime.now(timezone.utc)
        await asyncio.sleep(3600 - now.minute * 60 - now.second - now.microsecond / 1e6)
        try:
            await reconcile(service)
        except Exception:
            log.exception("Scheduled quote reconciliation failed")
