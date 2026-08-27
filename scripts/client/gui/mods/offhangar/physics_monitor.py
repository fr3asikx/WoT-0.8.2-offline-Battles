'''Physics telemetry writer for the offhangar mod.

When config.json "physics_debug" is true, the battle tick calls log() ~5x/s
with a physics.snapshot() row. Each row is appended to

    <game folder>/offhangar_user/physics_telemetry.csv

so you can open it in a spreadsheet, graph speed/accel/grip/slide over a drive,
and dial config.json "physics_tuning" until the numbers match original WoT.
Off by default = zero file I/O (the tick never calls log() when the flag is
false). Stdlib-only; opens+closes per row so no file handle survives a battle.
'''

import os

from gui.mods.offhangar import paths as _paths

_FILE = os.path.join(_paths.USER_DIR, 'physics_telemetry.csv')
_state = {'header_cols': None, 't0': None}


def reset():
	'''Start a fresh telemetry file for a new drive session (called at battle
	start when physics_debug is on).'''
	_state['header_cols'] = None
	_state['t0'] = None
	try:
		if os.path.exists(_FILE):
			os.remove(_FILE)
	except (OSError, IOError):
		pass


def _fmt(v):
	if isinstance(v, float):
		return '%.3f' % v
	return str(v)


def log(rows, t):
	'''Append one snapshot row (list of (name, value)) with a battle-relative
	timestamp. Silent on any error - telemetry must never disturb the battle.'''
	try:
		_paths.ensure_user_dir()
		if _state['t0'] is None:
			_state['t0'] = t
		rel = t - _state['t0']
		names = [n for n, _ in rows]
		f = open(_FILE, 'a')
		try:
			if _state['header_cols'] != names:
				f.write('t,' + ','.join(names) + '\n')
				_state['header_cols'] = names
			f.write('%.3f,' % rel + ','.join(_fmt(v) for _, v in rows) + '\n')
		finally:
			f.close()
	except (OSError, IOError):
		pass
	except Exception:
		pass
