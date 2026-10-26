"""Deadline decisions tested with a fixed clock and mocked quote service."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock

from fastapi import HTTPException

from quote_management.scheduler import passed, reconcile
from quote_management.service import QuoteService

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PAST = "2025-12-31T23:59:59Z"
FUTURE = "2026-01-01T00:00:01Z"


def make_quote(identifier="q1", category="tailored", state="inProgress", **fields):
    return {"id": identifier, "category": category,
            "quoteItem": [{"id": "item1", "state": state, "product": {"id": "keep"}}],
            "relatedParty": [{"id": "buyer", "role": "Buyer"}, {"id": "seller", "role": "Seller"}],
            **fields}


class DeadlineTests(TestCase):
    def test_deadline_comparison_handles_offsets_naive_dates_and_boundary(self):
        for value, expected in [(None, False), (PAST, True), (FUTURE, False),
                                ("2026-01-01T00:00:00Z", False),
                                ("2026-01-01T00:59:59+01:00", True),
                                ("2025-12-31T23:59:59", True)]:
            with self.subTest(value=value):
                self.assertEqual(passed(value, NOW), expected)


class SchedulerTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = Mock(spec=QuoteService)

    async def test_due_transitions_preserve_data_and_add_system_note(self):
        scenarios = [
            (make_quote(requestedQuoteCompletionDate=PAST), "cancelled"),
            (make_quote(category="coordinator", expectedFulfillmentStartDate=PAST), "approved"),
            (make_quote(category="coordinator", state="approved", effectiveQuoteCompletionDate=PAST), "accepted"),
        ]
        for q, expected in scenarios:
            with self.subTest(category=q["category"], expected=expected):
                self.service.reset_mock()
                self.service.list.return_value = [q]
                self.service.get.return_value = q
                original = deepcopy(q)
                await reconcile(self.service, NOW)
                payload = self.service.patch.await_args.args[1]
                self.assertEqual(payload["quoteItem"][0]["state"], expected)
                self.assertEqual(payload["quoteItem"][0]["product"], {"id": "keep"})
                self.assertEqual(payload["note"][-1]["author"], "SYSTEM")
                self.assertEqual(q, original)
                self.service.notify.assert_awaited_once()

    async def test_future_deadlines_and_terminal_states_are_untouched(self):
        self.service.list.return_value = [
            make_quote(requestedQuoteCompletionDate=FUTURE),
            make_quote(state="accepted", requestedQuoteCompletionDate=PAST),
            make_quote(category="coordinator", expectedFulfillmentStartDate=FUTURE),
            make_quote(category="tender", state="pending", externalId="missing-coordinator"),
        ]
        await reconcile(self.service, NOW)
        self.service.get.assert_not_awaited()
        self.service.patch.assert_not_awaited()

    async def test_tender_cancellation_only_updates_pending_items(self):
        coordinator = make_quote("coordinator", "coordinator", "accepted", expectedFulfillmentStartDate=PAST)
        tender = make_quote("tender", "tender", "pending", externalId="coordinator")
        tender["quoteItem"].append({"id": "accepted-item", "state": "accepted"})
        self.service.list.return_value = [coordinator, tender]
        self.service.get.return_value = tender
        await reconcile(self.service, NOW)
        self.service.patch.assert_awaited_once()
        payload = self.service.patch.await_args.args[1]
        self.assertEqual([i["state"] for i in payload["quoteItem"]], ["cancelled", "accepted"])

    async def test_state_changed_since_listing_skips_transition(self):
        listed = make_quote(requestedQuoteCompletionDate=PAST)
        fresh = deepcopy(listed)
        fresh["quoteItem"][0]["state"] = "accepted"
        self.service.list.return_value = [listed]
        self.service.get.return_value = fresh
        await reconcile(self.service, NOW)
        self.service.patch.assert_not_awaited()
        self.service.notify.assert_not_awaited()

    async def test_failure_for_one_quote_does_not_prevent_next_transition(self):
        first = make_quote("first", requestedQuoteCompletionDate=PAST)
        second = make_quote("second", requestedQuoteCompletionDate=PAST)
        self.service.list.return_value = [first, second]
        self.service.get.side_effect = [HTTPException(503, "Unavailable"), second]
        with self.assertLogs("quote_management.scheduler", level="ERROR"):
            await reconcile(self.service, NOW)
        self.assertEqual(self.service.patch.await_args.args[0], "second")
        self.service.patch.assert_awaited_once()

    async def test_failed_patch_does_not_send_notification(self):
        q = make_quote(requestedQuoteCompletionDate=PAST)
        self.service.list.return_value = [q]
        self.service.get.return_value = q
        self.service.patch.side_effect = HTTPException(503, "Unavailable")
        with self.assertLogs("quote_management.scheduler", level="ERROR"):
            await reconcile(self.service, NOW)
        self.service.notify.assert_not_awaited()
