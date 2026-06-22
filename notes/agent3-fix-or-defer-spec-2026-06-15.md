# Agent 3 — Fix-or-Defer Spec (Site Access & Issue Routing)

**Date:** 2026-06-15
**Branch:** feature/agent-azure
**References:** `SESSION-NOTE-2026-06-11.md`, `notes/agent-workflow-summary-2026-06-10.md`, `notes/agent3-agent4-pivot-2026-06-09.md`

---

## 1. Context

The 06-11 session proved the Agent 3 pipeline mechanics end-to-end (crawl → triage → ticket → fix attempt → Azure QA flag) but hit a real wall: **RankMath's REST `updateMeta` endpoint accepts writes (200 OK) but they don't render on the live page.** Today's demo wrote to the post's `excerpt` field as a stand-in — proves the pipeline, doesn't fix the actual issue.

The supervisor's framing question for this session: **"Why can't agents directly access and fix the site?"**

This spec answers that by drawing a hard line through LibreCrawl's full issue catalog (30 issue types from `src/core/issue_detector.py`):

- **Fix** — Agent 3 has (or could have, with WP-CLI) a real mechanism → applies it → ticket moves to `AZURE_QA_STATE` (existing mechanism, `fix_agent.py` `set_ticket_state()`), ready for the pending Agent 4 QA build.
- **Defer** — no agent action changes the ticket's `System.State`; Agent 3 posts an Azure DevOps comment explaining *why* it can't fix this, so a human knows it needs manual attention rather than LibreCrawl silently doing nothing.
- **Out of scope** — broken external links. Per current instruction, not categorized, not touched, behaves like an unmapped issue today (silent skip, logged).

---

## 2. The Case for Site Access (WP-CLI)

From the last live demo, 7 tickets were created. Of those:

| Issue type | Owner | REST/Application-Password outcome |
|---|---|---|
| Missing Meta Description | RankMath | Accepted (200), **did not render** |
| (4 others — title/canonical/OG/noindex-type) | RankMath | Same blocker, untested individually but same mechanism |
| (2 others) | Core WP / infra | Not in fix map, correctly skipped |

**Confirmed finding (06-11):** RankMath's per-post SEO save goes through an internal AJAX/nonce flow inside wp-admin that can't be replicated from outside via REST + Application Password. **WP-CLI runs as the WordPress application itself** — `wp post meta update <id> rank_math_description "..."` (and `rank_math_title`, `rank_math_canonical_url`, `rank_math_robots`, etc.) writes the same postmeta WordPress core itself would write, with no REST/nonce/AJAX layer in the way. This is the *only* identified fix for the issue types that dominate LibreCrawl's reports.

