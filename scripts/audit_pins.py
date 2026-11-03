#!/usr/bin/env python3
"""Read-only TCE Pinterest manifest audit; never publishes or changes approval."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
MANIFEST = ROOT / "campaign" / "pins.json"
VALID_STATUSES = {"sent", "scheduled", "draft", "asset-pending", "review", "approved"}

def audit():
    pins = json.loads(MANIFEST.read_text(encoding="utf-8"))["pins"]
    seen_ids, seen_intents, seen_posts = set(), set(), set()
    results = []
    for p in pins:
        pid = p.get("id", "")
        intent = (p.get("source_id"), p.get("search_intent"))
        asset = p.get("asset", "")
        path = (ASSETS / asset).resolve()
        hosted = bool(asset) and path.is_relative_to(ASSETS.resolve()) and path.is_file()
        parsed = urlparse(p.get("destination_url", ""))
        status = p.get("status", "")
        approved = p.get("editorial_approved") is True
        post_id = p.get("buffer_post_id")
        issues = []
        if not pid or pid in seen_ids: issues.append("missing or duplicate pin id")
        seen_ids.add(pid)
        if not all(intent) or intent in seen_intents: issues.append("missing or duplicate source/search intent")
        seen_intents.add(intent)
        if post_id:
            if post_id in seen_posts: issues.append("duplicate Buffer post id")
            seen_posts.add(post_id)
        if status not in VALID_STATUSES: issues.append("invalid status")
        if not hosted: issues.append("asset not hosted in repository")
        if parsed.scheme != "https" or not parsed.netloc: issues.append("invalid HTTPS destination")
        if not p.get("board_service_id"): issues.append("board missing")
        if status in ("sent", "scheduled") and not post_id: issues.append("published/scheduled without Buffer post id")
        if status in ("sent", "scheduled") and not approved: issues.append("published/scheduled without approval")
        if status == "approved" and not approved: issues.append("approved status without editorial approval")
        eligible = approved and hosted and not issues and status in ("approved", "draft")
        blocking = [x for x in issues if x not in ("asset not hosted in repository",)]
        if approved and not hosted: blocking.append("approved pin missing image")
        results.append({"id": pid, "status": status, "hosted": hosted,
                        "editorial_approved": approved, "eligible_to_schedule": eligible,
                        "issues": issues, "blocking": blocking})
    return results

def main():
    results = audit()
    summary = {"total": len(results), "sent": sum(x["status"] == "sent" for x in results),
               "scheduled": sum(x["status"] == "scheduled" for x in results),
               "ready_to_schedule": sum(x["eligible_to_schedule"] for x in results),
               "pending": sum(not x["eligible_to_schedule"] and x["status"] not in ("sent", "scheduled") for x in results),
               "blocking_errors": sum(bool(x["blocking"]) for x in results),
               "pins": results}
    print(json.dumps(summary, indent=2))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        lines = ["## TCE Pinterest readiness", "",
                 f"Sent: {summary['sent']} | Scheduled: {summary['scheduled']} | Ready: {summary['ready_to_schedule']} | Pending: {summary['pending']}", "",
                 "| Pin | State | Hosted | Approved | Action |", "|---|---|---|---|---|"]
        for p in results:
            action = "Ready" if p["eligible_to_schedule"] else "; ".join(p["issues"]) or "Already processed"
            lines.append(f"| {p['id']} | {p['status']} | {p['hosted']} | {p['editorial_approved']} | {action} |")
        Path(summary_file).open("a", encoding="utf-8").write("\n".join(lines) + "\n")
    if summary["blocking_errors"]:
        print("Manifest integrity errors detected", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()


"""Bounded read-only public-page probe. No cookies, keys, writes or redirects."""
import hashlib
from html.parser import HTMLParser
import json
import re
import urllib.request
import urllib.error

ROUTES = [
    ('pin-04-substack-context', 'https://davidkraase.substack.com/p/what-the-context-engine-is-about',
     'What The Context Engine Is About', 'That is what The Context Engine is about.'),
    ('pin-06-perspective', 'https://davidkraase.substack.com/p/the-distance-between-living-and-learning',
     'The Distance Between Living and Learning', 'Awareness can name a pattern. Learning changes the next attempt.'),
]

class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('redirect rejected')

class PublicMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical, self.og_url, self.og_type, self.headings, self.ld = [], [], [], [], []
        self.h1, self.script, self.parts = False, False, []
        self.article_count = self.markup_count = 0
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') == 'canonical': self.canonical.append(attrs.get('href'))
        if tag == 'meta' and attrs.get('property') == 'og:url': self.og_url.append(attrs.get('content'))
        if tag == 'meta' and attrs.get('property') == 'og:type': self.og_type.append(attrs.get('content'))
        if tag == 'h1': self.h1 = True; self.headings.append('')
        if tag == 'script' and attrs.get('type') == 'application/ld+json': self.script = True; self.parts = []
        if tag == 'article': self.article_count += 1
        if {'body', 'markup'}.issubset(set(attrs.get('class', '').split())): self.markup_count += 1
    def handle_data(self, data):
        if self.h1: self.headings[-1] += data
        if self.script: self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == 'h1': self.h1 = False
        if tag == 'script' and self.script: self.ld.append(''.join(self.parts)); self.script = False

def probe(pin_id, url, headline, marker):
    result = {'pin_id': pin_id, 'url': url, 'probe': 'read_only_public_GET'}
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'TCE-approved-pin-publisher', 'Accept': 'text/html'})
        with urllib.request.build_opener(RejectRedirect()).open(req, timeout=20) as response:
            raw = response.read(5_000_001)
            result.update(http_status=response.status, html=response.headers.get('Content-Type', '').split(';')[0] == 'text/html',
                          bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if len(raw) > 5_000_000: raise ValueError('too large')
        source = raw.decode('utf-8')
        page = PublicMetadata(); page.feed(source); page.close()
        norm = lambda value: ' '.join(value.split()).casefold()
        result.update(canonical_exact=page.canonical == [url], og_url_exact=page.og_url == [url],
                      og_type_article=page.og_type == ['article'], headline_exact=sum(norm(h) == norm(headline) for h in page.headings),
                      article_containers=page.article_count, markup_containers=page.markup_count,
                      marker_in_response=marker in source,
                      unavailable_wrapper='Site Unavailable' in source and len(raw) < 5000)
        nodes = []
        def visit(value, depth=0):
            if depth > 10 or len(nodes) > 1000: raise ValueError('schema bounds')
            if isinstance(value, dict):
                nodes.append(value)
                for child in value.values():
                    if isinstance(child, (dict, list)): visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value: visit(child, depth + 1)
        for value in page.ld: visit(json.loads(value))
        articles = [n for n in nodes if n.get('@type') in ('Article', 'NewsArticle', 'BlogPosting')]
        result['article_schema'] = []
        for article in articles:
            entity = article.get('mainEntityOfPage')
            author = article.get('author')
            authors = author if isinstance(author, list) else [author]
            result['article_schema'].append({'type': article['@type'],
                'headline_exact': norm(article.get('headline', '')) == norm(headline),
                'url_present': 'url' in article, 'url_exact': article.get('url') == url,
                'entity_kind': 'object' if isinstance(entity, dict) else 'string' if isinstance(entity, str) else 'missing',
                'entity_exact': (entity.get('@id') if isinstance(entity, dict) else entity) == url,
                'free_field_present': 'isAccessibleForFree' in article,
                'free_boolean_true': article.get('isAccessibleForFree') is True,
                'free_boolean_false': article.get('isAccessibleForFree') is False,
                'author_exact': len(authors) == 1 and isinstance(authors[0], dict) and authors[0].get('name') == 'David Kraase',
                'article_body_present': bool(article.get('articleBody'))})
        # Inspect only public article fields from page-embedded JSON. Never log,
        # store or emit runtime props, account/session data or raw script bodies.
        match = re.search(r'window\._preloads\s*=\s*JSON\.parse\(("(?:\\.|[^"\\])*")\)', source)
        result['public_post_metadata'] = {'recognized': bool(match)}
        if match:
            preloads = json.loads(json.loads(match.group(1)))
            post = preloads.get('post')
            if isinstance(post, dict):
                audience = post.get('audience')
                result['public_post_metadata'].update(
                    post_object=True, audience=audience if audience in ('everyone', 'only_paid', 'only_free', 'founding') else 'unrecognized',
                    title_exact=norm(post.get('title', '')) == norm(headline),
                    slug_exact=post.get('slug') == url.rsplit('/', 1)[-1],
                    body_html_present=bool(post.get('body_html')),
                    body_html_characters=len(post.get('body_html') or ''),
                    canonical_url_present='canonical_url' in post, canonical_url_exact=post.get('canonical_url') == url,
                    published_flag_present='is_published' in post, published_flag_true=post.get('is_published') is True)
        result['outcome'] = 'observed_metadata_only_not_publication_authority'
    except Exception as error:
        result['outcome'] = 'probe_blocked'
        result['error_type'] = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            result['http_status'] = error.code
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__':
    for args in ROUTES:
        probe(*args)
