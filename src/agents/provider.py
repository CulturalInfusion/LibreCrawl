import os
import time
from dotenv import load_dotenv
load_dotenv()

# Reasoning-quality models for the agentic loop.
ANTHROPIC_MODEL = os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6')
OPENAI_MODEL    = os.getenv('OPENAI_MODEL', 'gpt-4o')

# Fast/cheap models for explain-style calls (per-issue explanations, HITL chat filtering).
ANTHROPIC_EXPLAIN_MODEL = os.getenv('ANTHROPIC_EXPLAIN_MODEL', 'claude-haiku-4-5-20251001')
OPENAI_EXPLAIN_MODEL    = os.getenv('OPENAI_EXPLAIN_MODEL', 'gpt-3.5-turbo')

_provider_override = None

# Token usage tracking. In-memory only — same resets-on-restart pattern as
# _agent_state in main.py, no new infra.
#
# _usage holds running totals per (agent, model) — the aggregate header shown
# above each agent panel's token log.
# _usage_log holds one entry per call, newest last — the scrollable per-issue/
# per-ticket detail underneath, mirroring what already prints to docker logs.
_usage = {}
_usage_log = []
_USAGE_LOG_MAX = int(os.getenv('USAGE_LOG_MAX', 500))  # cap so a long-running container doesn't grow this unbounded

def record_usage(agent, model, input_tokens, output_tokens, label=''):
    entry = _usage.setdefault((agent, model), {'calls': 0, 'input_tokens': 0, 'output_tokens': 0})
    entry['calls'] += 1
    entry['input_tokens'] += input_tokens
    entry['output_tokens'] += output_tokens

    _usage_log.append({
        'agent': agent, 'model': model, 'label': label,
        'input_tokens': input_tokens, 'output_tokens': output_tokens,
        'total_tokens': input_tokens + output_tokens,
        'ts': time.time(),
    })
    if len(_usage_log) > _USAGE_LOG_MAX:
        del _usage_log[:len(_usage_log) - _USAGE_LOG_MAX]

def _as_agent_set(agent):
    """agent may be a single name, a list/tuple/set of names, or None (= all agents)."""
    if agent is None:
        return None
    return set(agent) if isinstance(agent, (list, tuple, set)) else {agent}

def get_usage_summary(agent=None):
    agents = _as_agent_set(agent)
    return [
        {
            'agent': a, 'model': model, 'calls': v['calls'],
            'input_tokens': v['input_tokens'], 'output_tokens': v['output_tokens'],
            'total_tokens': v['input_tokens'] + v['output_tokens'],
        }
        for (a, model), v in _usage.items()
        if agents is None or a in agents
    ]

def get_usage_log(agent=None, limit=200):
    """Most recent first — what a scrollable log panel wants to render top-down."""
    agents = _as_agent_set(agent)
    log = [e for e in _usage_log if agents is None or e['agent'] in agents]
    return list(reversed(log[-limit:]))

def _env(name):
    return (os.getenv(name) or '').strip()

def set_provider_override(provider):
    global _provider_override
    _provider_override = provider

def get_provider():
    if _provider_override:
        return _provider_override
    if _env('ANTHROPIC_API_KEY'):
        return 'anthropic'
    if _env('OPENAI_API_KEY'):
        return 'openai'
    return None

def call_with_tools(messages, tools, system="", agent="unknown", label=""):
    """
    Send a message + tool list to whichever provider is configured.
    Tools must be in Anthropic format — OpenAI conversion is handled here.

    agent/label identify the caller for token-usage tracking (e.g. agent="agent3",
    label="Title Too Long — https://...") — see record_usage/get_usage_summary above.

    Returns: (stop_reason, text, tool_calls, raw_content)
      stop_reason  'tool_use' if the model wants to call a tool, 'end_turn' if done
      text         any prose the model wrote before requesting a tool call
      tool_calls   list of tool call objects (shape differs by provider — see execute_tool)
      raw_content  Anthropic: resp.content list needed to reconstruct assistant messages
                   OpenAI: None (tool_calls list is sufficient)
    """
    provider = get_provider()

    if provider == 'anthropic':
        from anthropic import Anthropic
        client = Anthropic(api_key=_env('ANTHROPIC_API_KEY'))
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            system=system,
            messages=messages,
            max_tokens=4096,
            **({'tools': tools} if tools else {}),
        )
        tool_calls = [b for b in resp.content if b.type == 'tool_use']
        text       = next((b.text for b in resp.content if b.type == 'text'), '')
        record_usage(agent, ANTHROPIC_MODEL, resp.usage.input_tokens, resp.usage.output_tokens, label)
        return resp.stop_reason, text, tool_calls, resp.content

    elif provider == 'openai':
        from openai import OpenAI
        client = OpenAI(api_key=_env('OPENAI_API_KEY'))

        # Anthropic and OpenAI use the same JSON Schema for parameters,
        # but wrap them differently at the outer level.
        oai_tools = [{
            'type': 'function',
            'function': {
                'name':        t['name'],
                'description': t['description'],
                'parameters':  t['input_schema']
            }
        } for t in tools]

        msgs = ([{'role': 'system', 'content': system}] + messages) if system else messages
        resp  = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            max_tokens=4096,
            **({'tools': oai_tools} if oai_tools else {}),
        )
        choice     = resp.choices[0]
        tool_calls = choice.message.tool_calls or []
        text       = choice.message.content or ''
        stop       = 'tool_use' if choice.finish_reason == 'tool_calls' else 'end_turn'
        record_usage(agent, OPENAI_MODEL, resp.usage.prompt_tokens, resp.usage.completion_tokens, label)
        return stop, text, tool_calls, None

    return 'end_turn', '', [], None
