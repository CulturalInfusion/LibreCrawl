from mcp.server.fastmcp import FastMCP
import requests

BASE_URL = 'http://localhost:5000'  
mcp = FastMCP("LibreCrawl")

@mcp.tool()
def start_crawl(url: str) -> dict:
    """Starts a crawl for the given URL. Call poll_crawl_status after this until the crawl is complete."""
    response = requests.post(f"{BASE_URL}/api/start_crawl", json={"url": url})
    return response.json()

@mcp.tool()
def poll_crawl_status() -> dict:
    """Polls the status of an ongoing crawl. Returns status, urls[], issues[], and stats. Keep calling until status equals completed."""
    response = requests.get(f"{BASE_URL}/api/crawl_status")
    return response.json()

@mcp.tool()
def explain_issue(url: str, issue: str, category: str, details: str) -> dict:
    """Given an issue ID from the crawl results, returns a detailed explanation of the issue and how to fix it."""
    response = requests.post(f"{BASE_URL}/api/explain_issue/", json={
        "url": url,
        "issue": issue,
        "category": category,
        "details": details
    })
    return response.json()

@mcp.tool()
def check_existing_tickets(url: str, issue: str, category: str, details: str) -> dict:
    """Checks if there are existing tickets for the given issue ID. Returns a list of tickets if they exist."""
    response = requests.post(f"{BASE_URL}/api/devops_tickets/check", json={
        "url": url,
        "issue": issue,
        "category": category,
        "details": details
    })
    return response.json()

@mcp.tool()
def create_devops_ticket(url: str, issue: str, category: str, issue_type: str, ai_explanation: str, ai_how_to_fix: str, ai_priority: str, ai_role: str) -> dict:
    """Creates a new Azure DevOps ticket for the given issue ID with the provided title and description."""
    response = requests.post(f"{BASE_URL}/api/create_devops_ticket", json={
        "url": url,
        "issue": issue,
        "category": category,
        "issue_type": issue_type,
        "ai_explanation": ai_explanation,
        "ai_how_to_fix": ai_how_to_fix,
        "ai_priority": ai_priority,
        "ai_role": ai_role
    })
    return response.json()

@mcp.tool()
def probe_urls(issues: list) -> dict:
    """Probes a list of URLs to check their HTTP status codes and returns the results."""
    response = requests.post(f"{BASE_URL}/api/probe_http_errors", json={"urls": urls})
    return response.json()

if __name__ == "__main__":
    mcp.run()