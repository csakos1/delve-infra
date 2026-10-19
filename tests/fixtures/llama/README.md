# llama-server samples

`slots.json` and `metrics.txt` from `/slots` and `/metrics` (`--metrics`), taken right after
a thinking run. The slot shows the **last** call — here the session title generation
(410 prompt tokens), not the run itself; a client measuring context fill must sample
during the run.

`slots-run.jsonl`: `/slots` (without `params`) and selected `/metrics` counters every 0.5 s
during three live runs through the backend (llama.cpp e287bd872, Hermes v0.22.2), one
conversation each: `basic` (one call), `tool` (two calls around a `terminal` step) and
`thinking` (a denied `terminal` step, then the answer). `kind: mark` lines are the
backend's WebSocket events (`send`, `run.started`, `first_delta`, `tool.*`,
`run.finished` with usage, `conversation.updated`, …), `t` is seconds from the start.

What it shows:
- while generating, `n_prompt_tokens` grows with each decoded token
  (`n_prompt_tokens - next_token[0].n_decoded` is constant per task): it is the call's
  whole context;
- every llama call has a new `id_task`; a tool step means a second call over the history;
- the session-title call (~280–310 prompt tokens) starts ~0.2 s after `run.finished` and
  overwrites the slot;
- a one-call run can finish between two 0.5 s samples (`basic` has none), and
  `tokens_predicted_total` only moves when a request completes.
