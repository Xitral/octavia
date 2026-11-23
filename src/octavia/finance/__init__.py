"""Money: a private, read-only record of the user's accounts that she reads like any other source.

``importers`` reads the statements the user downloads (CSV, OFX, QFX), ``reader`` reads their bank pages in a browser
profile of its own that can't click or type, ``store`` keeps it all in SQLite on this PC, ``analysis`` turns it into
plain facts (recurring payments, income, odd charges, whether the money lasts to payday) and notices what changed,
and ``service`` runs it in the background. ``privacy`` holds what's never kept, which pages may be read, and the purge.
The tools are in ``octavia.tools.extensions.finance``. docs/finance.md has the whole picture.

Nothing here can move money, pay, or sign in for the user: there is no code for it.
"""
