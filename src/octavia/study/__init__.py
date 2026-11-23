"""Studying with her (docs/study.md): what each course's next exam covers, how far the user has got, their weak spots,
the questions they missed (brought back on a spaced schedule) and their study sessions, kept in ``data/study``.

``store`` keeps it, ``nudges`` turns it into the few things she brings up on her own (an exam close with no study
yet, missed questions due for review), through Futures' proactive gate like everything else she says unasked, and
``exams`` notices exams in what Canvas lists. The tools are in ``tools/extensions/study.py``.
"""
