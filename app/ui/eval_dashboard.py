"""
Admin-only "AI Eval (admin)" dashboard - read-only visualization of:
  1. Every Bill Import v2 job across ALL plans (status, mode, tokens, cost,
     cache hits, errors) - app/services/eval_dashboard.list_all_jobs().
  2. eval/history.jsonl - model-comparison scoring runs written by the CLI
     harness (eval/run_eval.py). Running new evals stays a CLI action in
     this version; there's no in-UI trigger.

Gated OWNER-only in app/main.py (apply_role_visibility) - this dashboard
spans every plan, so it must never be reachable by a plan-scoped MEMBER.
"""

from __future__ import annotations

import pandas as pd
import gradio as gr
import plotly.graph_objects as go

from app.services import eval_dashboard


def _jobs_df() -> pd.DataFrame:
    jobs = eval_dashboard.list_all_jobs(limit=300)
    if not jobs:
        return pd.DataFrame()
    return pd.DataFrame(jobs)


def _history_df() -> pd.DataFrame:
    hist = eval_dashboard.load_eval_history()
    if not hist:
        return pd.DataFrame()
    rows = [
        {
            "timestamp": r.get("timestamp"),
            "job_id": r.get("job_id"),
            "plan_id": r.get("plan_id"),
            "model": r.get("model"),
            "json_parse_success": r.get("json_parse_success"),
            "month_year_match": r.get("month_year_match"),
            "mean_abs_per_member_diff": r.get("mean_abs_per_member_diff"),
            "mean_abs_diff_active_members": (
                r.get("mean_abs_diff_active_members")
                if r.get("mean_abs_diff_active_members") is not None
                else r.get("mean_abs_per_member_diff")
            ),
            "total_amount_pct_diff": r.get("total_amount_pct_diff"),
            "unmatched_lines": r.get("unmatched_lines"),
            "error": (r.get("error") or "")[:150] if r.get("error") else "",
        }
        for r in hist
    ]
    return pd.DataFrame(rows)


def _model_comparison_df() -> pd.DataFrame:
    hist = eval_dashboard.load_eval_history()
    agg = eval_dashboard.aggregate_by_model(hist)
    if not agg:
        return pd.DataFrame()
    return pd.DataFrame(agg)


def _model_comparison_chart() -> go.Figure:
    """
    Per-job distribution (not just a single averaged bar) of the "active
    members only" mean abs $ error, one box per model - shows spread/
    consistency across jobs, not just a flat average that (with json-parse-
    success and month/year-match rates almost always sitting at 100% once
    the pipeline is working) used to read as "a constant chart." Those two
    reliability rates are still available - see the "Aggregated by model"
    table above, which is a better fit for near-constant percentages than a
    chart competing for the same axes as a dollar figure.
    """
    df = _history_df()
    fig = go.Figure()
    if df.empty or "mean_abs_diff_active_members" not in df.columns:
        fig.update_layout(title="No eval history yet - run `uv run python eval/run_eval.py` to populate this chart.")
        return fig

    df = df.dropna(subset=["mean_abs_diff_active_members"])
    if df.empty:
        fig.update_layout(title="No scored jobs yet.")
        return fig

    for model, sub in df.groupby("model"):
        fig.add_trace(
            go.Box(
                y=sub["mean_abs_diff_active_members"],
                name=str(model),
                boxpoints="all",
                jitter=0.4,
                pointpos=0,
                hovertemplate="Model: " + str(model) + "<br>Mean abs error (active members): $%{y:.2f}<extra></extra>",
            )
        )
    fig.update_layout(
        title="Accuracy spread per model (mean abs $ error, active members only, one point per scored job)",
        yaxis=dict(title="Mean abs. per-member $ error ($)"),
        showlegend=False,
    )
    return fig


