"""Sanitized synthetic public-page fixtures; no tokens or captured page props."""
import copy
import html
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('offer_publisher', Path(__file__).parents[1] / 'scripts/approved_pin_publisher.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class OfferTests(unittest.TestCase):
    def setUp(self):
        self.product = {'permalink': 'hynllx', 'long_url': p.DESTINATION, 'is_published': True,
            'is_compliance_blocked': False, 'currency_code': 'usd', 'price_cents': 3900,
            'pwyw': {}, 'options': [], 'waitlist': None, 'preorder': None,
            'is_sales_limited': False, 'quantity_remaining': None}
        self.schema = {'@type': 'Product', 'url': p.DESTINATION, 'sku': 'hynllx',
            'offers': {'@type': 'Offer', 'url': p.DESTINATION, 'availability': 'https://schema.org/InStock',
                       'priceCurrency': 'USD', 'price': 39.0}}
    def raw(self):
        page = {'component': 'Products/Show', 'props': {'product': self.product}}
        return ('<div data-page="' + html.escape(json.dumps(page), quote=True) + '"></div>'
                '<script type="application/ld+json">' + json.dumps(self.schema) + '</script>').encode()
    def test_available_product(self):
        self.assertTrue(p.verify_offer(self.raw())['offer_available'])
        self.assertFalse(p.verify_offer(self.raw())['checkout_tested'])
    def test_unpublished_compliance_price_url_and_waitlist_block(self):
        original = copy.deepcopy(self.product)
        for key, value in [('is_published', False), ('is_compliance_blocked', True), ('price_cents', 4900),
                           ('long_url', 'https://example.com'), ('waitlist', {}), ('preorder', {}),
                           ('pwyw', None), ('options', [{}]), ('currency_code', 'eur')]:
            with self.subTest(key=key):
                self.product = dict(original, **{key: value})
                with self.assertRaises(p.Blocked): p.verify_offer(self.raw())
    def test_missing_field_fails_closed(self):
        del self.product['is_published']
        with self.assertRaises(p.Blocked): p.verify_offer(self.raw())
    def test_stock_limit(self):
        self.product.update(is_sales_limited=True, quantity_remaining=1)
        p.verify_offer(self.raw())
        self.product['quantity_remaining'] = 0
        with self.assertRaises(p.Blocked): p.verify_offer(self.raw())
    def test_schema_out_of_stock_block(self):
        self.schema['offers']['availability'] = 'https://schema.org/OutOfStock'
        with self.assertRaises(p.Blocked): p.verify_offer(self.raw())
    def test_html_200_without_product_not_accepted(self):
        with self.assertRaises(p.Blocked): p.verify_offer(b'<html>Not for sale</html>')
    def test_duplicate_product_structure_blocks(self):
        with self.assertRaises(p.Blocked): p.verify_offer(self.raw() * 2)
    def test_error_does_not_expose_raw_input(self):
        with self.assertRaises(p.Blocked) as error: p.verify_offer(b'SENSITIVE_TEST_SENTINEL')
        self.assertNotIn('SENSITIVE_TEST_SENTINEL', str(error.exception))
