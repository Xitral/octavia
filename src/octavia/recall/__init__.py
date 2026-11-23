"""Screen history ("total recall"): a private, searchable record of the text that was on the user's screen.

``capture`` reads the window in front (UI Automation text, OCR as a fallback), ``privacy`` decides what's never kept
and cleans the rest, ``store`` keeps it in SQLite with full-text search, and ``service`` runs it in the background.
The tools are in ``octavia.tools.extensions.screen_history``. docs/screen-history.md has the whole picture.
"""
