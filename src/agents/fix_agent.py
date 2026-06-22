# TODO: this module uses print() for progress visibility (`docker logs -f librecrawl`).
# Review before merging — follow the str(e) handling pattern from PR9-CODEQL-FIXPLAN.md
# (don't let raw exception text leak into anything client-facing).

import os
import base64
import requests
from urllib.parse import quote, urlparse, urljoin
from bs4 import BeautifulSoup

from src.agents.wordpress import (
    resolve_wp_object, apply_rankmath_meta, apply_fix,
    probe_site, ensure_plugin_active, create_redirect_rule, NAMESPACE_PLUGIN_MAP,
)
from src.agents.provider import call_with_tools


def _confirm_rendered(url, expected):
    """Re-fetch url and check whether each expected {meta-name: value} pair actually
    appears in the rendered HTML. Returns the subset of expected keys NOT found —
    empty dict means everything rendered. The '__title__' key checks the <title>
    element instead of a <meta> tag."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        return {k: f'could not re-fetch page: {e}' for k in expected}

    missing = {}
    for name, value in expected.items():
        if name == '__title__':
            rendered = soup.title.string.strip() if soup.title and soup.title.string else None
        else:
            tag = soup.find('meta', attrs={'name': name}) or soup.find('meta', attrs={'property': name})
            rendered = tag.get('content', '') if tag else None
        if rendered != value:
            missing[name] = f'expected {value!r}, found {rendered!r}'
    return missing


def _fetch_page_context(url):
    """Re-fetch a page and pull title/H1/opening body text for LLM prompts."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string.strip() if soup.title and soup.title.string else ''
        h1 = soup.find('h1')
        h1_text = h1.get_text(strip=True) if h1 else ''
        body = soup.find('body')
        body_text = ' '.join(body.get_text(' ', strip=True).split())[:500] if body else ''
        return {'title': title, 'h1': h1_text, 'body_excerpt': body_text}
    except Exception:
        return {'title': '', 'h1': '', 'body_excerpt': ''}


def decide_required_plugin(issue, active_namespaces):
    """Ask the LLM which plugin (if any) is needed to fix this issue, constrained to
    NAMESPACE_PLUGIN_MAP's known candidates (rankmath/v1, redirection/v1) — never an
    open-ended choice from the whole plugin directory. Any answer outside the allowed
    set is treated as 'none', so a malformed/hallucinated reply fails safe to Defer
    rather than triggering an install.
    """
    candidates = {ns: slug for ns, slug in NAMESPACE_PLUGIN_MAP.items() if ns in ('rankmath/v1', 'redirection/v1')}
    prompt = (
        f"Issue: '{issue}'. Site's currently active plugin REST namespaces: {active_namespaces}.\n"
        f"Which plugin slug is needed to fix this issue? Valid answers, exactly as written, "
        f"nothing else: {list(candidates.values())} or 'none'."
    )
    _, text, _, _ = call_with_tools([{'role': 'user', 'content': prompt}], [])
    answer = text.strip().strip("'\"")
    return answer if answer in candidates.values() else 'none'


def _trace_redirect_chain(url, max_hops=10):
    """Walk a redirect chain one hop at a time with allow_redirects=False. The
    crawler itself fetches with allow_redirects=True (the default), which collapses
    chains transparently and never records individual hops — crawled_urls.redirects
    is always an empty list — so this re-fetches live at fix-time instead.
    Returns [(url, status_code), ...] ending with the final non-redirect response.
    """
    chain, current = [], url
    for _ in range(max_hops):
        resp = requests.get(current, allow_redirects=False, timeout=10)
        chain.append((current, resp.status_code))
        if resp.status_code in (301, 302, 303, 307, 308) and 'Location' in resp.headers:
            current = urljoin(current, resp.headers['Location'])
        else:
            break
    return chain


