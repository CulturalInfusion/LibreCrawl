import os
from dotenv import load_dotenv
load_dotenv()

# Reasoning-quality models for the agentic loop.
ANTHROPIC_MODEL = os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6')
OPENAI_MODEL    = os.getenv('OPENAI_MODEL', 'gpt-4o')

_provider_override = None

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

def call_with_tools(messages, tools, system=""):
    """
    Send a message + tool list to whichever provider is configured.
    Tools must be in Anthropic format — OpenAI conversion is handled here.

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
        return stop, text, tool_calls, None

    return 'end_turn', '', [], None
