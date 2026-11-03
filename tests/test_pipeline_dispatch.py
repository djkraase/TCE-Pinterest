"""Exact public dispatch contract; no live API calls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dispatch_publisher', Path(__file__).parents[1] / 'scripts/approved_pin_publisher.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.manifest = b'{"pins":[]}'
        self.ledger_sha = 'a' * 40
        self.request = {'schema_version': 1, 'action': 'replenish',
            'run_id': 'tce-pinterest-20261103T131500Z-0123abcd',
            'manifest_blob_sha': hashlib.sha1(b'blob 11\0' + self.manifest).hexdigest(),
            'ledger_blob_sha': self.ledger_sha}
    def validate(self):
        return p.validate_dispatch(json.dumps(self.request).encode(), self.manifest, self.ledger_sha)
    def test_valid_contract(self):
        self.assertEqual(self.validate(), self.request['run_id'])
    def test_stale_manifest_or_ledger_block(self):
        for field in ['manifest_blob_sha', 'ledger_blob_sha']:
            original = self.request[field]
            self.request[field] = 'b' * 40
            with self.assertRaises(p.Blocked): self.validate()
            self.request[field] = original
    def test_malformed_extra_fields_and_bad_action_block(self):
        for key,value in [('schema_version', True), ('schema_version', 2), ('run_id', '../../run'), ('action', 'shareNow'), ('private_source', 'not-permitted')]:
            original = dict(self.request)
            self.request[key] = value
            with self.assertRaises(p.Blocked): self.validate()
            self.request = original
    def test_missing_and_oversized_block(self):
        del self.request['run_id']
        with self.assertRaises(p.Blocked): self.validate()
        with self.assertRaises(p.Blocked): p.validate_dispatch(b' ' * 2049, self.manifest, self.ledger_sha)
    def test_authenticated_main_push_only(self):
        class Ledger:
            def read(inner): return {}, self.ledger_sha
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pipeline').mkdir(); (root / 'campaign').mkdir()
            (root / 'pipeline/dispatch.json').write_text(json.dumps(self.request))
            (root / 'campaign/pins.json').write_bytes(self.manifest)
            env = {'PUBLISHER_ENABLED': 'true', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': p.REPO, 'GITHUB_ACTOR': 'djkraase'}
            with patch.object(p, 'ROOT', root), patch.dict(os.environ, env):
                p.validate_push_dispatch('replenish', Ledger())
                with self.assertRaises(p.Blocked): p.validate_push_dispatch('execute', Ledger())
                for key,value in [('GITHUB_REF','refs/heads/other'), ('GITHUB_ACTOR','unknown'), ('PUBLISHER_ENABLED','false'), ('GITHUB_REPOSITORY','other/repo')]:
                    with patch.dict(os.environ, {key:value}):
                        with self.assertRaises(p.Blocked): p.validate_push_dispatch('replenish', Ledger())

    def test_duplicate_json_fields_rejected(self):
        raw = json.dumps(self.request).replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
        with self.assertRaises(p.Blocked): p.validate_dispatch(raw.encode(), self.manifest, self.ledger_sha)
