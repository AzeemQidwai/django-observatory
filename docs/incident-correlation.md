# Incident correlation

Raw telemetry answers "what happened". The incident layer answers "what is wrong, since when, who is
affected, and what probably caused it" — deterministically, offline, with its reasoning visible.

## How a detection pass works

Every `SCHEDULER.INTERVAL` seconds one process runs `correlation.engine.detect()`:

1. **Snapshot** – built-in metrics for the current window (`WINDOW_MINUTES`, default 5) against the
   preceding baseline (`BASELINE_MINUTES`, default 60).
2. **Symptoms** – observed anomalies. Each needs a relative jump *and* an absolute floor *and* a minimum
   sample, so quiet systems and handfuls of requests do not raise incidents.

   | Symptom | Condition |
   |---|---|
   | error rate | 5xx ≥ 5%, ≥ 5 errors, ≥ `MIN_REQUESTS`, and ≥ 2× baseline + 1 pt |
   | request latency | P95 ≥ 500 ms and ≥ 2× baseline |
   | external latency / failures | per service: P95 ≥ 500 ms and ≥ 2× baseline; failure rate ≥ 20% |
   | database latency | SQL P95 ≥ 100 ms and ≥ 3× baseline |
   | exception spike | ≥ 5 of a type and ≥ 3× the baseline rate |
   | server resources | CPU or memory average ≥ its `SYSTEM` threshold over the window; any volume ≥ `DISK_PERCENT` |
   | login failures / permission denied | ≥ 10 / ≥ 20 and ≥ 3× baseline |

   Without a baseline (new installation) absolute thresholds apply.
3. **Hypotheses** – each rule in `correlation/rules.py` may propose a cause. Its score is the sum of named
   pieces of evidence:

   | Rule | Evidence that raises the score |
   |---|---|
   | external service | service latency/failures up · request impact in the same window · share of failing or slow requests whose trace calls that service · share of their time spent in it · timeout exceptions |
   | database | SQL latency up · request impact · share of request time spent in SQL · slowest statement |
   | server resources | CPU / memory saturated over the window, or a volume nearly full · rise above the preceding hour · request impact · OSError / MemoryError / database errors · no external service degraded |
   | deployment | release first seen shortly before · healthy baseline before it · failing requests on the new release |
   | exception | one issue dominates the window · 5xx up · issue is new or regressed |
   | security | spike size · concentration on one source address |
   | unexplained | fallback: the engine says so rather than guessing |

   Confidence: HIGH ≥ 0.75, MEDIUM ≥ 0.5, otherwise LOW.
4. **Reconcile** – at most one active incident per scope (performance, security). An ongoing incident is
   updated in place; a better-supported hypothesis replaces the cause; other hypotheses are kept as
   "alternatives considered". With no symptoms for `RESOLVE_AFTER_MINUTES` it resolves itself.

## Fact versus inference

Stored and displayed separately:

- **Facts** – `IncidentSignal` rows with baseline → current numbers, and evidence statements.
- **Inference** – `probable_cause`, `confidence`, the probable start time, the recommendation.

The UI labels inference as such and states that it is not a certainty.

## Impact

Requests and 5xx counts come from metrics (exact, unaffected by sampling). Affected users are counted from
captured requests and labelled accordingly. For an external-service cause, affected endpoints are those
whose requests call that service.

## Alerts and incidents

Alerts firing when an incident opens are attached to it, and one notification is sent for the incident
rather than one per alert.

## Adding a rule

```python
# myproject/observability_rules.py
from django_observatory.correlation import rules

def rule_queue_backlog(s, kinds, signals, affected):
    ...
    h = rules.Hypothesis("queue", "orders", "Order queue backlog", "Queue consumers stalled",
                         "Check the consumer service.")
    return h.add(0.6, "queue.depth rose from 12 to 4,800")

rules.RULES = (*rules.RULES[:-1], rule_queue_backlog, rules.RULES[-1])   # keep the fallback last
```

## Per-request causal analysis

Every slow or failed request page shows a latency breakdown (external services, database, application
code), the suspected cause with confidence, and the evidence: failed or timed-out calls inside the trace,
whether the same service failed in other recent failures of the endpoint, repeated statements suggesting
an N+1 pattern, and the endpoint's usual median.

## Future AI layer

Incidents are structured, already-redacted records, so an optional local model could summarise them later.
Nothing in detection depends on one.
