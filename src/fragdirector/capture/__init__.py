"""Capture: one video clip per shot, recorded from CS2 itself.

The method (TCP console + FFmpeg window capture) was chosen in spike T4;
``docs/capture.md`` explains it. Clips are named from the shot alone
(``clip_path``), so the video assembler finds them from the shot list.

* ``plan``  pure: shot list -> jobs, clip names, console and FFmpeg commands
* ``game``  the CS2 console, the game session, the window recorder
* ``run``   one game session for a list of jobs, resumable
"""

from fragdirector.capture.plan import (
    CaptureError,
    CaptureOptions,
    ClipFailed,
    Job,
    clip_name,
    clip_path,
    plan_jobs,
)
from fragdirector.capture.run import JobResult, capture_jobs, demo_files

__all__ = [
    "CaptureError",
    "CaptureOptions",
    "ClipFailed",
    "Job",
    "JobResult",
    "capture_jobs",
    "clip_name",
    "clip_path",
    "demo_files",
    "plan_jobs",
]
