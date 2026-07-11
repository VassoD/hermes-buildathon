# Support Ticket Triage & Resolution Crew

A Hermes-powered support operation that accepts Telegram tickets, remembers prior customer cases, routes work to specialist agents, reviews drafts, executes approved actions, and exposes a full trace.

## Run locally

Create and seed the database:

```bash
python3 seed_data.py
```

Run the full pipeline:

```bash
python3 run_pipeline.py
```

Stage-by-stage scripts (`run_manager.py`, `run_specialists.py`, `run_review.py`) remain available for testing individual phases, but they do not perform the memory lookup step.

Inspect traces:

```bash
python3 trace_viewer.py --ticket 13
python3 trace_viewer.py --summary
```

Start the read-only dashboard:

```bash
python3 app.py
```

Open <http://127.0.0.1:5050>. To expose it for a demo, run this in a second terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:5050
```

Update `trace_config.json` with the resulting `*.trycloudflare.com` URL.

## Connect product context, GitHub, and Linear

Open <http://127.0.0.1:5050/workspace> from the local dashboard. Configure the
product/documentation URL, GitHub repository and branch, Linear team/project,
and the confidence threshold used for engineering escalation.

GitHub and Linear credentials can also be entered on this page. They are stored
in the current macOS user's login Keychain, are never written to `tickets.db`,
and are never rendered back into the form. Credential fields are disabled when
the dashboard is accessed through a remote hostname; update them through
`127.0.0.1:5050` or `localhost:5050` only. Environment variables
`GITHUB_TOKEN` and `LINEAR_API_KEY` remain supported and take precedence over
Keychain values.

Use a fine-grained GitHub token with read-only repository-content access. The
Linear API key must be able to read and create issues in the configured team.

## Convex support operations store

Convex is the realtime partner-power-up store for the support agency output.
SQLite remains the local execution ledger. After every pipeline run, reviewed
and finalized tickets are grouped into ranked clusters, converted into
versioned macros, QA-scored, and synced with the agent handoff log.

Install and configure it once:

```bash
python3 -m pip install -r requirements.txt
npm install
npx convex dev --once
```

The Convex CLI writes the cloud deployment URL to `.env.local`; the Python app
loads it automatically. Running the pipeline then syncs the playbook even when
there are no new open tickets:

```bash
python3 run_pipeline.py
# Convex: synced · 2 clusters · 2 macros
```

The cloud deployment contains four proof tables:

- `clusters` — ranked by volume with severity and source ticket IDs
- `macros` — versioned replies with QA and rating aggregates
- `ratings` — 1–5 feedback plus optional comments
- `runEvents` — Clusterer → MacroWriter → QAReviewer handoffs

The customer-facing inbox intentionally does not expose this internal
playbook. Inspect the live records and invoke `supportOps:rateMacro` from the
Convex dashboard:
<https://dashboard.convex.dev/t/vasiliki-doropoulou/hermes-buildathon>.

## Dodo Payments refund execution

Approved `issue_refund` actions can now create a real refund through the Dodo Payments API. Test mode is the default and live refunds are guarded against accidental use.

1. Create a Dodo **test-mode** payment and copy its `pay_...` payment ID.
2. Add the test API key to `~/.hermes/.env`:

   ```dotenv
   DODO_PAYMENTS_API_KEY=your_test_key
   DODO_ENVIRONMENT=test
   ```

3. Include the payment reference in the Telegram support request, for example:

   ```text
   I was charged twice. Please refund payment pay_abc123.
   ```

4. When the Manager approves `issue_refund`, the action executor calls:

   ```text
   POST https://test.dodopayments.com/refunds
   ```

5. The Dodo refund ID and provider status are persisted in `action_executions` and appear as an **EXECUTE ACTION** step in the ticket trace.

The system never marks an unverified refund as resolved:

- `succeeded` → ticket is resolved
- `pending` → ticket remains in review
- `review`, `failed`, missing payment ID, or missing credentials → ticket is escalated

To enable live mode deliberately, both settings are required:

```dotenv
DODO_ENVIRONMENT=live
DODO_ALLOW_LIVE=true
```

Do not enable live mode for a demo unless the account is activated and real-money execution is intended.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
