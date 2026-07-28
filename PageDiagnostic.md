# Page Diagnostic Plugin

## Description

A plugin tab (next to E-E-A-T) that turns a crawl's raw Issues list into an agentic
review-and-fix pipeline for WordPress sites, backed by Azure DevOps for ticketing. Four
agents, each a distinct step — not one "Ask AI" button:

- **Agent 1 — Crawl.** The core LibreCrawl crawler. Not part of this plugin, but everything
  below runs against its output.
- **Agent 2 — Ticket Review** (`src/agents/ticket_review_agent.py`). Triggered by the
  **Run Agent** button. An MCP-orchestrated agentic loop (`src/mcp_server.py`) that explains
  each crawl issue in plain English with a How-to-Fix, priority, and responsible role, posts
  the list back to the browser for human review, waits for approval, then creates the
  approved issues as Azure DevOps tickets.
- **Agent 3 — Auto-Fix** (`src/agents/fix_agent.py`). Runs automatically right after a
  ticket is created (if `AGENT3_ENABLED=true`). Applies the fix directly to WordPress via
  the REST API for the issue types it covers (see FIX_MAP below), then tags/comments the
  ticket with what it did.
- **Agent 4 — QA** (`src/agents/qa_agent.py`). Re-crawls a ticket's URL to confirm the
  issue is actually gone before a human marks it Done. Available per-ticket or as a bulk
  run across every ticket tagged `AZURE_QA_TAG`.

## Features
- **Run Agent** button on the Page Diagnostics tab kicks off Agent 2 for the current
  crawl's issues; results come back as a checklist for human approval before anything is
  ticketed.
