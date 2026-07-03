# Grafana dashboards for the live panel (richer live view + embedding)

> Extracted from `knowledge/observability.md` (§2 live-view choice + §5 embedding) — read this when
> the user asks about Grafana vs metrics-server or embedding their own Grafana. The entry file keeps
> a pointer (`read_knowledge('observability_grafana')`).

### Two live-view options to offer before a run — Grafana (richer) vs metrics-server (convenient)

When the user wants to *watch a run live*, there are two complementary surfaces. Offer BOTH as a
pair before the run (alongside the metrics-server offer in observability.md §2) and clarify what each is for — they
are not the same thing, and one is not a drop-in for the other:

- **Grafana — the richer, recommended view.** The llm-d observability dashboard (the upstream
  `--monitoring` Grafana, backed by Prometheus) shows the *full* picture: GPU utilization, vLLM/EPP
  latency (TTFT/ITL), throughput, queue depth, KV-cache, **and history across the whole run**. You
  CAN stand this stack up for the user — do NOT treat it as "their problem to solve" or claim you can
  "only advise". The upstream recipe is one approval-gated `run_shell` away, exactly like the
  metrics-server install and the kind-cluster create: read the exact commands from the upstream
  `install-prometheus-grafana.sh` / observability `setup.md` (`knowledge/useful_repo_docs.md` →
  `read_repo_doc`), then run them with `run_shell` (mutating → it raises the Approve card). Offer it
  the same way you offer the metrics-server install. The ONE piece that is genuinely the user's, not
  yours, is the backend env var `GRAFANA_DASHBOARD_URL` — you have no env/secret-write tool, so THEY
  set it (pointing at their Grafana). Once set, an **Open Grafana** button appears above the live
  metrics in the run panel and opens the dashboard in a modal. `probe_environment` reports
  `grafana_dashboard.configured` (true once the env var is set) — use it to tailor the message:
  configured → "it'll show up in the run panel"; not configured → "I can deploy the stack with you
  (one Approve); then set `GRAFANA_DASHBOARD_URL` and I'll embed it beside the run".
- **metrics-server — the convenient alternative.** Live **CPU/memory only** (no GPU, no latency, no
  history), but it is the zero-setup option **you can install for them** in one approval-gated step
  (`install_metrics_server.sh`, per observability.md §2.1) and it lights up the in-panel sparklines immediately. It is
  the right answer when the user just wants a quick "is anything melting?" view and has no Grafana
  stack.

Position Grafana as the fuller picture and metrics-server as the quick fallback. Be honest about the
real split: you can DEPLOY both for them (each an approval-gated `run_shell`) — the only thing you
can't do for Grafana is write their `GRAFANA_DASHBOARD_URL` backend env var (no env/secret tool), and
that controls only the in-panel embed, not whether the stack exists. So never refuse to stand up
Grafana; at most note the env var is the user's last step. The two live-views are independent — the
Grafana embed works even when metrics-server is absent, and vice-versa (see below for the embed).

### Embedding the user's own Grafana in the live panel (optional)
If the operator has their own llm-d observability stack (the upstream `--monitoring` Grafana),
they can set the backend env var **`GRAFANA_DASHBOARD_URL`** to that dashboard's URL. When set, an
**Open Grafana** button appears above the live metrics in the run panel; clicking it opens the
dashboard in a large modal overlay (with an **open-in-new-tab** fallback for Grafana instances that
refuse iframe embedding via `X-Frame-Options` / `frame-ancestors`). The button shows even when no
metrics-server is present, since the external Grafana is independent of it. Unset (the default) → no
button, and the panel shows only the agent's own kubectl-top view. This is mechanism only — it
surfaces the operator's dashboard; it does not make the benchmark itself stream metrics. So when a
user asks for "live Grafana during the run," the honest answer is: point me at your Grafana via
`GRAFANA_DASHBOARD_URL` and I'll show it in the run panel (see the paired offer in observability.md §2).
