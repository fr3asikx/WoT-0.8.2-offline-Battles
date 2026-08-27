import BigWorld

class CameraNode(BigWorld.UserDataObject):

    def __init__(self):
        BigWorld.UserDataObject.__init__(self)


# Polyfill for GUI Mod Loader
def load_mods():
    from constants import IS_DEVELOPMENT
    from debug_utils import LOG_DEBUG, LOG_NOTE, LOG_CURRENT_EXCEPTION, LOG_WARNING
    import ResMgr, os, glob, sys, types

    modulepath = '/scripts/client/gui/mods/mod_*'
    LOG_NOTE('Polyfill for GUI Mod Loader: idea by goofy67, implementation by WG & DrWeb7_1')
    sec = ResMgr.openSection('../paths.xml')
    subsec = sec['Paths']
    vals = subsec.values()[0:2]

    loaded = set()
    # Search order: prefer .pyc first, then fall back to .py
    for suffix in ('.pyc', '.py'):
        for val in vals:
            mp = val.asString + modulepath + suffix
            for fp in glob.iglob(mp):
                _, fn = os.path.split(fp)
                sn = fn.rsplit('.', 1)[0]
                if sn == '__init__' or sn in loaded:
                    continue
                loaded.add(sn)
                full_name = 'gui.mods.' + sn
                try:
                    LOG_DEBUG('GUI mod found', sn, suffix)
                    if suffix == '.py' and full_name not in sys.modules:
                        # Load .py directly via execfile so it works in production mode
                        mod = types.ModuleType(full_name)
                        mod.__file__ = fp
                        mod.__name__ = full_name
                        sys.modules[full_name] = mod
                        # Also register short name
                        short = 'gui.mods.' + sn
                        try:
                            execfile(fp, mod.__dict__)
                        except Exception:
                            del sys.modules[full_name]
                            raise
                    else:
                        exec 'import gui.mods.' + sn
                except Exception as e:
                    LOG_WARNING('A problem had occurred while importing GUI mod', sn)
                    LOG_CURRENT_EXCEPTION()

load_mods()