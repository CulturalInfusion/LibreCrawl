"""
Centralized LLM prompts and system messages for LibreCrawl's AI agents.

This module has no internal imports; it is a leaf
module so main.py, ticket_review_agent.py, and fix_agent.py can all import
from it with no risk of an import cycle.
"""


# ---------------------------------------------------------------------------
# explain_issue() — Flask route in main.py, POST /api/explain_issue
#
# Used by: main.py's explain_issue() route directly, and indirectly by
# src/agents/qa_agent.py's _draft_fix_suggestion(), which calls that route
# over HTTP rather than duplicating the prompt.
#
# Pattern: build_explain_issue_prompt() interpolates url/issue/category/
# details/context_str into a single user message. 
# Output must be JSON with exactly the keys explanation/
# how_to_fix/priority/role — main.py's route parses those keys directly, so
# they cannot be renamed without updating the route's response parsing.
# ---------------------------------------------------------------------------

def build_explain_issue_prompt(url, issue, category, details, context_str):
    return f"""You are an SEO expert providing actionable advice. Analyze this specific issue:

URL: {url}
Issue: {issue}
Category: {category}
Details: {details}

Page Context:
{context_str}

Provide a JSON response with exactly this structure:
{{
    "explanation": "2-3 sentence explanation of why this issue matters for SEO and user experience",
    "how_to_fix": "Step-by-step fix instructions (3-5 bullet points, use markdown formatting with • for bullets)",
    "priority": "high/medium/low based on SEO impact",
    "role": "Most suitable role from this list only: Webmaster (SEO config, robots.txt, sitemaps, schema markup, indexability), Copywriter / Content Editor (written content, meta descriptions, social tags, OG metadata), Web Developer (code, performance, accessibility implementation, responsive design, ARIA), Designer (visual design, color contrast, typography, layout, UX)"
}}

Keep the explanation concise but specific to this URL. Use SEMRush-style actionable language."""


EXPLAIN_ISSUE_SYSTEM_PROMPT = (
    'You are an SEO expert, specialized in WordPress websites. The URLs you crawl are '
    'WordPress sites so all fix guidance must reference WordPress-specific tooling: '
    'wp-admin, plugins, themes. Do not give generic CMS advice. Always respond with valid JSON.'
)


# ---------------------------------------------------------------------------
# agent_chat() — Flask route in main.py, POST /api/agent/chat
#
# Used by: main.py's agent_chat() route only.
#
# Pattern: build_agent_chat_prompt() interpolates issue_lines/message into a
# single user message. AGENT_CHAT_SYSTEM_PROMPT was previously copy-pasted
# verbatim into both the anthropic and openai branches — deduped to one
# constant here since the two copies were never meant to diverge. The route
# regexes the reply for a `{"matches": [...]}` JSON block (see
# `re.search(r'\{\s*"matches"\s*:', text)` in agent_chat()) — that key name
# is load-bearing and must stay in sync between the prompt and the route's
# parser.
# ---------------------------------------------------------------------------

def build_agent_chat_prompt(issue_lines, message):
    return f"""You are helping a developer review SEO audit issues before creating tickets.

Issue list (issue | url | priority | type):
{issue_lines}

User request: {message}

First, write 1-2 sentences summarising what you found and listing the matched issue names.
Then output a JSON block with the exact issue name and url for each match:
{{"matches": [{{"issue": "exact issue name", "url": "exact url"}}]}}

If all issues should be shown, include them all. Copy issue names and URLs exactly as written above. Always include the JSON block."""


AGENT_CHAT_SYSTEM_PROMPT = 'You are an SEO review assistant. Copy issue names and URLs exactly as given. Always end your reply with a JSON block.'


# ---------------------------------------------------------------------------
# run_agentic() — src/agents/ticket_review_agent.py, the WordPress
# ticket-review agentic loop (select_issues -> post_review_results ->
# poll_approval -> create_bulk_tickets).
#
# Used by: ticket_review_agent.py's run_agentic() only, passed as the
# `system` argument to call_with_tools(messages, REVIEW_TOOLS, system).
# Pattern: static string, no interpolation, no JSON output — it drives a
# strict 4-step tool-calling sequence. The tool names named in step 2
# ("post_review_results") and elsewhere must stay byte-identical to the
# `name` fields registered in src/agents/tools.py's REVIEW_TOOLS and to the
# dispatch in ticket_review_agent.py's execute_tool()/dispatch() — those are
# real function identifiers the model calls by name, not prose.
# ---------------------------------------------------------------------------

REVIEW_AGENT_SYSTEM_PROMPT = (
    "You are an SEO review agent for a WordPress website. "
    "Call these four tools in order — none require parameters:\n"
    "1. select_issues — explains the crawl issues and prepares them for review\n"
    "2. post_review_results — sends the list to the browser for human approval\n"
    "3. poll_approval — a human is reviewing the issues in their browser and may take "
    "several minutes. Call this tool again every time it returns success=false, with "
    "no limit on how many attempts that takes. Never decide on your own that the human "
    "isn't responding, never give up, and never stop to report a problem at this step — "
    "the only valid way to leave step 3 is a poll_approval result with success=true.\n"
    "4. create_bulk_tickets — creates the approved tickets in Azure DevOps\n"
    "Do not pass any parameters to any tool. Just call them in sequence."
)


