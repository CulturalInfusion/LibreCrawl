# Agents

Four agents run in sequence as part of the SEO audit workflow. Each agent has a single responsibility and hands off to the next.

| File | Agent | Role | Entry point |
|------|-------|------|-------------|
| `crawl_agent.py` | Agent 1 — Crawl | Crawls the target site and returns all detected SEO issues | `run_crawl(url)` |
| `ticket_review_agent.py` | Agent 2 — Ticket Review | Explains issues via AI, presents them for human approval, creates Azure DevOps tickets | `run()` / `run_agentic()` |
| `fix_agent.py` | Agent 3 — Fix | Applies automated SEO fixes to the WordPress site via WP REST API | `run_fix(ticket)` |
| `qa_agent.py` | Agent 4 — QA | Re-crawls fixed URLs to verify the issue is resolved; drafts a comment if it persists | `check_ticket(project, ticket_id)` |

## Supporting files

| File | Purpose |
|------|---------|
| `../mcp_server.py` | HTTP bridge between agents and the Flask app — all four agents import from here to start crawls, post triage results, poll approval, and create tickets. This is the communication layer; agents never call Flask routes directly. |
| `provider.py` | LLM abstraction — supports Anthropic and OpenAI; unified `call_with_tools()` interface |
| `tools.py` | Tool schemas for Agent 2's agentic mode (no-parameter design; Python handles all data flow) |
| `wordpress.py` | WordPress REST API client used by Agent 3 — resolves posts/pages, applies RankMath meta, manages plugins, creates redirects |

## Workflow

```
Agent 1 (Crawl)
  └─► issues list
        └─► Agent 2 (Ticket Review)
              └─► human approves subset in browser
                    └─► Azure DevOps tickets created
                          └─► Agent 3 (Fix) — per ticket
                                └─► Agent 4 (QA) — per ticket
```

## Environment flags

Agents can be enabled/disabled without code changes via `.env`:

```env
AGENT2_ENABLED=true
AGENT3_ENABLED=true
AGENT4_ENABLED=true
AZURE_QA_TAG=qa-agent  
```

## Agent 3 prerequisites

- RankMath plugin must be installed and active on the target WordPress site for most fix types
- `WP_CLI_ENABLED=false` on the current dev site (`ci-dev.xyz`) — fixes go through WP REST API only
- See `fix_agent.py` `FIX_MAP` and `DEFER_REASONS` for the full list of what can and cannot be automated
