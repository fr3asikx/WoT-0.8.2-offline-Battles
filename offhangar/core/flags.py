# -*- coding: utf-8 -*-
"""One shared reader for the mod's per-battle config flags.

Why this module exists: `offline_battle.py` grew to ~29.3k lines and every new
switch added another ~25-line reader that repeated the same three jobs - resolve
the battle generation, read `CONFIG_OPTIONS` once per battle and cache it, log one
line the first time. Nine near-identical copies were added on 03-04.10 alone
(`_offh_helpers_reenable_cfg`, `_offh_crosshair_cfg`, `_offh_wreck_rebind_cfg`,
`_offh_results_window_cfg`, `_offh_lobby_tail_cfg`, `_offh_hangar_data_cfg`,
`_offh_los_trim_cfg`, `_offh_los_budget_cfg`, `_offh_exit_sound_cfg`). This is the
first leaf extracted out of that file: pure logic, explicit arguments, and -
important for the extraction rule - NO `globals()` access, because `globals()`
resolves per module and a function moved to another file silently loses the mod's
shared state (`offline_battle.py` has 538 `globals()` reads and 168 writes).

Contract:
  value, fresh = battle_value(name, default, gen)
`fresh` is True only on the call that actually recomputed, so the caller can log
exactly once per battle generation - the same behaviour the nine originals had.

Everything is fail-safe: an unreadable config, a wrong type or an out-of-range
choice yields `default`, which for every flag in this mod equals the shipped
behaviour.
"""

# name -> {'gen': int, 'value': object}. Never cleared: one entry per flag name,
# and `gen` changes per battle, so a stale entry simply misses and recomputes.
_CACHES = {}


def battle_value(name, default, gen, kind='bool', choices=None):
	"""Read one config flag, cached per battle generation.

	`kind`: 'bool' | 'str' | 'float' | 'int'.
	`choices`: for 'str', the allowed values; anything else falls back to `default`.
	Returns `(value, recomputed)`.
	"""
	try:
		_gen = int(gen)
	except Exception:
		_gen = 0
	cache = _CACHES.get(name)
	if cache is not None and cache.get('gen') == _gen:
		return (cache.get('value'), False)
	value = default
	try:
		from gui.mods.offhangar._constants import CONFIG_OPTIONS as _CFG
		raw = _CFG.get(name, default)
		if kind == 'bool':
			value = bool(raw)
		elif kind == 'str':
			value = str(raw if raw is not None else default)
			if choices is not None and value not in choices:
				value = default
		elif kind == 'float':
			value = float(raw)
		elif kind == 'int':
			value = int(raw)
		else:
			value = default
	except Exception:
		value = default
	_CACHES[name] = {'gen': _gen, 'value': value}
	return (value, True)


def cache_state(name):
	"""Cached (gen, value) for one flag, or None - for probes and host tests."""
	cache = _CACHES.get(name)
	if cache is None:
		return None
	return (cache.get('gen'), cache.get('value'))


def reset(name=None):
	"""Drop cached flag values (a new battle generation invalidates them anyway)."""
	try:
		if name is None:
			for key in list(_CACHES.keys()):
				del _CACHES[key]
		else:
			_CACHES.pop(name, None)
	except Exception:
		pass


# ---- "two spirits" preset ------------------------------------------
SPIRITS = ('vanilla', 'modern')  # two spirits, no 'legacy'
SPIRIT_NODES = ('matchmaker', 'he_splash', 'physics', 'sway', 'camera_shake')


def spirit(node, gen):
	"""Resolved spirit of one node: spirit_<node> unless 'preset', else game_spirit.

	'vanilla' (default) = 0.8.2, 'modern' = modern WoT. an old 'legacy'
	value is unknown now and falls back to the default (vanilla).
	Returns (spirit, recomputed).
	"""
	global_value, fresh_g = battle_value('game_spirit', 'vanilla', gen, 'str', SPIRITS)
	node_value, fresh_n = battle_value('spirit_' + str(node), 'preset', gen, 'str',
		('preset',) + SPIRITS)
	if node_value == 'preset':
		return (global_value, fresh_n)
	return (node_value, fresh_n)