def _redirect_confidence_check(url):
    """notes/agent3-fix-or-defer-spec-2026-06-15.md §7's rule: only safe to collapse
    if every hop is a same-domain 301 ending in a single final 200. Returns the final
    URL if safe to collapse, or None if it should stay Defer (multi-hop judgment call,
    cross-domain, or doesn't resolve to a working page).
    """
    chain = _trace_redirect_chain(url)
    if len(chain) < 2:
        return None
    *hops, (final_url, final_status) = chain
    base_domain = urlparse(url).netloc
    if final_status != 200:
        return None
    if any(status != 301 or urlparse(hop_url).netloc != base_domain for hop_url, status in hops):
        return None
    return final_url


def fix_title_core(ticket):
    """LLM-generated title, sized to the ticket's specific complaint. Written via
    core REST's native `title` field — no RankMath/plugin dependency."""
    ctx = _fetch_page_context(ticket['url'])
    length = "between 30 and 60 characters" if 'Short' in ticket['issue'] else "at most 60 characters"
    prompt = (
        f"Write a single SEO page title, {length}, for this webpage.\n"
        f"Current title (may be missing or the wrong length): {ctx['title']}\n"
        f"Main heading: {ctx['h1']}\nPage content excerpt: {ctx['body_excerpt']}\n"
        f"Return only the title text, no quotes, no preamble."
    )
    _, text, _, _ = call_with_tools([{'role': 'user', 'content': prompt}], [])
    return text.strip()[:60]


def fix_meta_description(ticket):
    """Fix for 'Missing/Too Long/Too Short Meta Description' — sets RankMath's description field."""
    return {'rank_math_description': f"[Agent 3] Description for: {ticket['url']}"}


def fix_canonical_url(ticket):
    """Fix for 'Missing Canonical URL' — points RankMath's canonical field at the page's own URL."""
    return {'rank_math_canonical_url': ticket['url']}


def fix_og_tags(ticket):
    """Fix for 'Missing OpenGraph Tags' — sets RankMath's Facebook/OG title and description fields."""
    return {
        'rank_math_facebook_title': f"Agent 3 fix: {ticket['url']}"[:60],
        'rank_math_facebook_description': f"[Agent 3] Description for: {ticket['url']}",
    }


def fix_twitter_tags(ticket):
    """Fix for 'Missing Twitter Card Tags' — sets RankMath's Twitter title and description fields."""
    return {
        'rank_math_twitter_title': f"Agent 3 fix: {ticket['url']}"[:60],
        'rank_math_twitter_description': f"[Agent 3] Description for: {ticket['url']}",
    }


# Maps Azure ticket issue names (src/core/issue_detector.py) to fix functions.
# Each fix function takes the ticket dict and returns a dict of RankMath meta
# keys -> new values, passed to apply_rankmath_meta(). Anything not listed
# here is skipped — matches the documented Agent 3 fix map. Title issues are
# handled separately in run_fix() via core REST, not this map — see
# fix_title_core().
FIX_MAP = {
    'Missing Meta Description': fix_meta_description,
    'Meta Description Too Long': fix_meta_description,
    'Meta Description Too Short': fix_meta_description,
    'Missing Canonical URL': fix_canonical_url,
    'Canonical URL Different': fix_canonical_url,
    'Missing OpenGraph Tags': fix_og_tags,
    'Missing Twitter Card Tags': fix_twitter_tags,
}


