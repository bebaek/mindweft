# User data retention proposal

Status: future work; not implemented.

## Purpose

Mindweft stores user-associated data across several optional stores. This proposal defines a
future retention and erasure model that coordinates those stores, supports account deletion and
inactive-thread pruning, and leaves enough audit evidence to operate the service safely.

Retention periods in this document are illustrative product defaults, not legal advice. A
deployment must choose periods that satisfy its jurisdiction, contracts, legal holds, incident
response requirements, and infrastructure backup policy.

## Current behavior

Mindweft already has several independent lifecycle mechanisms:

- Users can delete a thread. The API removes the thread, its attachments, and its private-value
  state.
- Platform administrators can delete or prune tenant threads by `updated_before` and optional
  thread filters.
- Pending, unreferenced attachments expire automatically.
- Private values, consent requests, consent grants, pending private actions, and private
  disclosure audits have bounded lifetimes.
- Changing a tenant user to `suspended` or `deleted` creates a durable deprovisioning event. The
  deprovisioning worker removes user MCP catalog assignments and disables matching external
  grants.
- Deleting a tenant user is currently a status transition. It is not physical erasure of the
  user's records.
- Personal execution configuration and encrypted personal MCP credentials support explicit
  deletion.

These mechanisms do not yet form a complete retention policy. In particular, the administrator
thread deletion and pruning paths currently remove records only from the thread store, unlike the
user-facing thread deletion path, which also cleans up attachments and private state. Automated
retention must not build on these inconsistent deletion paths.

## Goals

1. Disable access immediately when an account is suspended or deleted.
2. Allow a configurable recovery or export grace period before irreversible erasure.
3. Delete eligible user content across every configured store.
4. Preserve only minimal, policy-required audit evidence after erasure.
5. Support legal holds without restoring account access.
6. Be safe to retry after process crashes and safe to run on multiple replicas.
7. Provide dry-run inventory, progress, metrics, and dead-letter recovery.
8. Apply the same cascade behavior to user deletion, administrator deletion, and scheduled
   retention.

## Non-goals

- Defining legally correct retention periods for every deployment.
- Treating suspension as a deletion request.
- Promising immediate removal from snapshots or backups outside the application databases.
- Per-user cryptographic erasure with the current shared store keys.
- Deleting tenant-shared data merely because one user originally executed work in it.

## Lifecycle model

Use four distinct lifecycle states:

1. **Suspended**: access is blocked and external grants are disabled, but data is retained. This
   state is reversible and does not start a purge clock.
2. **Deleted / pending purge**: access remains blocked and a configurable grace period begins.
   Export, restoration, and legal-hold evaluation remain possible.
3. **Purging**: a durable background job removes eligible records across stores. New activity for
   the subject remains blocked.
4. **Purged**: direct identifiers and user content have been removed. Only a minimal pseudonymous
   tombstone and required audit evidence remain.

Only an explicit transition to `deleted` should schedule user purge. A tenant may permit restoring
the user during the grace period, provided purge has not started.

## Data classes

| Data class | Proposed lifecycle |
| --- | --- |
| Pending unreferenced attachments | Keep the existing short TTL. |
| Private values | Keep the existing short TTL. |
| Consent requests, grants, and pending actions | Keep their existing expiry behavior. |
| Private disclosure audit | Keep the existing bounded policy unless deployment requirements override it. |
| Threads, messages, and compacted context | Delete with the owning account or after a configured inactivity period. |
| Referenced attachments | Follow the lifecycle of the thread that references them. |
| Personal execution configuration | Delete during user purge. |
| Personal MCP credentials | Delete early in purge, after any applicable recovery grace period. |
| Local identity and password setup records | Disable immediately; physically delete or pseudonymize during purge. |
| Rate-limit buckets and expired run leases | Use a short operational TTL and clear user-scoped state during purge. |
| User deprovisioning events | Retain for an operational audit period, then prune. |
| Administrative audit | Retain longer if required, but pseudonymize the erased user. |
| Application logs and traces | Govern through a separate infrastructure retention policy. |
| Backups and snapshots | Expire through a documented infrastructure schedule. |

## Ownership must be explicit

Threads currently carry `execution_user_id`, but execution identity is not necessarily ownership.
Before user purge can delete threads safely, define whether a thread is private, tenant-shared,
owned but shared, or collaborative.

