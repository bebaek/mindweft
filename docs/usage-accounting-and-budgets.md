# Durable Usage Accounting and Budget Enforcement

Status: deferred design / implementation backlog.

This document records the proposed follow-up to request rate limiting and concurrent-run
limits. It is intentionally not an active configuration contract. Names, schemas, status
codes, and defaults below are proposals until implementation lands.

## Motivation

Mindweft already receives provider usage metadata and emits usage in run results and stream
events. It also has shared request-rate limits, shared concurrent-run leases, tenant attachment
quotas, and per-thread entitlement limits. Those controls do not answer longer-period questions
such as:

- How many model tokens did a tenant or user consume this month?
- How much budget remains before another model call begins?
- Can two replicas reserve the last available budget without overspending it?
- How are retries, tool loops, cancellation, streaming disconnects, and pod crashes accounted for?

The next quota milestone should therefore be a durable, shared usage ledger with atomic budget
reservation and settlement.

## Goals

- Persist model usage by tenant and user without storing prompts, responses, tool arguments, or
  attachment contents.
- Enforce tenant-wide and optional per-user budgets across all replicas.
- Account for every model invocation in a run, including retries and tool-loop iterations, rather
  than treating a multi-call run as one usage event.
- Cover standard runs, streamed runs, resumed private-consent actions, and peer-agent usage when
  reliable usage metadata is available.
- Reserve budget before a billable model call and settle it against actual usage afterward.
- Recover automatically from process and pod crashes without permanently reserving budget.
- Provide aggregate admin visibility and include the store in readiness, backup, and restore
  verification.
- Preserve compatibility by leaving limits disabled unless explicitly configured.

## Non-goals for the first version

- Invoicing, payment collection, taxation, or a full billing system.
- Hard-coded model prices in application source.
- Exact monetary accounting when a provider does not return trustworthy usage metadata.
- Storing request or response content in the usage database.
- Replacing short-window request-rate or concurrent-run limits.

## Proposed accounting unit

The first version should enforce token budgets rather than currency budgets. Token accounting is
available across more providers and avoids embedding a price catalog that changes independently of
Mindweft releases.

Record the provider's available dimensions separately:

- input tokens
- output tokens
- cached input/read tokens
- cache-write tokens, when reported
- reasoning tokens, when reported separately
- total tokens

A later version may derive cost using a versioned, operator-managed provider/model price catalog.
Derived cost must retain the price-catalog version used so historical totals do not change when
prices are updated.

## Proposed durable model

Use a dedicated shared SQLite database, for example `MINIGENT_USAGE_DB_PATH`, on deployments with
multiple replicas. Keep usage separate from thread content and encrypted private-data stores.

The precise schema can change during implementation, but it should represent two concepts.

### Usage records

An append-only settled record should include:

- immutable record ID and unique idempotency key
- tenant ID and user ID
- thread ID and logical run ID
- model-invocation sequence or attempt ID
- execution source: standard, stream, consent resume, peer, or another defined source
- provider and model identifiers
- token dimensions reported by the provider
- whether usage was actual, estimated, partially reported, or unavailable
- reservation ID, timestamps, and settlement status

Do not store prompts, model responses, message excerpts, tool inputs/results, private-value
references, authorization headers, API keys, or raw provider payloads.

### Reservations

A reservation should include:

- opaque reservation ID and unique idempotency key
- tenant/user/run/invocation scope
- reserved token amount
- budget period identifiers
- lease expiration and heartbeat timestamps
- state such as active, settled, released, or expired

Reservation acquisition, budget checks, and counter updates must run in one `BEGIN IMMEDIATE`
transaction. Expired reservations must be reclaimed atomically before evaluating available budget.

## Budget hierarchy and periods

Initial proposed entitlement limits:

- `max_tokens_per_month` for a tenant
- `max_tokens_per_day` for a tenant
- `max_user_tokens_per_month` for each tenant/user pair
- `max_user_tokens_per_day` for each tenant/user pair

A missing or zero limit should remain disabled, consistent with current rate-limit configuration.
Periods should initially use UTC calendar boundaries. The reset timestamp used for enforcement must
be stored or derived consistently and returned to clients on rejection.

Tenant-specific limits should ultimately live in durable entitlements or tenant records, not only
in deployment environment variables. Static configuration may provide bootstrap defaults and an
observe-only rollout mode.

## Reservation and settlement flow

Before each model invocation:

1. Resolve the authenticated tenant/user context and applicable entitlements.
2. Derive a conservative reservation from the provider/model maximum output, known input size, and
   configured safety margin.
3. In one transaction, expire stale reservations, calculate settled plus active reserved usage,
   enforce tenant and user limits, and insert the reservation.
4. Begin the model call only after reservation succeeds.
5. Heartbeat the reservation if a model call may outlive its lease.
6. Settle the reservation exactly once using actual provider usage.
7. Release unused reserved capacity. If the call fails before usage is incurred, release the full
   reservation. If usage may have been incurred but is unknown, record that uncertainty and follow
   the configured fail-open or fail-closed policy.

