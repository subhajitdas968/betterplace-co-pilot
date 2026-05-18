"""Claude analyzer. Builds the prompt, calls the API with prompt-caching, parses JSON output."""
from __future__ import annotations
import json
import re
import sqlite3
from typing import Any

from anthropic import Anthropic

from . import config, db


# Lazy client — we don't construct it until something actually needs the API.
# Reason: when uvicorn is launched without ANTHROPIC_API_KEY in env (the
# Claude-Code/MCP setup strips it on purpose so the AI worker uses OAuth), the
# new Anthropic SDK raises "Could not resolve authentication method" at
# construction time. Lazy init lets the AI worker run cleanly on OAuth while
# letting metered calls (translate, smart_reply, generate_doc) report a clean
# error if the user hasn't set the key.
_client_instance = None


def _get_client():
    global _client_instance
    if _client_instance is not None:
        return _client_instance
    key = getattr(config, "ANTHROPIC_API_KEY", None)
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in .env — this call needs the metered API. "
            "Either add ANTHROPIC_API_KEY to .env or use Claude Desktop / MCP path."
        )
    _client_instance = Anthropic(api_key=key)
    return _client_instance


# Keep the old `_client` name working for code that still references it directly.
class _LazyClientProxy:
    @property
    def messages(self):
        return _get_client().messages


_client = _LazyClientProxy()


def _claude_via_cli(system: str, user_prompt: str, *, max_tokens: int = 1500) -> dict:
    """Fallback path: invoke `claude -p` headless when the API key isn't set.
    Returns the same shape as _claude_call so callers don't need to branch."""
    import subprocess, shutil
    if not shutil.which("claude"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set and `claude` CLI not found on PATH. "
            "Either configure ANTHROPIC_API_KEY in .env, or "
            "`npm install -g @anthropic-ai/claude-code` so this falls back to the CLI."
        )
    prompt = system + "\n\n" + user_prompt
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--max-turns", "1",
             "--permission-mode", "bypassPermissions"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude CLI timed out after 120s: {e}")
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {r.returncode}): {(r.stderr or r.stdout)[:400]}")
    return {
        "text": r.stdout.strip(), "input_tokens": 0, "output_tokens": 0,
        "cached_input_tokens": 0, "cost_usd": 0.0,
        "model": "claude-code-cli",
    }


SYSTEM_TEMPLATE = """You are the BetterPlace Support Co-Pilot. You analyse Zendesk tickets from BetterPlace's
two support groups (Product Support and Managed Services) and produce structured insights for agents.

Your job:
1. Summarise the conversation crisply (3–5 sentences) capturing customer issue, agent action, current state.
2. Recommend corrections to ticket custom fields where the current values don't match the conversation.
3. Run a completeness check — flag missing or thin entries in mandatory fields, especially KB Article, Jira ID,
   Bucketization (mandatory for Reliance), Root Cause L1/L2, and "How was this ticket resolved?".
4. If the ticket is solved/closed and the resolution describes a recurring failure mode, mark it KB-worthy.
5. If the ticket has no agent reply yet AND age > 4h AND assignee is null, set a pickup_flag.
6. Detect Outlook recall messages (subject starts with "Recall:") — recommend auto-merge.
7. If at least one agent reply exists in the conversation, evaluate it. Produce a suggested_reply object whenever
   ANY of these is true: (a) the reply is shorter than ~25 words, (b) Issue ticket reply doesn't state the root
   cause, (c) Task ticket reply doesn't describe the specific action taken (which entity, which script, which
   field changed), (d) Training ticket reply doesn't list steps. The suggested_reply structure is:
     {"flag": "short reason e.g. 'reply incomplete'",
      "flaws": ["specific gap 1", "specific gap 2"],
      "current": "the agent's most recent reply, verbatim",
      "suggested": "a better reply that fixes the flaws — keep the agent's tone but add what's missing"}
   If the reply is genuinely complete (root cause stated, action described, scope clear), set suggested_reply to null.

Field-tagging rules:
- "Issue" Root Cause L1 = something is broken/wrong (e.g. data quality, system bug). Use when the ticket says
  something is incorrect, failing, missing, or asks for corrections.
- "Task" Root Cause L1 = a routine request (provision access, run a script, deploy a profile). Use when the
  ticket describes a normal operational ask.
- Bucketization is MANDATORY when Customer Name matches Reliance / Reliance::O2C / similar. If empty on a
  Reliance ticket, flag it. Pick from the dropdown options provided.
- For text fields ("How was this ticket resolved?", "What was the issue?"), draft a structured note that
  includes (a) the action taken, (b) the entity affected, (c) verification done, (d) any known cause.
- "How was this ticket resolved?" should NEVER restate the problem. It should describe what was done.

Constraint: prefer values that exist in the dropdown option list. ONLY when no existing
option fits the conversation accurately, you may propose a NEW option:
- Set `propose_new_option: true`
- Put the new value name in `suggest`
- In `reason`, explain why no existing option fits and what the new value should mean

Output: ONLY valid JSON matching this schema. No prose, no markdown fences.
{
  "summary": "string · 3–5 sentences",
  "recommendations": [
    {"field": "string", "current": "string|null", "suggest": "string", "confidence": 0.85,
     "reason": "string · 1–2 sentences", "review": false, "propose_new_option": false}
  ],
  "completeness": [
    {"state": "ok|miss|thin", "text": "string", "hint": "optional string"}
  ],
  "similar_ticket_keys": ["string topic keys, e.g. reliance.o2c.site_attribute_modification"],
  "kb_worthy": false,
  "kb_topic": "string or null",
  "pickup_flag": null,
  "suggested_reply": null
}
"""