# Issue types Agent 3 cannot fix regardless of access level (notes/agent3-fix-or-defer-spec-2026-06-15.md,
# Section 5). Exact-match issue names -> human-readable reason for the deferred-ticket comment.
DEFER_REASONS = {
    # 3a — outside any access method's reach (infra/network, before WordPress is even reached)
    'DNS Not Found': "DNS-level error — outside WordPress/Agent 3 scope, needs infra investigation.",
    'Connection Refused': "Connection-level error — outside WordPress/Agent 3 scope, needs infra investigation.",
    'Request Timeout': "Request timeout — outside WordPress/Agent 3 scope, needs infra investigation.",
    'SSL/TLS Error': "SSL/TLS certificate error — outside WordPress/Agent 3 scope, needs infra investigation.",
    'Connection Error': "Connection-level error — outside WordPress/Agent 3 scope, needs infra investigation.",
    'Slow Response Time': "Server response time — likely infra/caching, outside Agent 3's WordPress-content scope.",
    'Moderate Response Time': "Server response time — likely infra/caching, outside Agent 3's WordPress-content scope.",

    # 3b — access isn't the gap; the gap is judgment or blast-radius
    'Missing H1 Tag': "Content judgment call — needs a human to write/approve the heading.",
    'Thin Content': "Content judgment call — rewriting page content needs human review.",
    'Duplicate Content Detected': "Content judgment call — whether this is intentional needs human review.",
    'Noindex Tag Present': "Confirmed sitewide cause: WP Admin → Settings → Reading → "
        "'Discourage search engines from indexing this site' is checked. This is a "
        "one-click admin setting, not a per-page fix — toggle it off there if indexing "
        "should resume.",
    'Nofollow Tag Present': "Nofollow may be deliberate — needs human confirmation before changing.",
    'Missing Viewport Meta Tag': "Theme/template-level fix (header.php) — out of scope for per-post Agent 3 fixes.",
    'Missing Language Attribute': "Theme/template-level fix (header.php) — out of scope for per-post Agent 3 fixes.",
    'No Structured Data': "Schema type selection is a judgment call — flagged as a possible future fix-map entry.",
    'Large Page Size': "Image compression needs a configured plugin (e.g. ShortPixel/Smush) — not assumed present, needs human review.",
    'Moderate Page Size': "Image compression needs a configured plugin (e.g. ShortPixel/Smush) — not assumed present, needs human review.",
    'Images Without Alt Text': "Image alt text needs human-written descriptive text for each asset before Agent 3 can safely write it.",
    'Broken Image (No Response)': "Referenced image doesn't exist — needs a human to source/select a replacement asset.",
}

# Issue names that embed a dynamic status code (e.g. "404 Client Error", "Broken Image (404)") —
# matched by substring since the exact string varies per ticket.
DEFER_PATTERNS = [
    (' Server Error', "Server-side error — outside WordPress/Agent 3 scope, needs infra investigation."),
    (' Client Error', "Page doesn't exist — content needs to be authored by a human, not generated by Agent 3."),
    (' Redirect', "Redirect chain — needs a human to confirm the correct final destination before collapsing."),
    ('Broken Image (', "Referenced image doesn't exist — needs a human to source/select a replacement asset."),
]


def _defer_reason(issue):
    """Return the defer reason for an issue type Agent 3 can't fix, or None if it's not a defer case."""
    if issue in DEFER_REASONS:
        return DEFER_REASONS[issue]
    for pattern, reason in DEFER_PATTERNS:
        if pattern in issue:
            return reason
    return None


