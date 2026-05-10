# EKS AI Ops Toolkit — First Demo Step-by-Step

> Hands-on walkthrough for running the proactive incident flow and the
> interactive Slack bot end-to-end on a fresh AWS account + Slack workspace.
>
> Repo: `eks-ai-ops-toolkit/`
> You start with: AWS account, Slack workspace. Nothing else configured.

---

## 0. One-time local setup

```bash
# In repo root
cd eks-ai-ops-toolkit

# Tooling
brew install awscli aws-sam-cli jq        # macOS
aws --version && sam --version

# AWS credentials (pick a default region you'll use everywhere)
aws configure                              # set region e.g. us-east-1
aws sts get-caller-identity                # confirm

# Python env
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Local env file (only used for local tests; Lambda reads from SSM)
cp .env.template .env
```

### Enable Bedrock model access (one-time, in AWS Console)

1. AWS Console → **Bedrock** → **Model access** → **Manage model access**.
2. Enable **Amazon Nova Lite** (`us.amazon.nova-lite-v1:0`).
3. (Optional, for interactive intent classifier) enable **Amazon Nova Micro** (`us.amazon.nova-micro-v1:0`).
4. Wait until status shows **Access granted**.

You're choosing Bedrock for the demo (one fewer secret than Anthropic API).

### Sanity check

```bash
make test-fast        # quick test pass, no coverage gate
```

---

## 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name: `EKS AI Ops`. Pick your workspace.
3. **OAuth & Permissions** → Bot Token Scopes → add:
   - `chat:write`
   - `app_mentions:read`
   - `channels:history`
4. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`).
5. **Basic Information** → copy the **Signing Secret**.
6. In Slack, create channel `#sre-alerts` and `/invite @EKS AI Ops`.

> Don't configure Event Subscriptions yet — you need the API Gateway URL from `sam deploy` first.

---

## 2. Push secrets to SSM

```bash
make setup-ssm
```

You'll be prompted for:
- Slack Bot Token → paste `xoxb-...`
- Slack Signing Secret → paste it
- GitHub Token → paste `placeholder` (skip for now; only needed for auto-fix PRs in Scenario A)
- Anthropic API Key → **press Enter to skip** (Bedrock-only demo)

Verify:
```bash
aws ssm get-parameters-by-path --path /eks-ai-ops-toolkit --query 'Parameters[].Name'
```

> **Anthropic key was skipped**: the SAM template still references `/eks-ai-ops-toolkit/anthropic-api-key`. Create a placeholder so the stack deploys:
>
> ```bash
> aws ssm put-parameter --name /eks-ai-ops-toolkit/anthropic-api-key \
>   --value "unused-bedrock-mode" --type SecureString --overwrite
> ```

---

## 3. Deploy the stack

First-time guided deploy:

```bash
make deploy
```

Answers:
- Stack name: `eks-ai-ops-toolkit` (default)
- Region: same as `aws configure`
- `ClusterName`: name of an existing EKS cluster (any cluster you have; if none, create a tiny one with `eksctl create cluster --name demo --nodes 1 --node-type t3.small`)
- `SlackChannel`: `#sre-alerts`
- `GitHubRepo`: `your-handle/sandbox` (any repo; only used if PR creation is triggered)
- `GitHubBaseBranch`: `main`
- `EksMcpServer`: `eks` (default)
- `McpGatewayUrl`: **leave empty** for first demo
- `IntentUseLlm`: `false`
- `IntentModelId`: default
- Confirm changes: `y`
- Allow IAM role creation: `y`
- Save args to samconfig.toml: `y`

