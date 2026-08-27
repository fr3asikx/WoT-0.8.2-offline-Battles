import functools
from gui.mods.offhangar.utils import *

doLog = functools.partial(doLog, 'OFFHANGAR')
LOG_NOTE = functools.partial(doLog, '[NOTE]')
LOG_ERROR = functools.partial(doLog, '[ERROR]')

# [DEBUG] lines are synchronous file I/O into python.log. config
# "debug_logging": false turns them off (errors stay); no recompile needed.
_DBG = [True]
try:
    from gui.mods.offhangar import paths as _paths
    _DBG[0] = bool(_paths.load_config()[0].get('debug_logging', True))
except Exception:
    pass
_dbg_log = functools.partial(doLog, '[DEBUG]')
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