Add an explicit, indexed `owner_user_id` to persistent threads if one-user ownership is the product
contract. If collaborative threads are required, add membership and deletion semantics instead.
Legacy threads whose ownership cannot be established safely should be retained for review rather
than assigned by guesswork.

A separate activity timestamp may also be necessary. Generic `updated_at` can change because of
background title generation or other maintenance; `last_user_activity_at` would provide a clearer
basis for inactivity retention.

## Unified thread deletion

Introduce one deletion service used by every API and retention worker:

```python
class ThreadDeletionService:
    def delete_thread(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        actor_user_id: str,
        reason: str,
    ) -> ThreadDeletionResult: ...
```

The service should:

1. Validate the thread and reject or coordinate deletion of an active run.
2. Delete the thread, messages, compacted context, run state, and cancellation state.
3. Delete all attachments for the thread.
4. Clear private values for the thread.
5. Clear private consent requests, grants, actions, and disclosure records according to policy.
6. Append a minimal deletion audit record.

User deletion, administrator deletion, administrator pruning, and scheduled pruning must all use
this service or equivalent bulk operations in every store. This consistency change should precede
all automated retention work.

## Retention configuration

Expose deployment defaults through unified TOML, with environment overrides following existing
Mindweft naming conventions. An illustrative configuration is:

```toml
[retention]
enabled = false
deleted_user_grace_days = 30
thread_inactivity_days = 365
admin_audit_days = 365
deprovisioning_event_days = 90
purge_interval_seconds = 300
purge_batch_size = 100
```

Possible environment aliases are:

```text
MINDWEFT_RETENTION_ENABLED
MINDWEFT_RETENTION_DELETED_USER_GRACE_DAYS
MINDWEFT_RETENTION_THREAD_INACTIVITY_DAYS
MINDWEFT_RETENTION_ADMIN_AUDIT_DAYS
MINDWEFT_RETENTION_DEPROVISIONING_EVENT_DAYS
MINDWEFT_RETENTION_PURGE_INTERVAL_SECONDS
MINDWEFT_RETENTION_PURGE_BATCH_SIZE
```

Start with retention disabled so deployments must opt in after reviewing policy and backups. If
tenant overrides are added, deployment policy should set safe lower and upper bounds rather than
allowing arbitrary values.

## Durable user purge workflow

Use a dedicated purge queue instead of overloading external-grant deprovisioning events. The two
workflows have different ordering, retry, and audit requirements.

A durable purge job needs at least:

```text
id
tenant_id
user_id
user_record_id
reason
requested_by
requested_at
purge_after
state
claimed_at
completed_at
attempts
next_attempt_at
last_error
progress_json
```

Suggested states are `pending`, `processing`, `blocked_by_hold`, `completed`, and `dead_letter`.
Claiming should use a lease, and stale claims should become eligible for retry, following the
existing user-deprovisioning worker pattern.

Because data can reside in separate SQLite databases and external systems, user purge cannot be a
single transaction. Implement it as an idempotent, resumable saga. Each participating store should
support preview and purge operations:

```python
class UserDataPurger(Protocol):
    def preview_user_data(self, tenant_id: str, user_id: str) -> PurgeCounts: ...

    def purge_user_data(self, tenant_id: str, user_id: str) -> PurgeResult: ...
```

Likely participants include the thread, attachment, private-value, private-consent,
administration/configuration, and rate-limit stores, plus external grant providers. Every step
must be safe when repeated after a crash. Persist completed steps, or make completion derivable
from there being no matching records.

### Proposed purge order

1. Confirm that the user remains deleted and the grace period has elapsed.
2. Check legal holds.
3. Confirm external deprovisioning has completed, or record why purge is blocked.
4. Disable or delete the local identity and invalidate password setup tokens.
5. Block or cancel active runs owned by the user.
6. Enumerate user-owned threads and cascade-delete them.
7. Delete remaining user-scoped private values, consents, and pending actions.
8. Delete encrypted personal MCP credentials.
9. Delete personal execution configuration and subject assignments.
10. Remove user-specific rate-limit state and expired leases.
11. Pseudonymize or remove the tenant membership record.
12. Append a minimal purge audit event and mark the job complete.

OAuth credentials currently lack tenant/user ownership in their storage schema. They must not be
deleted as user data until ownership is represented explicitly.

## Legal holds

Add an explicit hold model:

```text
id
tenant_id
subject_type  # tenant, user, or thread
subject_id
reason
created_by
created_at
expires_at
released_by
released_at
```