def run_fix(ticket):
    """Apply a fix for one ticket: {url, issue, details}.
    Returns a result dict describing what happened — used for logging/demo output.
    """
    url   = ticket['url']
    issue = ticket['issue']

    print(f"[Agent 3] Received ticket: '{issue}' on {url}")

    if issue in ('Missing Title Tag', 'Title Too Long', 'Title Too Short'):
        print("[Agent 3] Resolving WordPress content object...")
        try:
            wp_object = resolve_wp_object(url)
        except Exception:
            print("[Agent 3] Could not resolve WordPress object — request to WordPress failed.")
            return {'status': 'error', 'reason': 'resolve_wp_object failed', 'issue': issue, 'url': url}

        if wp_object is None:
            print(f"[Agent 3] No WordPress post/page found for {url} — skipping.")
            return {'status': 'skipped', 'reason': 'no matching WordPress post/page', 'issue': issue, 'url': url}

        print(f"[Agent 3] Found {wp_object['type']} ID {wp_object['id']}.")
        new_title = fix_title_core(ticket)
        print(f"[Agent 3] Applying fix: updating title on {wp_object['type']} {wp_object['id']}...")

        try:
            apply_fix(url, wp_object['id'], wp_object['type'], 'title', new_title)
        except Exception:
            print("[Agent 3] Could not apply fix — WordPress write failed.")
            return {'status': 'error', 'reason': 'apply_fix failed', 'issue': issue, 'url': url}

        missing = _confirm_rendered(url, {'__title__': new_title})
        if missing:
            print(f"[Agent 3] Write succeeded but title did not render: {missing}")
            return {'status': 'error', 'reason': f'write succeeded but did not render: {missing}', 'issue': issue, 'url': url}

        print(f"[Agent 3] Fix applied and confirmed rendered. New title: {new_title}")
        return {
            'status': 'fixed',
            'issue': issue,
            'url': url,
            'object_id': wp_object['id'],
            'object_type': wp_object['type'],
            'meta': {'title': new_title},
            'caveat': 'AI-generated title, applied and confirmed live on the page. Please verify it reads correctly.',
        }

    if ' Redirect' in issue:
        print(f"[Agent 3] Tracing redirect chain for {url}...")
        try:
            final_url = _redirect_confidence_check(url)
        except Exception as e:
            print(f"[Agent 3] Could not trace redirect chain for {url}: {e}")
            final_url = None
        if not final_url:
            reason = _defer_reason(issue)
            print(f"[Agent 3] Redirect chain isn't a safe single-hop same-domain collapse — deferring. {reason}")
            return {'status': 'deferred', 'reason': reason, 'issue': issue, 'url': url}

        print(f"[Agent 3] Safe to collapse: {url} -> {final_url}")
        namespaces = probe_site(url)['namespaces']
        try:
            plugin_slug = decide_required_plugin(issue, namespaces)
        except Exception as e:
            print(f"[Agent 3] decide_required_plugin failed: {e}")
            plugin_slug = 'none'
        caveat = None
        if plugin_slug == 'redirection' and 'redirection/v1' not in namespaces:
            print(f"[Agent 3] Redirection plugin not found on {url} — attempting to install '{plugin_slug}'...")
            try:
                ensure_plugin_active(url, plugin_slug)
                print(f"[Agent 3] '{plugin_slug}' installed and active.")
                caveat = f"Installed and activated '{plugin_slug}' on this site to apply this fix — please confirm it's appropriate."
            except Exception as e:
                print(f"[Agent 3] Could not auto-install '{plugin_slug}': {e}")
                return {'status': 'deferred', 'reason': "Redirection plugin not found and auto-install failed — see server logs for detail.", 'issue': issue, 'url': url}

        try:
            create_redirect_rule(url, urlparse(url).path, final_url)
        except Exception:
            print("[Agent 3] Could not apply fix — create_redirect_rule failed.")
            return {'status': 'error', 'reason': 'create_redirect_rule failed', 'issue': issue, 'url': url}

        print(f"[Agent 3] Fix applied. Collapsed redirect to {final_url}.")
        return {'status': 'fixed', 'issue': issue, 'url': url, 'meta': {'collapsed_to': final_url}, 'caveat': caveat}

    fix_fn = FIX_MAP.get(issue)
    if not fix_fn:
        reason = _defer_reason(issue)
        if reason:
            print(f"[Agent 3] '{issue}' is not fixable by Agent 3 — deferring to human. {reason}")
            return {'status': 'deferred', 'reason': reason, 'issue': issue, 'url': url}
        print(f"[Agent 3] '{issue}' is not in the fix map — skipping.")
        return {'status': 'skipped', 'reason': 'not in fix map', 'issue': issue, 'url': url}

    print("[Agent 3] Resolving WordPress content object...")
    try:
        wp_object = resolve_wp_object(url)
    except Exception:
        print("[Agent 3] Could not resolve WordPress object — request to WordPress failed.")
        return {'status': 'error', 'reason': 'resolve_wp_object failed', 'issue': issue, 'url': url}

    if wp_object is None:
        print(f"[Agent 3] No WordPress post/page found for {url} — skipping.")
        return {'status': 'skipped', 'reason': 'no matching WordPress post/page', 'issue': issue, 'url': url}

    object_id = wp_object['id']
    object_type = wp_object['type']
    print(f"[Agent 3] Found {object_type} ID {object_id}.")

    namespaces = probe_site(url)['namespaces']
    try:
        plugin_slug = decide_required_plugin(issue, namespaces)
    except Exception as e:
        print(f"[Agent 3] decide_required_plugin failed: {e}")
        plugin_slug = 'none'
    caveat = None
    if plugin_slug == 'seo-by-rank-math' and 'rankmath/v1' not in namespaces:
        print(f"[Agent 3] RankMath not found on {url} — attempting to install '{plugin_slug}'...")
        try:
            ensure_plugin_active(url, plugin_slug)
            print(f"[Agent 3] '{plugin_slug}' installed and active.")
            caveat = f"Installed and activated '{plugin_slug}' on this site to apply this fix — please confirm it's appropriate."
        except Exception as e:
            print(f"[Agent 3] Could not auto-install '{plugin_slug}': {e}")
            return {'status': 'deferred', 'reason': "RankMath not found and auto-install failed — see server logs for detail.", 'issue': issue, 'url': url}

    meta_dict = fix_fn(ticket)
    print(f"[Agent 3] Applying fix: updating {list(meta_dict.keys())} on {object_type} {object_id}...")

    try:
        result = apply_rankmath_meta(url, object_id, meta_dict)
    except Exception:
        print("[Agent 3] Could not apply fix — WordPress write failed.")
        return {
            'status': 'error',
            'reason': 'apply_rankmath_meta failed',
            'issue': issue,
            'url': url,
            'object_id': object_id,
            'object_type': object_type,
        }

    print(f"[Agent 3] Fix applied. Updated: {meta_dict}")
    return {
        'status': 'fixed',
        'issue': issue,
        'url': url,
        'object_id': object_id,
        'object_type': object_type,
        'meta': meta_dict,
        'result': result,
        'caveat': caveat,
    }


