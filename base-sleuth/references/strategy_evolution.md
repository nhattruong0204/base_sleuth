# Base Sleuth — Strategy Evolution Log

This document tracks the evolution of the gem-hunting strategy based on paper trading results on Base chain.

**AGENT INSTRUCTION**: Update this document after every 10 trades with performance analysis and strategy adjustments. This is how you learn and improve.

---

## Current Strategy Version: 1.0

**Last Updated**: 2026-02-07 (initial)
**Based on**: Live Clanker API data analysis (431K+ tokens, 38K/day)

### Active Scanning Strategy

| Source | Interval | Priority | Expected Hit Rate |
|--------|----------|----------|-------------------|
| Champagne scan | 120s | ⭐ Highest | ~58% have real liquidity |
| Direct launches (firehose) | 30s | Medium | ~5% survive |
| Clawnch launches | 30s | Low | ~2% survive |
| Bankr launches | skipped | ❌ Skipped | <0.5% survive |

### Active Scoring Weights

| Stage | Weight | Threshold |
|-------|--------|-----------|
| S2 DEX Metrics | 0.30 | liq≥$500, vol_1h≥$50 |
| S3 Momentum | 0.20 | 5+ buys/hr, 50% surge |
| S4 Smart Money | 0.20 | any tracked wallet |
| S5 Context | 0.15 | social links, origin URL |
| Champagne Bonus | +0.15 | flat additive |
| **Alert Threshold** | — | **≥ 0.45** |

### Active Exit Rules
- Stop Loss: -30% (mandatory, never override)
- TP1: +50% (sell 33%)
- TP2: +100% (sell 33%)
- TP3: +300% (sell remaining)
- Time stop: Exit if flat after 24h

### Active Pre-Filter
- Skip Bankr: **true** (92% of tokens)
- Require social links: **false**
- Scam keywords: rug, scam, honeypot, ponzi, fake, drain
- Recheck delay: 300s (DexScreener indexing time)

---

## Evolution History

### Iteration #0 — 2026-02-07 — Initial Setup

**Strategy**: Based on comprehensive analysis of live Clanker data.

**Key Findings That Shaped v1.0**:
1. 431K+ tokens deployed, ~38K/day — need aggressive pre-filtering
2. 92% are Bankr bot launches — auto-skip saves massive API cost
3. Only 96 champagne tokens out of 431K (0.02%) — strongest quality signal
4. Champagne tokens: 58% have real liquidity ($5K+) vs <1% regular
5. Only ~5% of all tokens appear on DexScreener at all
6. DexScreener needs 2-5 minutes to index new Base tokens

**Hypotheses to Validate**:
- Is 0.45 score threshold too low/high for quality alerts?
- Does momentum (Stage 3) actually predict 1h-forward returns?
- How many smart money wallets do we need tracked to be useful?
- Is skipping ALL Bankr tokens too aggressive? (Some champagne are Bankr)
- Does time-of-day matter for Base token launches?
- What's the optimal recheck delay? (Currently 300s)

**Baseline Metrics**:
- Pre-filter rejection rate: ~92% (Bankr skip)
- Expected eval candidates per day: ~3,000
- Expected alerts per day: unknown (need data)
- Champagne alerts per day: 0-2 (very rare)

---

<!--
TEMPLATE FOR FUTURE ITERATIONS:

### Iteration #X — YYYY-MM-DD

**Trades Analyzed**: #XX to #XX

#### Performance Metrics
- Win Rate: XX% (target: >35%)
- Average Win: +XX%
- Average Loss: -XX%
- Profit Factor: X.XX
- Net P&L: +/-$XXX
- Largest Win: $XX (+XX%)
- Largest Loss: $XX (-XX%)
- Avg Hold Time: Xh
- Champagne Win Rate: XX%
- Non-Champagne Win Rate: XX%

#### Stage Accuracy Analysis

Which stages predicted winners?

| Stage | Accuracy | Correlation with Profit |
|-------|----------|------------------------|
| S2 DEX Metrics | XX% | +/- |
| S3 Momentum | XX% | +/- |
| S4 Smart Money | XX% | +/- |
| S5 Context | XX% | +/- |
| Champagne Bonus | XX% | +/- |

#### Pattern Analysis

**What Worked (Keep/Increase Weight)**:
1. [Pattern that predicted wins]
2. [Factor that correlated with success]

**What Failed (Remove/Decrease Weight)**:
1. [Pattern that led to losses]
2. [Factor that didn't predict well]

**New Observations**:
1. [New pattern discovered]
2. [Market condition insight]

#### Strategy Adjustments Made

**Scoring Weight Changes**:
- [Stage X]: Weight changed from Y to Z because [reason]
- [New signal added]: [Description] because [reason]

**Threshold Changes**:
- Score threshold: changed from X to Y because [reason]
- [Metric] minimum: changed from X to Y because [reason]

**Pre-Filter Changes**:
- [Filter]: Added/Removed because [reason]

#### Updated Hypothesis

[What you now believe about Base token survival based on data]

#### Next Iteration Focus

[What to watch for in next 10 trades]

-->

---

## Key Learnings Repository

**ADD INSIGHTS HERE AS DISCOVERED:**

### Market Conditions
- [e.g., "Base tokens perform better when ETH is consolidating"]

### Timing
- [e.g., "US market hours produce more organic launches"]

### Token Characteristics  
- Champagne tag is by far the strongest quality signal (58% liquidity rate)
- Non-Bankr tokens have ~10x higher survival rate
- [ADD MORE AS DISCOVERED]

### Platform Signals
- Direct launches > Clawnch > Bankr (quality ordering)
- [ADD MORE AS DISCOVERED]

### Clanker Ecosystem
- Token volume has been increasing (~38K/day as of Feb 2026)
- Champagne curation is extremely selective (96/431K)
- [ADD MORE AS DISCOVERED]

---

## Failed Strategies Archive

**Document strategies that were tried and abandoned:**

<!--
### [Strategy Name] — Abandoned YYYY-MM-DD

**What it was**: [Description]
**Why it failed**: [Reason with data]
**Lesson**: [What to remember]
-->
