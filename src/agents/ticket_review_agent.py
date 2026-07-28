"""
Agent 2 — Ticket Review.
Triggered by: Agent 1 completing a crawl and posting issues to the workflow queue.
Input: list of crawl issues. Output: Azure DevOps tickets for human-approved issues.
Flow: explain issues (parallel AI calls) → post to browser for review → poll for
human approval → create tickets for approved items.
Does NOT apply any fixes — that is Agent 3's job.
"""
import time
import json
from src.mcp_server import (
    poll_workflow_trigger,
    poll_crawl_status,
    explain_issue,
    post_review_results,
    poll_approval,
    create_bulk_tickets,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.agents.provider import call_with_tools, get_provider
from src.agents.tools    import REVIEW_TOOLS
from src.agents.prompts  import REVIEW_AGENT_SYSTEM_PROMPT
from src.crawl_db        import get_cached_explanations, save_issue_explanation
from src.agents.wordpress import resolve_wp_object
from src.agents.fix_agent import WP_OBJECT_REQUIRED_ISSUES

POLL_INTERVAL = 3  # seconds between polling calls


def execute_tool(name, inputs):
    """Dispatches an LLM tool call to the matching mcp_server function."""
    if name == 'explain_issue':
        return explain_issue(
            inputs['url'],
            inputs['issue'],
            inputs['category'],
            inputs['details']
        )
    if name == 'post_review_results':
        return post_review_results(inputs['issues'])
    if name == 'poll_approval':
        return poll_approval()
    if name == 'create_bulk_tickets':
        return create_bulk_tickets(inputs['approved'])

    return {'error': f'Unknown tool: {name}'}

def explain(issue):
    explanation = explain_issue(issue['url'], issue['issue'], issue['category'], issue['details'])
    return {**issue, **explanation}

def review_issues(issues):
    """Check the per-URL explanation cache first, then call explain_issue once per unique
    (issue, category) pair among the cache misses, then apply to all matching cache-miss
    issues. Cache is strictly per (url, issue) — never shared across different URLs, even
    ones with the same issue type, so a cached explanation stays specific to the URL that
    actually needs the fix (see src/crawl_db.py's issue_explanations table).
    Returns list of dicts: {url, issue, category, type, priority, explanation, how_to_fix, role}
    Sort order: errors first, then warnings. Within each group, 'high' priority before 'medium'/'low'.
    """
    filtered = [i for i in issues if i['type'] != 'info']
    if not filtered:
        return []

    cached = get_cached_explanations(
        [{'url': i['url'], 'issue': i['issue']} for i in filtered]
    )
    to_explain = [i for i in filtered if (i['url'], i['issue']) not in cached]

    # WP-resolvability pre-check — free/zero-token REST, only for issue types whose fix
    # actually needs an editable WordPress post/page (WP_OBJECT_REQUIRED_ISSUES, defined
    # in fix_agent.py — the same set Agent 3 already gates real fixes behind). Never
    # applied to anything else: a Broken Image or Slow Response Time issue on a
    # non-resolvable page is still a real, human-actionable issue that has nothing to do
    # with whether a WP object exists — those issue types are deferred via a completely
    # separate mechanism (fix_agent.py's DEFER_REASONS/DEFER_PATTERNS) and must never be
    # filtered here.
    needs_wp_check  = [i for i in to_explain if i['issue'] in WP_OBJECT_REQUIRED_ISSUES]
    no_check_needed = [i for i in to_explain if i['issue'] not in WP_OBJECT_REQUIRED_ISSUES]

    resolvable_urls = {}
    if needs_wp_check:
        unique_urls = {i['url'] for i in needs_wp_check}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(resolve_wp_object, url): url for url in unique_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    resolvable_urls[url] = future.result() is not None
                except Exception as e:
                    print(f"resolve_wp_object error for {url}: {e}")
                    # Fails open — an inconclusive check shouldn't silently drop a real
                    # issue; worst case is one avoidable explain_issue call, same as
                    # before this check existed.
                    resolvable_urls[url] = True

    not_resolvable_keys = {
        (i['url'], i['issue']) for i in needs_wp_check if not resolvable_urls.get(i['url'], True)
    }
    resolvable = [i for i in needs_wp_check if (i['url'], i['issue']) not in not_resolvable_keys]

    # Resolvable WP-required issues rejoin the normal pool so a resolvable URL can still
    # become its (issue, category) group's representative, even if a non-resolvable one
    # for the same issue type appeared earlier in the list.
    to_explain = no_check_needed + resolvable

    # Collect one representative per unique (issue, category) among cache misses only —
    # explanation is type-level for generation purposes (still cheaper to generate once per
    # crawl for issues sharing an (issue, category) pair), but gets cached per-URL below so
    # future crawls only ever reuse an explanation for the exact URL it was written for.
    unique_pairs = {}
    for i in to_explain:
        key = (i['issue'], i['category'])
        if key not in unique_pairs:
            unique_pairs[key] = i

    explanations = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                explain_issue,
                rep['url'], rep['issue'], rep['category'], rep['details']
            ): key
            for key, rep in unique_pairs.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                explanations[key] = future.result()
            except Exception as e:
                print(f"explain_issue error for {key}: {e}")
                explanations[key] = {}

    ranked = []
    for issue in filtered:
        cache_key = (issue['url'], issue['issue'])
        if cache_key in not_resolvable_keys:
            # Structural answer, not a generated one — nothing to cache, no AI call spent.
            merged = {**issue, 'wp_resolvable': False,
                      'explanation': ("This URL doesn't correspond to an editable WordPress "
                                      "page — likely an archive, category, or dynamically-"
                                      "generated page. Agent 3 won't be able to apply this fix."),
                      'how_to_fix': '', 'priority': issue.get('priority', 'low')}
        elif cache_key in cached:
            merged = {**issue, **cached[cache_key]}
        else:
            result = explanations.get((issue['issue'], issue['category']), {})
            merged = {**issue, **result}
            if result.get('explanation'):
                save_issue_explanation(
                    issue['url'], issue['issue'], issue['category'],
                    result.get('explanation'), result.get('how_to_fix'),
                    result.get('priority'), result.get('role')
                )
        ranked.append(merged)

    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    ranked = sorted(ranked, key=lambda i: priority_order.get(i.get('priority', 'low'), 2))
    return ranked