def add_ticket_comment(ticket_id, project, comment):
    """Post a comment on an Azure DevOps work item.
    Used to explain to a human why Agent 3 deferred a ticket without changing its state.
    Returns True on success, False on failure (logs the failure).
    """
    org = os.getenv('AZURE_DEVOPS_ORG')
    pat = os.getenv('AZURE_DEVOPS_PAT')

    token   = base64.b64encode(f':{pat}'.encode()).decode()
    api_url = f'https://dev.azure.com/{org}/{quote(project)}/_apis/wit/workItems/{ticket_id}/comments?api-version=7.1-preview.3'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {token}',
    }
    body = {'text': comment}

    try:
        resp = requests.post(api_url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        print(f"[Agent 3] Comment added to ticket {ticket_id}.")
        return True
    except Exception:
        print(f"[Agent 3] Could not add comment to ticket {ticket_id} — request to Azure DevOps failed.")
        return False


def set_ticket_state(ticket_id, project, state):
    """Update an Azure DevOps work item's System.State field.
    Used to flag a ticket as ready for manual QA after Agent 3 applies a fix.
    Returns True on success, False on failure (logs the failure).
    """
    org = os.getenv('AZURE_DEVOPS_ORG')
    pat = os.getenv('AZURE_DEVOPS_PAT')

    token   = base64.b64encode(f':{pat}'.encode()).decode()
    api_url = f'https://dev.azure.com/{org}/{quote(project)}/_apis/wit/workitems/{ticket_id}?api-version=7.1'
    headers = {
        'Content-Type': 'application/json-patch+json',
        'Authorization': f'Basic {token}',
    }
    body = [{'op': 'add', 'path': '/fields/System.State', 'value': state}]

    try:
        resp = requests.patch(api_url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        print(f"[Agent 3] Ticket {ticket_id} moved to '{state}'.")
        return True
    except Exception:
        print(f"[Agent 3] Could not update ticket {ticket_id} state — request to Azure DevOps failed.")
        return False
