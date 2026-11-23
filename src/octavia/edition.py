"""Which edition this copy is: the owner's own install, or the public build others install.

The public build (``octavia release build``, src/octavia/release/) is made from a clean export of the repository and
rewrites the line below to ``PUBLIC = True``. Nothing else in the code base sets it, so the owner's install, the test
suite and self-improvement builds all run as the personal edition, exactly as before.

What the public edition changes:

* ``config/public.yaml`` is merged over ``config/default.yaml`` (before ``config/local.yaml``): a generic persona,
  the public default voice, no local brain paths, no self-improvement until the user turns it on.
* The setup wizard (``octavia welcome``) runs before the first start when nothing is set up yet.
"""

PUBLIC = True


def windows_name(personal: str, public: str) -> str:
    """A name this install claims in Windows (a startup entry, a scheduled task, menu keys): the public build's own,
    so a public install on the owner's PC never takes over or removes his install's."""
    return public if PUBLIC else personal