def wait_for_approval():
    """Poll until the browser posts the human's approval decision.
    Returns the approved list of issue dicts.
    Reference: main.py GET /api/agent/approval (Route 6)
    """
    while True:
        result = poll_approval()
        if result.get("success"):
            return result.get("approval", [])
        time.sleep(POLL_INTERVAL)


def create_tickets(approved):
    """Send approved issues to bulk ticket creation.
    Returns {created: [...], errors: [...]}.
    Reference: main.py POST /api/agent/create_bulk_tickets (Route 7)
    project and feature come from _agent_state on the Flask side — no need to pass them here.
    """

    return create_bulk_tickets(approved)


def run(issues):
    """Main agent loop. Runs once per workflow trigger."""
    ranked = review_issues(issues)
    print(f"[Agent] Review complete. Posting digest ({len(ranked)} issues).")
    post_review_results(ranked)

    print("[Agent] Waiting for human approval...")
    approved = wait_for_approval()
    print(f"[Agent] {len(approved)} issues approved. Creating tickets...")

    result = create_tickets(approved)
    print(f"[Agent] Done. Created: {len(result.get('created', []))}, Errors: {len(result.get('errors', []))}")


def run_agentic(issues):
    """
    Agentic version of run(). The LLM orchestrates the sequence by calling
    tools in order. Python handles all data flow — the LLM passes no parameters.

    Falls back to run() if no AI provider is configured.
    """
    if not issues:
        print("[Agent] No issues to review.")
        return

    if not get_provider():
        print("[Agent] No AI provider configured — falling back to pipeline run().")
        return run(issues)

    # System prompt built in src/agents/prompts.py — see REVIEW_AGENT_SYSTEM_PROMPT
    system = REVIEW_AGENT_SYSTEM_PROMPT

    messages = [{
        "role": "user",
        "content": f"There are {len(issues)} SEO issues ready for review. Start the workflow."
    }]

    print(f"[Agent] Starting agentic loop ({len(issues)} issues, provider: {get_provider()}).")

    provider       = get_provider()
    explained      = []   # filled by select_issues, consumed by post_review_results
    approval_cache = []   # filled by poll_approval, consumed by create_bulk_tickets

    def dispatch(name, _inputs):
        """Routes each tool call. All data flows through Python — LLM passes nothing."""
        if name == 'select_issues':
            # Filter out issues that already have Azure tickets in the database.
            # Prevents the agent from re-offering issues the team has already actioned.
            new_issues = issues
            skipped    = 0
            try:
                from src.mcp_server import http_session, BASE_URL
                pairs = [{'url': i['url'], 'issue': i['issue']} for i in issues]
                resp  = http_session.post(
                    f"{BASE_URL}/api/devops_tickets/check",
                    json={'pairs': pairs}
                )
                resp.raise_for_status()
                existing   = {(t['url'], t['issue']) for t in resp.json().get('tickets', [])}
                new_issues = [i for i in issues if (i['url'], i['issue']) not in existing]
                skipped    = len(issues) - len(new_issues)
                if skipped:
                    print(f"[Agent] Skipping {skipped} issues already ticketed in Azure.")
            except Exception as e:
                print(f"[Agent] Could not check existing tickets ({e}) — processing all issues.")

            print(f"[Agent] Explaining {len(new_issues)} issues in parallel...")
            result = review_issues(new_issues)
            explained[:] = result
            print(f"[Agent] {len(result)} issues explained.")
            return {'status': 'ready', 'count': len(result), 'skipped_existing': skipped}

        if name == 'post_review_results':
            print(f"[Agent] Posting {len(explained)} issues for review.")
            return post_review_results(explained)

        if name == 'poll_approval':
            result = poll_approval()
            if result.get('success'):
                approval_cache[:] = result.get('approval', [])
            else:
                time.sleep(POLL_INTERVAL)
            return result

        if name == 'create_bulk_tickets':
            print(f"[Agent] Creating {len(approval_cache)} tickets...")
            return create_bulk_tickets(approval_cache)

        return {'error': f'Unknown tool: {name}'}

    while True:
        stop, text, tool_calls, raw_content = call_with_tools(messages, REVIEW_TOOLS, system)

        if text:
            print(f"[Agent] {text}")

        if stop == 'end_turn' or not tool_calls:
            print("[Agent] Loop complete.")
            break

        if provider == 'anthropic':
            messages.append({"role": "assistant", "content": raw_content})
        else:
            messages.append({"role": "assistant", "content": text or None, "tool_calls": tool_calls})

        tool_results = []
        for call in tool_calls:
            if provider == 'anthropic':
                name    = call.name
                inputs  = call.input
                call_id = call.id
            else:
                name    = call.function.name
                inputs  = json.loads(call.function.arguments)
                call_id = call.id

            print(f"[Agent] → {name}")
            try:
                result = dispatch(name, inputs)
            except Exception as e:
                result = {'error': str(e)}
                print(f"[Agent] Tool error: {e}")
            tool_results.append((call_id, result))

        if provider == 'anthropic':
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": cid, "content": json.dumps(res)}
                    for cid, res in tool_results
                ]
            })
        else:
            for cid, res in tool_results:
                messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(res)})


if __name__ == "__main__":
    trigger = poll_workflow_trigger()  # Get the URL and context from the workflow trigger
    issues = trigger.get('issues', [])
    run(issues)
