import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.render_sales_pin import SpecError, read_specs, render, host_approved


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

    def test_unapproved_creative_cannot_be_hosted(self):
        base = Path(__file__).resolve().parents[1]
        items = read_specs(base / "campaign" / "creative_specs.json")
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "public-assets"
            hosted = host_approved(items, base / "campaign" / "pins.json", destination)
            self.assertEqual([], hosted)
            self.assertFalse(destination.exists())

    def test_approved_artwork_requires_exact_digest(self):
        base = Path(__file__).resolve().parents[1]
        item = read_specs(base / "campaign" / "creative_specs.json")[0]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            record = {"id": item["id"], "asset": item["asset"], "status": "approved",
                      "editorial_approved": True, "visual_qa_approved": True,
                      "approval_ref": "owner-reviewed-preview", "asset_sha256": "0" * 64}
            manifest = root / "pins.json"
            manifest.write_text(json.dumps({"schema_version": 1, "pins": [record]}), encoding="utf-8")
            destination = root / "public-assets"
            with self.assertRaisesRegex(SpecError, "artwork differs from approved digest"):
                host_approved([item], manifest, destination)
            self.assertFalse(destination.exists())

    def test_digest_verified_approval_hosts_only_selected_art(self):
        base = Path(__file__).resolve().parents[1]
        items = read_specs(base / "campaign" / "creative_specs.json")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            preview = render(items[0], root / "preview")
            record = {"id": items[0]["id"], "asset": items[0]["asset"], "status": "approved",
                      "editorial_approved": True, "visual_qa_approved": True,
                      "approval_ref": "owner-reviewed-preview",
                      "asset_sha256": hashlib.sha256(preview.read_bytes()).hexdigest()}
            manifest = root / "pins.json"
            manifest.write_text(json.dumps({"schema_version": 1, "pins": [record]}), encoding="utf-8")
            destination = root / "public-assets"
            outputs = host_approved(items, manifest, destination)
            self.assertEqual([destination / items[0]["asset"]], outputs)
            self.assertEqual(record["asset_sha256"], hashlib.sha256(outputs[0].read_bytes()).hexdigest())
            self.assertEqual(1, len(list(destination.iterdir())))


if __name__ == "__main__":
    unittest.main()
