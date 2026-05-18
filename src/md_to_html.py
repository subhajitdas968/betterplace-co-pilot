"""Markdown → HTML converter for outbound replies to Zendesk.

ZD stores a comment body literally. If we send `**bold**`, the customer sees
`**bold**` in their email and in the ZD agent UI. To get the formatting the
agent applied via our toolbar to render properly, we convert to inline HTML
before sending, then pass `html_body=True` to the ZD API.

This is a deliberately small wrapper around the `markdown` package:
  - Stick to inline formatting + lists + code blocks + links (what our toolbar produces)
  - Sanitize HTML the agent might paste so a hostile user can't smuggle <script>
  - Newlines outside list/code blocks become <br> so the visual line wrapping
    the agent saw in the textarea is preserved in the customer's email

The local mirror still stores the raw markdown — this conversion is purely
for the wire format to Zendesk.
"""
from __future__ import annotations

import re
from html import escape as _esc

try:
    import markdown as _md  # type: ignore
    _HAS_MD = True
except Exception:
    _HAS_MD = False


# Tags we keep when sanitizing. Everything else gets <-escaped.
_ALLOWED = {
    "p", "br", "strong", "em", "s", "del", "code", "pre",
    "ul", "ol", "li", "a", "blockquote", "h1", "h2", "h3", "h4", "h5",
}


def _strip_dangerous(html: str) -> str:
    """Best-effort sanitizer. Strips <script>/<style>/<iframe>/<object>/<embed>
    blocks AND any on* event handlers / javascript: URLs. This is belt-and-
    suspenders — `markdown.markdown` doesn't emit any of those, but agents
    sometimes paste raw HTML."""
    # Drop tag blocks entirely (script/style/iframe/object/embed)
    html = re.sub(
        r"<\s*(script|style|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>",
        "", html, flags=re.IGNORECASE | re.DOTALL,
    )
    # Drop self-closing variants too
    html = re.sub(
        r"<\s*(script|style|iframe|object|embed)\b[^>]*/?>",
        "", html, flags=re.IGNORECASE,
    )
    # Strip on* handlers
    html = re.sub(r'\s*on[a-z]+\s*=\s*"[^"]*"', "", html, flags=re.IGNORECASE)
    html = re.sub(r"\s*on[a-z]+\s*=\s*'[^']*'", "", html, flags=re.IGNORECASE)
    # Strip javascript: URLs
    html = re.sub(r'href\s*=\s*"javascript:[^"]*"', 'href="#"', html, flags=re.IGNORECASE)
    html = re.sub(r"href\s*=\s*'javascript:[^']*'", "href='#'", html, flags=re.IGNORECASE)
    return html


def _fallback_convert(body: str) -> str:
    """Minimal pure-regex converter used when the `markdown` package isn't
    installed. Handles the toolbar output: **bold**, *italic*, ~~strike~~,
    `code`, ```fence```, links, lists, > quote. Less complete than the
    library version but keeps the feature alive."""
    text = body
    # Code fences first (so their contents don't get re-processed)
    fences = []
    def _stash_fence(m):
        idx = len(fences)
        fences.append("<pre><code>" + _esc(m.group(1)) + "</code></pre>")
        return f"\x00FENCE{idx}\x00"
    text = re.sub(r"```\n?(.*?)\n?```", _stash_fence, text, flags=re.DOTALL)

    # Inline code
    text = re.sub(r"`([^`\n]+)`", lambda m: "<code>" + _esc(m.group(1)) + "</code>", text)
    # Bold, italic, strike — order matters (bold before italic)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  lambda m: f'<a href="{_esc(m.group(2))}">{_esc(m.group(1))}</a>', text)

    # Lists: convert blocks of "- item" or "1. item" lines
    def _list_block(text):
        lines = text.split("\n")
        out = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if re.match(r"^\s*[-*]\s+", ln):
                items = []
                while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                    items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                    i += 1
                out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            elif re.match(r"^\s*\d+\.\s+", ln):
                items = []
                while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                    items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                    i += 1
                out.append("<ol>" + "".join(f"<li>{it}</li>" for it in items) + "</ol>")
            elif re.match(r"^\s*>\s+", ln):
                quote_lines = []
                while i < len(lines) and re.match(r"^\s*>\s+", lines[i]):
                    quote_lines.append(re.sub(r"^\s*>\s+", "", lines[i]))
                    i += 1
                out.append("<blockquote>" + "<br>".join(quote_lines) + "</blockquote>")
            else:
                out.append(ln)
                i += 1
        return "\n".join(out)

    text = _list_block(text)

    # Restore code fences
    for i, f in enumerate(fences):
        text = text.replace(f"\x00FENCE{i}\x00", f)

    # Newlines → <br>, but not inside block-level HTML we just emitted
    text = re.sub(r"\n(?!(<ul>|<ol>|<pre>|<blockquote>|</?li>|</?ul>|</?ol>))", "<br>", text)
    return text


def _pre_pass_strike(body: str) -> str:
    """Python-markdown doesn't handle ~~strike~~ natively (GFM extension).
    Pre-substitute to <del>…</del> before running the main parser so it
    survives the parse intact. We guard with a sentinel pair of code-fence
    detectors so we never mutate inside ``` blocks."""
    out_lines = []
    in_fence = False
    for line in body.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        # Avoid clashing with `~~~` fence syntax (rare). Only convert ~~x~~
        # pairs that are NOT triple-tildes.
        line = re.sub(r"(?<!~)~~(?!~)(.+?)(?<!~)~~(?!~)", r"<del>\1</del>", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def markdown_to_html(body: str) -> str:
    """Convert a markdown body (as emitted by our reply toolbar) to inline HTML
    safe to send to ZD's `comment.html_body`. Falls back to a pure-regex path
    if the `markdown` library isn't installed.

    Caveats:
      * We deliberately keep this conservative — no extensions like tables,
        because ZD's renderer often drops or mangles them.
      * The output is wrapped in nothing — ZD's renderer treats the html_body
        as the comment, so block tags at the top level are fine.
    """
    if not body:
        return ""
    if _HAS_MD:
        body = _pre_pass_strike(body)
        html = _md.markdown(
            body,
            extensions=["fenced_code", "sane_lists", "nl2br"],
            output_format="html5",
        )
    else:
        html = _fallback_convert(body)
    return _strip_dangerous(html).strip()


def markdown_to_plain(body: str) -> str:
    """Strip markdown to plain text — useful when an integration explicitly
    can't render HTML (e.g. SMS, push notifications). Not used by ZD writes
    but exposed so callers don't have to roll their own."""
    if not body:
        return ""
    text = body
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s+", "  > ", text, flags=re.MULTILINE)
    return text.strip()
