import asyncio
import base64
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from quote_management.api import create_app
from quote_management.config import Settings
from quote_management.scheduler import reconcile
from quote_management.wiring import create_quote_service


class TMForum:
    def __init__(self):
        self.quotes = {}
        self.calls = []
        self.documents = {}

    def __call__(self, req):
        import json
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, dict(req.url.params), body))
        path = req.url.path
        if '/organization/' in path:
            return httpx.Response(200, json={'externalReference': [{'name': 'Buyer Ltd'}], 'tradingName': 'Buyer Ltd'})
        if path.endswith('/organization'):
            return httpx.Response(200, json=[{'id': 'operator', 'organizationIdentification': [{'identificationId': 'did:test'}]}])
        if '/productOffering/' in path:
            return httpx.Response(200, json={'name': 'Cloud', 'relatedParty': [{'id': 'remote-op', 'href': 'remote-op', 'name': 'Remote', 'role': 'SellerOperator'}]})
        if path.endswith('/notify'):
            return httpx.Response(204)
        if path.endswith('/documentSpecification'):
            self.documents['urn:ngsi-ld:document-specification:new'] = body
            return httpx.Response(201, json={'id': 'urn:ngsi-ld:document-specification:new'})
        if '/documentSpecification/' in path:
            self.documents.pop(path.rsplit('/', 1)[1], None)
            return httpx.Response(204)
        if path.endswith('/quote'):
            if req.method == 'POST':
                body = {**body, 'id': 'q' + str(len(self.quotes))}
                self.quotes[body['id']] = body
                return httpx.Response(201, json=body)
            values = list(self.quotes.values())
            offset, limit = int(req.url.params['offset']), int(req.url.params['limit'])
            # Deliberately ignore category and party filters to exercise local validation.
            return httpx.Response(200, json=values[offset:offset + limit])
        identifier = path.rsplit('/', 1)[1]
        if identifier not in self.quotes:
            return httpx.Response(404, json={'message': 'not found'})
        if req.method == 'PATCH':
            self.quotes[identifier].update(body)
        if req.method == 'DELETE':
            del self.quotes[identifier]
            return httpx.Response(204)
        return httpx.Response(200, json=self.quotes[identifier])


@pytest.fixture
def backend():
    upstream = TMForum()
    settings = Settings(base_url='http://tmf/tmf-api', did='did:test', scheduler_enabled=False,
                        page_size=2, verify_delay=0, notification_url='http://notifications')
    with TestClient(create_app(settings, httpx.MockTransport(upstream))) as client:
        yield client, upstream, settings


