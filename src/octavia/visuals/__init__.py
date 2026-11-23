"""Her pop-up panels: diagrams, charts, formatted notes, key facts, steps and comparisons she puts on the screen
while she explains something (docs/visuals.md).

- ``spec``: what a panel holds (``Visual`` and its blocks), checked with messages she can act on.
- ``svg_clean``: a drawing of hers rebuilt from harmless parts before it's shown.
- ``host``: ``PanelHost`` runs the panel app (``avatar/panels``) and shows, updates and closes panels.

The ``show_visual`` and ``close_visual`` tools (``tools/extensions/visuals.py``) are how she uses it; other features
call ``PanelHost.show`` from ``ctx.extras["visuals"]``.
"""
