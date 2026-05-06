"""
Tableau to Power BI Migration Accelerator — Streamlit UI
Mastech Digital | Office of the CTO

Run from project root:
    streamlit run ui/app.py
"""
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

# ── Bootstrap ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# Ensure API key from .env is active — no UI input needed
_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Folders ──────────────────────────────────────────────────────────────────
INPUT_FOLDER  = ROOT / "input_workbooks"
OUTPUT_FOLDER = ROOT / "pbi_output"
INPUT_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────────
_INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def safe_name(name: str, max_len: int = 80) -> str:
    """Sanitize any string for use as a Windows/Linux folder or file name."""
    s = _INVALID_PATH_CHARS.sub("_", name)
    s = re.sub(r"_+", "_", s).strip("_. ")
    return s[:max_len] or "output"


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Tableau → Power BI Accelerator",
    page_icon="⚡",
    layout="wide",
)

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #f4f6fb; }

.hero {
    background: linear-gradient(100deg,#0078D4 0%,#106EBE 55%,#00BCF2 100%);
    padding: 1.5rem 2rem; border-radius: 10px; color: white; margin-bottom: 1.2rem;
}
.hero h2 { margin:0; font-size:1.65rem; }
.hero p  { margin:.25rem 0 0; opacity:.85; font-size:.9rem; }

.file-pill {
    display:inline-block; background:#e8f0fe; border:1px solid #c5d8ff;
    border-radius:20px; padding:2px 14px; font-size:.85rem; margin:3px 2px;
    font-weight:500;
}

/* Pipeline step cards */
.step-row {
    display:flex; align-items:center; gap:12px;
    padding:10px 16px; border-radius:8px; margin-bottom:6px;
    font-size:.95rem; font-weight:500;
    transition: background .2s;
}
.step-done    { background:#e6f9ee; border:1px solid #a8ebc4; color:#1a6e3c; }
.step-running { background:#fff3cd; border:1px solid #ffd966; color:#7d5a00; }
.step-pending { background:#f0f2f5; border:1px solid #dde1e7; color:#8a8f99; }
.step-error   { background:#fde8e8; border:1px solid #f5b0b0; color:#8b1a1a; }
.step-icon    { font-size:1.2rem; width:28px; text-align:center; }

div[data-testid="stMetricValue"] { font-size:1.55rem !important; }
</style>
""", unsafe_allow_html=True)

# ── Hero ──────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero">
  <h2>⚡ Tableau → Power BI Migration Accelerator</h2>
  <p>Mastech Digital &nbsp;|&nbsp;  &nbsp;|&nbsp;
     Deterministic pipeline + Claude Sonnet 4.6 for LOD / table calc → DAX</p>
</div>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Settings")

    project_name = st.text_input(
        "Power BI Project Name", value="MigratedReport",
        help="Used as the .pbip/.pbit filename and SemanticModel folder name.",
    )

    st.divider()

    output_format = st.radio(
        "Output Format",
        options=["pbip", "pbit", "both"],
        index=0,
        help=(
            "**pbip** — folder-based project (open directly in Power BI Desktop, git-friendly)\n\n"
            "**pbit** — single-file template (easy to email/share, opens in Power BI Desktop)\n\n"
            "**both** — write both formats"
        ),
        captions=[
            "Folder project · git-friendly",
            "Single file · easy to share",
            "Write both",
        ],
    )

    st.divider()

    # API key status — read-only, sourced from .env
    if _api_key:
        st.success("🤖 Claude API active")
        st.caption("LLM translation enabled for LOD expressions & table calcs.")
    else:
        st.warning("⚠️ No Anthropic API key found in `.env`")
        st.caption("Complex formulas will be added to manual review queue.")

    st.divider()
    st.caption("**Supported input formats**")
    st.caption("`.twb`  `.twbx`  `.tds`  `.tdsx`")
    st.divider()
    st.caption("**Output per workbook**")
    st.caption("`.pbip` / `.pbit` · TMDL semantic model · Power Query M · Report JSON · Review queue · Migration report")


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1 — Upload
# ═══════════════════════════════════════════════════════════════════════════════
st.subheader("📂 Upload Workbooks")

uploaded = st.file_uploader(
    "Drag & drop `.twbx` / `.twb` / `.tds` / `.tdsx` files, or click Browse",
    type=["twb", "twbx", "tds", "tdsx"],
    accept_multiple_files=True,
)

for uf in uploaded:
    dest = INPUT_FOLDER / uf.name
    dest.write_bytes(uf.getvalue())

queued = sorted(
    list(INPUT_FOLDER.glob("*.tw*")) + list(INPUT_FOLDER.glob("*.td*")),
    key=lambda p: p.name.lower(),
)
queued = list(dict.fromkeys(queued))

if queued:
    st.markdown(f"**{len(queued)} file(s) queued for migration:**")
    cols = st.columns(min(len(queued), 4))
    for i, f in enumerate(queued):
        with cols[i % 4]:
            st.markdown(f'<span class="file-pill">📄 {f.name}</span>', unsafe_allow_html=True)
            if st.button("✕ Remove", key=f"rm_{f.name}", use_container_width=True):
                f.unlink()
                st.rerun()
else:
    st.info("No files queued. Upload files above or copy them into the `input_workbooks/` folder.")
    st.stop()

st.divider()

# ═══════════════════════════════════════════════════════════════════════════════
# Section 2 — Run migration
# ═══════════════════════════════════════════════════════════════════════════════
run_col, clear_col = st.columns([3, 1])
run_clicked   = run_col.button("🚀  Run Migration", type="primary", use_container_width=True)
clear_clicked = clear_col.button("🗑️  Clear Results", use_container_width=True)

if clear_clicked:
    st.session_state.pop("results", None)
    st.session_state.pop("ran", None)
    st.rerun()

# ── Pipeline step definitions ──────────────────────────────────────────────────
STEPS = [
    ("📥", "Ingesting file"),
    ("🔍", "Parsing Tableau XML"),
    ("📊", "Analyzing — complexity, lineage & anti-patterns"),
    ("🔄", "Translating — deterministic rules (connections, visuals, filters)"),
    ("🤖", "Translating — AI (LOD expressions & table calcs → DAX)"),
    ("🏗️", "Generating — TMDL · Power Query M · Report JSON"),
    ("📦", "Packaging PBIP output"),
    ("✅", "Migration complete"),
]

def render_steps(current: int, error_at: int = -1, container=None):
    """Render the pipeline step tracker. current = 0-based index of active step."""
    target = container or st
    html_parts = []
    for i, (icon, label) in enumerate(STEPS):
        if error_at == i:
            cls, prefix = "step-error", "❌"
        elif i < current:
            cls, prefix = "step-done", "✓"
        elif i == current:
            cls, prefix = "step-running", "▶"
        else:
            cls, prefix = "step-pending", "·"
        html_parts.append(
            f'<div class="step-row {cls}">'
            f'<span class="step-icon">{icon}</span>'
            f'<span>{prefix}&nbsp; {label}</span>'
            f'</div>'
        )
    target.markdown("\n".join(html_parts), unsafe_allow_html=True)


if run_clicked:
    results_store = []

    for file_path in queued:
        st.markdown(f"---\n#### ⚙️ `{file_path.name}`")

        step_placeholder = st.empty()
        status_msg       = st.empty()

        def update(step_idx: int, msg: str = ""):
            render_steps(step_idx, container=step_placeholder)
            if msg:
                status_msg.caption(msg)

        error_step = -1
        try:
            from accelerator.ingestion           import ingest
            from accelerator.parser              import parse
            from accelerator.analyzer            import analyze
            from accelerator.pipeline            import MigrationPipeline
            from accelerator.generators          import generate_tmdl, generate_m, generate_report
            from accelerator.generators.packager import package, build_migration_report

            # Step 0 — Ingest
            update(0, f"Reading `{file_path.name}` …")
            artifact = ingest(file_path)

            # Step 1 — Parse
            update(1, "Extracting data sources, worksheets, dashboards and calculated fields …")
            unit = parse(artifact)
            ds_count  = len(unit.data_sources)
            ws_count  = len(unit.worksheets)
            dash_count = len(unit.dashboards)
            calc_count = sum(len([c for c in ds.columns if c.is_calculated]) for ds in unit.data_sources)
            status_msg.caption(
                f"Found {ds_count} data source(s) · {ws_count} worksheet(s) · "
                f"{dash_count} dashboard(s) · {calc_count} calculated field(s)"
            )

            # Step 2 — Analyze
            update(2, "Scoring complexity, detecting anti-patterns, inferring star schema …")
            analysis = analyze(unit)
            ap_count = len(analysis.anti_patterns)
            unused_count = len(analysis.unused_worksheets)
            status_msg.caption(
                f"Anti-patterns detected: {ap_count} · Unused worksheets: {unused_count}"
            )

            # Step 3 — Deterministic translation
            update(3, "Mapping connections → Power Query M · visuals → PBI JSON · filters → slicers …")
            pipeline     = MigrationPipeline(output_base=str(OUTPUT_FOLDER), project_name=project_name, output_format=output_format)
            translations = pipeline.translate(unit)
            det_count = sum(1 for t in translations if t.method == "deterministic")
            status_msg.caption(f"{det_count} artifact(s) translated deterministically")

            # Step 4 — LLM translation (happens inside pipeline.translate above, surfaced here)
            update(4, "Claude Sonnet 4.6 translating LOD expressions & table calculations → DAX …")
            llm_count = sum(1 for t in translations if t.method == "llm")
            manual_count = sum(1 for t in translations if t.method == "manual")
            if llm_count:
                status_msg.caption(f"{llm_count} formula(s) translated via LLM · {manual_count} queued for manual review")
            elif manual_count:
                status_msg.caption(f"No API key active — {manual_count} complex formula(s) added to review queue")
            else:
                status_msg.caption("No complex formulas requiring LLM translation")

            # Step 5 — Generate
            update(5, "Writing TMDL semantic model · Power Query M scripts · report pages …")
            out_dir = OUTPUT_FOLDER / safe_name(file_path.stem)
            out_dir.mkdir(parents=True, exist_ok=True)
            translations += generate_tmdl(unit, out_dir)
            translations += generate_m(unit, out_dir)
            translations += generate_report(unit, out_dir)
            status_msg.caption(f"Output directory: `{out_dir}`")

            # Step 6 — Package
            fmt_label = {"pbip": "PBIP folder", "pbit": ".pbit template", "both": "PBIP + .pbit"}
            update(6, f"Assembling {fmt_label.get(output_format, 'package')} · review queue · migration report …")
            report = build_migration_report(unit, translations, out_dir)
            package(project_name, unit, translations, out_dir)

            pbit_path = None
            if output_format in ("pbit", "both"):
                from accelerator.generators.pbit_generator import build_pbit
                pbit_path = build_pbit(project_name, unit, translations, out_dir)

            # Step 7 — Done
            update(7)
            stats = report.get("translation_stats", {})
            status_msg.success(
                f"✅  Coverage **{stats.get('coverage_pct')}%** · "
                f"Avg confidence **{stats.get('average_confidence', 0):.0%}** · "
                f"Blockers **{report.get('review_queue_summary', {}).get('blockers', 0)}**"
            )

            results_store.append({
                "file":         file_path.name,
                "output_dir":   out_dir,
                "report":       report,
                "translations": translations,
                "pbit_path":    pbit_path,
                "output_format": output_format,
                "success":      True,
            })

        except Exception as exc:
            import traceback
            render_steps(error_step if error_step >= 0 else 0,
                         error_at=error_step if error_step >= 0 else 0,
                         container=step_placeholder)
            status_msg.error(f"❌ {exc}")
            with st.expander("Show traceback"):
                st.code(traceback.format_exc(), language="text")
            results_store.append({"file": file_path.name, "success": False, "error": str(exc)})

    st.session_state["results"] = results_store
    st.session_state["ran"]     = True
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3 — Results
# ═══════════════════════════════════════════════════════════════════════════════
if not (st.session_state.get("ran") and st.session_state.get("results")):
    st.stop()

st.divider()
st.subheader("📊 Migration Results")

for res in st.session_state["results"]:

    if not res.get("success"):
        st.error(f"❌ **{res['file']}** — {res.get('error','Unknown error')}")
        continue

    report   : dict  = res["report"]
    out_dir  : Path  = Path(res["output_dir"])
    stats    : dict  = report.get("translation_stats", {})
    inv      : dict  = report.get("inventory", {})
    rq_sum   : dict  = report.get("review_queue_summary", {})
    blockers : int   = rq_sum.get("blockers", 0)
    coverage : float = stats.get("coverage_pct", 0)
    icon = "✅" if blockers == 0 else ("⚠️" if blockers < 3 else "🔴")

    with st.expander(
        f"{icon}  **{res['file']}**   "
        f"Coverage {coverage}%  ·  "
        f"Confidence {stats.get('average_confidence',0):.0%}  ·  "
        f"Review items {rq_sum.get('needs_review',0)}  ·  "
        f"Blockers {blockers}",
        expanded=True,
    ):
        # ── KPI row ────────────────────────────────────────────────────────────
        k1,k2,k3,k4,k5,k6,k7 = st.columns(7)
        k1.metric("Data Sources",   inv.get("data_sources", 0))
        k2.metric("Worksheets",     inv.get("worksheets", 0))
        k3.metric("Dashboards",     inv.get("dashboards", 0))
        k4.metric("Calc Fields",    inv.get("calculated_fields", 0))
        k5.metric("Coverage",       f"{coverage}%")
        k6.metric("Avg Confidence", f"{stats.get('average_confidence',0):.0%}")
        k7.metric("Blockers",       blockers,
                  delta=f"{blockers} to fix" if blockers else None,
                  delta_color="inverse")

        st.divider()

        # ── Tabs ───────────────────────────────────────────────────────────────
        t_dl, t_report, t_rq, t_ap = st.tabs([
            "⬇️  Download Files",
            "📋  Migration Report",
            "🔍  Review Queue",
            "⚠️  Anti-patterns",
        ])

        # ── Download ───────────────────────────────────────────────────────────
        with t_dl:
            fmt_chosen = res.get("output_format", "pbip")
            stem = res["file"].rsplit(".", 1)[0]
            all_out = [f for f in sorted(out_dir.rglob("*")) if f.is_file()]

            # ── .pbit one-click download ───────────────────────────────────────
            pbit_p = res.get("pbit_path")
            if pbit_p and Path(pbit_p).exists():
                st.success(f"💼  **{Path(pbit_p).name}** is ready — single file, open directly in Power BI Desktop")
                st.download_button(
                    f"⬇️  Download {Path(pbit_p).name}",
                    data=Path(pbit_p).read_bytes(),
                    file_name=Path(pbit_p).name,
                    mime="application/octet-stream",
                    use_container_width=True,
                    type="primary",
                    key=f"pbit_{res['file']}",
                )
                st.caption(
                    "Open workflow: **File → Import → Power BI Template** in Power BI Desktop, "
                    "then connect your data in Power Query Editor."
                )
                st.divider()

            # ── .pbip ZIP package download (pbip or both) ─────────────────────
            if fmt_chosen in ("pbip", "both"):
                # Collect only the PBIP-relevant files (exclude the .pbit itself)
                pbip_files = [
                    f for f in all_out
                    if not f.name.endswith(".pbit")
                ]
                pbip_zip_buf = io.BytesIO()
                with zipfile.ZipFile(pbip_zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in pbip_files:
                        zf.write(f, f.relative_to(out_dir.parent))
                pbip_zip_buf.seek(0)
                pbip_zip_name = f"{safe_name(stem)}_pbip.zip"

                st.info(
                    "**PBIP format** — must be downloaded as a ZIP and extracted together.  \n"
                    "The `.pbip` launcher file is **not usable on its own** — it needs the "
                    "`*.Report/` and `*.SemanticModel/` folders alongside it.  \n"
                    "**Steps:** Download ZIP → Extract all → Double-click the `.pbip` file in Power BI Desktop."
                )
                st.download_button(
                    f"⬇️  Download PBIP Package — {pbip_zip_name}",
                    data=pbip_zip_buf,
                    file_name=pbip_zip_name,
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                    key=f"pbip_zip_{res['file']}",
                )
                st.divider()

            # ── Individual file listing ────────────────────────────────────────
            EXT_ICON = {"json":"📄","tmdl":"🗂️","m":"⚙️","pbip":"💼","txt":"📝","pbir":"📊","pbit":"💼"}

            by_folder: dict[str, list[Path]] = {}
            for f in all_out:
                folder = str(f.parent.relative_to(out_dir)) if f.parent != out_dir else "."
                by_folder.setdefault(folder, []).append(f)

            for folder, files in by_folder.items():
                st.markdown(f"**📁 {folder}**")
                for f in files:
                    c_name, c_size, c_btn = st.columns([5, 1, 1])
                    icon2 = EXT_ICON.get(f.suffix.lstrip("."), "📄")
                    label = f"{icon2} `{f.name}`"
                    # Warn that the lone .pbip file won't open without its folders
                    if f.suffix == ".pbip":
                        label += "  ⚠️ *use ZIP above*"
                    c_name.markdown(label)
                    c_size.caption(f"{max(f.stat().st_size//1024,1)} KB")
                    c_btn.download_button(
                        "⬇", data=f.read_bytes(), file_name=f.name,
                        mime="application/octet-stream",
                        key=f"dl_{res['file']}_{f.relative_to(out_dir)}",
                    )

            st.divider()
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in all_out:
                    zf.write(f, f.relative_to(out_dir.parent))
            zip_buf.seek(0)
            st.download_button(
                f"⬇️  Download ALL as ZIP — {safe_name(stem)}_pbi_output.zip",
                data=zip_buf,
                file_name=f"{safe_name(stem)}_pbi_output.zip",
                mime="application/zip",
                use_container_width=True,
                key=f"zip_{res['file']}",
            )

        # ── Migration report ───────────────────────────────────────────────────
        with t_report:
            txt = out_dir / "migration_report.txt"
            if txt.exists():
                st.code(txt.read_text(encoding="utf-8"), language="text")
            with st.expander("Full JSON"):
                st.json(report)

        # ── Review queue ───────────────────────────────────────────────────────
        with t_rq:
            rq_path = out_dir / "review_queue.json"
            if not rq_path.exists():
                st.info("No review queue file found.")
            else:
                rq_data = json.loads(rq_path.read_text(encoding="utf-8"))
                items = rq_data.get("items", [])
                summ  = rq_data.get("summary", {})
                sb,sh,sm,sl = st.columns(4)
                sb.metric("🔴 Blockers", summ.get("blockers",0))
                sh.metric("🟠 High",     summ.get("high",0))
                sm.metric("🟡 Medium",   summ.get("medium",0))
                sl.metric("🟢 Low",      summ.get("low",0))
                st.divider()
                P_ICON = {"blocker":"🔴","high":"🟠","medium":"🟡","low":"🟢"}
                for item in items:
                    p    = item.get("review_priority","low")
                    conf = item.get("confidence",0)
                    kind = item.get("kind","").upper()
                    with st.expander(
                        f"{P_ICON.get(p,'⚪')} [{p.upper()}]  {kind}  "
                        f"— confidence {conf:.0%}  ·  method: {item.get('method','')}",
                    ):
                        if item.get("rationale"):
                            st.info(f"💡 {item['rationale']}")
                        for cav in item.get("caveats",[]):
                            st.warning(cav)
                        if item.get("artifact_preview"):
                            st.code(item["artifact_preview"], language="dax")

        # ── Anti-patterns ──────────────────────────────────────────────────────
        with t_ap:
            patterns = report.get("anti_patterns", [])
            unused   = report.get("unused_worksheets", [])
            if not patterns and not unused:
                st.success("✅ No anti-patterns or unused worksheets detected.")
            else:
                SEV = {"high":"🔴","medium":"🟡","low":"🟢"}
                for ap in patterns:
                    sev = ap.get("severity","medium")
                    st.warning(
                        f"{SEV.get(sev,'⚪')} **{ap.get('type','').replace('_',' ').title()}**  \n"
                        f"{ap.get('description','')}"
                    )
                if unused:
                    st.divider()
                    st.markdown("**Unused worksheets** (not referenced by any dashboard):")
                    for ws in unused:
                        st.markdown(f"- `{ws}`")

# ── Global ZIP (multiple workbooks) ───────────────────────────────────────────
ok_results = [r for r in st.session_state.get("results",[]) if r.get("success")]
if len(ok_results) > 1:
    st.divider()
    all_zip = io.BytesIO()
    with zipfile.ZipFile(all_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in ok_results:
            for f in Path(r["output_dir"]).rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(Path(r["output_dir"]).parent.parent))
    all_zip.seek(0)
    st.download_button(
        "⬇️  Download ALL Migrations as Single ZIP",
        data=all_zip, file_name="all_migrations.zip",
        mime="application/zip", use_container_width=True, type="primary",
    )
