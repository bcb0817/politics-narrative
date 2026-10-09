import io
import json
import unittest
from src.hunter_site_sync import sync, NoRedirect

class Response(io.BytesIO):
    pass

class SyncTests(unittest.TestCase):
    def test_readback_and_no_search(self):
        data={'run_id':'hunter-20261010-8','status':'ambiguous','actual_cost_usd':None,'topics':[]}
        class Opener:
            def open(self,req,timeout):
                return Response(json.dumps(dict(saved=True,**data)).encode())
        self.assertTrue(sync('fictional',data,Opener())['saved'])

    def test_readback_mismatch(self):
        class Opener:
            def open(self,req,timeout):
                return Response(b'{"saved":true,"run_id":"wrong"}')
        with self.assertRaises(ValueError):
            sync('fictional',{'run_id':'expected'},Opener())

    def test_no_credential_redirect(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.example'))
