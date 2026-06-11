# Evolution SSE Events

`GET /api/v1/evolutions/{evolution_id}/events` streams Server-Sent Events for one evolution run.

Each frame uses the SSE event name as the canonical event type:

```text
event: individual_evaluated
data: {"evolution_id":"...","sequence":3,"emitted_at":"...","payload":{...}}
```

The JSON `data` envelope is stable:

| Field | Type | Notes |
| --- | --- | --- |
| `evolution_id` | string | Evolution run id. |
| `sequence` | integer | Monotonic per-evolution sequence. Consumers can use it to de-dupe. |
| `emitted_at` | RFC3339 timestamp | Server emission time. |
| `payload` | object | Event-specific fields below. |

The stream opens with `event: snapshot` carrying the current `EvolutionResponse` record directly as `data`. Idle connections receive `: heartbeat` comments.

## Event Types

| Event | Required payload | Optional payload | Notes |
| --- | --- | --- | --- |
| `generation_started` | `status`, `current_generation` | `best_individual_id` | Emitted on transition to running or when a generation starts. |
| `individual_evaluated` | `individual_id`, `generation`, `fitness_scores` | `parent_id`, `parent_ids`, `chain_content`, `chain`, `content`, `usage`, `summary`, `mutation_kind` | `parent_id` should name the direct parent when known. Older producers may only send `parent_ids`. |
| `best_updated` | `individual_id`, `primary_objective`, `score` | `generation`, `fitness_scores`, `usage`, `chain_content` | Emitted when the primary objective best changes. |
| `cost_tick` | one of `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd` | `usage` object with the same fields | Payload is a delta. Producers must avoid double-emitting the same usage in both `cost_tick` and per-individual events. |
| `completed` | `status`, `current_generation` | `best_individual_id`, `usage`, `cost_usd` | Terminal successful run state. |
| `accepted` | `individual_id` | `chain_id`, `previous_version`, `new_version`, `version_id`, `generation`, `fitness_scores`, `note` | Emitted after Memory accepts the new version and the platform record is persisted. |
| `paused` | `status`, `current_generation` |  | Lifecycle control event. |
| `resumed` | `status`, `current_generation` |  | Lifecycle control event. |
| `cancelled` | `status`, `current_generation` | `error_message` | Lifecycle terminal event. |
| `failed` | `status`, `current_generation` | `error_message` | Terminal failure state. |

## Compatibility

Consumers should tolerate missing optional fields. CARE currently accepts chain payloads under `chain`, `chain_content`, or `content`, and fitness values under either a scalar `fitness` field or the first entry in `fitness_scores` / `objectives`.
