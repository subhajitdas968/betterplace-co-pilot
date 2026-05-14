// One-off script to build the phasing plan as a Word doc.
const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, LevelFormat, HeadingLevel, BorderStyle, WidthType,
  ShadingType, PageNumber, PageBreak, Header, Footer, TabStopType,
  TabStopPosition, TableOfContents
} = require('docx');

const P = (text, opts={}) => new Paragraph({ children: [new TextRun({ text, ...opts })], ...opts });
const H1 = t => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun({ text: t, bold: true })] });
const H2 = t => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun({ text: t, bold: true })] });
const H3 = t => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun({ text: t, bold: true })] });
const BL = t => new Paragraph({ numbering: { reference: 'bullets', level: 0 }, children: [new TextRun(t)] });
const BLb = (lead, rest) => new Paragraph({
  numbering: { reference: 'bullets', level: 0 },
  children: [new TextRun({ text: lead, bold: true }), new TextRun({ text: rest })]
});
const NUM = t => new Paragraph({ numbering: { reference: 'numbers', level: 0 }, children: [new TextRun(t)] });
const SP = () => new Paragraph({ children: [new TextRun('')] });

const border = { style: BorderStyle.SINGLE, size: 1, color: 'CCCCCC' };
const borders = { top: border, bottom: border, left: border, right: border };
const TWIDTH = 9360;

const headerCell = (text, w) => new TableCell({
  borders,
  width: { size: w, type: WidthType.DXA },
  shading: { fill: 'D5E8F0', type: ShadingType.CLEAR },
  margins: { top: 80, bottom: 80, left: 120, right: 120 },
  children: [new Paragraph({ children: [new TextRun({ text, bold: true, size: 20 })] })]
});
const cell = (text, w, opts={}) => new TableCell({
  borders,
  width: { size: w, type: WidthType.DXA },
  margins: { top: 80, bottom: 80, left: 120, right: 120 },
  shading: opts.fill ? { fill: opts.fill, type: ShadingType.CLEAR } : undefined,
  children: [new Paragraph({ children: [new TextRun({ text, size: 20, bold: opts.bold })] })]
});

const table = (headers, rows, widths) => new Table({
  width: { size: TWIDTH, type: WidthType.DXA },
  columnWidths: widths,
  rows: [
    new TableRow({ children: headers.map((h, i) => headerCell(h, widths[i])) }),
    ...rows.map(r => new TableRow({ children: r.map((c, i) => cell(c, widths[i])) }))
  ]
});

