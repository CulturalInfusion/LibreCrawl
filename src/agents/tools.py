"""
Tool schemas for the triage agent.

Design principle: no tool requires parameters. All data flow is handled
by Python's dispatch layer, not by the LLM passing data between calls.
The LLM's only job is to call tools in the right sequence.
"""

TRIAGE_TOOLS = [
    {
        "name": "select_issues",
        "description": (
            "Analyses all crawl issues and explains the high and medium priority ones. "
            "Skips low-priority and info-type items to save tokens. "
            "Call this first — no parameters needed. "
            "Returns the count of issues ready for review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "post_triage_results",
        "description": (
            "Sends the explained issue list to the browser so the human can review "
            "and select which ones to ticket. Call this after select_issues — "
            "no parameters needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "poll_approval",
        "description": (
            "Checks whether the human has approved the issue list in the browser. "
            "Returns success=true when the user clicks Approve. "
            "Call this repeatedly until success=true — no parameters needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "create_bulk_tickets",
        "description": (
            "Creates Azure DevOps tickets for all approved issues. "
            "Call this once after poll_approval returns success=true — "
            "no parameters needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
]
