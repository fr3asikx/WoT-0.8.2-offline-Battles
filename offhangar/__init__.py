# -*- coding: utf-8 -*-
"""WoT 0.8.2 Offline Battles modular package (core, native, ai, combat, physics, ui, network)."""

import os as _os

_PKG_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_SUBFOLDERS = ('core', 'native', 'ai', 'combat', 'physics', 'ui', 'network')

try:
	__path__
except NameError:
	__path__ = [_PKG_ROOT]

for _sub in _SUBFOLDERS:
	_sub_path = _os.path.join(_PKG_ROOT, _sub)
	if _os.path.isdir(_sub_path) and _sub_path not in __path__:
		__path__.append(_sub_path)
