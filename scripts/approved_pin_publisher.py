#!/usr/bin/env python3
"""Fail-closed, one-Pin publisher. Default is offline plan; no implicit retries."""
import argparse
import base64
import copy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit, parse_qsl

ROOT = Path(__file__).resolve().parents[1]
ORG = '6ad44c983788a9f13eb0c4cf'
CHANNEL = '6ae8780be29c69a171371a76'
BOARD = '1083467691545125927'
DESTINATION = 'https://kraase.gumroad.com/l/kg-diagnostic'
IMAGE_BASE = 'https://djkraase.github.io/TCE-Pinterest/assets/'
FIRST_ID = 'pin-01-handoff'
FIRST_POST = '6ae9060f3b6f22c15ed12663'
FIRST_ASSET_SHA = '4c1d382d862d39d2659d89fbc0d860ecfc09d826cd5bc044cf4beec16371845f'
POST_STATUSES = {'draft', 'error', 'needs_approval', 'scheduled', 'sending', 'sent'}
REPO = 'djkraase/TCE-Pinterest'
STATE_BRANCH = 'pinterest-state'
STATE_PATH = 'publisher-ledger.json'
POST_FIELDS = '''id channelId status text dueAt sentAt externalLink
assets { ... on ImageAsset { source image { altText } } }
metadata { ... on PinterestPostMetadata { title url board { serviceId } } }'''

class Blocked(Exception):
    pass

class CapacityDeferred(Blocked):
    """Queue full before acquiring any durable claim."""
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked('redirect rejected')


