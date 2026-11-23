"""Octavia, a local voice assistant for Windows.

On import, Windows' own C++ runtime (``System32\\msvcp140.dll``) is loaded before any package can bring its own:
a process uses whichever ``msvcp140.dll`` loaded first. The ``winrt`` package (media control) ships an old one
(14.29), and once that's in, loading onnxruntime (the wake word, voice activity and speech recognition models,
built against 14.40 or newer) crashes the whole process with an access violation. Windows' copy is kept current by
Windows Update and serves both.

numpy's OpenBLAS is held to one thread before anything imports numpy. By default it reserves a buffer for every
logical core (24 here), about 740 MB of commit charge that is almost never touched but still counts against the
PC's memory limit. Her numpy work is small audio vectors; the models run on onnxruntime's own threads.
"""

import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

if sys.platform == "win32":  # pragma: no branch

    def _load_system_cpp_runtime() -> None:
        import ctypes

        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll")
        if os.path.isfile(path):
            try:
                ctypes.WinDLL(path)
            except OSError:
                pass  # no worse off than before: a package's own copy loads as it always did

    _load_system_cpp_runtime()
    del _load_system_cpp_runtime
