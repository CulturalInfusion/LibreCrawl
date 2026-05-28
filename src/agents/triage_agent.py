import time
from src.mcp_server import (
    poll_workflow_trigger,   # GET /api/agent/workflow_trigger
    start_crawl,             # POST /api/start_crawl
    poll_crawl_status,       # GET /api/crawl_status
    explain_issue,           # POST /api/explain_issue
    post_triage_results,     # POST /api/agent/triage      ← new tool you added
    poll_approval,           # GET /api/agent/approval     ← new tool you added
    create_bulk_tickets,     # POST /api/agent/create_bulk_tickets ← new tool you added
)

POLL_INTERVAL = 3  # seconds between polling calls


def wait_for_trigger():
    """Poll until the browser clicks 'Run Agent Analysis'.
    Returns {url, project, feature} when ready.
    Reference: main.py GET /api/agent/workflow_trigger (Route 2)
    """
    while True:
        result = poll_workflow_trigger()
        if result.get("ready"):
            return result
        time.sleep(POLL_INTERVAL)


def run_crawl(url):
    """Start a crawl and block until it completes.
    Returns the full list of issues from the crawl.
    Reference: main.py POST /api/start_crawl (line 750), GET /api/crawl_status (line 813)
    Crawl status values to handle: 'idle', 'running', 'completed'
    """
    start_crawl(url)

    while True:
        status = poll_crawl_status()
        if status['status'] == 'completed':
            return status['issues']
        time.sleep(POLL_INTERVAL)


def triage_issues(issues):
    """Call explain_issue for every non-info issue, sort by severity, return ranked list.
    Returns list of dicts: {url, issue, category, type, priority, explanation, how_to_fix, role}
    Reference: main.py POST /api/explain_issue (line 1528) for response shape
    WordPress constraint: explain_issue prompt already handles this server-side.
    Sort order: errors first, then warnings. Within each group, 'high' priority before 'medium'/'low'.
    """
    ranked = []

    for issue in issues:
        if issue['type'] == 'info':
            continue  # skip info-level issues
        explanation = explain_issue(issue['url'], issue['issue'], issue['category'], issue['details'])
        ranked.append({**issue, **explanation})
    
    ranked = sorted(ranked, key=lambda issue: (issue['type'] != 'error', issue['priority'] != 'high'))
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


def run():
    """Main agent loop. Runs once per workflow trigger."""
    print("[Agent] Waiting for workflow trigger...")
    trigger = wait_for_trigger()
    url = trigger["url"]
    print(f"[Agent] Trigger received. Crawling {url}")

    issues = run_crawl(url)
    print(f"[Agent] Crawl complete. {len(issues)} issues found.")

    ranked = triage_issues(issues)
    print(f"[Agent] Triage complete. Posting digest ({len(ranked)} issues).")
    post_triage_results(ranked)

    print("[Agent] Waiting for human approval...")
    approved = wait_for_approval()
    print(f"[Agent] {len(approved)} issues approved. Creating tickets...")

    result = create_tickets(approved)
    print(f"[Agent] Done. Created: {len(result.get('created', []))}, Errors: {len(result.get('errors', []))}")


if __name__ == "__main__":
    run()
