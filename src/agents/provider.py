import os

# Reasoning-quality models for the agentic loop.
# These are deliberately higher-tier than explain_issue (Haiku/GPT-3.5)
# because the loop needs to make multi-step decisions, not just generate text.
ANTHROPIC_MODEL = 'claude-sonnet-4-6'
OPENAI_MODEL    = 'gpt-4o'

def get_provider():
    """Returns which AI provider is configured based on available API keys."""
    if os.getenv('ANTHROPIC_API_KEY'):
        return 'anthropic'
    if os.getenv('OPENAI_API_KEY'):
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
        client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=4096,
        )
        tool_calls = [b for b in resp.content if b.type == 'tool_use']
        text       = next((b.text for b in resp.content if b.type == 'text'), '')
        return resp.stop_reason, text, tool_calls, resp.content

    elif provider == 'openai':
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

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
            tools=oai_tools,
            messages=msgs,
            max_tokens=4096,
        )
        choice     = resp.choices[0]
        tool_calls = choice.message.tool_calls or []
        text       = choice.message.content or ''
        stop       = 'tool_use' if choice.finish_reason == 'tool_calls' else 'end_turn'
        return stop, text, tool_calls, None

    return 'end_turn', '', [], None