def request(url, data=None, token=None, method=None, limit=6_000_000):
    # Fixed endpoints only; caller-supplied URLs never receive credentials.
    if token and not (url == 'https://api.buffer.com' or url.startswith(
            'https://api.github.com/repos/' + REPO + '/contents/' + STATE_PATH)):
        raise Blocked('credential endpoint rejected')
    headers = {'User-Agent': 'TCE-approved-pin-publisher', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise Blocked('response too large')
            if response.status != 200 and response.status != 201:
                raise Blocked('unexpected HTTP status')
            return body, response.headers.get('Content-Type', '')
    except Exception:
        # Do not log remote bodies, headers, exception strings, or request objects.
        raise Blocked('network/API failure; do not retry a create') from None


class Buffer:
    def __init__(self, key):
        if not key:
            raise Blocked('BUFFER_API_KEY missing')
        self.key = key

    def query(self, query, variables=None):
        raw, _ = request('https://api.buffer.com', {'query': query, 'variables': variables or {}}, self.key)
        result = json.loads(raw)
        if result.get('errors') or not isinstance(result.get('data'), dict):
            raise Blocked('Buffer GraphQL failure')
        return result['data']

    def verify(self):
        data = self.query('''query { account { organizations { id } }
        channel(input: {id: "%s"}) { id organizationId service isDisconnected isLocked
        isQueuePaused metadata { ... on PinterestMetadata { boards { serviceId } } } } }''' % CHANNEL)
        c = data['channel']
        if (ORG not in [o['id'] for o in data['account']['organizations']]
                or c['id'] != CHANNEL or c['organizationId'] != ORG or c['service'] != 'pinterest'
                or c['isDisconnected'] or c['isLocked'] or c['isQueuePaused']
                or BOARD not in [b['serviceId'] for b in c['metadata']['boards']]):
            raise Blocked('channel/account/board preflight failed')

    def posts(self):
        # All statuses, no date filter: drafts/errors also suppress duplicates.
        query = '''query($after: String) { posts(first: 100, after: $after,
        input: {organizationId: "%s", filter: {channelIds: ["%s"]}}) {
        pageInfo { hasNextPage endCursor } edges { node { %s } } } }''' % (ORG, CHANNEL, POST_FIELDS)
        found, cursors, cursor = {}, set(), None
        for _ in range(20):
            result = self.query(query, {'after': cursor})['posts']
            if not isinstance(result.get('edges'), list):
                raise Blocked('incomplete post listing')
            for edge in result['edges']:
                p = edge['node']
                if p['channelId'] != CHANNEL or p.get('status') not in POST_STATUSES:
                    raise Blocked('unexpected channel in listing')
                found[p['id']] = p
            page = result['pageInfo']
            if page['hasNextPage'] is False:
                return list(found.values())
            cursor = page['endCursor']
            if not cursor or cursor in cursors:
                raise Blocked('invalid pagination')
            cursors.add(cursor)
        raise Blocked('post history exceeds safe pagination bound')

    def post(self, post_id):
        return self.query('query($input: PostInput!) { post(input: $input) { ' + POST_FIELDS + ' } }',
                          {'input': {'id': post_id}})['post']

    def create(self, payload):
        result = self.query('''mutation($input: CreatePostInput!) { createPost(input: $input) {
        __typename ... on PostActionSuccess { post { id } }
        ... on MutationError { message } } }''', {'input': payload})['createPost']
        if result.get('__typename') != 'PostActionSuccess' or not result.get('post', {}).get('id'):
            raise Blocked('create unconfirmed; reconcile before any retry')
        return result['post']['id']


class GitHubLedger:
    """Durable compare-and-swap lock. Missing state NEVER initializes automatically."""
    def __init__(self, token):
        if not token:
            raise Blocked('GITHUB_TOKEN missing')
        self.token = token
        self.url = 'https://api.github.com/repos/' + REPO + '/contents/' + STATE_PATH

    def read(self):
        raw, _ = request(self.url + '?ref=' + STATE_BRANCH, token=self.token)
        result = json.loads(raw)
        state = json.loads(base64.b64decode(result['content'], validate=False))
        if state.get('schema_version') != 1 or not isinstance(state.get('records'), dict) or 'lock' not in state:
            raise Blocked('invalid durable ledger')
        return state, result['sha']

    def cas(self, state, sha):
        # Any conflict/timeout stops. Never retry using a newly read SHA.
        content = base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()
        raw, _ = request(self.url, {'message': 'Record publisher state', 'branch': STATE_BRANCH,
                                  'sha': sha, 'content': content}, self.token, 'PUT')
        return json.loads(raw)['content']['sha']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def image_dimensions(body):
    # Header checks complement visual approval and byte-exact public retrieval.
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 24 and body[12:16] == b'IHDR':
        return int.from_bytes(body[16:20], 'big'), int.from_bytes(body[20:24], 'big')
    if body.startswith(b"\xff\xd8"):
        i = 2
        while i + 4 <= len(body):
            if body[i] != 255:
                i += 1
                continue
            marker = body[i + 1]
            i += 2
            if marker in (0xD8, 0xD9):
                continue
            size = int.from_bytes(body[i:i + 2], 'big')
            if size < 2 or i + size > len(body):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and size >= 7:
                return int.from_bytes(body[i + 5:i + 7], 'big'), int.from_bytes(body[i + 3:i + 5], 'big')
            i += size
    raise Blocked('invalid image header')


def tracked_destination(pin_id):
    if not re.fullmatch(r'pin-[a-z0-9-]{1,70}', pin_id):
        raise Blocked('invalid tracking Pin ID')
    return DESTINATION + '?' + urlencode({'utm_source': 'pinterest', 'utm_medium': 'organic_social',
        'utm_campaign': 'kg_diagnostic', 'utm_content': pin_id})


def candidate(pin, assets=ROOT / 'assets'):
    if pin.get('id') == FIRST_ID or pin.get('buffer_post_id') or pin.get('status') in ('sent', 'scheduled'):
        raise Blocked('already processed')
    if pin.get('status') != 'approved' or pin.get('editorial_approved') is not True or pin.get('visual_qa_approved') is not True:
        raise Blocked('individual editorial and visual approval required')
    if pin.get('board_service_id') != BOARD or pin.get('destination_url') != DESTINATION:
        raise Blocked('board/destination outside MVP allowlist')
    if not re.fullmatch(r'pin-[a-z0-9-]{1,70}', pin.get('id', '')):
        raise Blocked('invalid Pin ID')
    for key, maxlen in [('title', 100), ('text', 500), ('alt_text', 500), ('approval_ref', 200), ('source_id', 100), ('search_intent', 200)]:
        if not isinstance(pin.get(key), str) or not 1 <= len(pin[key].strip()) <= maxlen:
            raise Blocked('approved record missing or invalid ' + key)
    name = pin.get('asset', '')
    if not re.fullmatch(r'tce_pin_[0-9]{2}_publish\.(jpg|png)', name):
        raise Blocked('invalid asset name')
    path = assets / name
    if path.is_symlink() or not path.is_file():
        raise Blocked('approved image missing')
    body = path.read_bytes()
    if not 1 <= len(body) <= 5_000_000 or hashlib.sha256(body).hexdigest() != pin.get('asset_sha256'):
        raise Blocked('approved image digest/size mismatch')
    w, h = image_dimensions(body)
    if w < 500 or h < 750 or h <= w:
        raise Blocked('invalid vertical image dimensions')
    if hashlib.sha256(body).hexdigest() == FIRST_ASSET_SHA:
        raise Blocked('first published creative cannot be republished')
    payload = {'text': pin['text'], 'channelId': CHANNEL, 'schedulingType': 'automatic', 'mode': 'addToQueue', 'needsApproval': False, 'saveToDraft': False,
               'assets': [{'image': {'url': IMAGE_BASE + name, 'metadata': {'altText': pin['alt_text']}}}],
               'metadata': {'pinterest': {'boardServiceId': BOARD, 'title': pin['title'], 'url': tracked_destination(pin['id'])}}}
    return payload, body


class OfferPage(HTMLParser):
    """Extract only public offer fields; never persist page/session props."""
    def __init__(self):
        super().__init__()
        self.pages, self.structured = [], []
        self.in_ld = False
        self.ld_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-page' in attrs:
            self.pages.append(attrs['data-page'])
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.in_ld = True
            self.ld_parts = []

    def handle_data(self, data):
        if self.in_ld:
            self.ld_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.in_ld:
            self.structured.append(''.join(self.ld_parts))
            self.in_ld = False


def verify_offer(raw):
    # Observed public Products/Show structure, not an undocumented API call.
    # Schema drift is a block, not permission to silently skip this gate.
    try:
        page = OfferPage()
        page.feed(raw.decode('utf-8'))
        if len(page.pages) != 1:
            raise ValueError()
        data = json.loads(page.pages[0])
        product = data['props']['product']
        if (data.get('component') != 'Products/Show' or product.get('permalink') != 'hynllx'
                or product.get('long_url') != DESTINATION
                or product.get('is_published') is not True
                or product.get('is_compliance_blocked') is not False
                or product.get('currency_code') != 'usd' or product.get('price_cents') != 3900
                or not isinstance(product.get('pwyw'), dict)
                or product.get('options') != []
                or 'waitlist' not in product or product['waitlist'] is not None
                or 'preorder' not in product or product['preorder'] is not None):
            raise ValueError()
        if product.get('is_sales_limited') is not False:
            quantity = product.get('quantity_remaining')
            if product.get('is_sales_limited') is not True or type(quantity) is not int or quantity <= 0:
                raise ValueError()
        structured = [json.loads(x) for x in page.structured]
        products = [x for x in structured if isinstance(x, dict) and x.get('@type') == 'Product']
        if len(products) != 1:
            raise ValueError()
        schema = products[0]
        offer = schema['offers']
        if (schema.get('url') != DESTINATION or schema.get('sku') != 'hynllx'
                or offer.get('@type') != 'Offer' or offer.get('url') != DESTINATION
                or offer.get('availability') != 'https://schema.org/InStock'
                or offer.get('priceCurrency') != 'USD' or offer.get('price') != 39):
            raise ValueError()
    except Exception:
        # No raw HTML or JSON, which can contain runtime tokens, in logs.
        raise Blocked('destination offer unavailable, changed, or unrecognized; manual review required') from None
    return {'offer_available': True, 'currency': 'USD', 'minimum_price_cents': 3900,
            'checkout_tested': False}


def verify_urls(payload, body):
    raw, mime = request(payload['assets'][0]['image']['url'], limit=5_000_000)
    if raw != body or mime.split(';')[0] not in ('image/jpeg', 'image/png'):
        raise Blocked('hosted image not identical to approved image')
    raw, mime = request(payload['metadata']['pinterest']['url'], limit=5_000_000)
    if 'text/html' not in mime:
        raise Blocked('destination not retrievable HTML')
    verify_offer(raw)


def canonical_destination(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.fragment or parsed.scheme + '://' + parsed.netloc + parsed.path != DESTINATION:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(k not in {'utm_source', 'utm_medium', 'utm_campaign', 'utm_content'} for k, _ in pairs):
        return None
    return DESTINATION


def matches(post, payload):
    m = post.get('metadata') or {}
    target = payload['metadata']['pinterest']
    return (post.get('channelId') == CHANNEL and post.get('text') == payload['text']
            and m.get('title') == target['title'] and canonical_destination(m.get('url')) == DESTINATION
            and (m.get('board') or {}).get('serviceId') == BOARD)


def same_asset(post, payload):
    return any(a.get('source') == payload['assets'][0]['image']['url'] for a in post.get('assets', []))


def verified_content(post, payload):
    assets = post.get('assets', [])
    return (matches(post, payload) and (post.get('metadata') or {}).get('url') == payload['metadata']['pinterest']['url'] and len(assets) == 1 and same_asset(post, payload)
            and (assets[0].get('image') or {}).get('altText') == payload['assets'][0]['image']['metadata']['altText'])


def outcome(post):
    if post.get('channelId') != CHANNEL:
        raise Blocked('post belongs to another channel')
    status = post['status']
    url = post.get('externalLink')
    published = status == 'sent' and bool(post.get('sentAt')) and bool(re.fullmatch(
        r'https://www\.pinterest\.com/pin/[0-9]+/?', url or ''))
    return {'buffer_post_id': post['id'], 'status': status, 'due_at': post.get('dueAt'),
            'sent_at': post.get('sentAt'), 'publication_url': url if published else None,
            'publication_id': url.rstrip('/').split('/')[-1] if published else None,
            'publication_verified': published, 'verification_basis': 'Buffer status + sentAt + Pinterest URL; not independent page fetch'}


def publish(pin, api, ledger, execute=False, assets=ROOT / 'assets', url_check=verify_urls):
    payload, body = candidate(pin, assets)
    api.verify()
    posts = api.posts()
    duplicates = [p for p in posts if matches(p, payload) or any(
        a.get('source') == payload['assets'][0]['image']['url'] for a in p.get('assets', []))]
    if duplicates:
        return {'action': 'remote_duplicate_no_create', 'posts': [outcome(p) for p in duplicates]}
    if sum(p['status'] in ('scheduled', 'sending') for p in posts) >= 10:
        raise CapacityDeferred('queue capacity reached')
    url_check(payload, body)
    if not execute:
        return {'action': 'dry_run_ready', 'pin_id': pin['id'], 'payload_sha256': digest(payload)}
    state, sha = ledger.read()
    fingerprints = {'payload_sha256': digest(payload), 'asset_sha256': pin['asset_sha256'],
                    'intent_sha256': digest([pin['source_id'], pin['search_intent']])}
    if (state['lock'] or pin['id'] in state['records'] or any(
            any(record.get(k) == value for k, value in fingerprints.items())
            for record in state['records'].values())):
        raise Blocked('durable claim exists; reconcile, never repeat create')
    # Global durable lock coordinates even runners outside workflow concurrency.
    state = copy.deepcopy(state)
    state['lock'] = pin['id']
    state['records'][pin['id']] = {'state': 'intent', **fingerprints}
    sha = ledger.cas(state, sha)  # Must be durable BEFORE create, including crash window.
    posts = api.posts()  # Recheck cap/dedup after owning the lock.
    if any(matches(p, payload) or same_asset(p, payload) for p in posts) or sum(p['status'] in ('scheduled', 'sending') for p in posts) >= 10:
        raise Blocked('remote state changed; intent retained for review')
    post_id = api.create(payload)  # Exactly one attempt. All failures retain intent/lock.
    post = api.post(post_id)
    if not verified_content(post, payload):
        raise Blocked('created post verification failed; intent retained')
    result = outcome(post)
    state['records'][pin['id']].update(state='recorded', **result)
    state['lock'] = None
    ledger.cas(state, sha)
    return {'action': 'created', **result}


def reconcile(pin, api, ledger, assets=ROOT / 'assets'):
    # Separate, explicit action; never calls create and never releases a missing intent.
    payload, _ = candidate(pin, assets)
    api.verify()
    state, sha = ledger.read()
    record = state['records'].get(pin['id'])
    if not record or record['payload_sha256'] != digest(payload):
        raise Blocked('missing or changed intent; manual review needed')
    posts = api.posts()
    found = [p for p in posts if matches(p, payload)]
    if len(found) != 1:
        raise Blocked('intent unresolved or duplicate matches; keep lock and review manually')
    post = api.post(found[0]['id'])
    if not verified_content(post, payload):
        raise Blocked('reconciled post content mismatch; retain lock')
    result = outcome(post)
    state['records'][pin['id']].update(state='recorded', **result)
    if state['lock'] == pin['id']:
        state['lock'] = None
    ledger.cas(state, sha)
    return {'action': 'reconciled_no_create', **result}


def write_guard():
    if (os.environ.get('PUBLISHER_ENABLED') != 'true'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_REPOSITORY') != REPO):
        raise Blocked('write mode disabled or not canonical main')


def replenish(pins, api, ledger):
    """One mutation at a time; unknown write outcomes stop the whole run."""
    api.verify()
    results = []
    for pin in pins:
        try:
            candidate(pin)
        except Blocked as e:
            results.append({'pin_id': pin['id'], 'action': 'skipped', 'reason': str(e)})
            continue
        # publish owns all live checks. Do not catch an uncertain write and continue.
        try:
            result = publish(pin, api, ledger, execute=True)
        except CapacityDeferred:
            results.append({'pin_id': pin['id'], 'action': 'deferred', 'reason': 'queue_capacity_reached'})
            break
        results.append({'pin_id': pin['id'], **result})
    return {'mode': 'sequential_approved_replenishment', 'pins': results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pin-id')
    parser.add_argument('--mode', choices=('plan', 'audit', 'check', 'execute', 'reconcile', 'status', 'replenish'), default='plan')
    args = parser.parse_args()
    pins = json.loads((ROOT / 'campaign/pins.json').read_text())['pins']
    ids = [p['id'] for p in pins]
    intents = [(p.get('source_id'), p.get('search_intent')) for p in pins]
    if len(set(ids)) != len(ids) or len(set(intents)) != len(intents):
        raise Blocked('duplicate campaign IDs or source intents')
    if args.mode == 'plan':
        report = []
        for p in pins:
            try:
                candidate(p)
                report.append({'pin_id': p['id'], 'action': 'needs_live_check'})
            except Blocked as e:
                report.append({'pin_id': p['id'], 'action': str(e)})
        print(json.dumps({'mode': 'offline_plan', 'pins': report}, indent=2))
        return
    if args.mode == 'audit':
        api = Buffer(os.environ.get('BUFFER_API_KEY'))
        api.verify()
        posts = api.posts()
        report = []
        for pin in pins:
            if pin.get('buffer_post_id'):
                report.append({'pin_id': pin['id'], 'action': 'existing_post', **outcome(api.post(pin['buffer_post_id']))})
                continue
            try:
                payload, body = candidate(pin)
                verify_urls(payload, body)
                duplicate = any(matches(p, payload) or same_asset(p, payload) for p in posts)
                report.append({'pin_id': pin['id'], 'action': 'remote_duplicate_no_create' if duplicate else 'ready_to_schedule'})
            except Blocked as e:
                report.append({'pin_id': pin['id'], 'action': 'blocked', 'reason': str(e)})
        queued = sum(p['status'] in ('scheduled', 'sending') for p in posts)
        print(json.dumps({'mode': 'read_only_audit', 'channel_verified': True, 'queue_cap': 10,
                          'queued_or_sending': queued, 'available_slots': max(0, 10 - queued),
                          'remote_posts_checked': len(posts), 'pins': report}, indent=2))
        return
    if args.mode in ('execute', 'reconcile', 'replenish'):
        write_guard()
    if args.mode == 'replenish':
        api = Buffer(os.environ.get('BUFFER_API_KEY'))
        ledger = GitHubLedger(os.environ.get('GITHUB_TOKEN'))
        print(json.dumps(replenish(pins, api, ledger), indent=2))
        return
    selected = [p for p in pins if p['id'] == args.pin_id]
    if len(selected) != 1:
        raise Blocked('select exactly one existing campaign Pin')
    api = Buffer(os.environ.get('BUFFER_API_KEY'))
    ledger = GitHubLedger(os.environ.get('GITHUB_TOKEN')) if args.mode in ('execute', 'reconcile') else None
    if args.mode == 'status':
        post_id = selected[0].get('buffer_post_id')
        if not post_id:
            state, _ = GitHubLedger(os.environ.get('GITHUB_TOKEN')).read()
            post_id = state['records'].get(selected[0]['id'], {}).get('buffer_post_id')
        if not post_id:
            raise Blocked('no confirmed post ID; reconcile intent first')
        api.verify()
        print(json.dumps(outcome(api.post(post_id)), indent=2))
        return
    result = reconcile(selected[0], api, ledger) if args.mode == 'reconcile' else publish(
        selected[0], api, ledger, execute=args.mode == 'execute')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    try:
        main()
    except Blocked as e:
        print('BLOCKED: ' + str(e))
        sys.exit(2)
    except Exception:
        print('BLOCKED: invalid data or unexpected failure; reconcile before retrying writes')
        sys.exit(2)
