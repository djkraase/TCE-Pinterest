"""Review regressions: no network or credentials."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('review_pub', Path(__file__).resolve().parents[1] / 'scripts/approved_pin_publisher.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class ReviewTests(unittest.TestCase):
    def test_unapproved_essay_reviews_remain_ineligible(self):
        # Concept approval or an accidental status change is not final approval.
        for status in ('review', 'approved'):
            for visual_qa in (False, True):
                with self.subTest(status=status, visual_qa=visual_qa):
                    pin = {'id': 'essay-review-fixture', 'status': status,
                           'editorial_approved': False, 'visual_qa_approved': visual_qa,
                           'visual_concept_approved': True}
                    with self.assertRaisesRegex(p.Blocked, 'individual editorial and visual approval required'):
                        p.candidate(pin)

    def test_matches_must_require_image(self):
        payload = {'text': 'approved copy', 'assets': [{'image': {'url': p.IMAGE_BASE + 'tce_pin_02_publish.jpg', 'metadata': {'altText': 'approved alt'}}}], 'metadata': {'pinterest': {'title': 'approved title', 'url': p.DESTINATION}}}
        post = {'channelId': p.CHANNEL, 'text': payload['text'], 'metadata': {'title': 'approved title', 'url': p.DESTINATION, 'board': {'serviceId': p.BOARD}}, 'assets': []}
        self.assertFalse(p.verified_content(post, payload), 'A post with no approved image must not verify as matching')

    def test_first_image_cannot_be_relabelled(self):
        with tempfile.TemporaryDirectory() as d:
            body = (p.ROOT / 'assets/tce_pin_01_publish.jpg').read_bytes()
            Path(d, 'tce_pin_01_publish.jpg').write_bytes(body)
            pin = {'id': 'pin-renamed-first', 'status': 'approved', 'editorial_approved': True, 'visual_qa_approved': True, 'board_service_id': p.BOARD, 'destination_url': p.DESTINATION, 'title': 'title', 'text': 'copy', 'alt_text': 'alt', 'approval_ref': 'approval', 'source_id': 'source', 'search_intent': 'intent', 'asset': 'tce_pin_01_publish.jpg', 'asset_sha256': hashlib.sha256(body).hexdigest()}
            with self.assertRaises(p.Blocked):
                p.candidate(pin, Path(d))

if __name__ == '__main__':
    unittest.main()