def _accuracy_over_time_chart() -> go.Figure:
    df = _history_df()
    fig = go.Figure()
    if df.empty or "timestamp" not in df.columns:
        fig.update_layout(title="No eval history yet - run `uv run python eval/run_eval.py` to populate this chart.")
        return fig

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    if df.empty:
        fig.update_layout(title="No eval history yet.")
        return fig

    for model, sub in df.groupby("model"):
        fig.add_trace(
            go.Scatter(
                x=sub["timestamp"],
                y=sub["mean_abs_diff_active_members"],
                mode="lines+markers",
                name=str(model),
                hovertemplate="%{x}<br>Mean abs error (active members): $%{y:.2f}<extra></extra>",
            )
        )
    fig.update_layout(
        title="Accuracy over time (mean abs. $ error, active members only, lower is better)",
        xaxis_title="Scored at",
        yaxis_title="Mean abs. per-member $ error ($)",
    )
    return fig


def ui_eval_dashboard(demo, current_role):
    with gr.Column():
        gr.Markdown(
            """
# 📊 AI Eval (admin)

Read-only visualization of every Bill Import v2 job across **all plans**, plus scoring runs from
`eval/history.jsonl` - two kinds, both shown together below:

- **`<model> (approved)`** rows are written **automatically**, once, the moment a NORMAL-mode job
  is approved - the model's original prediction vs. whatever amounts actually got approved
  (corrected or not). This is the real-world accuracy signal and never changes after the fact,
  even if the invoice is edited later from the Invoices ledger.
- Plain `<model>` rows (no suffix) come from manually **re-running** the CLI model-comparison tool:

```bash
uv run python eval/run_eval.py --models openai/gpt-4o-mini,anthropic/claude-sonnet-4.6 --limit 20
```

Model slugs get retired/renamed over time - if one errors with "No endpoints found", check
`https://openrouter.ai/api/v1/models` for the current catalog before assuming it's a bug.

**Reading the numbers:** `mean_abs_diff_active_members_usd` only averages over members who actually
had a nonzero amount (predicted or approved) for that bill - `mean_abs_per_member_diff_usd` (kept
for older rows) averages over *every* roster member, including ones correctly sitting at $0/$0
because they're not on that particular bill, which waters the number down. Prefer the "active
members" one; the chart below plots it too.
"""
        )

        refresh_btn = gr.Button("🔄 Refresh dashboard", variant="primary")

        with gr.Accordion("All AI import jobs (every plan)", open=True):
            jobs_table = gr.Dataframe(value=_jobs_df(), interactive=False, wrap=True, label="Bill Import v2 jobs")

        with gr.Accordion("Model comparison (from eval/history.jsonl)", open=True):
            model_table = gr.Dataframe(value=_model_comparison_df(), interactive=False, wrap=True, label="Aggregated by model")
            model_chart = gr.Plot(value=_model_comparison_chart())

        with gr.Accordion("Accuracy over time", open=True):
            accuracy_chart = gr.Plot(value=_accuracy_over_time_chart())

        with gr.Accordion("Raw eval history (every scoring run)", open=False):
            history_table = gr.Dataframe(value=_history_df(), interactive=False, wrap=True, label="eval/history.jsonl")

        outputs = [jobs_table, model_table, model_chart, accuracy_chart, history_table]

        def _refresh_all():
            return _jobs_df(), _model_comparison_df(), _model_comparison_chart(), _accuracy_over_time_chart(), _history_df()

        def _refresh_if_owner(role):
            # This dashboard spans every plan - only ever compute/ship its
            # payload to the browser when the session is actually OWNER,
            # even though the tab's visibility is already gated in app/main.py.
            if (role or "").strip().upper() != "OWNER":
                return (gr.skip(),) * len(outputs)
            return _refresh_all()

        refresh_btn.click(fn=_refresh_all, inputs=[], outputs=outputs)
        gr.on(triggers=[demo.load, current_role.change], fn=_refresh_if_owner, inputs=[current_role], outputs=outputs)

    return