def build_taxonomy_block(c: sqlite3.Connection, scope_fields: list[str]) -> str:
    """Build the field taxonomy section of the prompt — this is what we cache."""
    rows = c.execute("SELECT id, title, type, options FROM ticket_fields").fetchall()
    by_title = {r["title"]: r for r in rows}
    parts = ["## Custom field taxonomy"]
    for title in scope_fields:
        r = by_title.get(title)
        if not r:
            continue
        parts.append(f"\n### {title} (id={r['id']}, type={r['type']})")
        opts = json.loads(r["options"] or "[]")
        if opts:
            for o in opts[:200]:  # cap to keep prompt small
                parts.append(f"- {o.get('name')}  (value: {o.get('value')})")
            if len(opts) > 200:
                parts.append(f"- … {len(opts)-200} more options omitted")
        else:
            parts.append("(free text)")
    return "\n".join(parts)


PRODUCT_SUPPORT_FIELDS = [
    "Customer Name", "Priority", "Product", "Module", "Section",
    "Bucketization (Mandatory for Reliance)",
    "Root Cause - Level 1", "Root Cause - Level 2",
    "Jira ID", "KB Article", "How was this ticket resolved?",
    "What was the issue?", "Total Number of Profiles Mentioned On This Ticket",
]

MANAGED_SERVICES_FIELDS = [
    "Request type", "Assigned Name", "Priority", "Parent Client", "Customer Name",
    "Product", "Service type", "Service Sub-Type", "Root Cause - Level 1",
    "Number of Sites", "Number of Profiles Updated/Terminated/Created",
    "How was this ticket resolved?",
]