A hold prevents account purge and inactivity pruning but does not restore account access or prevent
credentials and external grants from being disabled. Dry-run previews and operator diagnostics
must report holds without exposing protected content.

## API and console operations

Possible administrator operations are:

```text
GET  /admin/tenants/{tenant_id}/retention-policy
PUT  /admin/tenants/{tenant_id}/retention-policy
POST /admin/tenants/{tenant_id}/users/{user_id}/purge-preview
POST /admin/tenants/{tenant_id}/users/{user_id}/purge
GET  /admin/tenants/{tenant_id}/user-purge-jobs
GET  /admin/tenants/{tenant_id}/user-purge-jobs/{job_id}
POST /admin/tenants/{tenant_id}/user-purge-jobs/{job_id}/retry
```

A preview should return eligibility, holds, and aggregate counts, never message or credential
content. Destructive operations require confirmation and should expose conflict details when a
hold, active run, grace period, or concurrent purge blocks progress.

A future self-service deletion flow should explain the grace period and retained audit evidence,
offer export, require recent authentication, invalidate ordinary sessions, and show deletion
status without exposing internal worker errors.

## Scheduled inactive-thread pruning

The existing `updated_before` pruning mechanism is a useful foundation, with these additions:

- Exclude running threads by default.
- Check active run leases immediately before deletion.
- Respect legal holds.
- Use unified cascade deletion.
- Process bounded batches with a stable cursor such as `(retention_timestamp, thread_id)`.
- Preserve dry-run preview.
- Record policy version, cutoff, candidate count, deletion count, and failure count.
- Base eligibility on a defined retention timestamp rather than incidental maintenance updates.

## Audit after erasure

A purge audit should prove that policy ran without recreating the erased dataset. Retain only the
purge job ID, tenant ID, a pseudonymous subject digest, required actor identity, policy version,
timestamps, aggregate counts, and completion state.

Do not retain profile fields, thread titles, message excerpts, attachment names, credential values,
or indefinite lists of deleted thread IDs. If correlation requires a stable user digest, use an
HMAC with a separately managed audit key rather than an unsalted hash of a predictable user ID.

## SQLite, logs, and backups

A SQL `DELETE` does not guarantee immediate removal of bytes from SQLite free pages, WAL files,
snapshots, or backups. The operational policy must cover checkpointing, WAL handling, periodic
`VACUUM` or `auto_vacuum`, encrypted storage volumes, replicas, snapshots, logs, traces, and
third-party provider retention.

Current encrypted stores use shared, versioned store keys rather than one key per user. Removing a
single user's records therefore cannot use per-user crypto-shredding. Per-user envelope keys would
be a separate design if that guarantee becomes necessary.

User-facing retention language should state the backup expiry window and distinguish logical
application deletion from eventual backup expiration.

## Delivery plan

### Phase 1: deletion correctness

- Add the unified thread deletion service.
- Route user deletion, administrator deletion, and pruning through it.
- Prevent accidental pruning of active runs.
- Add cascade and idempotency tests.

### Phase 2: retention foundation

- Add retention configuration and policy validation.
- Define thread ownership and retention timestamps.
- Add paginated candidate enumeration and dry-run inventory.
- Add legal holds.

### Phase 3: user purge

- Add the durable purge queue and leased worker.
- Add preview/purge methods to user-scoped stores.
- Schedule purge from the deleted-user transition.
- Add administrator status, confirmation, and retry operations.
- Keep automatic execution feature-flagged during rollout.

### Phase 4: operations

- Add metrics for eligible records, purge latency, failures, and dead letters.
- Add scheduled pruning for audit and completed deprovisioning records.
- Document deployment-specific log, provider, and backup retention.
- Run failure-injection tests for every saga checkpoint.

## Required tests

- Suspension never starts content purge.
- Deletion does not purge during the grace period.
- Legal holds block user purge and inactive-thread pruning.
- Running threads cannot be pruned.
- Every thread deletion path removes attachments and private state.
- Replaying a partial or completed purge is safe.
- A crash after any purge step resumes without duplicate external effects.
- Tenant isolation applies to previews, holds, and purge jobs.
- Ambiguous or shared thread ownership does not cause data loss.
- Personal credentials and execution configuration are gone after purge.
- Retained audits contain no erased profile or content fields.
- In-memory and SQLite stores implement equivalent behavior.
- Dead-letter jobs can be inspected and retried safely.
