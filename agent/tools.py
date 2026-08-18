"""Tool (function-calling) definitions offered to the discovery LLM.

Deliberately role+name based, not coordinates: the model always refers to an
element the way a person would describe it ("the Search button"), never a
pixel position. That's what makes the resulting recording meaningful on a
DOM that has no stable selectors, and it's what core.locators resolves
against on replay.
"""
from __future__ import annotations

TOOLS = [
    {
        "name": "click",
        "description": "Click a button, link, or other clickable control, identified by its "
        "accessibility role and accessible name exactly as shown in the current 'Interactive "
        "elements' listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "e.g. 'button', 'link'"},
                "name": {"type": "string", "description": "The accessible name/visible text, exact."},
                "expect_text": {
                    "type": ["string", "null"],
                    "description": "Text you expect to newly appear on the page after this click, "
                    "confirming it worked (used as this step's checkpoint). Null if not applicable.",
                },
                "reasoning": {"type": "string", "description": "Why this action, briefly."},
            },
            "required": ["role", "name", "expect_text", "reasoning"],
        },
    },
    {
        "name": "fill",
        "description": "Type text into a textbox, identified by its accessibility role and accessible "
        "name (usually its label).",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "text": {"type": "string", "description": "The text to type."},
                "parameter_name": {
                    "type": ["string", "null"],
                    "description": "If this value should become a typed input parameter of the "
                    "resulting reusable capability (e.g. a member ID the caller supplies), give it a "
                    "snake_case name here, e.g. 'member_id'. Null if this value is always the same "
                    "every time this capability runs.",
                },
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "text", "parameter_name", "reasoning"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a dropdown/select, identified by its accessibility role "
        "and accessible name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "option": {"type": "string", "description": "The visible option text to select."},
                "parameter_name": {
                    "type": ["string", "null"],
                    "description": "Same convention as fill's parameter_name.",
                },
                "expect_text": {"type": ["string", "null"]},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "option", "parameter_name", "expect_text", "reasoning"],
        },
    },
    {
        "name": "navigate",
        "description": "Go directly to a URL. Only use this for the very first step (the entry point) "
        "or if you have no other way to reach a required page -- prefer clicking real links/buttons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "expect_text": {"type": ["string", "null"]},
                "reasoning": {"type": "string"},
            },
            "required": ["url", "expect_text", "reasoning"],
        },
    },
    {
        "name": "extract",
        "description": "Read a value off the current page to return as a typed output of the "
        "resulting capability, e.g. a balance or a generated reference number. Give the exact label "
        "text of the row/field the value belongs to (e.g. 'Savings Balance', 'Reference Number') as "
        "shown in the visible page text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "output_name": {"type": "string", "description": "snake_case output name, e.g. 'savings_balance'."},
                "output_type": {"type": "string", "enum": ["string", "number", "boolean"]},
                "reasoning": {"type": "string"},
            },
            "required": ["label", "output_name", "output_type", "reasoning"],
        },
    },
    {
        "name": "finish",
        "description": "Call this once the goal has been fully achieved and verified on the page. "
        "Ends the run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "summary": {"type": "string", "description": "One or two sentences on what was accomplished."},
                "checkpoint_text": {
                    "type": "string",
                    "description": "Exact text currently visible on the page that proves the goal was "
                    "reached (this becomes the capability's success checkpoint for replay).",
                },
            },
            "required": ["success", "summary", "checkpoint_text"],
        },
    },
    {
        "name": "escalate",
        "description": "Call this if you are stuck and cannot safely proceed: you don't recognize the "
        "page state, an action you need isn't available, you're blocked by something outside your "
        "permissions, or you've made no progress after several attempts. This pauses the run and asks "
        "a human operator to take over.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "What's wrong and what you need help with."},
            },
            "required": ["reason"],
        },
    },
]
