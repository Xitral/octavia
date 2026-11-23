"""Tools added after the first version, one module per feature, loaded automatically.

Each module exports ``TOOLS = [...]`` (Tool classes) like the built-in tool modules.
Nothing else needs to change to add one: ``build_registry`` loads every module here.
This is where the self-improvement builder puts new capabilities.
"""