Idempotency keys must prevent retries, disconnect cleanup, and duplicate callbacks from charging the
same provider response twice. Settlement must also be safe when the actual count exceeds the
reservation: record the actual amount, flag the overage, and block subsequent calls rather than
silently discarding usage.

Run-level orchestration must release both budget reservations and concurrent-run leases on all exit
paths. Budget reservation must happen before every provider call, not merely once at the HTTP
endpoint, because one run can perform multiple model iterations.

## Streaming, cancellation, and crash recovery

- A streaming response must reserve capacity before billable work starts.
- Client disconnect or cancellation must attempt to cancel the provider call, then settle known
  usage or release the reservation when no usage occurred.
- Reservation leases must expire after a pod crash so capacity is not held forever.
- A heartbeat interval must be shorter than the reservation lease and renewal failure should cancel
  the owning model call where practical.
- Shutdown draining should stop new reservations and allow active calls to settle during the grace
  period.

## Peer-agent and incomplete usage

Peer-agent usage should be recorded only when the peer returns structured, attributable usage.
Keep local and peer usage distinguishable. Do not invent exact token counts from response character
lengths and label them as provider-reported usage.

For providers or peers that omit usage, choose and document one policy:

- conservative estimated settlement
- block budget-enforced execution for that provider
- allow execution but mark usage unavailable and expose the accounting gap

Production enforcement should not imply exact totals while unaccounted providers remain enabled.

## API behavior

A budget rejection should be structured and machine-readable. Proposed shape:

```json
{
  "detail": {
    "error": "usage_budget_exceeded",
    "scope": "tenant",
    "period": "month",
    "reset_at": "2030-01-01T00:00:00Z",
    "retry_after_seconds": 86400
  }
}
```

HTTP `429` is the initial recommendation because the request may succeed after the period resets.
The response should include bounded `Retry-After` and avoid exposing another user's usage.

Proposed admin endpoint:

```http
GET /admin/tenants/{tenant_id}/usage?period=month
```

Safe aggregate output may include:

- period start, end, and reset time
- settled and actively reserved tokens
- remaining tenant budget
- active reservation count
- aggregate provider/model breakdown
- counts of actual, estimated, partial, and unavailable usage records

Per-user breakdowns should require an explicit administrative permission if granular admin roles
exist by implementation time. The default endpoint must not expose prompts, responses, thread
messages, tool content, credentials, or raw provider payloads.

## Observability

Add low-cardinality metrics and structured logs for:

- reservation accepted, rejected, renewed, expired, released, and settled
- rejection scope and period
- settled token dimensions
- settlement source: actual, estimated, partial, or unavailable
- duplicate/idempotent settlement attempts
- reservation overages and renewal failures

Avoid tenant and user IDs as unbounded metric labels. IDs may appear in access-controlled structured
logs under the existing logging policy, but content must not.

## Rollout plan

1. **Ledger only:** persist usage and compare it with emitted run metadata; no rejection.
2. **Observe-only reservations:** compute decisions and log would-reject outcomes without blocking.
3. **Tenant budgets:** enable conservative tenant limits for selected tenants.
4. **User budgets:** enable per-user limits after admin visibility and support procedures exist.
5. **Cost derivation:** optionally add a versioned operator-managed price catalog.

Every phase should preserve a quick disable switch that stops new enforcement without deleting
historical accounting records.

## Testing and operational acceptance

Implementation is not complete until tests cover:

- atomic reservation races across independent SQLite connections
- tenant and user isolation
- daily/monthly boundary behavior
- idempotent settlement and duplicate callbacks
- actual usage below, equal to, and above the reservation
- tool-loop and retry calls charged as separate invocations
- standard, streaming, consent-resume, cancellation, and disconnect paths
- stale reservation expiration and heartbeat renewal
- unknown or partial provider usage policy
- admin aggregate privacy boundaries
- readiness failure when a configured shared store is unavailable
- online backup and isolated restore with schema, integrity, reservation, settlement, expiry, and
  idempotency canaries
- a production cross-replica reservation/rejection/renewal/settlement canary before enabling hard
  budgets

## Open decisions

Resolve these before implementation begins:

- Which token dimensions consume each budget, especially cached and reasoning tokens?
- Should input reservation use exact serialized provider input or a conservative estimator?
- What is the policy when actual provider usage is unavailable?
- Are UTC calendar periods sufficient, or do tenants require billing time zones and custom cycles?
- Should tenant limits be explicit overrides, plan-derived defaults, or both?
- How should peer-agent usage be trusted and reconciled?
- Does budget rejection use `429`, `402`, or a tenant-policy-specific status?
- What retention and aggregation schedule is required for detailed usage records?
- Which granular admin role may view per-user usage?