# ---------------------------------------------------------------------------
# decide_required_plugin() — src/agents/fix_agent.py
#
# Used by: fix_agent.py's decide_required_plugin() only, sent as a bare user
# message with no system prompt (there wasn't one before this move either).
#
# Pattern: interpolates issue/active_namespaces/candidates. Deliberately
# constrained to answer with one of the caller-supplied candidate slugs or
# the literal string 'none' — decide_required_plugin() treats any other
# answer as 'none' (fail-safe to Defer rather than risking an install off a
# hallucinated slug), so the prompt's "exactly as written, nothing else"
# instruction is load-bearing for that fail-safe to work as intended.
# ---------------------------------------------------------------------------

def build_plugin_decision_prompt(issue, active_namespaces, candidates):
    return (
        f"Issue: '{issue}'. Site's currently active plugin REST namespaces: {active_namespaces}.\n"
        f"Which plugin slug is needed to fix this issue? Valid answers, exactly as written, "
        f"nothing else: {candidates} or 'none'."
    )


# ---------------------------------------------------------------------------
# fix_title_core() — src/agents/fix_agent.py
#
# Used by: fix_agent.py's fix_title_core() only, sent as a bare user message
# with no system prompt (there wasn't one before this move either).
#
# Pattern: interpolates length/current_title/h1/body_excerpt. Expects plain
# title text only, no partial words — fix_title_core() takes the raw
# response, strips it, and truncates at a word boundary if it overshoots the
# computed character budget, so it depends on the model not wrapping the
# answer in quotes/preamble, per the prompt's explicit instruction.
# ---------------------------------------------------------------------------

def build_fix_title_prompt(length, current_title, h1, body_excerpt):
    return (
        f"Write a single SEO page title, {length}, for this webpage.\n"
        f"Current title (may be missing or the wrong length): {current_title}\n"
        f"Main heading: {h1}\nPage content excerpt: {body_excerpt}\n"
        f"Return only the title text, no quotes, no preamble, no partial words."
    )


# ---------------------------------------------------------------------------
# fix_meta_description() / fix_og_tags() / fix_twitter_tags() — src/agents/fix_agent.py
#
# Used by: fix_agent.py's fix_meta_description(), fix_og_tags(), fix_twitter_tags() —
# same prompt, different length budget, since a meta description/OG description/Twitter
# description are the same kind of text with different ideal lengths.
#
# Pattern: mirrors build_fix_title_prompt exactly (grounds the model in the page's
# actual title/h1/body content instead of letting it invent something ungrounded) —
# replaces what used to be a hardcoded "[Agent 3] Description for: <url>" placeholder
# written with no LLM call at all.
# ---------------------------------------------------------------------------

def build_fix_meta_description_prompt(length, title, h1, body_excerpt):
    return (
        f"Write a single SEO meta description, {length}, for this webpage.\n"
        f"Page title: {title}\nMain heading: {h1}\nPage content excerpt: {body_excerpt}\n"
        f"Return only the description text, no quotes, no preamble, no partial words."
    )


# ---------------------------------------------------------------------------
# fix_missing_h1() — src/agents/fix_agent.py
#
# Used by: fix_agent.py's fix_missing_h1() only, sent as a bare user message
# with no system prompt (same pattern as build_plugin_decision_prompt /
# build_fix_title_prompt).
#
# Pattern: interpolates the post's raw block content + page title. The model
# must return text EXACTLY as it appears in that raw content (or the literal
# string 'NONE') — fix_missing_h1() does an exact-match search for that text
# afterward and refuses to write anything unless it matches precisely once,
# so "verbatim, nothing paraphrased" is load-bearing for that safety check.
# ---------------------------------------------------------------------------

def build_fix_h1_prompt(raw_content, page_title):
    return (
        f"This WordPress page (title: \"{page_title}\") is missing a proper <h1> heading. "
        f"Below is its raw block content. Does any existing block's text read like it was "
        f"meant to be this page's main heading (e.g. the post title rendered as a lower-level "
        f"heading, or a short standalone line at the top functioning as one)?\n\n"
        f"Raw content:\n{raw_content}\n\n"
        f"If yes, return that block's text EXACTLY as it appears above — verbatim, no "
        f"paraphrasing, no added or removed punctuation, since it needs to match precisely "
        f"against the content afterward. If no clear candidate exists, return exactly: NONE"
    )


# ---------------------------------------------------------------------------
# fix_images_alt_text() — src/agents/fix_agent.py
#
# Used by: fix_agent.py's fix_images_alt_text() only, sent through
# call_with_vision() alongside the actual image bytes — this is the text half
# of a multimodal message, not a bare user message like the other fix_agent
# prompts in this file.
#
# Pattern: asks the model to classify decorative vs. descriptive before
# describing anything, since icons/spacers/dividers are correctly served with
# empty alt text (alt=""), not a generated caption — a vision call that always
# describes every image would produce confidently wrong alt text for those.
# The literal string 'DECORATIVE' is load-bearing: fix_images_alt_text() checks
# for that exact token (case-insensitive) to decide whether to write an empty
# string instead of the model's text, so it can't be rephrased without updating
# that check too.
# ---------------------------------------------------------------------------


def build_fix_alt_text_prompt(page_title):
    return (
        f"This image appears on a webpage titled \"{page_title}\".\n"
        f"First decide: is this image purely decorative (an icon, spacer, divider, or "
        f"background flourish with no informational content), or does it convey real "
        f"content a screen-reader user would need described?\n"
        f"If decorative, respond with exactly: DECORATIVE\n"
        f"Otherwise, write a concise, descriptive alt text (under 125 characters) describing "
        f"what the image shows, in plain language. Return only that text — no quotes, no preamble."
    )
