"""
Memory-reclaim helper for the DXF review pipeline.

gc.collect() frees Python objects but glibc's malloc typically keeps the
underlying arenas mapped rather than returning them to the OS, so process RSS
stays at its DXF-parse high-water mark afterward. On the 512 MB Render
instance this leftover RSS stacks with the next stage's (vision render/API
call) allocations and can trip the OOM killer even though nothing is
simultaneously "live" in Python's own accounting. malloc_trim(0) forces glibc
to release fully-free arenas back to the kernel.
"""
import ctypes
import gc


def trim_memory():
    gc.collect()
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError):
        pass  # non-glibc platform (e.g. macOS dev) — gc.collect() above still ran
