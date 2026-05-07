"""NiceGUI port sketch — SEC search section only, for visual comparison.

This is a *sketch*, not production code. It demonstrates what one
section of the GUI would look like in NiceGUI so you can compare:

  * design fidelity to gc.ai (Quasar gives proper theming primitives)
  * code shape vs the equivalent Streamlit block in app.py
  * scaffolding cost (event handlers, state holding, layout)

To actually run this:
    pip install nicegui
    python nicegui_sketch.py
    # then open http://localhost:8080

NiceGUI runs a FastAPI server under the hood; deploy anywhere Python
runs (Fly.io, Render, Railway, a tiny VPS). Free-tier hosting works
the same as Streamlit.

What the equivalent Streamlit block looks like: see app.py around the
'with left:' / 'with right:' columns plus the run_clicked handler.
This sketch is roughly the same line count but produces visibly more
gc.ai-aligned chrome by default — proper rounded inputs, real card
containers, a left drawer that survives page interactions, and a
status banner that updates live without a full script rerun.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# nicegui is not in requirements.txt by design — this is a sketch.
# Install with: pip install nicegui
from nicegui import ui  # type: ignore

from sec_contract_scanner import (
    EdgarClient,
    scan_and_download,
    PROFILE_DEFAULT_FORMS,
)

# --------------------------------------------------------------------------
# gc.ai-aligned theme (Quasar exposes these as first-class app config,
# unlike Streamlit's config.toml which is a small fixed surface)
# --------------------------------------------------------------------------

ui.colors(
    primary="#0E1A2D",      # deep navy — gc.ai brand
    secondary="#5A6273",    # softer slate
    accent="#F2EBDC",       # cream highlight
    positive="#0E1A2D",
    negative="#B23A48",
    info="#0E1A2D",
)

# Cream canvas + system font. NiceGUI's `ui.add_head_html` lets us
# inject CSS at document level, exactly like a normal web app.
ui.add_head_html("""
<style>
  body { background-color: #FAF5EE; color: #0E1A2D;
         font-family: -apple-system, BlinkMacSystemFont, 'Inter',
                      'Segoe UI', sans-serif; }
  .gc-card { background-color: #FFFFFF; border: 1px solid #E8E1D2;
             border-radius: 14px; padding: 1.25rem 1.5rem; }
  .gc-pill { background-color: #F2EBDC; border: 1px solid #E8E1D2;
             border-radius: 9999px; padding: 0.5rem 1.25rem;
             color: #0E1A2D; font-size: 0.88rem; }
  .gc-rail { background-color: #0E1A2D; color: #FAF5EE;
             min-width: 64px; padding: 1rem 0; }
</style>
""")


# --------------------------------------------------------------------------
# Three-pane chrome: dark navy rail, cream sidebar, white-card main.
# This is the gc.ai-style layout that Streamlit can't easily produce.
# --------------------------------------------------------------------------

with ui.row().classes("w-full h-screen gap-0"):

    # Dark navy left rail (icon-only nav). gc.ai's chrome.
    with ui.column().classes("gc-rail items-center gap-4"):
        ui.icon("search", size="md").classes("text-white")
        ui.icon("folder", size="md").classes("text-white opacity-60")
        ui.icon("schedule", size="md").classes("text-white opacity-60")
        ui.icon("settings", size="md").classes("text-white opacity-60")

    # Cream sidebar — credentials section.
    with ui.column().classes("p-6 gap-4").style(
        "background-color: #F5EDDF; min-width: 280px;"
    ):
        ui.label("Credentials").classes("text-lg font-bold")
        sec_user_agent = ui.input(
            label="SEC User-Agent",
            placeholder="DEMO_OPERATOR contact@example.invalid",
        ).props("outlined dense rounded").classes("w-full")
        gc_ai_key = ui.input(
            label="GC AI API key",
            password=True,
            password_toggle_button=True,
            placeholder="u:gcai_...",
        ).props("outlined dense rounded").classes("w-full")
        with ui.element("div").classes("gc-pill text-center"):
            ui.label(
                "Credentials stay on your machine. "
                "Uploads to GC AI are encrypted in transit."
            ).style("font-size: 0.8rem;")

    # Main content
    with ui.column().classes("flex-grow p-8 gap-6"):
        ui.label("ContractReview · GC AI").classes(
            "text-3xl font-bold tracking-tight"
        )
        ui.label(
            "Find SEC filings, federal court records, and PACER documents "
            "containing a target phrase — then stage matches into a GC AI "
            "folder for risk-disclosure mapping."
        ).classes("text-secondary")

        # SEC search card
        with ui.element("div").classes("gc-card w-full"):
            ui.label("SEC search").classes("text-xl font-bold mb-3")

            with ui.row().classes("w-full gap-4"):
                with ui.column().classes("flex-grow gap-3"):
                    phrase = ui.input(
                        label="Phrase to find",
                        value="DEMO_ENTITY_001",
                    ).props("outlined dense rounded").classes("w-full")

                    forms = ui.select(
                        options=PROFILE_DEFAULT_FORMS + [
                            "10-K/A", "10-Q/A", "S-3", "DEF 14A",
                            "F-1", "F-3", "F-4", "6-K",
                        ],
                        value=list(PROFILE_DEFAULT_FORMS),
                        multiple=True,
                        label="Form types",
                    ).props("outlined dense rounded use-chips").classes("w-full")

                    include_exhibits = ui.checkbox(
                        "Also scan exhibits attached to each filing",
                        value=True,
                    )

                with ui.column().classes("flex-grow gap-3"):
                    with ui.row().classes("gap-3"):
                        start_date = ui.input(
                            label="Start date",
                            placeholder="YYYY-MM-DD",
                        ).props("outlined dense rounded").classes("flex-grow")
                        end_date = ui.input(
                            label="End date",
                            placeholder="YYYY-MM-DD",
                        ).props("outlined dense rounded").classes("flex-grow")
                    max_filings = ui.number(
                        label="Max filings (0 = no limit)",
                        value=0,
                        min=0,
                    ).props("outlined dense rounded").classes("w-full")
                    output_dir = ui.input(
                        label="Local output folder",
                        value="./contracts",
                    ).props("outlined dense rounded").classes("w-full")

            preview_mode = ui.radio(
                ["Preview — let me pick which to save / upload",
                 "Save & upload everything that matches"],
                value="Preview — let me pick which to save / upload",
            ).props("inline")

            # Status + log live-updating without a full page rerun
            status_label = ui.label("").classes("text-secondary")
            log_area = ui.log(max_lines=400).classes(
                "w-full h-64 bg-white text-xs"
            ).style(
                "border: 1px solid #E8E1D2; border-radius: 10px; "
                "padding: 0.75rem; font-family: ui-monospace, monospace;"
            )

            async def run_scan():
                if not sec_user_agent.value or "@" not in sec_user_agent.value:
                    ui.notify(
                        "SEC User-Agent must include a contact email.",
                        type="negative",
                    )
                    return
                run_btn.props("loading")
                status_label.text = "Scan running…"

                # Capture print output from scan_and_download into the log.
                # In a real port we'd refactor scan_and_download to take a
                # log callback; for the sketch we redirect stdout.
                import io, contextlib

                def do_scan():
                    edgar = EdgarClient(user_agent=sec_user_agent.value.strip())
                    buf = io.StringIO()
                    try:
                        with contextlib.redirect_stdout(buf), \
                             contextlib.redirect_stderr(buf):
                            scan_and_download(
                                client=edgar,
                                phrase=phrase.value.strip(),
                                output_dir=Path(output_dir.value.strip()),
                                forms=list(forms.value),
                                start=start_date.value.strip() or None,
                                end=end_date.value.strip() or None,
                                max_filings=int(max_filings.value) or None,
                                include_exhibits=include_exhibits.value,
                                staging_only=preview_mode.value.startswith("Preview"),
                            )
                    finally:
                        for line in buf.getvalue().splitlines():
                            log_area.push(line)

                # NiceGUI's run.io_bound handles offloading sync work to a
                # threadpool so the UI stays responsive — equivalent of the
                # threading.Thread + queue dance in app.py, but built-in.
                from nicegui import run
                await run.io_bound(do_scan)

                run_btn.props(remove="loading")
                status_label.text = "Scan complete."

            run_btn = ui.button(
                "Run scan", on_click=run_scan,
            ).props("rounded color=primary unelevated").classes(
                "w-full text-base"
            ).style("padding: 0.75rem;")


ui.run(
    title="ContractReview · GC AI",
    favicon=None,
    port=8080,
    reload=False,
    show=False,
)
