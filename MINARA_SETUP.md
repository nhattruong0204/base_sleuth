# Minara Setup

This repo uses Minara only for the `New Token Alert` path. Other Telegram messages
such as wallet buys, convictions, Nansen signals, milestones, dead-token notices,
and flow alerts intentionally skip Minara to avoid unnecessary API or x402 spend.

## Bot Integration

The bot supports two Minara auth methods:

- `api_key`: subscription/API-key access through `https://api-developer.minara.ai/v1/developer/chat`.
- `x402`: pay-as-you-go access through `https://x402.minara.ai/x402/chat`.

Example `config.yaml` for x402:

```yaml
minara:
  enabled: true
  auth_method: "x402"
  x402_chain: "base"
  mode: "fast"
  include_thesis: true
  gate_alerts: false
  fail_open: true
```

Environment:

```bash
EVM_PRIVATE_KEY=0x...
```

Use a dedicated funded wallet with USDC on Base. Do not use your main wallet.

Install optional Python dependencies only when using x402:

```bash
pip install -r requirements-x402.txt
```

## Minara Skill For Development

The Minara Skill is separate from the bot's API integration. It is for using
Minara from tools such as Cursor, VS Code, Claude Desktop, or other MCP-ready
clients during development.

The Minara CLI is installed globally on this machine:

```bash
minara --version
# 0.4.7
```

`clawhub` was not found locally, so the ClawHub skill step still needs the
ClawHub client before it can be completed.

Install path from Minara docs:

```bash
npm install -g minara
minara login
clawhub install minara
```

The login step is interactive and may open a browser or print a login URL. Run it
only when you are ready to connect your Minara account.

Useful installed CLI commands for development:

```bash
minara login --device
minara ask "Analyze this Base token thesis..."
minara research "Deep research this token/project..."
```
