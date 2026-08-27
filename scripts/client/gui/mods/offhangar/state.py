import cPickle
import os

from debug_utils import LOG_CURRENT_EXCEPTION
from items import ITEM_TYPE_INDICES

from gui.mods.offhangar.logging import LOG_DEBUG, LOG_ERROR
from gui.mods.offhangar import paths as _paths


# Lives outside res_mods so mod updates never touch the garage save.
STATE_FILE = _paths.USER_STATE_FILE
_paths.ensure_user_dir()
STATE_VERSION = 1

VEHICLE_FIELDS = (
	'compDescr',
	'eqs',
	'eqsLayout',
	'shells',
	'shellsLayout',
	'settings'
)


def _default_state():
	return {'version': STATE_VERSION, 'vehicles': {}, 'items': {}}


BACKUP_FILE = STATE_FILE + '.bak'
TEMP_FILE = STATE_FILE + '.tmp'


def load_state():
	'''Newest readable save, preferring the live file over the .bak.

	save_state() moves the previous save to .bak instead of deleting it, so a
	crash mid-save can leave the live file missing or half-written while the
	previous one is still intact next to it. Trying both here is what makes
	that scheme actually recover a garage instead of silently starting empty.'''
	for path in (STATE_FILE, BACKUP_FILE):
		if not os.path.exists(path):
			continue
		try:
			f = open(path, 'rb')
			try:
				state = cPickle.load(f)
			finally:
				f.close()
		except Exception:
			LOG_ERROR('State: %s is unreadable, trying the next candidate' % (path,))
			LOG_CURRENT_EXCEPTION()
			continue
		if not isinstance(state, dict):
			LOG_ERROR('State: %s does not hold a save, ignoring it' % (path,))
			continue
		version = state.get('version')
		if version != STATE_VERSION:
			# Loud on purpose. This used to return an empty state without a word,
			# which reaches the player as 'my whole garage was wiped'. The file is
			# left on disk either way - a downgrade back to the older build finds
			# it again.
			LOG_ERROR('State: %s was written by save format %r, this build reads %r. '
			          'It is being IGNORED, not deleted - your garage starts fresh, and the '
			          'file is still there if you go back to the older build.'
			          % (path, version, STATE_VERSION))
			continue
		state.setdefault('vehicles', {})
		state.setdefault('items', {})
		if path != STATE_FILE:
			LOG_ERROR('State: recovered the garage from %s - %s was missing or damaged.'
			          % (path, STATE_FILE))
		return state
	return _default_state()


def save_state(state):
	'''Write the save so that no crash can leave the player with nothing.

	The old sequence was: write .tmp, os.remove(save), os.rename(.tmp, save).
	Between the remove and the rename there was NO save on disk at all, and
	this client does die on its own (python.log carries EXCEPTION_ACCESS_
	VIOLATION crashes) - landing in that window destroyed the new write AND
	the garage it was replacing.

	Now the previous save steps aside to .bak rather than being deleted, and
	the .tmp is read back before it is allowed to take over, so a short or
	truncated write can never replace a good save. Every crash point in the
	sequence below leaves at least one complete file behind, and load_state()
	tries both.

	(os.rename cannot overwrite an existing file on Windows, which is why this
	is a three-step dance and not a single atomic replace.)'''
	try:
		f = open(TEMP_FILE, 'wb')
		try:
			cPickle.dump(state, f, cPickle.HIGHEST_PROTOCOL)
			f.flush()
			try:
				os.fsync(f.fileno())
			except (OSError, IOError, AttributeError):
				# No fsync here just means the bytes may still sit in the OS
				# cache; the .bak below still covers us.
				pass
		finally:
			f.close()
		# Read it back before it is allowed to replace anything.
		f = open(TEMP_FILE, 'rb')
		try:
			cPickle.load(f)
		finally:
			f.close()
		if os.path.exists(STATE_FILE):
			if os.path.exists(BACKUP_FILE):
				os.remove(BACKUP_FILE)
			os.rename(STATE_FILE, BACKUP_FILE)
		os.rename(TEMP_FILE, STATE_FILE)
		return True
	except Exception:
		LOG_CURRENT_EXCEPTION()
		# Failed part-way: if the old save had already stepped aside and the
		# new one never landed, put it back rather than leaving no save.
		try:
			if not os.path.exists(STATE_FILE) and os.path.exists(BACKUP_FILE):
				os.rename(BACKUP_FILE, STATE_FILE)
		except (OSError, IOError):
			pass
		try:
			if os.path.exists(TEMP_FILE):
				os.remove(TEMP_FILE)
		except (OSError, IOError):
			pass
	return False


def apply_state_to_inventory(inventory):
	state = load_state()
	vehData = inventory.get(ITEM_TYPE_INDICES['vehicle'], {})
	for vehInvID, saved in state.get('vehicles', {}).iteritems():
		for fieldName in VEHICLE_FIELDS:
			if fieldName in saved and fieldName in vehData:
				vehData[fieldName][vehInvID] = saved[fieldName]
	for itemTypeIdx, savedItems in state.get('items', {}).iteritems():
		bucket = inventory.setdefault(itemTypeIdx, {})
		if isinstance(bucket, dict):
			bucket.update(savedItems)
	LOG_DEBUG('State.apply', len(state.get('vehicles', {})), 'vehicles')
	return inventory


def save_vehicle_state(inventory, vehInvID):
	state = load_state()
	vehData = inventory.get(ITEM_TYPE_INDICES['vehicle'], {})
	saved = {}
	for fieldName in VEHICLE_FIELDS:
		field = vehData.get(fieldName, {})
		if isinstance(field, dict) and vehInvID in field:
			saved[fieldName] = field[vehInvID]
	state.setdefault('vehicles', {})[vehInvID] = saved
	ok = save_state(state)
	if ok:
		LOG_DEBUG('State.saveVehicle', vehInvID)
	return ok


def save_item_state(inventory, itemTypeIdx):
	state = load_state()
	bucket = inventory.get(itemTypeIdx, {})
	if isinstance(bucket, dict):
		state.setdefault('items', {})[itemTypeIdx] = bucket.copy()
		ok = save_state(state)
		if ok:
			LOG_DEBUG('State.saveItems', itemTypeIdx, len(bucket))
		return ok
	return False
