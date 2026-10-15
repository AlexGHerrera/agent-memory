"""Small locale table for user-facing text (INDEX.md headings and the `day` view). Default language: en."""
from __future__ import annotations

LANGUAGES = ("en", "es")
# Used in prompts: "Write the summary in <name>."
LANGUAGE_NAMES = {"es": "Spanish", "en": "English"}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # day view
        "invalid_date": "Invalid date: {date!r}. Use YYYY-MM-DD, 'today' or 'yesterday'.",
        "day_title": "# Day {d}",
        "day_summary": "## Day summary",
        "summary_generated": "(summary generated {at})",
        "no_day_summary": "(No consolidated summary for this day yet; sessions and their first message are below.)",
        "no_messages": "No messages recorded that day.",
        "sessions_heading": "## Sessions (use session(id) for the raw transcript)",
        "n_subagents": " · {n} subagents",
        "session_line": "- `{sid}` · {project} · {machine} · {start}–{end} (local time) · {n} messages{sub}",
        "session_start": "  Start: {title}",
        "orphan_line": "- `{sid}` · subagent of {parent} · {start}–{end}",
        "notes_heading": "## Notes",
        # INDEX.md
        "index_title": "# INDEX — agent memory (updated {now} {tz})",
        "index_tools": ("MCP `memory` tools: search(query, since?, until?, project?), day(date), session(id), "
                        "note(text, project?), facts(query?, category?, include_history?). For detail: day(date), "
                        "then session(id)."),
        "index_days": "Recent days",
        "index_weeks": "Weeks",
        "index_months": "Months",
        "index_years": "Years",
        "index_notes": "Recent notes",
        "index_projects": "Projects",
        "index_project_line": "- {project}: {n} sessions, last {last}",
        "index_totals": "_{n} raw messages, from {a} to {b}._",
        "index_truncated": "_(truncated to {n} lines)_",
    },
    "es": {
        "invalid_date": "Fecha no válida: {date!r}. Usa YYYY-MM-DD, 'hoy' o 'ayer'.",
        "day_title": "# Día {d}",
        "day_summary": "## Resumen del día",
        "summary_generated": "(resumen generado {at})",
        "no_day_summary": "(Aún no hay resumen consolidado de este día; abajo están las sesiones y su primer mensaje.)",
        "no_messages": "No hay mensajes registrados ese día.",
        "sessions_heading": "## Sesiones (usa session(id) para ver el bruto)",
        "n_subagents": " · {n} subagentes",
        "session_line": "- `{sid}` · {project} · {machine} · {start}–{end} (hora local) · {n} mensajes{sub}",
        "session_start": "  Inicio: {title}",
        "orphan_line": "- `{sid}` · subagente de {parent} · {start}–{end}",
        "notes_heading": "## Notas",
        "index_title": "# INDEX — memoria del agente (actualizado {now} {tz})",
        "index_tools": ("Herramientas MCP `memory`: search(query, since?, until?, project?), day(date), session(id), "
                        "note(text, project?), facts(query?, category?, include_history?). Para el detalle: "
                        "day(fecha) y luego session(id)."),
        "index_days": "Últimos días",
        "index_weeks": "Semanas",
        "index_months": "Meses",
        "index_years": "Años",
        "index_notes": "Notas recientes",
        "index_projects": "Proyectos",
        "index_project_line": "- {project}: {n} sesiones, última {last}",
        "index_totals": "_{n} mensajes en bruto, del {a} al {b}._",
        "index_truncated": "_(recortado a {n} líneas)_",
    },
}


def t(lang: str, key: str, **kw) -> str:
    """Localized string; falls back to English if the language or key is missing."""
    s = STRINGS.get(lang, {}).get(key) or STRINGS["en"][key]
    return s.format(**kw) if kw else s


def language_name(lang: str) -> str:
    return LANGUAGE_NAMES.get(lang, "English")