def build_user_prompt(c: sqlite3.Connection, ticket_id: int) -> str:
    """The non-cached portion: ticket-specific context."""
    t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return ""
    cfs = json.loads(t["custom_fields"] or "{}")

    # Resolve names for context
    requester = c.execute("SELECT name, email, role FROM users WHERE id=?", (t["requester_id"],)).fetchone()
    org = c.execute("SELECT name FROM organizations WHERE id=?", (t["organization_id"],)).fetchone() if t["organization_id"] else None
    grp = c.execute("SELECT name FROM groups WHERE id=?", (t["group_id"],)).fetchone() if t["group_id"] else None
    assignee = c.execute("SELECT name, email FROM users WHERE id=?", (t["assignee_id"],)).fetchone() if t["assignee_id"] else None

    # Field labels for display
    field_rows = {r["id"]: r for r in c.execute("SELECT * FROM ticket_fields").fetchall()}

    parts = [
        f"# Ticket #{ticket_id}",
        f"Subject: {t['subject']}",
        f"Status: {t['status']} | Priority: {t['priority']} | Group: {grp['name'] if grp else '-'}",
        f"Created: {t['created_at']} | Updated: {t['updated_at']}",
        f"Requester: {requester['name'] if requester else '-'} <{requester['email'] if requester else '-'}> "
        f"role={requester['role'] if requester else '-'}",
        f"Organization: {org['name'] if org else '-'}",
        f"Assignee: {assignee['name'] if assignee else 'Unassigned'}",
        f"Tags: {', '.join(json.loads(t['tags'] or '[]')) or '(none)'}",
        "",
        "## Current custom field values",
    ]
    for fid_str, val in cfs.items():
        fid = int(fid_str)
        f = field_rows.get(fid)
        if not f or not val:
            continue
        # Translate option value → display name where applicable
        display = val
        if f["type"] in ("tagger", "multiselect"):
            for o in json.loads(f["options"] or "[]"):
                if o.get("value") == val:
                    display = o.get("name") or val
                    break
        parts.append(f"- {f['title']}: {display}")

    parts.append("")
    parts.append("## Conversation (chronological)")
    cmts = c.execute("""
        SELECT tc.*, u.name AS author_name, u.role AS author_role
        FROM ticket_comments tc LEFT JOIN users u ON u.id = tc.author_id
        WHERE tc.ticket_id = ? ORDER BY tc.created_at
    """, (ticket_id,)).fetchall()
    for cm in cmts:
        kind = "PUBLIC" if cm["public"] else "INTERNAL"
        body = (cm["body"] or "").strip()
        body = re.sub(r"\s+", " ", body)[:1500]
        parts.append(f"[{kind}] {cm['author_name']} ({cm['author_role']}) @ {cm['created_at']}")
        parts.append(f"  {body}")

    parts.append("")
    parts.append("Produce the JSON insight payload now. JSON only, no prose.")
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    """Robustly pull the first {...} JSON block."""
    text = text.strip()
    # Strip ```json fences if any
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Find first { and matching }
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth, end = 0, -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        raise ValueError("unterminated JSON")
    return json.loads(text[start:end])


SMART_REPLY_PROMPT = """You evaluate a draft reply being typed by a BetterPlace support agent and
suggest a better version of it. Listen to the agent's draft as the primary signal — improve THEIR
voice, do not write a fresh reply from scratch unless the draft is empty.

Output ONLY this JSON shape, no prose, no markdown fences:
{
  "flag": "short reason · e.g. 'add root cause' / 'add steps' / 'looks good'",
  "flaws": ["specific gap 1", "specific gap 2"],
  "current": "the agent's draft, as you received it (or empty string)",
  "suggested": "an improved version. Keep the agent's tone. Add: root cause for Issue tickets;
                action+entity for Task tickets; numbered steps for Training tickets."
}
If the draft is empty, output a first-pass reply derived from the ticket conversation."""


DOC_PROMPT = """You are drafting a Confluence-ready runbook page for a BetterPlace support team
based on a single solved or open ticket. Output a complete markdown document with these sections:

# {ticket_subject}
> Status: {status}; auto-drafted by Co-Pilot; awaiting human review.

## Problem statement
(1 paragraph: what the customer reported)

## Symptoms
(bullet list of observable signs)

## Root cause
(what was actually wrong, with the technical explanation)

## Resolution steps
(numbered list — repeatable, specific, includes entity IDs/names)

## Verification
(how to confirm the fix worked)

## Next time you see this
(1 paragraph: when to apply this runbook, when to escalate)

## Source
- Ticket #{ticket_id}
- Customer: {customer}
- Resolved by: {assignee}

Output ONLY the markdown. No JSON, no fences."""


def _claude_call(system: str, user_prompt: str, *, max_tokens: int = 1500,
                 model: str | None = None) -> dict:
    """Plain Claude call — no caching, used for one-shot smart-reply / doc generation.
    Pass `model="haiku"` (or any explicit model id) to override the configured default —
    useful for short tasks like translation where Sonnet/Opus latency is overkill."""
    chosen_model = model or config.ANTHROPIC_MODEL
    # Allow short aliases — the AI worker uses these too.
    aliases = {
        "haiku":  "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-5",
        "opus":   "claude-opus-4-6",
    }
    chosen_model = aliases.get(chosen_model, chosen_model)
    resp = _client.messages.create(
        model=chosen_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    in_tok = usage.input_tokens
    out_tok = usage.output_tokens
    cached_in = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        (in_tok - cached_in) * config.COST_PER_1M_INPUT / 1_000_000
        + cached_in * config.COST_PER_1M_CACHED_INPUT / 1_000_000
        + out_tok * config.COST_PER_1M_OUTPUT / 1_000_000
    )
    return {
        "text": text,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "cached_input_tokens": cached_in,
        "cost_usd": round(cost, 6), "model": config.ANTHROPIC_MODEL,
    }


