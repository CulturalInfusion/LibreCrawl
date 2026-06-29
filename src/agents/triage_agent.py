import time
import json
from src.mcp_server import (
    poll_workflow_trigger,
    poll_crawl_status,
    explain_issue,
    post_triage_results,
    poll_approval,
    create_bulk_tickets,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.agents.provider import call_with_tools, get_provider
from src.agents.tools    import TRIAGE_TOOLS

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
    if name == 'post_triage_results':
        return post_triage_results(inputs['issues'])
    if name == 'poll_approval':
        return poll_approval()
    if name == 'create_bulk_tickets':
        return create_bulk_tickets(inputs['approved'])

    return {'error': f'Unknown tool: {name}'}

def explain(issue):
    explanation = explain_issue(issue['url'], issue['issue'], issue['category'], issue['details'])
    return {**issue, **explanation}

def triage_issues(issues):
    """Call explain_issue for every non-info issue, sort by severity, return ranked list.
    Returns list of dicts: {url, issue, category, type, priority, explanation, how_to_fix, role}
    Reference: main.py POST /api/explain_issue (line 1528) for response shape
    WordPress constraint: explain_issue prompt already handles this server-side.
    Sort order: errors first, then warnings. Within each group, 'high' priority before 'medium'/'low'.
    """
    ranked = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(explain, issue) for issue in issues if issue['type'] != 'info']
        for future in as_completed(futures):
            ranked.append(future.result())
    
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    ranked = sorted(ranked, key=lambda issue: priority_order.get(issue.get('priority', 'low'), 2))
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
    ranked = triage_issues(issues)
    print(f"[Agent] Triage complete. Posting digest ({len(ranked)} issues).")
    post_triage_results(ranked)

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
        print("[Agent] No issues to triage.")
        return

    if not get_provider():
        print("[Agent] No AI provider configured — falling back to pipeline run().")
        return run(issues)

    system = (
        "You are an SEO triage agent for a WordPress website. "
        "Call these four tools in order — none require parameters:\n"
        "1. select_issues — explains the crawl issues and prepares them for review\n"
        "2. post_triage_results — sends the list to the browser for human approval\n"
        "3. poll_approval — a human is reviewing the issues in their browser and may take "
        "several minutes. Call this tool again every time it returns success=false, with "
        "no limit on how many attempts that takes. Never decide on your own that the human "
        "isn't responding, never give up, and never stop to report a problem at this step — "
        "the only valid way to leave step 3 is a poll_approval result with success=true.\n"
        "4. create_bulk_tickets — creates the approved tickets in Azure DevOps\n"
        "Do not pass any parameters to any tool. Just call them in sequence."
    )

    messages = [{
        "role": "user",
        "content": f"There are {len(issues)} SEO issues ready for triage. Start the workflow."
    }]

    print(f"[Agent] Starting agentic loop ({len(issues)} issues, provider: {get_provider()}).")

    provider       = get_provider()
    explained      = []   # filled by select_issues, consumed by post_triage_results
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
            result = triage_issues(new_issues)
            explained[:] = result
            print(f"[Agent] {len(result)} issues explained.")
            return {'status': 'ready', 'count': len(result), 'skipped_existing': skipped}

        if name == 'post_triage_results':
            print(f"[Agent] Posting {len(explained)} issues for review.")
            return post_triage_results(explained)

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
        stop, text, tool_calls, raw_content = call_with_tools(messages, TRIAGE_TOOLS, system)

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
