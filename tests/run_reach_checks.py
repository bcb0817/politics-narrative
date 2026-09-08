"""Offline regression runner: never read production .env or use live sockets."""
import os
import socket
import ipaddress
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]


def main():
    read=Path.read_text
    original_connect=socket.socket.connect
    def guarded_connect(sock,address):
        if isinstance(address,tuple) and ipaddress.ip_address(address[0]).is_loopback:
            return original_connect(sock,address)
        raise AssertionError('external network forbidden')
    def guarded_read(path,*args,**kwargs):
        if path.name=='.env': return ''
        return read(path,*args,**kwargs)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
        'STATE_DIR':tmp,'LOG_DIR':tmp,'POST_ENABLED':'false',
        'XAI_ENABLED':'false','REACH_POLICY_ENABLED':'false',
        'TOPICAL_EDITOR_ENABLED':'false',
        'POLITICS_FETCH_ARTICLE_BODY':'false','DISABLE_TIME_API':'true',
    },clear=True), patch.object(Path,'read_text',guarded_read), patch.object(socket.socket,'connect',guarded_connect):
        modules=sys.argv[1:] or ['tests.test_topical_editor','tests.test_reach_foundation','tests.test_reach_completion','tests.test_x_private_metrics',
            'tests.test_monthly_budget_61','tests.test_article_content',
            'tests.test_politics_post_integration','tests.test_crosspost',
            'tests.test_disaster_updates','tests.test_startup_xai_ledger_v3',
            'tests.test_short_video_factory']
        suite=unittest.defaultTestLoader.loadTestsFromNames(modules)
        result=unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__=='__main__': raise SystemExit(main())
