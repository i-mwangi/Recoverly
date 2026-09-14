# Deploy Recoverly to AgentCore

RecoverlyAgent is the AgentCore Runtime project. It packages the repository root and invokes `src/agentcore_runtime.py`, which calls the same autonomous Strands workflow used locally.

## Prerequisites

Install AWS CLI v2 and sign in with the AWS account that will own the runtime by running `aws login`. Install the AgentCore CLI with `npm install -g @aws/agentcore`. Enable access to your selected Bedrock model in the deployment region.

Set the AgentCore Runtime environment values through its AWS deployment configuration or a secrets provider:

```text
RECOVERLY_MODEL_PROVIDER=bedrock
BEDROCK_MODEL_ID=your-enabled-bedrock-model-id
STRANDS_ENABLED=1
AUTONOMOUS_AGENT_ENABLED=1
AUTONOMOUS_MAX_BALANCE_USD=20000
```

The runtime IAM role needs Bedrock model invocation permission. It also needs access to the durable case and audit store used by Recoverly. The current local JSON store is appropriate for the desktop demo only; deploy the case store to a durable shared service before scheduling production cycles.

## Validate and deploy

```powershell
cd RecoverlyAgent
agentcore validate
agentcore package
agentcore deploy
```

Invoke a deployed runtime with a date when needed:

```powershell
agentcore invoke --input '{"reference_date":"2026-09-14"}'
```

Use EventBridge Scheduler or an equivalent authenticated scheduler to invoke the AgentCore runtime every 15 minutes. Keep the Flask service for Slack, Resend, Twilio, Paystack, and Hedera webhooks.
