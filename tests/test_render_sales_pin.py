import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.render_sales_pin import SpecError, read_specs, render


class SalesCreativeTests(unittest.TestCase):
    def test_proposed_spec_produces_valid_vertical_assets(self):
        base = Path(__file__).resolve().parents[1]
        items = read_specs(base / "campaign" / "creative_specs.json")
        self.assertEqual(3, len(items))
        with tempfile.TemporaryDirectory() as folder:
            for item in items:
                output = render(item, Path(folder))
                with Image.open(output) as image:
                    self.assertEqual((1000, 1500), image.size)
                    self.assertEqual("PNG", image.format)
                self.assertLess(output.stat().st_size, 5_000_000)

    def test_rejects_unsafe_asset_and_implicit_approval(self):
        payload = {"schema_version": 1, "items": [{"id": "pin-09-test", "asset": "../other.png",
            "eyebrow": "Test", "headline": "A short title", "support": "A short support line",
            "button": "Learn more", "editorial_approved": False}]}
        with tempfile.TemporaryDirectory() as folder:
            spec = Path(folder) / "spec.json"
            spec.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SpecError):
                read_specs(spec)
            payload["items"][0]["asset"] = "tce_pin_09_publish.png"
            payload["items"][0]["editorial_approved"] = True
            spec.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SpecError):
                read_specs(spec)


if __name__ == "__main__":
    unittest.main()
