'''User-data locations and config loading for the offhangar mod.

User-owned files (config.json, offhangar_state.dat) live in
<game folder>/offhangar_user/, OUTSIDE res_mods, so mod updates - even
deleting res_mods entirely - never touch them. Paths are relative to the
game root, which is the client's working directory (the same assumption
state.py has always made for its res_mods-relative path).

This module must stay stdlib-only: it sits at the bottom of the offhangar
import graph (logging.py reads the debug flag through it before anything
else loads).
'''

import json
import os
import shutil
import sys

USER_DIR = 'offhangar_user'
USER_CONFIG_FILE = os.path.join(USER_DIR, 'config.json')
USER_STATE_FILE = os.path.join(USER_DIR, 'offhangar_state.dat')

DEFAULTS_NAME = 'config_defaults.json'

_OLD_MOD_DIR = os.path.join('res_mods', '0.8.2', 'scripts', 'client', 'gui', 'mods', 'offhangar')


def mod_dir():
	# co_filename may be the author's compile-time path baked into a shipped
	# .pyc; fall back to the standard install location (same pattern the old
	# config readers used).
	d = os.path.dirname(sys._getframe().f_code.co_filename)
	if not os.path.isdir(d):
		d = _OLD_MOD_DIR
	return d


def defaults_file():
	return os.path.normpath(os.path.join(mod_dir(), DEFAULTS_NAME))


def _migrate_file(oldPath, newPath):
	if os.path.exists(newPath) or not os.path.exists(oldPath):
		return
	try:
		os.rename(oldPath, newPath)
	except (OSError, IOError):
		# Locked or cross-volume source: copy and leave the original behind.
		try:
			shutil.copy2(oldPath, newPath)
		except (OSError, IOError):
			pass


def ensure_user_dir():
	'''Creates the user-data dir; moves config.json / offhangar_state.dat from
	the old in-res_mods location (pre-offhangar_user versions) exactly once.'''
	try:
		if not os.path.isdir(USER_DIR):
			os.makedirs(USER_DIR)
	except (OSError, IOError):
		return
	_migrate_file(os.path.join(_OLD_MOD_DIR, 'config.json'), USER_CONFIG_FILE)
	_migrate_file(os.path.join(_OLD_MOD_DIR, 'offhangar_state.dat'), USER_STATE_FILE)


def _read_json(path):
	f = open(path, 'r')
	try:
		return json.load(f)
	finally:
		f.close()


_cache = [None]


def load_config():
	'''Returns (options, newKeys, errors).

	options: shipped defaults (config_defaults.json next to this module)
	overlaid with the user's offhangar_user/config.json. On first run the
	defaults file is copied out verbatim as the user's editable config.
	newKeys: option names added by a mod update that the user's config does
	not have yet (their defaults are in effect). errors: human-readable
	strings for the caller to log (this module cannot import offhangar
	logging without creating an import cycle). Result is cached.
	'''
	if _cache[0] is not None:
		return _cache[0]
	ensure_user_dir()
	errors = []
	defaults = {}
	defaultsPath = defaults_file()
	try:
		defaults = _read_json(defaultsPath)
	except (OSError, IOError, ValueError) as e:
		errors.append('cannot read %s: %s' % (defaultsPath, e))
	userOptions = None
	if os.path.exists(USER_CONFIG_FILE):
		try:
			userOptions = _read_json(USER_CONFIG_FILE)
		except (OSError, IOError, ValueError) as e:
			# Broken user file: run on defaults, never overwrite their file.
			errors.append('cannot read %s, using defaults: %s' % (USER_CONFIG_FILE, e))
	elif defaults:
		try:
			# Verbatim copy keeps key order and the _-prefixed doc entries.
			shutil.copyfile(defaultsPath, USER_CONFIG_FILE)
		except (OSError, IOError) as e:
			errors.append('cannot create %s: %s' % (USER_CONFIG_FILE, e))
	options = dict(defaults)
	newKeys = []
	if isinstance(userOptions, dict):
		options.update(userOptions)
		newKeys = sorted([k for k in defaults
		                  if k not in userOptions and not k.startswith('_')])
		# Options the user's file carries that this build knows nothing about.
		# They used to be merged in and then simply never read, so a typo
		# ('spoting_enabled') and an option a later build dropped both looked
		# exactly like a setting that works. Reported, never removed - the file
		# belongs to the user.
		unknownKeys = sorted([k for k in userOptions
		                      if k not in defaults and not k.startswith('_')])
		if unknownKeys and defaults:
			errors.append('%s sets options this build does not use, so they have no '
			              'effect - a typo, or left over from an older build: %s'
			              % (USER_CONFIG_FILE, ', '.join(unknownKeys)))
	_cache[0] = (options, newKeys, errors)
	return _cache[0]
