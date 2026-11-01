# -*- coding: utf-8 -*-
import functools
import os
from gui.mods.offhangar.utils import *

# Process attribution. In the authority/worker split BOTH processes append to
# the SAME python.log, so a marker could not be told apart by process: when the
# headless worker disappears the visible client is promoted to bot authority and
# starts logging role=authority as well, and the 04.10 13:4x session left 55 such
# windows that could not be analysed at all (CONTEXT_AI #88). Rotating the logs
# cannot fix this - one file with two writers is inherent to the split - so every
# line this mod prints now carries [W] (worker) or [C] (visible client) right
# after the [OFFHANGAR] tag. That literal is kept byte-identical on purpose:
# docs and _pi_tools exports grep it.
try:
    _PROC = ('[W]' if os.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker'
             else '[C]')
except Exception:
    _PROC = '[?]'

doLog = functools.partial(doLog, 'OFFHANGAR')
_raw_log_note = functools.partial(doLog, _PROC, '[NOTE]')
_raw_log_error = functools.partial(doLog, _PROC, '[ERROR]')


def _flush_bw_log():
    try:
        import BigWorld
        if hasattr(BigWorld, 'flushPythonLog'):
            BigWorld.flushPythonLog()
    except Exception:
        pass


def LOG_NOTE(*args, **kwargs):
    _raw_log_note(*args, **kwargs)


def LOG_ERROR(*args, **kwargs):
    _raw_log_error(*args, **kwargs)
    _flush_bw_log()


# [DEBUG] lines are synchronous file I/O into python.log. config
# "debug_logging": false turns them off (errors stay); no recompile needed.
_DBG = [False]
try:
    from gui.mods.offhangar import paths as _paths
    _DBG[0] = bool(_paths.load_config()[0].get('debug_logging', False))
except Exception:
    pass
_dbg_log = functools.partial(doLog, _PROC, '[DEBUG]')
def LOG_DEBUG(*args, **kwargs):
    if _DBG[0]:
        _dbg_log(*args, **kwargs)


# --- API the adopted internal-layout modules expect -------------------------
# They come from a build with a structured logger. Mapping their three entry
# points onto this one keeps those files unmodified, so a newer copy can be
# dropped in without re-patching it.

def FULL_DIAGNOSTICS_ENABLED():
    '''True when [DEBUG] lines are being written. The layout modules use this to
    skip work that only exists to produce diagnostics.'''
    return bool(_DBG[0])


def LOG_EVENT(category, event, **fields):
    '''Structured line: "modules internal_layout_built vehicle=... errors=0".'''
    if not _DBG[0]:
        return
    try:
        parts = ['%s=%s' % (key, fields[key]) for key in sorted(fields)]
        _dbg_log('%s %s %s' % (category, event, ' '.join(parts)))
    except Exception:
        pass


def LOG_EXCEPTION(category='python', event='exception', *args, **fields):
    '''Errors stay visible even with debug logging off - a swallowed traceback
    in the layout code is exactly what would make a missing profile look like a
    silently empty tank.'''
    try:
        import traceback
        detail = traceback.format_exc()
    except Exception:
        detail = '<no traceback>'
    try:
        parts = ['%s=%s' % (key, fields[key]) for key in sorted(fields)]
        if args:
            parts.extend([str(a) for a in args])
        LOG_ERROR('%s %s %s' % (category, event, ' '.join(parts)))
        LOG_ERROR(detail)
    except Exception:
        pass