_LANG_NAMES = {
    "en": "English",   "hi": "Hindi",   "ta": "Tamil",      "te": "Telugu",
    "kn": "Kannada",   "mr": "Marathi", "bn": "Bengali",    "gu": "Gujarati",
    "pa": "Punjabi",
}


def translate(text: str, target_lang: str) -> dict:
    """Translate `text` into target_lang. Single Claude call — we ask it to
    return JSON with both the translation and the detected source language so
    we don't pay for a second round-trip (and don't fail twice as often)."""
    lang_name = _LANG_NAMES.get(target_lang, target_lang)
    system = (
        f"You translate B2B customer-support replies into {lang_name}.\n"
        f"Return strict JSON: {{\"source\": \"<ISO-639-1 code>\", "
        f"\"translated\": \"<text>\"}}\n"
        f"Preserve product names, ticket IDs, error codes, technical terms in English.\n"
        f"Preserve Markdown punctuation (**, *, `code`, lists, links) verbatim.\n"
        f"No preamble. No commentary. JSON only."
    )
    # Two-tier: metered API first (cheaper + faster), then `claude` CLI fallback
    # for setups where ANTHROPIC_API_KEY isn't configured.
    #
    # FORCE HAIKU for translation — Sonnet/Opus take 10-15s with no quality win
    # for a single-paragraph translation. Haiku rounds to ~1-2s for support-reply
    # length inputs. max_tokens trimmed to 1200 (was 2000) because translations
    # are short and 2000 lets the model meander.
    try:
        out = _claude_call(system, text, max_tokens=1200, model="haiku")
    except RuntimeError as auth_err:
        # Our own lazy-init RuntimeError ("ANTHROPIC_API_KEY is not set …") —
        # try the CLI fallback transparently. The CLI is slow (~10s boot), so
        # we tell the caller via the model name.
        try:
            out = _claude_via_cli(system, text, max_tokens=1200)
        except Exception as cli_err:
            raise RuntimeError(
                f"Translate failed. {auth_err} CLI fallback also failed: {cli_err}"
            ) from cli_err
    except Exception as e:
        raise RuntimeError(f"Claude call failed during translate: {type(e).__name__}: {e}") from e
    src = ""
    translated = (out.get("text") or "").strip()
    # Parse the JSON envelope. Fall back to using the raw text if it didn't
    # come back as JSON — we still got a useful translation.
    try:
        parsed = _extract_json(translated)
        translated = (parsed.get("translated") or "").strip()
        src = (parsed.get("source") or "").strip().lower()[:2]
    except Exception:
        # Strip an optional leading "Source: xx\n" prefix if Claude ignored the JSON ask.
        if translated.lower().startswith("source:"):
            first_nl = translated.find("\n")
            if first_nl > 0:
                src = translated[7:first_nl].strip().lower()[:2]
                translated = translated[first_nl + 1:].strip()
    if not translated:
        raise RuntimeError("translation came back empty — try again")
    return {
        "translated": translated,
        "source_lang": src,
        "model": out["model"],
        "input_tokens": out["input_tokens"],
        "output_tokens": out["output_tokens"],
        "cached_input_tokens": out.get("cached_input_tokens", 0),
        "cost_usd": out["cost_usd"],
    }