def create(client, kind=''):
    body = {'customerIdRef': 'buyer', 'providerIdRef': 'seller', 'customerMessage': 'Request'}
    if kind == 'tendering/':
        body['externalId'] = 'process'
    response = client.post('/quoteManagement/' + kind + 'createQuote', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_and_party_resolution(backend):
    client, upstream, _ = backend
    response = client.post('/quoteManagement/createQuote', json={
        'customerIdRef': 'buyer', 'providerIdRef': 'seller', 'productOfferingId': 'cloud'})
    assert response.status_code == 201
    q = response.json()
    assert q['externalId'] == '0000'
    assert q['category'] == 'tailored'
    assert [(p['role'], p['id']) for p in q['relatedParty']] == [
        ('Seller', 'seller'), ('SellerOperator', 'remote-op'), ('Buyer', 'buyer'), ('BuyerOperator', 'operator')]
    assert q['quoteItem'][0]['productOffering'] == {'id': 'cloud', '@type': 'ProductOfferingRef'}
    assert any(c[1].endswith('/notify') for c in upstream.calls)


def test_validation_and_conflict(backend):
    client, upstream, _ = backend
    assert client.post('/quoteManagement/createQuote', json={}).status_code == 400
    assert client.post('/quoteManagement/tendering/createQuote', json={
        'customerIdRef': 'b', 'providerIdRef': 's', 'externalId': ' '}).status_code == 400
    assert client.post('/quoteManagement/createQuote', json={
        'customerIdRef': 'same', 'providerIdRef': 'same'}).status_code == 409
    assert not upstream.calls


def test_pagination_filters_and_coordinator(backend):
    client, upstream, _ = backend
    create(client)
    create(client, 'tendering/')
    response = client.post('/quoteManagement/tendering/createCoordinatorQuote', json={
        'customerMessage': 'Tender', 'customerIdRef': 'buyer'})
    assert response.status_code == 201
    assert 'externalId' not in response.json()
    assert len(client.get('/quoteManagement/listAllQuotes').json()) == 3
    for role in ('Buyer', 'Customer', 'buyer'):
        rows = client.get('/quoteManagement/quoteByUser/buyer', params={'role': role}).json()
        assert len(rows) == 1 and rows[0]['category'] == 'tailored'
    assert len(client.get('/quoteManagement/tendering/quotes/seller', params={'role': 'Seller', 'externalId': 'process'}).json()) == 1
    assert len(client.get('/quoteManagement/tendering/coordinatorQuotes/buyer').json()) == 1
    assert client.get('/quoteManagement/quoteByUser/buyer', params={'role': 'unknown'}).json() == []


def test_mutations_preserve_nested_fields(backend):
    client, upstream, _ = backend
    q = create(client)
    identifier = q['id']
    upstream.quotes[identifier]['quoteItem'][0]['product'] = {'id': 'product', 'productCharacteristic': [{'value': 'keep'}]}
    upstream.quotes[identifier]['note'] = [{'id': 'old', 'text': 'History'}]
    response = client.patch('/quoteManagement/updateQuoteStatus/' + identifier, params={'statusValue': 'approved'})
    assert response.json()['quoteItem'][0]['product']['id'] == 'product'
    response = client.patch('/quoteManagement/addNoteToQuote/' + identifier, params={'userId': 'buyer', 'messageContent': 'Hello'})
    assert [n['text'] for n in response.json()['note']] == ['History', 'Hello']
    for kind, field in [('requested', 'requestedQuoteCompletionDate'), ('expected', 'expectedQuoteCompletionDate'),
                        ('effective', 'effectiveQuoteCompletionDate'), ('expectedFulfillment', 'expectedFulfillmentStartDate')]:
        r = client.patch('/quoteManagement/updateQuoteDate/' + identifier, params={'date': '31-12-2026', 'dateType': kind})
        assert r.json()[field] == '2026-12-31T00:00:00Z'
    assert client.patch('/quoteManagement/updateQuoteDate/' + identifier, params={'date': '31-02-2026', 'dateType': 'requested'}).status_code == 400
    assert client.delete('/quoteManagement/quote/' + identifier).status_code == 204
    assert client.get('/quoteManagement/quoteById/' + identifier).status_code == 404


def test_attachment_lifecycle(backend):
    client, upstream, _ = backend
    identifier = create(client)['id']
    old = 'urn:ngsi-ld:document-specification:old'
    upstream.quotes[identifier]['quoteItem'][0]['attachment'] = [{'content': old}]
    r = client.patch('/quoteManagement/addAttachmentToQuote/' + identifier,
                     files={'file': ('offer.pdf', b'%PDF-test', 'application/pdf')}, data={'description': 'Offer'})
    assert r.status_code == 200, r.text
    document = upstream.documents['urn:ngsi-ld:document-specification:new']
    assert base64.b64decode(document['attachment'][0]['content']) == b'%PDF-test'
    assert r.json()['quoteItem'][0]['attachment'][0]['content'] == 'urn:ngsi-ld:document-specification:new'
    assert any(c[0] == 'DELETE' and c[1].endswith(old) for c in upstream.calls)
    assert client.delete('/quoteManagement/removeAttachmentFromQuote/' + identifier).json()['quoteItem'][0]['attachment'] == []
    assert client.delete('/quoteManagement/removeAttachmentFromQuote/' + identifier).status_code == 400
    assert client.patch('/quoteManagement/addAttachmentToQuote/' + identifier,
                        files={'file': ('bad.txt', b'bad', 'text/plain')}).status_code == 400


def test_upstream_failure_is_not_empty_success():
    def fail(req):
        return httpx.Response(503, text='Unavailable')
    with TestClient(create_app(Settings(scheduler_enabled=False), httpx.MockTransport(fail))) as client:
        response = client.get('/quoteManagement/listAllQuotes')
        assert response.status_code == 503
        assert response.json()['status'] == 503
        assert response.json()['path'] == '/quoteManagement/listAllQuotes'


def test_scheduler(backend):
    _, upstream, settings = backend
    past = '2020-01-01T00:00:00Z'
    def q(identifier, category, state, **extra):
        return {'id': identifier, 'category': category, 'quoteItem': [{'id': 'item', 'state': state, 'product': {'id': 'keep'}}], **extra}
    upstream.quotes = {
        'expired': q('expired', 'tailored', 'inProgress', requestedQuoteCompletionDate=past),
        'coordinator': q('coordinator', 'coordinator', 'inProgress', expectedFulfillmentStartDate=past, effectiveQuoteCompletionDate=past),
        'pending': q('pending', 'tender', 'pending', externalId='coordinator'),
        'safe': q('safe', 'tender', 'approved', externalId='coordinator'),
    }
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            service = create_quote_service(settings, client)
            await reconcile(service, datetime(2026, 1, 1, tzinfo=timezone.utc))
            assert upstream.quotes['coordinator']['quoteItem'][0]['state'] == 'approved'
            await reconcile(service, datetime(2026, 1, 1, tzinfo=timezone.utc))
    asyncio.run(run())
    assert upstream.quotes['expired']['quoteItem'][0]['state'] == 'cancelled'
    assert upstream.quotes['coordinator']['quoteItem'][0]['state'] == 'accepted'
    assert upstream.quotes['pending']['quoteItem'][0]['state'] == 'cancelled'
    assert upstream.quotes['safe']['quoteItem'][0]['state'] == 'approved'
    assert upstream.quotes['expired']['quoteItem'][0]['product'] == {'id': 'keep'}


def test_attachment_timeout_keeps_old_document(backend):
    client, upstream, settings = backend
    identifier = create(client)['id']
    old = 'urn:ngsi-ld:document-specification:old'
    upstream.quotes[identifier]['quoteItem'][0]['attachment'] = [{'content': old}]
    settings.verify_attempts = 2
    original = upstream.__call__
    def stale(req):
        response = original(req)
        if req.method == 'GET' and req.url.path.endswith('/' + identifier):
            payload = response.json()
            payload['quoteItem'][0]['attachment'] = [{'content': old}]
            return httpx.Response(200, json=payload)
        return response
    with TestClient(create_app(settings, httpx.MockTransport(stale))) as other:
        r = other.patch('/quoteManagement/addAttachmentToQuote/' + identifier,
                        files={'file': ('offer.pdf', b'%PDF-test', 'application/pdf')})
    assert r.status_code == 502
    assert not any(c[0] == 'DELETE' for c in upstream.calls)


def test_openapi_and_correlation(backend):
    client, _, _ = backend
    schema = client.get('/api-docs').json()
    assert len([p for p in schema['paths'] if p.startswith('/quoteManagement')]) == 14
    response = client.get('/actuator/health', headers={'X-Correlation-ID': 'trace-123'})
    assert response.json() == {'status': 'UP'}
    assert response.headers['X-Correlation-ID'] == 'trace-123'
    assert client.get('/quoteManagement/quoteByUser/buyer').status_code == 400


def test_repeated_pagination_fails_instead_of_looping():
    def stuck(req):
        return httpx.Response(200, json=[{'id': 'same'}])
    with TestClient(create_app(Settings(scheduler_enabled=False, page_size=1), httpx.MockTransport(stuck))) as client:
        assert client.get('/quoteManagement/listAllQuotes').status_code == 502


def test_note_email_and_no_direct_status_notifications(backend):
    client, upstream, _ = backend
    identifier = create(client)['id']
    upstream.calls.clear()
    client.patch('/quoteManagement/updateQuoteStatus/' + identifier, params={'statusValue': 'inProgress'})
    assert not any(c[1].endswith('/notify') for c in upstream.calls)
    client.patch('/quoteManagement/addNoteToQuote/' + identifier,
                 params={'userId': 'seller', 'messageContent': 'Status changed to: approved'})
    notification = next(c[3] for c in upstream.calls if c[1].endswith('/notify'))
    assert notification['recipient'] == 'buyer'
    assert notification['subject'] == 'Quote Status Update'
    assert 'The provider has sent you a document proposal' in notification['message']
