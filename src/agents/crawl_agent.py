"""
Agent 1 — Crawl.
Triggered by: supervisor clicking "Run Agent" in the browser UI.
Input: target URL from the workflow trigger. Output: full list of crawl issues passed
to Agent 2 for triage.
Does NOT create tickets or apply fixes — discovery only.
"""
import time
from src.mcp_server import (
    poll_workflow_trigger,   # GET /api/agent/workflow_trigger
    start_crawl,             # POST /api/start_crawl
    poll_crawl_status,       # GET /api/crawl_status
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