def suggest_reply(c: sqlite3.Connection, ticket_id: int, draft: str) -> dict:
    """Smart reply: takes the agent's current draft + full conversation,
    returns improved reply. Mirrors translate()'s two-tier path — metered
    API first, then `claude -p` CLI fallback so it works even without
    ANTHROPIC_API_KEY configured (matches our admin AI worker setup)."""
    user_prompt = build_user_prompt(c, ticket_id)
    user_prompt += "\n\n## Agent's current draft\n" + (draft or "(empty)")
    user_prompt += "\n\nProduce the JSON suggested_reply object now. JSON only."
    try:
        out = _claude_call(SMART_REPLY_PROMPT, user_prompt, max_tokens=900)
    except RuntimeError as auth_err:
        # Lazy-init "ANTHROPIC_API_KEY not set" — try CLI fallback.
        try:
            out = _claude_via_cli(SMART_REPLY_PROMPT, user_prompt, max_tokens=900)
        except Exception as cli_err:
            raise RuntimeError(
                f"Improve-with-AI failed. {auth_err} CLI fallback also failed: {cli_err}"
            ) from cli_err
    try:
        reply = _extract_json(out["text"])
    except Exception:
        reply = {"flag": "draft", "flaws": [], "current": draft, "suggested": out["text"]}
    return {**out, "reply": reply}


def generate_doc(c: sqlite3.Connection, ticket_id: int) -> dict:
    """Generate a Confluence-ready markdown runbook from this ticket's history."""
    t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    cfs = json.loads(t["custom_fields"] or "{}") if t else {}
    field_rows = {r["id"]: r for r in c.execute("SELECT id, title, options, type FROM ticket_fields").fetchall()}
    customer = ""
    cn_field = field_rows.get(15315331275025)
    if cn_field:
        cn = cfs.get("15315331275025")
        if cn:
            for o in json.loads(cn_field["options"] or "[]"):
                if o.get("value") == cn:
                    customer = o.get("name") or cn
                    break
    assignee_row = c.execute("SELECT name FROM users WHERE id=?", (t["assignee_id"],)).fetchone() if t and t["assignee_id"] else None
    assignee = assignee_row["name"] if assignee_row else "Unassigned"

    system = DOC_PROMPT.format(
        ticket_id=ticket_id,
        ticket_subject=(t["subject"] or "Untitled") if t else "Untitled",
        status=(t["status"] or "open") if t else "open",
        customer=customer or "—",
        assignee=assignee,
    )
    user_prompt = build_user_prompt(c, ticket_id)
    user_prompt += "\n\nGenerate the complete markdown runbook now. Markdown only."
    out = _claude_call(system, user_prompt, max_tokens=2500)
    return {**out, "markdown": out["text"]}


def analyze_ticket(c: sqlite3.Connection, ticket_id: int) -> dict:
    """Run Claude on a single ticket. Returns the parsed insight + token/cost info."""
    # Determine scope (PS vs MS) from group
    t = c.execute("SELECT group_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    grp = c.execute("SELECT name FROM groups WHERE id=?", (t["group_id"],)).fetchone() if t and t["group_id"] else None
    is_ms = grp and "managed" in (grp["name"] or "").lower()
    scope_fields = MANAGED_SERVICES_FIELDS if is_ms else PRODUCT_SUPPORT_FIELDS

    taxonomy = build_taxonomy_block(c, scope_fields)
    user_prompt = build_user_prompt(c, ticket_id)

    system_blocks = [
        {"type": "text", "text": SYSTEM_TEMPLATE},
        # Cache the taxonomy so subsequent tickets reuse it
        {"type": "text", "text": taxonomy, "cache_control": {"type": "ephemeral"}},
    ]

    resp = _client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=2000,
        system=system_blocks,
        messages=[{"role": "user", "content": user_prompt}],
    )
    out_text = "".join(b.text for b in resp.content if b.type == "text")
    insight = _extract_json(out_text)

    usage = resp.usage
    in_tok = usage.input_tokens
    out_tok = usage.output_tokens
    cached_in_tok = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create_tok = getattr(usage, "cache_creation_input_tokens", 0) or 0

    # Cost (Haiku 4.5)
    cost = (
        (in_tok - cached_in_tok - cache_create_tok) * config.COST_PER_1M_INPUT / 1_000_000
        + cache_create_tok * config.COST_PER_1M_INPUT * 1.25 / 1_000_000  # cache writes are 25% more
        + cached_in_tok * config.COST_PER_1M_CACHED_INPUT / 1_000_000
        + out_tok * config.COST_PER_1M_OUTPUT / 1_000_000
    )

    return {
        "insight": insight,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_input_tokens": cached_in_tok,
        "cost_usd": round(cost, 6),
        "model": config.ANTHROPIC_MODEL,
    }