**Scope of the access ask** (carried from 06-11):
- **Today**: Editor-level Application Password — content operations only, can't install plugins, can't touch files, can't run code.
- **Proposed**: SSH/WP-CLI — a materially larger trust grant, closer to "agent has developer shell access." Prototyping needs a local Docker WordPress + WP-CLI sandbox (TasteWP's free tier has no SSH/WP-CLI).

---

## 3. Site Capability Probe — Atlas as Production Reference, TasteWP as Sandbox

These are two different roles and should not be conflated:

- **Atlas (`atlas.culturalinfusion.com`)** — a real **production** client site. Confirmed: standalone WordPress, REST API live, RankMath active (via `/wp-json/` namespace list), Application Passwords supported. Atlas represents the *kind* of plugin/version baseline real client sites have. It is the reference used to define what the capability probe checks for, and would be the first real domain the Fix bucket targets once WP-CLI is approved.
- **TasteWP** — the actual **sandbox**. Disposable, free-tier, used specifically so nothing on a real client site breaks while testing new fix mechanisms (06-11's `excerpt`-field demo ran here). No SSH/WP-CLI on the free tier — WP-CLI prototyping needs a local Docker WP+WP-CLI sandbox instead. Still not Atlas.

### Capability probe

Before trusting a domain's fix map, run `GET /wp-json/` on that domain and check:
- `rankmath/v1` namespace present → RankMath-field fixes available
- `redirection/v1` namespace (or RankMath's own redirect manager) present → redirect-collapse fixes available
- ShortPixel / Smush active + configured → image-compression fixes available
- WP version (from the `wp-json` root response)

This probe decides, per domain in the future `WP_SITES` config, which Fix-bucket entries are *available* for that site — e.g. a production site without RankMath active can't get RankMath-field fixes even with WP-CLI access. No config / failed probe for a domain → skip, same pattern as "not in fix map" today (`fix_agent.py` line 42).

**Workflow**: new fix mechanisms are built and proven on TasteWP/local Docker first; only after they're proven do they get pointed at a probed production domain (Atlas first, since it's already confirmed-compatible).

### Making TasteWP a near-copy of Atlas (for testing parity)

TasteWP's free tier supports creating a staging instance *from a backup file* of an existing site (the "TasteWP (external server)" option, fed by a backup/migration plugin such as Backup Migration or All-in-One WP Migration). Practically:

1. Export a backup of Atlas — plugins, theme, content, RankMath settings included.
2. Import it into a fresh TasteWP instance.
3. The result is a disposable clone with the **same plugin/version stack as Atlas**, safe to run WP-CLI fix prototypes against with zero risk to the real site.

This answers "can WP-CLI fixes be validated against something that behaves like Atlas before touching Atlas itself?" — yes, via backup-based cloning. The clone step itself needs no SSH; running WP-CLI against the clone still needs the local-Docker-WP-CLI layer from Section 2 if TasteWP itself doesn't expose WP-CLI.

---

## 4. Decision Model — Three Buckets

1. **Fix** → Agent 3 applies it (via WP-CLI once approved, or REST/core today where it already works) → on success, `set_ticket_state(ticket_id, project, AZURE_QA_STATE)` — existing mechanism, `main.py` ~line 1953.
2. **Defer** → `System.State` untouched → Agent 3 posts a comment on the Azure work item (new, small addition to `fix_agent.py`, same PATCH-style call shape as `set_ticket_state()` but targeting the comments endpoint) explaining *why* — e.g. *"Server-side 500 — outside WordPress/Agent 3 scope, needs infra investigation."*
3. **Out of scope** → broken external links: not categorized, not touched, behaves exactly as an unmapped issue does today (silent skip, logged).

---

## 5. Issue Catalog → Bucket Mapping

All 30 issue-type strings from `src/core/issue_detector.py`.

### Fix bucket (RankMath postmeta via WP-CLI, or core REST where already proven)

| Issue type | Mechanism |
|---|---|
| Missing Title Tag, Title Too Long, Title Too Short | `rank_math_title` |
| Missing Meta Description, Meta Description Too Long, Meta Description Too Short | `rank_math_description` (already proven end-to-end on 06-11, modulo the RankMath-render blocker) |
| Missing Canonical URL, Canonical URL Different | `rank_math_canonical_url` (flag Canonical-Different for Agent 4 re-verification — could be intentional) |
| Missing OpenGraph Tags | `rank_math_facebook_*` fields |
| Missing Twitter Card Tags | `rank_math_twitter_*` fields |
| Images Without Alt Text | `wp/v2/media` `alt_text` — core WP, no plugin, doesn't even need WP-CLI |

### Defer bucket

Split into two distinct kinds of "why," since WP-CLI access changes one and not the other.

**3a. Outside WP-CLI's reach entirely** — WP-CLI is a process *within* the WordPress install; these problems occur before/outside that install:

| Issue type | Reason |
|---|---|
| `{status} Server Error` (5xx) | Server/infra-level (PHP fatal, DB connection, hosting resource limits). WP-CLI runs on the same broken stack — if PHP is fataling or the DB is unreachable, `wp` commands fail the same way. Needs hosting/ops access, not WordPress access. |
| DNS Not Found, Connection Refused, Request Timeout, SSL/TLS Error, Connection Error | Pure network/DNS/certificate errors. WP-CLI itself needs to *reach* the site/DB to run — these errors mean it can't either. |
| Slow/Moderate Response Time | Often server CPU/DB-query/caching-layer bound — same "outside the WP process" category as 5xx. |

**3b. WP-CLI is technically capable here — the missing piece is judgment or blast-radius, not access:**

| Issue type | Reason |
|---|---|
| Missing H1 Tag, Thin Content, Duplicate Content Detected, Noindex Tag Present, Nofollow Tag Present | WP-CLI can trivially write any field (`wp post meta update`, `wp post update --post_content=...`) — zero technical barrier. Stays Defer because the *decision* (what should the H1 say, is duplicate content intentional, is a noindex deliberate — e.g. a staging/thank-you page) requires business intent an LLM can't reliably infer. WP-CLI access doesn't move these out of Defer. |
| Missing Viewport Meta Tag, Missing Language Attribute | Theme/template-level (`header.php`), not per-post. WP-CLI *can* edit theme files or run arbitrary PHP (`wp eval`) — but a bug here breaks every page on the site, not one. This is the "`wp eval` — extremely powerful, extremely high-risk" line flagged in 06-11. Needs a stronger gate (staging-first/code review) than the per-post Fix bucket, even with full WP-CLI access. |
| `{status} Redirect` (3xx redirect chains) | **Conditionally fixable.** Confirmed via research: the Redirection plugin ships full WP-CLI support (`redirection.me/developer/wp-cli`) — list/create/import-export redirect rules, including importing existing rules from RankMath's own redirect manager. If the capability probe finds Redirection (or RankMath's redirect manager) active on a domain, collapsing a *simple* one-hop redirect becomes a frictionless `wp redirection` command. Still Defer for multi-hop chains — a human needs to confirm which hop is the "real" destination before collapsing (06-09's concern is *correctness*, not plugin availability). Most likely 3b item to graduate to Fix once a confidence rule is designed (see Section 7). |
| Large/Moderate Page Size (image compression) | **Conditionally fixable.** Confirmed via research: ShortPixel (`wp spio bulk auto`) and Smush (`wp smush compress`, multisite bulk) both ship WP-CLI bulk-optimize commands that run off the plugin's *existing* settings — no install step if one is already active and configured. Probe finds an active, configured compression plugin → "Large Page Size" becomes a one-command Fix. No compression plugin configured → stays Defer, since installing+configuring one (API keys, quota) is a site-wide decision needing the human gate from 06-09. |
| No Structured Data | RankMath's schema module requires picking a schema *type* per page (judgment call), more than a single field write. Flag as a possible Phase 2 fix once simpler postmeta fixes are proven. |
| `{status} Client Error` (404), Broken Image (No Response / {status}) | Content doesn't exist yet — "no access level can write what hasn't been authored" (06-09), WP-CLI included. |

### Out of scope (not categorized)

- Broken external links — per current instruction, ignored for now.

---

## 6. Ticket Outcome Summary

| Bucket | Ticket state change | New code needed |
|---|---|---|
| Fix | → `AZURE_QA_STATE` | Already implemented (`fix_agent.py` / `main.py`) |
| Fix (conditional — probe-gated) | → `AZURE_QA_STATE`, only if probe finds the required plugin active+configured (Redirection for simple redirects, ShortPixel/Smush for page-size); otherwise falls through to Defer | Probe + conditional routing |
| Defer | Untouched + new comment | New comment-post call, small addition to `fix_agent.py` |
| Out of scope | Untouched, no comment | None — matches today's "not in fix map" behavior |

---

## 7. Open Items Carried Forward

- **WP-CLI/SSH access decision** still pending supervisor sign-off (06-11). This spec's Fix bucket size — **6 of 30 issue types unconditionally** (covering 5 of the 7 from the last live demo), **plus up to 2 more conditionally** (redirect chains, page size — if Redirection/ShortPixel/Smush already active on a given site) — is the concrete number WP-CLI access would unlock *right now*. The 3a/3b split is the rest of the pitch: 3a issues stay Defer no matter what access is granted (sets expectations correctly); 3b issues are technically unlockable by WP-CLI but deliberately stay Defer this phase pending judgment/blast-radius gating, so granting WP-CLI doesn't silently expand Agent 3's scope into riskier territory without a separate decision.
- **`WP_SITES` domain-keyed config + per-domain capability probe** (Section 3) still need building. Probe targets production domains (Atlas first), checks for RankMath + Redirection + image-optimization plugins. Prototyping happens on a TasteWP clone of Atlas (via backup/migration import) or local Docker. Ties into the multi-URL Agent 1 redesign already noted in 06-10.
- **Defer-bucket comment-posting** is new scope, not yet built — small, isolated addition alongside `set_ticket_state()`.
- **Redirect-collapse confidence rule** — "collapse only if all hops are same-domain 301s ending in a single final 200" — needs design before redirect chains can graduate from conditional-Fix to Fix.