- Per-URL explanation caching — a recurring crawl re-explaining the same unresolved issue
  on the same URL reuses the previous AI output instead of re-asking the model
  (`src/crawl_db.py`'s `issue_explanations` table).
- WP-resolvability pre-check — before spending an AI call, Agent 2 checks (via free REST,
  no tokens) whether a WordPress-fix-requiring issue type actually lands on an editable
  post/page; non-resolvable ones (archives, taxonomy pages) are tagged and shown to the
  human without an AI explanation call.
- **Create Ticket** sends approved issues to Azure DevOps as Product Backlog Items,
  deduplicated against tickets already created for the same (URL, issue) pair.
- Agent 3 auto-fixes the following issue types once a ticket exists:
  - Title (missing/too long/too short), Missing H1 Tag — via WordPress's core REST fields
  - Meta Description (missing/too long/too short), Canonical URL (missing/different),
    OpenGraph tags, Twitter Card tags — via `FIX_MAP`
  - Images Without Alt Text — context-based generation (surrounding text/heading/figcaption,
    not vision), gated on the media library resolving the image
  - Everything else (Broken Image, Slow Response Time, Noindex, 404s, thin/duplicate
    content, JSON-LD, viewport, redirects) is deferred with an explanation, not silently
    dropped — see `fix_agent.py`'s `DEFER_REASONS`/`DEFER_PATTERNS`.
- Agent 4 QA panel — single-ticket recheck or bulk run across every `AZURE_QA_TAG`-tagged
  ticket; marks Done or posts an AI-drafted comment on what's still wrong.
- Token-usage panel shows live Agent 2/3 spend during a run (per-model, from
  `src/agents/provider.py`'s usage log).
- Chat panel (`/api/agent/chat`) for ad-hoc questions about the current issue list against
  whichever AI provider is configured.
- Authorizing only users with the configured domain email as passwordless, via a magic
  link (unrelated to the agent pipeline — pre-existing auth).

## The Agent Chain, End to End

1. Crawl starts after choosing provider, Azure project and Feature and then finishes → Page Diagnostics tab lists issues.
2. Click **Run Agent** → browser POSTs `/api/agent/start_workflow` (captures the current
   crawl's ID into a shared workflow state, then launches Agent 2 in a background thread).
3. Agent 2 explains issues (parallelized, cache- and resolvability-checked first) and posts
   results back via its own `post_review_results` MCP tool.
4. Browser polls `/api/agent/review` (GET) until results are ready, shows the checklist.
5. Human selects issues, clicks Approve → POSTs `/api/agent/approval`.
6. Agent 2 polls approval, then calls its `create_bulk_tickets` MCP tool → hits
   `/api/agent/create_bulk_tickets`, which validates every approved (url, issue) pair
   against this session's actual crawl data before creating anything (rejects anything not
   from a real crawl the server ran).
7. Each successful ticket immediately triggers Agent 3 (`run_fix`) if enabled.
8. Once a ticket looks fixed, Agent 4 (per-ticket or bulk) re-crawls the URL to confirm
   before a human marks it Done.

## Modified/New Files

- `.env.example` — copy to `.env`; AI provider keys, model names (orchestration vs.
  explain-tier are separate), Azure DevOps org/PAT/tags, per-agent enable flags
  (`AGENT2_ENABLED`/`AGENT3_ENABLED`/`AGENT4_ENABLED`), WordPress Application Password for
  Agent 3's writes.
- `src/crawl_db.py` — `devops_tickets` table (dedup) and `issue_explanations` table
  (per-URL AI-explanation cache).
- `src/auth_db.py` — magic-link auth table.
- `src/email_service.py` — magic-link email sending.
- `src/agents/ticket_review_agent.py` — Agent 2, the review loop.
- `src/agents/fix_agent.py` — Agent 3, `FIX_MAP` + WordPress-object-resolution gating.
- `src/agents/qa_agent.py` — Agent 4, QA recheck.
- `src/agents/wordpress.py` — shared WordPress REST helpers (`resolve_wp_object`,
  `apply_fix`, `probe_site`), all routed through `url_safety.py`'s guarded HTTP calls.
- `src/mcp_server.py` — MCP tool definitions the agentic loop calls; self-calls
  `main.py`'s own routes over HTTP via a dedicated `requests.Session()`.
- `web/static/css/styles.css` — tab layout for varying screen sizes.
- `web/static/js/app.js` — Azure project/board selectors, dynamic instead of hardcoded.
- `web/static/js/plugin-loader.js` — tab-overflow fix on screen size changes.
- `web/static/plugins/page-diagnostics.js` — the plugin itself: issue checklist,
  agent-run UI, ticket creation, QA panel, token-usage panel, chat panel.
- `docker-compose.yml` — restricts self-hosted instances to the configured email domain.
- `main.py` — `CATEGORY_ROLE_MAP` (issue category → responsible role), the full
  `/api/agent/*` route family (workflow start/trigger/review/approval/results, bulk ticket
  creation, QA single/bulk, token usage, provider selection, chat), plus the original
  Azure project/feature/ticket endpoints.

## Setup

1. `cp .env.example .env`, fill in at minimum: an AI provider key (`OPENAI_API_KEY` or
   `ANTHROPIC_API_KEY`) and its model names, `AZURE_DEVOPS_ORG`/`AZURE_DEVOPS_PAT`/
   `AZURE_DEVOPS_SM_EMAIL`, `AZURE_QA_STATE`/`AZURE_QA_DONE_STATE`/`AZURE_QA_TAG` matching
   your Azure process's real state/tag names, and `WP_USERNAME`/`WP_APP_PASSWORD` if you
   want Agent 3 to actually write fixes.
2. `docker compose up -d` (see main `README.md` for the non-Docker path).
3. Project and board must be selected in the plugin **before** crawling a site, or ticket
   creation will reject with "no project selected."
4. Agent 3/4 require a real WordPress REST-accessible site; without WP credentials, Agent 3
   still runs but every fix defers with a clear reason instead of erroring.

## Known Limitations (as of this doc)

- Canonical URL fixes write correctly but don't always render — a RankMath sitewide
  quirk, not an Agent 3 bug (escalated, not fixed in this codebase).
- Plugin-bundled/lazy-loaded images that never register in the WordPress Media Library
  can't get alt text fixed — no code fix possible, needs manual sourcing.
- Custom post types (event calendars, portfolios) aren't resolved by
  `resolve_wp_object()` — only core posts/pages — so tickets on those pages defer.
- Workflow state (`_agent_state` in `main.py`) is a single in-process dict, not
  per-session — built for one operator at a time; concurrent use by multiple people is
  untested and likely to cross-contaminate an in-flight review.

## Conclusion

The open-source crawler is extended into a full technical-SEO-to-fix pipeline: crawl →
AI-reviewed ticket creation → automated WordPress fixes → automated QA recheck, gated by a
human approval step before anything is written anywhere. Further SEO extensions (keyword
research, rank tracking, competitor analysis) remain a roadmap item, not yet started.
