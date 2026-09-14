# Recoverly

Recoverly is an autonomous Professional Agent for Kenyan coffee and tea exporters managing overdue B2B invoices. Built with the Strands Agents SDK, it wakes on a persistent schedule, finds cases that need attention, sends safe routine reminders, watches for payment, and asks an operator in Slack only when a dispute, anomaly, high-value balance, call, final notice, or legal step requires judgment.

**[▶ Watch the Recoverly demo on YouTube](https://www.youtube.com/watch?v=4p-7WtbMgEM)**

The primary demonstration flow is:

1. Upload a contract and invoice to the Recoverly Slack channel.
2. Pair the documents and extract the buyer, invoice, balance, due date, and governing-law context.
3. Leave Recoverly running while its Strands agent checks the queue every 15 minutes.
4. Let the agent send policy-approved day-7-to-day-14 reminders without operator busywork.
5. Review Slack only when the agent asks for a real decision, such as a dispute or escalation.
6. Give the buyer a case-specific payment portal offering configured card, USDC, wire, and ACH options.
7. Verify the selected method's provider response or signed webhook, or obtain operator confirmation for bank transfers, then match the payment to the case and update the balance. The USDC watcher also publishes receipts to Slack, the local console, and email.

Calls, final notices, disputed cases, high-value cases, and legal steps always require an operator decision. The autonomous path is limited to ordinary reminders that pass deterministic policy checks.

## Judge quick start

### 1. Project overview

Recoverly removes the repetitive work of checking due dates, choosing the next recovery step, sending ordinary follow-ups, and monitoring settlement. One Strands agent runs in the background and calls application tools to inspect every actionable case. It completes routine, low-risk reminders itself and routes judgment-heavy work to Slack with the evidence and reason an operator must decide. Provider confirmations are matched to the case and reconciled against its balance.

### 2. External apps and services used

Recoverly connects to more than three external applications in the working flow:

| App / service | How Recoverly uses it |
|---|---|
| Slack | Contract and invoice intake, operator approvals, recovery cards, and payment receipts |
| Payment options | Paystack card checkout, Hedera USDC transfers, and wire/ACH bank instructions; confirmation follows the selected method |
| Resend | Approved demand/reminder email delivery and settlement notifications |
| Gmail | Inbox used in the demo to receive and review collection notices and payment receipts; email delivery uses Resend, with no direct Gmail API integration |
| Twilio | Operator-approved buyer calls and call-status updates |
| Fish Audio | Generates spoken audio from collection scripts for the Twilio call flow |
| ngrok | Secure public HTTPS tunnel for local Slack and Twilio webhook demonstrations |

### 3. Setup instructions

Follow the [Quick start](#quick-start) section to install Python dependencies, create `.env`, run the configuration check, and start the Flask app. For the complete demo, start the application and configure Slack and the selected payment provider's callbacks with an ngrok HTTPS URL. If using USDC, run the wallet watcher in a separate terminal. See [Payments](#payments) for each method's setup and confirmation flow.

### 4. Reliability testing

Recoverly is tested with an isolated automated suite covering case intake, document pairing, approval actions, payment routing, payment transfer matching, reconciliation, deduplication, and notification handling. Tests replace external network calls with controlled doubles, so they do not send real emails, place calls, or move funds. Run the checks in [Testing](#testing). The recorded payment path demonstrates USDC transfer verification before settlement; it does not establish live verification of every payment option.

### 5. Demo video

[Watch the Recoverly demo on YouTube](https://www.youtube.com/watch?v=4p-7WtbMgEM)

## Features

- **Slack-first case intake** — contract and invoice uploads create and enrich a recovery case.
- **Document pairing and preflight** — reads PDFs, identifies key case facts, and routes work by balance and risk.
- **Autonomous Strands worker** — a persistent scheduled agent discovers due work and calls tools that send routine reminders or request a decision.
- **Specialized recovery skills** — Preflight, Investigator, Diplomat, Tone Coach, Voice, Payment, Escalator, and AAA modules provide focused case capabilities.
- **Decision-only interruption** — Slack cards appear for disputes, anomalies, high balances, calls, final notices, and escalation actions.
- **Professional communications** — builds invoice reminders, demand-letter drafts, email messages, and call scripts.
- **Voice escalation** — uses Twilio for approved buyer calls and records the outcome in the case activity.
- **Payments** — offers configured payment methods and reconciles confirmed amounts against the case balance. Available methods are Paystack cards, Hedera USDC, wire transfers, and ACH instructions. See [Payments](#payments) for confirmation details.
- **Local operator console** — shows case activity and agent progress at `/console`.
- **Auditable local state** — keeps case state, activity, payment ledger entries, and audit events locally for the prototype.

## Architecture

```mermaid
flowchart LR
    A[Slack: contract + invoice] --> B[Concierge and intake pairing]
    B --> Q[Case queue]
    T[Persistent APScheduler job] --> S[Recoverly Strands agent]
    Q --> S
    S --> C[list_actionable_cases tool]
    C --> R{Deterministic policy}
    R -->|Routine and low risk| F[Send reminder via Resend]
    R -->|Judgment required| E[Decision card in Slack]
    E -->|Approve or revise| G[Specialized recovery action]
    F --> H{Payment portal: selected method}
    H --> P[Card: Paystack]
    H --> U[USDC: Hedera / HashPack]
    H --> W[Wire / ACH bank instructions]
    P --> V[Verify signed provider webhook]
    U --> M[Verify Mirror Node API response]
    W --> O[Operator confirms bank receipt]
    V --> K[Reconcile case balance]
    M --> K
    O --> K
    K --> L[Receipt + audit event]
```

## Project structure

```text
recoverly/
├── src/
│   ├── webapp.py                # Flask app and registered routes
│   ├── config.py                # Environment-driven configuration
│   ├── agents/                  # Autonomous Strands agent, tools, scheduler, roles, and case state
│   ├── concierge/               # Slack intake, cards, actions, email workflow
│   ├── preflight/               # PDF extraction, document pairing, risk routing
│   ├── diplomat/                # Collection-message templates and delivery
│   ├── investigator/            # Buyer-history and payment-pattern analysis
│   ├── aaa/                     # Arbitration / escalation guidance and letters
│   ├── voice/                   # Twilio call flow, transcripts, signals
│   ├── payments/                # Payment providers, bank instructions, watcher, reconciliation
│   └── local_console/           # Browser-based case activity console
├── web/                         # Payment portal template
├── config/                      # Reference configuration such as referrals
├── data/                        # Local runtime data; ignored by Git where appropriate
├── output/                      # Generated letters and documents
├── samples/                     # Sample intake material
├── tools/                       # Local simulation and configuration commands
├── tests/                       # Committed isolated reliability tests
├── .env.example                 # Safe environment-variable template
├── requirements.txt             # Python dependencies
└── README.md
```

## Technology

| Area | Technology | Purpose |
|---|---|---|
| Application | Python and Flask | Webhooks, payment portal, local console, and HTTP routes |
| Agent runtime | Strands Agents SDK | Autonomous reasoning cycle and consequential recovery tools |
| Background work | APScheduler with a SQLite job store | Persistent 15-minute recovery cycle with one concurrent run |
| Managed deployment | Amazon Bedrock AgentCore Runtime | Optional serverless hosting, versioning, and observability for the Strands worker |
| Collaboration | Slack Events API, Block Kit, and interactive actions | Intake, operator approvals, and case notifications |
| AI workflow | OpenAI-compatible LLM provider | Drafting, analysis, tone review, and role-specific recommendations |
| Email | Resend; Gmail inbox in demo | Resend sends notices and receipts; Gmail displays received messages |
| Voice | Twilio | Approved buyer calls and call-status webhooks |
| Payments | Paystack cards, Hedera USDC, wire / ACH | Method-specific checkout or instructions and payment reconciliation |
| Wallet | HashPack | Demo buyer wallet used to send USDC |
| Document handling | pypdf | Contract and invoice text extraction |
| Local exposure | ngrok | Public HTTPS URLs for local webhook demonstrations |
| Storage | JSON and JSONL files | Prototype case state, audit trail, and watcher state |

## Quick start

Use Python **3.11 or newer**.

```powershell
py -3.14 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Update `.env` with credentials for only the services you plan to demonstrate. Do not commit `.env` or private keys.

Strands is enabled by default and uses the existing OpenAI-compatible Qwen configuration:

```text
RECOVERLY_QWEN_API_KEY=your-key
RECOVERLY_QWEN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
LLM_TIER=1
STRANDS_ENABLED=1
STRANDS_MAX_TOKENS=800
AUTONOMOUS_AGENT_ENABLED=1
AUTONOMOUS_AGENT_INTERVAL_MINUTES=15
AUTONOMOUS_MAX_BALANCE_USD=20000
```

Check the configuration and start the application:

```powershell
.venv/Scripts/python.exe -m tools.config_check
.venv/Scripts/python.exe -m src.webapp
```

The local server listens on `http://127.0.0.1:8400` by default.

| URL | Purpose |
|---|---|
| `/health` | Service health check |
| `/console` | Local multi-agent case console |
| `/pay/<case_id>` | Case-specific settlement portal |
| `/webhooks/paystack` | Signed card-payment settlement events |
| `/slack/events` | Slack Events API request URL |
| `/slack/interactivity` | Slack interactive action request URL |
| `/webhooks/resend` | Resend inbound/reply webhook |
| `/voice-webhook/twiml` | Twilio voice instructions |
| `/voice-webhook/call-status` | Twilio call-status webhook |

For Slack or Twilio callbacks during a local demo, expose port 8400 through ngrok and use the resulting HTTPS URL in the provider configuration.

## Payments

The buyer opens `/pay/<case_id>` and selects a configured payment method. The saved case supplies the amount and invoice context. Confirmation depends on the selected provider; opening checkout or displaying instructions does not itself confirm payment.

### Available methods

| Method | Payment flow | Confirmation |
|---|---|---|
| Card via Paystack | Creates a checkout session and redirects the buyer to the provider | A signed `charge.success` webhook reconciles the payment to the case |
| USDC via Hedera | Provides receiving account, token, amount, and `recoverly:<case_id>` memo; buyer pays with a wallet such as HashPack | The separate wallet watcher reads Mirror Node API responses, matches the transfer, and reconciles it |
| Wire transfer | Displays configured bank details and invoice reference | Operator confirms receipt using a transaction reference |
| ACH | Uses the configured bank-instruction flow | Operator confirms receipt; no automatic bank API monitoring is implemented |

Stripe is a possible future provider extension; it is not currently implemented.

Confirmed payments reduce the outstanding balance. Partial payments leave the remaining balance open. The USDC watcher additionally posts Slack and console receipts and sends email notifications.

### Payment method configuration

Configure the variables for the methods you intend to offer:

| Method | Environment variables |
|---|---|
| Card via Paystack | `PAYSTACK_SECRET_KEY`, `PAYSTACK_CALLBACK_URL`; configure `/webhooks/paystack` for settlement events |
| USDC via Hedera | `HEDERA_NETWORK`, `HEDERA_RECEIVING_ACCOUNT_ID`, `HEDERA_USDC_TOKEN_ID`, optional `HEDERA_MIRROR_NODE`, `WALLET_POLL_INTERVAL_SEC` |
| Wire / ACH instructions | `WIRE_BENEFICIARY_NAME`, `WIRE_BANK_NAME`, `WIRE_BANK_ADDRESS`, `WIRE_SWIFT_BIC`, `WIRE_ACCOUNT_NUMBER`, `WIRE_ROUTING_CODE` |
| Shared payment links | `RECOVERLY_PAYLINK_BASE` pointing to the public application URL ending in `/pay` |

Card webhooks are served by the Flask app. For USDC, also start the watcher in a second terminal:

```powershell
.venv/Scripts/python.exe -m src.payments.wallet_poller
```

The watcher is separate from the Flask server so its polling lifecycle can be managed independently. Wire and ACH use the Slack payment-received workflow with a case ID, amount, and unique bank transaction reference.

## Slack setup

Configure a Slack app with:

- Event request URL: `https://YOUR-PUBLIC-URL/slack/events`
- Interactivity request URL: `https://YOUR-PUBLIC-URL/slack/interactivity`
- Event subscriptions for messages and `file_shared`
- Scopes required for the configured workflow, including file access and chat posting
- A channel configured through `SLACK_CONCIERGE_CHANNEL`

The operator reviews all approve/revise/reject cards in that channel. Do not enable live communication paths until Slack request signing is configured.

## Strands autonomous agent

Starting the Flask application also starts an APScheduler interval job backed by `data/recoverly_jobs.sqlite`. Every 15 minutes, one Recoverly agent built with the Strands Agents SDK calls `list_actionable_cases`, then executes the tool assigned to every returned case. `auto_send_routine_reminder` performs real Resend delivery only for ordinary cases that are 7–14 days overdue, below the configured balance ceiling, have a recipient, are open, and contain no dispute, legal-threat, anomaly, or halt signal. The tool records a receipt and cannot send twice on the same day.

All other situations use `request_operator_decision`, which posts one evidence-based Slack card and waits for approve, revise, or reject input. Calls, final notices, and legal actions always require a person. Payment webhooks and watchers continue the workflow by matching provider-confirmed transfers and closing or updating the case. The browser console is an optional audit view; operators do not need to keep it open or manage the routine queue.

## AgentCore deployment

Recoverly includes an Amazon Bedrock AgentCore Runtime project in `RecoverlyAgent/`. Its runtime entrypoint is `src/agentcore_runtime.py`, which invokes the existing autonomous Strands worker. Set `RECOVERLY_MODEL_PROVIDER=bedrock` and an enabled `BEDROCK_MODEL_ID` for the deployed runtime so it uses the runtime IAM role rather than a Qwen API key. From `RecoverlyAgent/`, run `agentcore validate`, `agentcore package`, then `agentcore deploy`.

The normal flow runs through `python -m src.webapp`. A role can also be invoked directly for development by passing a task after the module name:

```powershell
.venv/Scripts/python.exe -m src.agents.preflight_agent "Review case RC-2026-123456"
.venv/Scripts/python.exe -m src.agents.investigator_agent "Assess the payment history for case RC-2026-123456"
.venv/Scripts/python.exe -m src.agents.diplomat_agent "Draft the next action for case RC-2026-123456"
```

## Local data model

This prototype deliberately uses files instead of a hosted database:

```text
data/
├── case_state/                 # Per-case JSON state
├── local_console/sessions.json # Console sessions and activity
├── audit_trail.jsonl           # Append-only audit events
├── wallet_poller_state.json    # Hedera watcher cursor and deduplication state
├── processed_transactions.json # Applied transaction identifiers
└── wallet_case_ledger.json     # Wallet-to-case associations
```

For production, these records should move to a transactional database such as PostgreSQL, with authenticated users, durable background jobs, encrypted secrets, and a formal audit-retention policy.

## Testing

The test suite is committed so judges can reproduce the reliability checks. It covers case intake, document pairing, approvals, autonomous policy gates, duplicate-send prevention, payment routing, transfer matching, reconciliation, and notification handling. External network calls are blocked during tests.

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check src tests --select E9,F63,F7,F82
.venv/Scripts/python.exe -m pip check
```

Tests use isolated temporary data and test doubles for external services. They do not send real emails, make live calls, or move funds.

## Security and operational notes

- Review all AI-generated wording and escalation actions before sending them to a buyer.

## License

No license has been selected yet. Add an open-source license before public distribution.
