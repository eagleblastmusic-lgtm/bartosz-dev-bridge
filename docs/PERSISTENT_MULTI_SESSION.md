# Persistent multi-session handoff

This increment lets one continuously running Bridge process move from a completed active session to a different waiting session without a service restart.

## Safety contract

The scheduler may auto-complete the current active session only when all of the following are true:

- the active Bridge loop supplies its current `service_instance_id`;
- exactly that service instance is `running` and none is `stopping`;
- another `created` session already has a non-expired, validated command `000001`;
- the active session has at least one command and every command is in a final state;
- the active session has no pending or colliding outbox entry;
- there is no blocking ingestion issue;
- the active session has a registered workspace that can be preserved.

A stale database row, a foreign service identifier, or an offline scheduler invocation cannot authorize the handoff.

The scheduler does **not** auto-complete a session merely because it is temporarily idle. This preserves the existing behavior where later commands such as `000002` and `000003` can continue in the same active session.

## Handoff sequence

1. The normal scheduler claim is attempted first.
2. If the active session has no claimable command, the active service identity and handoff preconditions are checked in one journal transaction.
3. The active session transitions `active -> completing -> completed`.
4. Its workspace lifecycle is recorded as `preserved`.
5. The scheduler retries the normal claim, which activates the waiting session and claims its first command.

The single-worker invariant remains unchanged. The feature does not introduce parallel session execution.

## Non-goals

- no automatic workspace cleanup;
- no arbitrary shell execution;
- no change to the manual offline `bridge session finalize` contract;
- no connection to a business repository;
- no automatic merge or push of source changes.