const doc = new Document({
  creator: 'Claude (Cowork)',
  title: 'Zendesk Co-Pilot — Phasing & Cost-Optimised Plan',
  styles: {
    default: { document: { run: { font: 'Arial', size: 22 } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 32, bold: true, font: 'Arial', color: '03363D' },
        paragraph: { spacing: { before: 320, after: 200 }, outlineLevel: 0 } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 26, bold: true, font: 'Arial', color: '1F73B7' },
        paragraph: { spacing: { before: 240, after: 140 }, outlineLevel: 1 } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 22, bold: true, font: 'Arial', color: '2F3941' },
        paragraph: { spacing: { before: 180, after: 100 }, outlineLevel: 2 } },
    ]
  },
  numbering: {
    config: [
      { reference: 'bullets', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•',
        alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: 'numbers', levels: [{ level: 0, format: LevelFormat.DECIMAL, text: '%1.',
        alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
      }
    },
    headers: {
      default: new Header({ children: [new Paragraph({
        children: [
          new TextRun({ text: 'Zendesk Co-Pilot · BetterPlace', bold: true, color: '03363D' }),
          new TextRun({ text: '\tPhasing & Cost-Optimised Plan' }),
        ],
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: '1F73B7', space: 4 } }
      })] })
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        children: [
          new TextRun({ text: 'Prepared for Subhajit Das · 30 Apr 2026' }),
          new TextRun({ text: '\tPage ' }),
          new TextRun({ children: [PageNumber.CURRENT] }),
        ],
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      })] })
    },
    children: [
      // --- Title ---
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 1800, after: 200 },
        children: [new TextRun({ text: 'Zendesk Co-Pilot', size: 56, bold: true, color: '03363D' })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 },
        children: [new TextRun({ text: 'Phasing & Cost-Optimised Plan', size: 32, color: '1F73B7' })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 },
        children: [new TextRun({ text: 'How we ship end-to-end with Claude API as the only ongoing cost', size: 22, italics: true, color: '68737D' })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 },
        children: [new TextRun({ text: 'Prepared for: Subhajit Das · BetterPlace', size: 22 })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 },
        children: [new TextRun({ text: 'Date: 30 April 2026', size: 22 })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 },
        children: [new TextRun({ text: 'Version: 0.1', size: 22 })] }),
      new Paragraph({ children: [new PageBreak()] }),

      H1('1. The cost constraint, in plain language'),
      P('You said: "I want to spend only on Claude API cost and nothing else if possible apart from my already ZD license. Plan and optimise accordingly. If we need to host on a server absolutely let me know."'),
      SP(),
      P('Short answer: yes, this is achievable. Every piece of software in the stack — database, vector search, web framework, sync workers, AI workers, tunnel, auth — has a free/open-source choice that handles your volume comfortably. The only ongoing cost driver is Claude API calls. Estimated $150–400/month at your ticket volume, optimisable downward.'),
      SP(),
      P('There is one infrastructure question that does need a decision: where the always-on Linux box lives. Three options below; the cheapest is $0/month (use a machine you already have).'),

      H1('2. The cost-zero stack'),
      P('Every layer of the system uses open-source / free-tier software. None of these have a license fee or usage cost at your volume.'),
      SP(),
      table(
        ['Layer', 'Choice', 'Cost'],
        [
          ['Database', 'SQLite with sqlite-vec extension (vector search built in)', '$0 forever'],
          ['Web framework', 'FastAPI + HTMX (Python), server-rendered, low-JS', '$0'],
          ['Sync worker', 'Python — httpx, tenacity, schedule', '$0'],
          ['AI worker', 'Python — official anthropic SDK', '$0'],
          ['Auth', 'Google Workspace SSO via OAuth (you already have Workspace)', '$0'],
          ['Tunnel / remote access', 'Cloudflare Tunnel (free tier) or Tailscale (free up to 100 devices)', '$0'],
          ['Backups', 'Daily SQLite snapshot to a NAS or external drive', '$0'],
          ['Process manager', 'systemd or launchd (built into the OS)', '$0'],
          ['Container orchestration', 'docker-compose (one config file, no Kubernetes)', '$0'],
        ],
        [2200, 5400, 1760]
      ),
      SP(),
      P('Notes on why these choices fit the cost constraint:'),
      BLb('SQLite over Postgres: ', 'at <= 1k tickets/day, SQLite is faster than Postgres and needs no server. sqlite-vec gives you vector similarity search inside the same DB — no separate vector store.'),
      BLb('FastAPI + HTMX over React: ', 'a single Python process serves the UI. No build pipeline, no Node, no CDN. Pages render server-side and feel snappy.'),
      BLb('Cloudflare Tunnel over a public IP: ', 'creates a stable HTTPS URL that points at your local machine. No ports opened, no firewall changes, no public IP needed. Free tier covers your volume by orders of magnitude.'),
      BLb('Google SSO over building auth: ', 'you already have Workspace. Each engineer logs in with their @betterplace.co.in account. No password DB to manage.'),

      H1('3. The Claude API cost — how we keep it low'),
      P('At ~1,000 tickets/day (the upper bound of your range), naive per-ticket Claude calls cost roughly $300–400/month on Sonnet. With the optimisations below we expect $150–250/month for production use; the same techniques cap unexpected spikes.'),
      SP(),
      H2('3.1 Optimisations baked into Phase 1'),
      BLb('Skip re-analysis on quiescent tickets. ', 'A ticket gets re-run only when its conversation has actually changed. Solved tickets that no-one touches don\'t cost anything.'),
      BLb('Hard monthly budget cap. ', 'Set in code. When usage approaches it the AI worker pauses and emails you. No surprise bills.'),
      BLb('Embeddings computed once. ', 'When the conversation extends, only the new comments are re-embedded.'),
      BLb('JSON-mode strict outputs. ', 'Claude returns a tight schema; no wasted tokens on chatty natural-language responses.'),

      H2('3.2 Optimisations available in Phase 2+'),
      BLb('Route easy tickets to Haiku. ', 'Short, well-formed Operations Tasks (e.g. agreement-copy requests) go to Haiku at ~10x lower cost; only ambiguous or complex tickets escalate to Sonnet. Roughly cuts cost 50–60%.'),
      BLb('Prompt caching. ', 'Anthropic caches the long system prompt (your field taxonomy is ~3k tokens of stable context). Caching cuts input cost ~80% on the cached portion. Net: another ~25% reduction on average.'),
      BLb('Batch nightly re-runs. ', 'Solved tickets get one re-analysis at end of day in a batch (cheaper than per-ticket calls) to take advantage of any model improvements.'),

      H2('3.3 Cost ceiling vs. value'),
      P('At your ticket volume the worst-case Claude bill is ~$400/month. For comparison, eliminating one hour of agent re-tagging and resolution-writing per day at typical cost is more than that. The system pays for itself almost immediately on the field-correction workflow alone, before any KB or write-back features are turned on.'),

      H1('4. The infrastructure decision: where the always-on box lives'),
      P('Everything else is free. The one decision that has cost implications is what hardware runs the system 24/7.'),
      SP(),
      table(
        ['Option', 'Up-front', 'Ongoing', 'Trade-offs'],
        [
          ['A. Use an existing always-on machine in your office (RECOMMENDED if available)', '$0', '$0', 'Best fit. If you have any spare desktop, NAS, or always-on workstation — even an older Mac — it can run this. Needs ~16 GB RAM and Docker.'],
          ['B. Buy a Mac mini M2 (or NUC equivalent)', '~$700', '$0', 'One-time hardware spend. Sits in the office, sleep disabled. Effectively zero cost over time. Best if no spare machine exists.'],
          ['C. Run on Subhajit\'s laptop temporarily', '$0', '$0', 'Works for the proof-of-value phase but team access drops every time you close the lid. Acceptable for Phase 1 testing only.'],
          ['D. Small VPS (Hetzner, Contabo)', '$0', '~$5–10 / month', 'Predictable but breaks the "Claude API only" constraint. Recommended only if you have no spare hardware AND need true 24/7 access.'],
        ],
        [2400, 1200, 1300, 4460]
      ),
      SP(),
      P('Recommendation: start on Option C (your laptop) for the first build week, then move to Option A if you have a spare machine, otherwise Option B. The Mac mini purchase pays for itself in roughly 2 months versus a VPS at typical ICP pricing, and is yours to keep.'),

      H1('5. Phases — what we ship, in what order'),
      P('Each phase is independently shippable. After Phase 1 the system is delivering value; everything after is incremental. You can pause between phases at no risk.'),
      SP(),
      table(
        ['Phase', 'Duration', 'What ships', 'Net new cost'],
        [
          ['0', 'DONE', 'Design + 10-ticket proof + agent-view mockup + repository seed', '$0 (we are here)'],
          ['1 — Local MVP', '~2 weeks', 'Sync worker, AI worker, agent dashboard live, single-host deploy with tunnel. Read-only.', 'Claude API only ($150–250/mo)'],
          ['2 — Confluence + Jira + KB', '~2–3 weeks', 'Confluence indexing + auto-link, Jira ID auto-detect, KB article auto-draft → review → publish.', 'Claude API only (slight increase from embedding new pages)'],
          ['3 — Suggested replies + write-back', '~2 weeks', 'AI evaluates every agent reply; drafts improved version when reply is incomplete. "Apply correction" writes back to ZD. Auto-rules (recall merge, backfill on merge, Reliance bucketization required).', 'Claude API only'],
          ['4 — Leadership dashboard', '~1–2 weeks', 'Separate URL, customer health scorecards, agent quality metrics, emerging-bucket alerts, weekly exec digest.', 'Claude API only'],
          ['5 — Continuous polish', 'ongoing', 'Add new groups, customer-specific rules, accuracy tuning, cost tuning.', 'Claude API only'],
        ],
        [1700, 1100, 4660, 1900]
      ),

      H2('5.1 Phase 1 deliverables in detail'),
      BL('Sync worker pulls from Zendesk every 5 minutes, writes to local SQLite.'),
      BL('AI worker calls Claude on every new/updated ticket, writes structured insights into the same DB.'),
      BL('Web app renders the agent view (same shape as the mockup we already built).'),
      BL('SLA pickup banner appears automatically when a ticket meets the rule (status ∈ new/open + no agent comment + no assignee + age > 4h).'),
      BL('Repository auto-grows: every solved ticket adds an entry that future tickets can find.'),
      BL('Cloudflare Tunnel exposes the dashboard at a stable URL like https://copilot.betterplace.co.in.'),
      BL('Read-only first — agents see suggestions but click through to Zendesk to apply them. This is deliberate so you build trust before write-back goes live in Phase 3.'),

      H2('5.2 Phase 2 deliverables in detail'),
      BL('Confluence index: weekly job that pulls every page from your chosen spaces and embeds them into the same vector store as tickets. Suggestions cite page title + URL.'),
      BL('Jira auto-detect: regex on conversation text catches Jira IDs (e.g. SMS-1124) and pre-fills the Jira ID field with one-click confirmation.'),
      BL('KB auto-draft: when a solved ticket is marked KB-worthy, AI drafts a Confluence page in markdown (sample already created at data/kb_drafts/593795_color_code_format.md). Drafts queue for review; one-click "Publish" via Atlassian API uploads to your chosen space.'),
      BL('Existing-page suggestion: on a new ticket, the top-matching Confluence page surfaces in the AI panel and pre-fills the KB Article field.'),

      H2('5.3 Phase 3 deliverables in detail'),
      BL('Suggested-reply engine: AI evaluates each agent reply against the ticket type. Failure modes covered: (a) Issue ticket with root cause not stated, (b) Task ticket with action not described, (c) Training ticket with steps not provided. Live demo for #593721 and #593760 already in the agent view report.'),
      BL('"Apply correction" writes back to Zendesk via /api/v2/tickets/{id}.json with full audit log of who applied what and when.'),
      BL('Auto-rules: Outlook recall merge, Reliance Bucketization required (block closure when empty), backfill custom fields on merge.'),
      BL('Approval threshold configurable: e.g. "auto-apply field corrections at >= 95% confidence; surface 80–95% to agent for one-click approval; suppress < 80%."'),

      H2('5.4 Phase 4 deliverables in detail'),
      BL('Separate Leadership URL (e.g. /leadership) with role-based access — only managers see it.'),
      BL('Customer health scorecards: tickets per customer per week, time-to-resolve trend, escalation rate, SLA breach count.'),
      BL('Agent quality metrics: field-tagging accuracy, KB-link rate, "How resolved?" completeness score, suggested-reply acceptance rate.'),
      BL('Emerging-bucket detection: cluster of similar tickets in 24h that don\'t map to existing categories — early warning for new failure modes.'),
      BL('Weekly exec digest: auto-generated email or Slack message with the top 3 trends, the top 3 customer pain points, the top 3 KB-article gaps.'),

      H1('6. Suggested-reply engine — how it works'),
      P('Triggered after the agent writes a reply but before they hit send. Adds 2–4 seconds latency for the AI evaluation; can be skipped per-agent if not wanted.'),
      H2('6.1 Reply-evaluation prompt (sketch)'),
      P('The AI gets: the ticket type (Issue / Task / Training), the conversation so far, and the agent\'s draft reply. It outputs a JSON object: {complete: bool, missing: [list of named gaps], suggested_reply: text, reasoning: text}.'),
      H2('6.2 Failure modes covered'),
      table(
        ['Ticket type', 'What gets flagged'],
        [
          ['Issue', 'Reply doesn\'t state the root cause; doesn\'t reassure on recurrence; doesn\'t set expectations on follow-up.'],
          ['Task', 'Reply doesn\'t name the action taken; doesn\'t reference the entity (driver code, profile ID, ticket ID); doesn\'t confirm verifiable side-effects (e.g. push notification dispatched).'],
          ['Training', 'Reply doesn\'t walk through the steps; doesn\'t link to the relevant Confluence runbook; doesn\'t confirm the user has access to the feature being explained.'],
        ],
        [2200, 7160]
      ),
      H2('6.3 Live examples in the agent view report'),
      BLb('#593721 — Task ticket. ', 'Original reply: "Vendor has been updated." AI flags: doesn\'t name what was done, doesn\'t reference the driver code, doesn\'t confirm the push notification. Suggested reply incorporates all three.'),
      BLb('#593760 — Issue ticket. ', 'Original reply mentions the cause (yesterday\'s lag) but as an aside. AI flags: customer might miss it, no reassurance on recurrence. Suggested reply leads with the root cause as a clearly-labelled section.'),

      H1('7. Confluence integration — how it works end-to-end'),
      H2('7.1 Direction A: suggest existing pages on new tickets'),
      NUM('Weekly job pulls every Confluence page from the spaces you nominate (e.g. PS, OPS).'),
      NUM('Each page is chunked, embedded with the same model used for tickets, and stored in the vector index alongside ticket embeddings.'),
      NUM('When a new ticket arrives, the AI worker embeds the conversation and runs vector similarity against all indexed Confluence pages.'),
      NUM('Top match (above a similarity threshold) is surfaced in the agent panel with a citation, an excerpt, and a one-click "Attach to KB Article field" action.'),

      H2('7.2 Direction B: auto-draft new pages from KB-worthy resolutions'),
      NUM('When a ticket is solved, the AI worker scores it for "KB-worthiness" — recurring failure mode + no existing matching Confluence page + clear resolution steps in the conversation.'),
      NUM('Above the threshold, AI drafts a full markdown page: problem statement, symptoms, root cause, resolution steps, verification checklist, "next time" guidance.'),
      NUM('Draft saved to data/kb_drafts/<ticket_id>_<topic>.md. Sample already created for #593795 — open it to see the shape.'),
      NUM('Drafts queue in the dashboard for human review. Reviewer can edit in-line, then click "Publish to Confluence". Publish uses Atlassian REST API to create the page in the configured space + parent page.'),
      NUM('Once published, the new page is indexed (Direction A) and immediately available to suggest on similar future tickets.'),

      H2('7.3 What this means for the "agents skip KB Article" problem you flagged'),
      P('In your data, 9 of 10 tickets had KB Article empty or "NA". The two-direction system flips this: existing pages are pre-suggested (no agent typing required) and new pages are auto-drafted (agent reviews instead of writes). Net effect: KB hygiene becomes the AI\'s job, not the agent\'s; the agent\'s job is just to approve.'),

      H1('8. Risks and how we mitigate them'),
      table(
        ['Risk', 'Mitigation'],
        [
          ['Claude API cost runs higher than estimated.', 'Hard monthly cap in code; alerts at 50% / 75% / 100%. Worst-case the worker pauses, not your bank account.'],
          ['Agents distrust AI suggestions and ignore them.', 'Read-only Phase 1 builds trust before any write-back. Confidence rings on every suggestion let agents calibrate themselves. The 10-ticket proof gives senior agents (Eshwaran, Wrishov) the chance to validate before rollout.'],
          ['Your Mac/laptop offline = team locked out.', 'Choose Option A or B from the hosting table; not Option C long-term. Cloudflare Tunnel can route to a backup machine if you set one up.'],
          ['Zendesk API rate limits.', 'Sync worker uses incremental exports + exponential backoff. Worst-case impact is delayed sync, not data loss.'],
          ['Reliance custom rules change.', 'Bucketization-required and Customer Name dropdown updates are config, not code. You add new options without a developer.'],
          ['Sensitive ticket data sent to Anthropic.', 'Anthropic API does not train on data sent via the API. We can also redact PII (phone, exact addresses) before the prompt is sent — this is a checkbox in the AI worker config.'],
        ],
        [3200, 6160]
      ),

      H1('9. Decision points needed from you'),
      BLb('Hosting option (A / B / C / D from §4). ', 'Best to confirm before Phase 1 starts so the deploy step matches the chosen target. If unsure, Option B (Mac mini) is the safe long-term pick.'),
      BLb('Cloudflare vs Tailscale tunnel. ', 'Cloudflare gives a stable public URL like copilot.betterplace.co.in. Tailscale gives zero-config private network access (team installs the Tailscale app). Either works.'),
      BLb('Auth method. ', 'Google Workspace SSO recommended if you use Workspace. Magic-link email is the alternative.'),
      BLb('Confluence spaces to index. ', 'Need the space keys (e.g. PS, OPS) and a label or page-prefix that marks something as a runbook/SOP.'),
      BLb('PII redaction. ', 'Should we redact customer phone numbers / full email addresses / aadhaar / etc. before sending content to Claude? Default off; can be on per-customer (e.g. Reliance only).'),
      BLb('Auto-apply threshold for field corrections in Phase 3. ', 'Always require human approval, or auto-apply above a confidence threshold like 95%? Recommend always-approval for the first 2 weeks of Phase 3, then re-decide.'),

      H1('10. What\'s done already'),
      P('Phase 0 is complete. Concrete artefacts in your project folder:'),
      BL('docs/architecture.docx — the full system design.'),
      BL('docs/dashboard_mockup.html — the UI design for the per-ticket agent view.'),
      BL('docs/agent_view_report.html — same shape, populated with 10 real BetterPlace tickets analysed by Claude. Now includes the suggested-reply demo for #593721 and #593760.'),
      BL('docs/ai_analysis_report.html — the cross-cutting findings from the same 10 tickets.'),
      BL('data/zd_pull_output.json — the raw Zendesk pull (10 tickets + comments + 106 field defs).'),
      BL('data/ticket_repository.json — the structured AI repository, seeded with all 10 tickets and their AI insights. This is what the Phase 1 system replaces with a live database.'),
      BL('data/kb_drafts/593795_color_code_format.md — sample auto-drafted Confluence page for the color-code issue. Shape every future KB draft will follow.'),
      BL('scripts/zd_pull.py — the working pull script, virtualenv-aware, .env-driven.'),

      P(' '),
      P('— End of plan —', { italics: true, color: '68737D' }),
    ]
  }]
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync('/sessions/sweet-upbeat-bardeen/mnt/Documents/zd-copilot/docs/phasing_plan.docx', buf);
  console.log('OK ' + buf.length + ' bytes');
});
