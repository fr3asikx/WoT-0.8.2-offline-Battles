# -*- coding: utf-8 -*-
'''Compatibility shim for the internal-layout modules.

Those modules came from a separate build of this mod that keeps its user-owned
files behind a `user_config` API. This mod already has one - paths.py, with the
same `offhangar_user/` directory - so the shim maps their three entry points
onto it instead of introducing a second user-data location.

Kept deliberately small and stdlib-only: paths.py sits at the bottom of the
import graph and nothing here may pull BigWorld in.
'''

import os

from gui.mods.offhangar import paths as _paths


def user_data_path(name):
    '''Absolute path of a user-owned file in offhangar_user/.'''
    _paths.ensure_user_dir()
    return os.path.abspath(os.path.join(_paths.USER_DIR, name))


def user_subdirectory_path(name):
    '''Absolute path of a user-owned SUBDIRECTORY, created on demand.'''
    _paths.ensure_user_dir()
    path = os.path.abspath(os.path.join(_paths.USER_DIR, name))
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except (OSError, IOError):
            pass
    return path


def atomic_write_user_file(name, payload, validate=None):
    '''Write payload to offhangar_user/<name> via a temp file + rename.

    validate, when given, is called with the temporary path before the rename
    and must raise to abort. Returns the final path, or None when the write
    failed - callers in the layout store treat None as "not persisted".
    '''
    target = user_data_path(name)
    temporary = target + '.tmp'
    handle = None
    try:
        handle = open(temporary, 'wb')
        if isinstance(payload, bytes):
            handle.write(payload)
        else:
            handle.write(payload.encode('utf-8'))
        handle.close()
        handle = None
        if validate is not None:
            validate(temporary)
        if os.path.exists(target):
            os.remove(target)
        os.rename(temporary, target)
        return target
    except (OSError, IOError, ValueError):
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except (OSError, IOError):
                pass
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except (OSError, IOError):
                pass
