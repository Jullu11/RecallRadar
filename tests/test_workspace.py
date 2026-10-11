import os
import unittest
from unittest.mock import Mock, patch
os.environ['DEMO_MODE'] = 'true'
from fastapi.testclient import TestClient
from app.main import app
from app.auth import owner
from app import storage
from app.receipt_import import email_text

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        storage.reset_memory_store()
        app.dependency_overrides[owner] = lambda: 'alice'
        self.client = TestClient(app)
    def tearDown(self):
        app.dependency_overrides.clear()
    def test_purchase_claim_export_and_isolation(self):
        c = self.client
        p = c.post('/api/workspace/products', json={'product_name':'Kettle','merchant':'Shop','amount_paid':50}).json()
        a = c.post('/api/workspace/actions', json={'product_id':p['id'],'case_type':'rebate','due_date':'2027-01-01'})
        self.assertEqual(a.status_code, 200, a.text)
        case = a.json()
        claim = {'case_id':case['id'],'merchant':'Shop','issue':'Rebate request','status':'won','recovered_amount':10}
        self.assertEqual(c.put('/api/workspace/claims',json=claim).status_code,200)
        self.assertIn('Rebate request', c.get('/api/workspace/claims/'+case['id']+'/packet').text)
        self.assertEqual(len(c.get('/api/workspace/export').json()['products']),1)
        app.dependency_overrides[owner] = lambda: 'bob'
        self.assertEqual(c.get('/api/workspace').json()['products'],[])
        self.assertEqual(c.post('/api/workspace/products/'+p['id']+'/archive').status_code,404)
        self.assertEqual(c.get('/api/workspace/claims/'+case['id']+'/packet').status_code,404)
        app.dependency_overrides[owner] = lambda: 'alice'
        self.assertEqual(c.delete('/api/workspace').status_code,200)
        self.assertEqual(c.get('/api/workspace').json()['products'],[])
    def test_document_retained_if_extraction_fails(self):
        raw = b'From: shop@example.com\nSubject: Receipt\n\nKettle $50'
        with patch('app.workspace.extract_text',side_effect=RuntimeError('unavailable')):
            r = self.client.post('/api/workspace/documents', files={'file':('receipt.eml',raw,'message/rfc822')})
        self.assertEqual(r.status_code,200,r.text)
        docs = self.client.get('/api/workspace').json()['documents']
        url = '/api/workspace/documents/'+docs[0]['fingerprint']
        self.assertEqual(self.client.get(url).content,raw)
        app.dependency_overrides[owner] = lambda: 'bob'
        self.assertEqual(self.client.get(url).status_code,404)
    def test_demo_seed_idempotent(self):
        self.assertEqual(self.client.post('/api/workspace/demo-seed').status_code,200)
        self.assertEqual(self.client.post('/api/workspace/demo-seed').status_code,200)
        self.assertEqual(len(self.client.get('/api/workspace').json()['products']),1)
    def test_production_insert_is_executed(self):
        client = Mock()
        client.table.return_value.insert.return_value.execute.return_value.data = [{'id':'saved'}]
        with patch('app.storage.uses_memory_store',return_value=False), patch('app.storage.get_supabase',return_value=client):
            result = storage.save_confirmed_product(owner_handle='alice',brand=None,product_name='Kettle',identifiers={},source_type='manual')
        self.assertEqual(result['id'],'saved')
        client.table.return_value.insert.assert_called_once()
    def test_notification_lease_and_ack(self):
        storage.enqueue_notification(owner_handle='alice',dedupe_key='one',message='Due')
        first=storage.list_pending_notifications()
        self.assertEqual(storage.list_pending_notifications(),[])
        self.assertFalse(storage.acknowledge_notification(notification_id=first[0]['id'],claim_token='wrong'))
        self.assertTrue(storage.acknowledge_notification(notification_id=first[0]['id'],claim_token=first[0]['claim_token']))
    def test_email_html_ignores_script(self):
        parsed=email_text(b'From: shop@example.com\nSubject: Receipt\nContent-Type: text/html\n\n<p>Kettle</p><script>secret</script>')
        self.assertIn('Kettle',parsed)
        self.assertNotIn('secret',parsed)
    def test_production_requires_auth_and_hides_demo(self):
        from types import SimpleNamespace
        from app.auth import authenticated_user_id
        from fastapi import HTTPException
        settings=SimpleNamespace(demo_mode=False,supabase_url='https://example.supabase.co',supabase_service_role_key='test')
        with patch('app.auth.get_settings',return_value=settings):
            with self.assertRaises(HTTPException) as caught:
                authenticated_user_id(None)
            self.assertEqual(caught.exception.status_code,401)
        with patch('app.main.get_settings',return_value=settings):
            self.assertEqual(self.client.post('/api/demo/chat',json={'text':'HELP'}).status_code,404)
        with patch('app.storage.get_settings',return_value=settings):
            self.assertFalse(storage.uses_memory_store())