After it completes, **save the outputs** (especially `SlackEventsUrl` if defined; otherwise it's the API Gateway URL of `SlackBotFunction`):

```bash
aws cloudformation describe-stacks --stack-name eks-ai-ops-toolkit \
  --query 'Stacks[0].Outputs' --output table
```

---

## 4. Wire the Slack event URL (for Scenario B)

1. Slack app → **Event Subscriptions** → **Enable Events**.
2. Request URL: `<API Gateway URL from step 3>/slack/events` (Slack will verify ✓).
3. Subscribe to bot events: `app_mention`.
4. **Save Changes** → Slack may prompt to reinstall app → do it.

---

## Scenario A — Proactive incident demo

Goal: a CloudWatch alarm flips to ALARM → Lambda runs analysis → Slack message appears.

### A.1 Create a throwaway alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name demo-eks-cpu-high \
  --metric-name CPUUtilization \
  --namespace AWS/EC2 \
  --statistic Average \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching
```

### A.2 Force the alarm into ALARM state

```bash
aws cloudwatch set-alarm-state \
  --alarm-name demo-eks-cpu-high \
  --state-value ALARM \
  --state-reason "demo trigger"
```

### A.3 Watch it land

```bash
make logs-agent           # tail the proactive Lambda
```

Within ~10–30s you should see:
- Lambda invocation log line
- A new message in `#sre-alerts` with the analysis block
- A row in DynamoDB:

```bash
aws dynamodb scan --table-name sre-incidents --max-items 5 \
  --query 'Items[].{id:incident_id.S, slack_ts:slack_ts.S}'
```

### A.4 Cleanup (cost: $0 — stops billing for this scenario)

Delete the throwaway alarm so it doesn't keep evaluating:

```bash
make destroy-demo                        # deletes demo-eks-cpu-high
```

If you're done with the whole demo (Scenario A and B), jump straight to **Teardown** at the bottom — that's the one-shot cleanup target.

---

## Scenario B — Interactive Slack bot demo

Goal: mention the bot in `#sre-alerts` → orchestrator classifies intent → bot replies in thread.

> For this first demo `MCP_GATEWAY_URL` is empty, so the specialist falls back to LLM-only answering (no real `kubectl` calls). That's enough to prove the orchestrator + intent + Bedrock pipeline.

### B.1 Test a non-K8s message (early-exit path)

In `#sre-alerts`:
```
@EKS AI Ops what's the weather today?
```

Expected: bot replies in thread that it only handles EKS/K8s questions.

### B.2 Test a K8s troubleshooting message (specialist path)

```
@EKS AI Ops one of my pods is in CrashLoopBackOff, what should I check first?
```

Expected: threaded reply with a structured K8s troubleshooting answer (image pull errors, init failures, OOM, readiness probe, logs, describe events, etc.).

### B.3 Tail logs while testing

```bash
make logs-bot
```

You should see the Slack signature verification, the orchestrator log line, and the Bedrock invocation.

### B.4 Cleanup

Nothing scenario-specific to clean up beyond the Slack messages themselves —
the bot stays available until you run **Teardown** below. Slack app config
(Event Subscriptions, OAuth scopes) lives in api.slack.com and is free; you can
leave it or delete the app from <https://api.slack.com/apps>.

---

## Troubleshooting quick hits

| Symptom | Fix |
|---|---|
| Slack URL verification fails | Confirm `SLACK_SIGNING_SECRET` SSM value matches the app; check `make logs-bot` |
| Bot stays silent | Bot not invited to channel, or `app_mention` event not subscribed |
| Bedrock `AccessDeniedException` | Model access not enabled in this region — go back to step 0 |
| Lambda `ResourceNotFoundException` on SSM | Anthropic param placeholder not created — see step 2 |
| `make deploy` asks for VPC subnets | The kubectl helper needs them; see the `Kubectl helper VPC` section in `README.md` for the SSM parameter setup |

---

## Teardown — zero ongoing AWS cost

One command nukes every billable resource this project created:

```bash
make destroy-confirm                     # prompts before deleting
# or, non-interactive:
make destroy                             # same, no prompt
```

What that removes:

| Resource | How | Why it matters |
|---|---|---|
| CloudFormation stack `eks-ai-ops-toolkit` | `sam delete` | Lambdas, API Gateway, EventBridge rules, security group |
| DynamoDB tables `sre-incidents`, `sre-deployments` | `aws dynamodb delete-table` | Tables have `DeletionPolicy: Retain`, so `sam delete` leaves them on purpose |
| SSM parameters under `/eks-ai-ops-toolkit/*` | `aws ssm delete-parameters` | Free, but tidies the namespace |
| Lambda log groups | `aws logs delete-log-group` | Auto-created by Lambda, **not** managed by CFN, otherwise billed for storage |
| Demo CloudWatch alarm `demo-eks-cpu-high` | `aws cloudwatch delete-alarms` | First alarm is free; this is just hygiene |

Verify everything is gone:

```bash
aws cloudformation describe-stacks --stack-name eks-ai-ops-toolkit 2>&1 | head -1
# expect: "Stack with id eks-ai-ops-toolkit does not exist"

aws dynamodb list-tables | grep -E 'sre-incidents|sre-deployments' || echo "clean"
aws ssm get-parameters-by-path --path /eks-ai-ops-toolkit --query 'Parameters[].Name'
```

Different stack name or region? Override:

```bash
make destroy STACK=my-stack REGION=eu-west-1
```

> **Bedrock model access** is account-level config and incurs no standing cost
> — leave it enabled. **EKS clusters** you created with `eksctl create cluster`
> for the demo are **not** managed by this stack and bill independently:
>
> ```bash
> eksctl delete cluster --name demo --region us-east-1
> ```

---

## Next steps after the demo works

1. Stand up a real MCP gateway and set `McpGatewayUrl` so the interactive bot can run real EKS tool calls (see `README.md` → `MCP gateway`).
2. Flip `IntentUseLlm=true` to use Nova Micro for sharper intent classification.
3. Point `GitHubRepo` at a real infra repo and test the auto-fix PR path.
