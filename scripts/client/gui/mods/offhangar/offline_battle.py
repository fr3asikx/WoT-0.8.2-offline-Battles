# -*- coding: utf-8 -*-
import time
import utils
import cPickle
from debug_utils import LOG_DEBUG, LOG_CURRENT_EXCEPTION

# Frame/tick measurement - DISABLED, and deliberately not by a config flag.
#
# perf_probe is under suspicion for a client crash. Two sessions on 15 Aug 2026
# died with the identical native fault:
#     EXCEPTION_ACCESS_VIOLATION 0xC0000005 @ 0x0095AF97 (Read @ 0x00000008)
# and both ran the probe. An older crash on 12 Aug faulted at a DIFFERENT
# address (0x00642AAB, Read @ 0x14), so it does not excuse this one. No
# mechanism has been found in the probe itself, which is exactly why it has to
# leave the code path completely rather than be switched off from config: an
# isolation test is only worth anything if the suspect is provably gone.
#
# perf_probe.py stays on disk, unimported and unreferenced. To measure again,
# restore the import below - the call sites in _aih_tick and _offh_battle_sweep
# are untouched and keep working.
class _NullProbe(object):
	enabled = False
	def start(self): pass
	def stop(self): pass
	def begin(self): pass
	def end(self): pass

_PROBE = _NullProbe()

def _dump_graphics(): pass

_g_device_damage = None

def _dd():
	"""device_damage, resolved once. The tick helpers used to run a
	'from gui.mods.offhangar import device_damage' statement on EVERY call -
	38128 __import__ round trips per 300 ticks in perf_profile_run2_before.txt.
	Deliberately lazy: importing at module scope here is circular."""
	global _g_device_damage
	if _g_device_damage is None:
		from gui.mods.offhangar import device_damage as _m
		_g_device_damage = _m
	return _g_device_damage

_g_bot_lod_cfg = None

def _bot_lod(td):
	"""LOD distances for a BOT fashion, as the 4-tuple setLods() takes:
	(traces, wheels, tracks, swinging). A lodDist is the range past which the
	feature stops being updated, and 0 disables it outright - the same
	convention _offh_make_swinging_fashion already uses.

	Bots used to be handed the tank descriptor's OWN values, i.e. exactly the
	detail a player tank renders at, times 30 of them. That is the largest
	render-side cost of a full battle: perf_tick.csv puts the non-tick part of
	the frame at 23.66 ms with 30 bots, which caps the frame rate at ~42 FPS
	however fast the Python tick gets. The player fashion does NOT come through
	here and keeps full detail."""
	global _g_bot_lod_cfg
	if _g_bot_lod_cfg is None:
		scale, traces_on, swinging_on = 0.35, False, False
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS as _c
			scale = float(_c.get('bot_lod_scale', 0.35))
			traces_on = bool(_c.get('bot_ground_traces', False))
			swinging_on = bool(_c.get('bot_hull_swinging', False))
		except Exception:
			pass
		scale = max(0.0, min(1.0, scale))
		_g_bot_lod_cfg = (scale, traces_on, swinging_on)
	scale, traces_on, swinging_on = _g_bot_lod_cfg
	chassis = td.chassis
	return (
		chassis['traces']['lodDist'] * scale if traces_on else 0.0,
		chassis['wheels']['lodDist'] * scale,
		chassis['tracks']['lodDist'] * scale,
		td.hull['swinging']['lodDist'] * scale if swinging_on else 0.0,
	)

_g_destr_authority = None

def _get_destr_authority():
	"""offhangar.destructibles_authority, with the same execfile fallback
	the package bootstrap uses (the module ships without a .pyc)."""
	global _g_destr_authority
	if _g_destr_authority is not None:
		return _g_destr_authority
	try:
		from gui.mods.offhangar import destructibles_authority as _da
		_g_destr_authority = _da
		return _da
	except Exception:
		pass
	import sys, os, types
	full_name = 'gui.mods.offhangar.destructibles_authority'
	if full_name in sys.modules:
		_g_destr_authority = sys.modules[full_name]
		return _g_destr_authority
	candidates = []
	try:
		candidates.append(os.path.dirname(os.path.abspath(__file__)))
	except Exception:
		pass
	candidates.append(os.path.join('res_mods', '0.8.2', 'scripts', 'client', 'gui', 'mods', 'offhangar'))
	for _dir in candidates:
		py_path = os.path.join(_dir, 'destructibles_authority.py')
		if os.path.exists(py_path):
			mod = types.ModuleType(full_name)
			mod.__file__ = py_path
			sys.modules[full_name] = mod
			try:
				execfile(py_path, mod.__dict__)
			except Exception:
				del sys.modules[full_name]
				raise
			_g_destr_authority = mod
			return mod
	raise ImportError('destructibles_authority not found')

g_offline_models = []
g_offline_enemies = []
def _add_model(m):
	global g_offline_models
	g_offline_models.append(m)
	import BigWorld
	BigWorld.addModel(m)


def _offh_cursor_shown():
	'''True while a modal GUI (ESC menu) owns the mouse: its clicks must not
	drive in-battle actions such as the post-mortem vehicle cycle.'''
	try:
		import GUI
		return bool(GUI.mcursor().visible)
	except Exception:
		return False


def _offh_is_ally(mock):
	'''True when this mock shares the player's team. Friendly fire IS possible
	(the shot loop does not filter by team), and sound_notifications.xml carries
	separate ally_* events - reporting a team-mate as an enemy kill is wrong.'''
	try:
		import BigWorld
		_pt = getattr(BigWorld.player(), '_offhangar_team', 1)
		_t = getattr(mock, '_bot_team', None)
		if _t is None:
			_pi = getattr(mock, 'publicInfo', None)
			_t = _pi.get('team', 2) if _pi else 2
		return _t == _pt
	except Exception:
		return False


def _offh_resolve_hull_hit(shot, dist_m, all_hits):
	'''Find the first STRUCTURAL plate behind any spaced armour.

	Returns (result, eff_armor, pierce, spaced_mm, angle_cos) where result is the
	_offh_penetration verdict for that plate, or None when the round never reaches
	structure - i.e. the track absorbed it.

	Tracks and external devices carry vehicleDamageFactor 0.0: they are not the
	hull, so they must not take hull damage. What they DO is cost penetration on
	the way through, which is why a shot that clips the track at a shallow angle
	(long path, thick effective plate) is swallowed while a square-on hit carries
	into the hull behind it.

	HEAT is a special case, as in the game: the shaped charge detonates on the
	first spaced plate it touches and the jet does not survive the standoff, so a
	track absorbs it outright regardless of the angle.'''
	import math
	if not all_hits:
		return None
	shell = (shot.get('shell') or {}) if hasattr(shot, 'get') else {}
	kind = shell.get('kind', 'ARMOR_PIERCING')
	spaced = 0.0
	try:
		_ordered = sorted(all_hits, key=lambda h: h[0])
	except Exception:
		_ordered = all_hits
	for _h in _ordered:
		try:
			_d, _ac, _mat = _h[0], _h[1], _h[2]
		except Exception:
			continue
		if _mat is None:
			continue
		_vdf = getattr(_mat, 'vehicleDamageFactor', 1.0)
		_arm = float(getattr(_mat, 'armor', 0.0) or 0.0)
		if _vdf == 0.0:
			# spaced: never structure. HEAT dies here; everything else pays armour.
			if kind == 'HOLLOW_CHARGE':
				return None
			_a = abs(float(_ac))
			if _a > 1.0: _a = 1.0
			if _a < 0.087: _a = 0.087
			spaced += _arm / _a
			continue
		if _arm <= 0.0:
			continue
		_res, _eff, _p = _offh_penetration(shot, dist_m, _arm, _ac, spaced)
		return (_res, _eff, _p, spaced, _ac)
	return None


def _offh_postmortem_grading():
	'''Retail's desaturated postmortem look, forced past the quality gate.
	
	g_postProcessing.enable('postmortem') on its own is not enough on this client.
	Two separate faults:
	
	* Every _Effect is gated by __isSupported, which needs __curQuality to be IN
	  the effect's qualityMask range - and _fromMaskToQualityRange only ever
	  produces 0/1/2. python.log reports "The quality = 4 was selected", so
	  NOTHING is supported: enable() pushes an empty chain and silently produces
	  no grading at all. That is why the grey look vanished after a graphics
	  settings change and why no error was ever logged.
	* enable() only APPENDS to __curEffects; WG relies on the OUTGOING control
	  mode's disable() to clear it. We switch to arcade first, so without an
	  explicit disable() the chain came out as arcade + postmortem mixed.
	
	The chains themselves are loaded at startup - WGPostProcessing.init() calls
	effect.create() for every mode regardless of quality - so when the supported
	path yields nothing, push the loaded chains straight through.'''
	try:
		import PostProcessing
		from post_processing import g_postProcessing as _pp
	except Exception as _ppi:
		LOG_DEBUG('postmortem grading: no post_processing (%s)' % str(_ppi))
		return
	try: _pp.disable()
	except Exception: pass
	try: _pp.enable('postmortem')
	except Exception as _ppe:
		LOG_DEBUG('postmortem grading enable err:', str(_ppe))
		return
	_cur = getattr(_pp, '_WGPostProcessing__curEffects', None) or []
	_set = getattr(_pp, '_WGPostProcessing__settings', None) or {}
	for _e in _cur:
		try:
			if _e._Effect__isSupported(_set):
				return         # quality gate passed - retail path already did the work
		except Exception:
			pass
	_chain = []
	for _e in _cur:
		# 'advanced' effects need MRT, which __isSupported hard-refuses in this
		# build; map-depended ones build their chain per arena in enable().
		if getattr(_e, '_Effect__isAdvanced', False) or getattr(_e, '_Effect__isMapDepended', False):
			continue
		_c = getattr(_e, '_Effect__chain', None)
		if not _c:
			continue
		_chain += list(_c)
		_ct = getattr(_e, '_Effect__ctrl', None)
		if _ct is not None:
			try: _ct.enable()
			except Exception: pass
	if _chain:
		try:
			PostProcessing.chain(_chain)
			LOG_DEBUG('postmortem grading forced past the quality gate: %d effects' % len(_chain))
		except Exception as _pce:
			LOG_DEBUG('postmortem grading chain err:', str(_pce))
	else:
		LOG_DEBUG('postmortem grading: no loaded chain to force')


def _module_ui_name(name):
	'''Damage-panel device name = extra name minus 'Health'; tracks keep their side.

	The battle scope defines its own and publishes it over this one. This module-level
	copy exists because _offh_knock_out_everything is module-level too: without it the
	name lookup raised NameError and took the whole panel block down with it.'''
	return name[:-6] if name.endswith('Health') else name


class _OffhAliveState(object):
	'''Alive flag that answers to BOTH `mock.isAlive()` and `if mock.isAlive:`.

	The mocks used to carry a method that always returned True, and every death
	path then overwrote it with a plain bool. From that moment WG's own code
	broke on the tank: gui/Scaleform/Battle.py DamagePanel._setup does
	`if not vehicle.isAlive():`, which on a bool raises

	    TypeError: 'bool' object is not callable

	and takes the whole panel setup with it - that traceback appears 13 times in
	one battle log, every time the panel binds to a dead mock (postmortem, and
	each spectator switch). Our own code reads the same attribute as a value in
	a dozen places, so it has to work both ways.'''
	__slots__ = ('value',)

	def __init__(self, value=True):
		self.value = bool(value)

	def set(self, value):
		self.value = bool(value)

	def __call__(self):
		return self.value

	def __nonzero__(self):      # Python 2 truth test
		return self.value

	__bool__ = __nonzero__      # and Python 3, for the desktop self-tests

	def __repr__(self):
		return 'alive' if self.value else 'dead'


def _offh_set_alive(mock, value):
	'''Set a mock's alive flag without ever turning it back into a plain bool.'''
	state = getattr(mock, 'isAlive', None)
	if isinstance(state, _OffhAliveState):
		state.set(value)
	else:
		try:
			mock.isAlive = _OffhAliveState(value)
		except Exception:
			pass


class _SynthDeviceExtra(object):
	'''Stand-in for a vehicle-type extra, carrying the one field the crit loop
	reads off it.'''
	__slots__ = ('name',)

	def __init__(self, name):
		self.name = name


class _SynthMaterial(object):
	'''Stand-in for the MaterialInfo of an INTERIOR device hit.

	0.8.2 ships no collision geometry for the interior: all 1975 collision meshes
	in this client carry only armor_N, gun, the two tracks, surveyingDevice and
	gunBreech (the interior kinds survive on two leftover models and no crewman
	kind exists at all). WG resolved those hits server-side against a model that
	was never distributed, so the crit loop is handed one of these instead. The
	values are the era common/vehicle.xml entry for a device material: armor 0,
	damageKind 1 (device), vehicleDamageFactor 0 (the hull damage is already
	accounted for by the penetrating shot) and the real hit chances, which is
	what device_damage.saving_throw reads.'''
	__slots__ = ('extra', 'armor', 'damageKind', 'vehicleDamageFactor',
	             'chanceToHitByProjectile', 'chanceToHitByExplosion')

	def __init__(self, name):
		from gui.mods.offhangar import device_damage as _dd
		self.extra = _SynthDeviceExtra(name)
		self.armor = 0
		self.damageKind = 1
		self.vehicleDamageFactor = 0.0
		self.chanceToHitByProjectile = _dd.fallback_chance(name, False)
		# Crew is the one group where the two differ: 0.33 by shell, 0.15 by blast.
		self.chanceToHitByExplosion = _dd.fallback_chance(name, True)


def _offh_interior_zone(target_mock, all_hits, start_pos, end_pos=None, td=None):
	'''Which interior compartment the shell entered: 'turret', 'hullFront',
	'hullRear' or 'hullSide'.

	The turret case is read straight off the component the shell crossed - that
	geometry IS in the collision model. The hull case is placed from the real
	entry point: the distance of the first structural plate along the shot
	segment gives a world point, the tank's inverse matrix turns it into hull
	coordinates, and device_damage.interior_zone splits it against THIS tank's
	own turret-ring position and half width (both on the descriptor). So "behind
	the ring" means behind that tank's actual ring, not a fixed fraction.

	If any of that is unavailable the bearing to the shooter decides, which is
	coarse but never wrong about a shot coming from directly astern.'''
	import Math
	_comp = None
	_dist = None
	try:
		for _h in sorted(all_hits, key=lambda _x: _x[0]):
			_m = _h[2]
			if _m is None:
				continue
			# The plate that stopped or admitted the round: structural, with thickness.
			if getattr(_m, 'vehicleDamageFactor', 1.0) != 0.0 and float(getattr(_m, 'armor', 0.0) or 0.0) > 0.0:
				_comp = _h[3]
				_dist = _h[0]
				break
	except Exception:
		_comp = None
	try:
		if _comp is not None and hasattr(_comp, 'get'):
			if str(_comp.get('itemTypeName', '')) in ('vehicleTurret', 'vehicleGun'):
				return 'turret'
	except Exception:
		pass
	# Entry point in the tank's own frame, compared against its own geometry.
	try:
		if td is None:
			td = getattr(target_mock, 'typeDescriptor', None)
		if td is not None and _dist is not None and end_pos is not None:
			_dx = float(end_pos.x) - float(start_pos.x)
			_dy = float(end_pos.y) - float(start_pos.y)
			_dz = float(end_pos.z) - float(start_pos.z)
			_dl = (_dx * _dx + _dy * _dy + _dz * _dz) ** 0.5
			if _dl > 0.001:
				_s = float(_dist) / _dl
				_wp = Math.Vector3(float(start_pos.x) + _dx * _s,
				                   float(start_pos.y) + _dy * _s,
				                   float(start_pos.z) + _dz * _s)
				_inv = Math.Matrix(target_mock.matrix)
				_inv.invert()
				_lp = _inv.applyPoint(_wp)
				# Vehicle origin sits on the chassis; the hull model is offset from it.
				_hp = td.chassis['hullPosition']
				_ring = td.hull['turretPositions'][0]
				_bb = td.hull['hitTester'].bbox
				_hw = max(abs(float(_bb[0].x)), abs(float(_bb[1].x)))
				from gui.mods.offhangar import device_damage as _DDz
				return _DDz.interior_zone(float(_lp.x) - float(_hp.x),
				                          float(_lp.z) - float(_hp.z),
				                          float(_ring.z), _hw)
	except Exception as _ze:
		LOG_DEBUG('interior zone from geometry failed, using bearing:', str(_ze))
	try:
		_pos = target_mock.position
		_fwd = Math.Matrix(target_mock.matrix).applyVector(Math.Vector3(0.0, 0.0, 1.0))
		_fx, _fz = float(_fwd.x), float(_fwd.z)
		_fl = (_fx * _fx + _fz * _fz) ** 0.5
		_tx = float(start_pos.x) - float(_pos.x)
		_tz = float(start_pos.z) - float(_pos.z)
		_tl = (_tx * _tx + _tz * _tz) ** 0.5
		if _fl > 0.001 and _tl > 0.001:
			_cos = (_fx * _tx + _fz * _tz) / (_fl * _tl)
			if _cos >= 0.5:
				return 'hullFront'      # within 60 deg of the nose
			if _cos <= -0.5:
				return 'hullRear'
	except Exception:
		pass
	return 'hullSide'


_OFFH_VOICE_BURST = [None]

# How many crew/module lines may WAIT behind the one being spoken. Each line is
# ~2.5 s and WG discards anything older than its 3 s stamp, so in practice this
# is 'the current line plus the next one or two' - enough that a shell which
# crits several things gets to report them, without the queue running seconds
# behind the fight.
_OFFH_VOICE_QUEUE_MAX = 3


def _offh_voice_burst_order(pending):
	'''Every distinct line one strike produced, most important first.

	This used to return a single line - the best one - and throw the rest away.
	One shell routinely crits two or three things at once, so a track break plus a
	downed driver was reported as ONE of the two and the other was never even
	queued. That is the bulk of "some crew voices are missing": the mod discarded
	them before the sound engine ever saw them.

	Speaking them all is safe because WG already bounds the backlog - play() stamps
	each queue item with time + timeout (3 s) and __playFirstFromQueue silently
	drops any whose stamp has passed. At ~2.5 s a line that means about two are
	actually spoken per burst, but they are the two that MATTER, because they go in
	worst-first instead of one being picked and the rest binned.

	De-duplicated: both tracks breaking is one 'track_destroyed', not two.'''
	seen = set()
	ranked = []
	for idx, snd in enumerate(pending):
		if not snd or snd in seen:
			continue
		seen.add(snd)
		rank = 2 if (snd.endswith('_destroyed') or snd.endswith('_killed')) else 1
		ranked.append((-rank, idx, snd))   # idx keeps report order within a rank
	ranked.sort()
	return [snd for _r, _i, snd in ranked]


def _offh_prepare_notifications(sn):
	'''Make one IngameSoundNotifications instance usable offline.

	THE reason the crew never spoke. play() resolves every event whose XML
	carries shouldBindToPlayer through BigWorld.player().vehicle:

	    if idToBind is None and soundDesc['shouldBindToPlayer']:
	        if BigWorld.player().vehicle is not None:

	Offline the player IS the account entity, and a PlayerAccount has no
	`vehicle` at all - account.def declares none, and the getattribute
	override in mod_offhangar only answers that name when
	_offhangar_mock_veh is set, which nothing in the mod ever sets. So the
	lookup raises AttributeError out of play() before a single line is
	queued, and every call site swallows it.

	Which events set the flag decides exactly what was missing: every module
	line (engine/ammo bay/fuel tank/radio/tracks/gun/turret ring/optics),
	every crew line, crew_deactivated, fire_started, fire_stopped,
	enemy_sighted and sight_convergence. The hit and kill reports
	(armor_*_by_player, enemy_killed*, vehicle_destroyed) do NOT set it,
	which is why those were the only ones ever heard.

	__readConfig builds __events per INSTANCE, so clearing the flag touches
	only the queues this mod owns. play() then leaves idToBind at whatever
	the caller passed: None for the player's own chatter, which always
	plays, or an explicit vehicle id where retail's "drop the line if that
	tank is already gone" rule is wanted (the bounce/no-pen reports about a
	specific enemy still pass one).'''
	try:
		_events = getattr(sn, '_IngameSoundNotifications__events', None) or {}
		_n = 0
		for _ev in _events.itervalues():
			for _desc in _ev.itervalues():
				if _desc.get('shouldBindToPlayer'):
					_desc['shouldBindToPlayer'] = False
					_n += 1
		sn._offh_unbound = True
		LOG_DEBUG('OfflineBattle.voice: unbound %d player-bound notification(s)' % _n)
	except Exception as _ue:
		LOG_DEBUG('OfflineBattle.voice: unbind failed:', str(_ue))
	return sn


def _offh_make_notifications():
	'''A started, offline-safe IngameSoundNotifications, or None.'''
	try:
		import gui.IngameSoundNotifications as _ISN
		_sn = _ISN.IngameSoundNotifications()
		_sn.start()
		return _offh_prepare_notifications(_sn)
	except Exception as _ce:
		LOG_DEBUG('OfflineBattle.voice: create failed:', str(_ce))
		return None


def _offh_player_notifications():
	'''The player's report queue - hit, kill, fire and battle-start lines.

	Retail hangs this off the Avatar in __startGUI and tears it down with the
	arena. Offline the account outlives the battle and the exit sweep
	destroys the instance, so every path that wants a line has to be able to
	rebuild one. Built lazily here, in ONE place, so no path can install an
	instance that skipped _offh_prepare_notifications.'''
	import BigWorld
	_p = BigWorld.player()
	if _p is None:
		return None
	_sn = getattr(_p, 'soundNotifications', None)
	if _sn is None:
		_sn = _offh_make_notifications()
		if _sn is None:
			return None
		try:
			_p.soundNotifications = _sn
		except Exception:
			return _sn
		return _sn
	# An instance from somewhere else, or one this build predates: bind-proof it.
	if not getattr(_sn, '_offh_unbound', False):
		_offh_prepare_notifications(_sn)
	# destroy() leaves an object that looks fine but has __soundQueues None and
	# __isEnabled False, and it then swallows every line without a word. start()
	# re-arms it in place.
	try:
		if getattr(_sn, '_IngameSoundNotifications__soundQueues', None) is None \
				or not getattr(_sn, '_IngameSoundNotifications__isEnabled', False):
			_sn.start()
			LOG_DEBUG('OfflineBattle.voice: report queue re-armed (was destroyed)')
	except Exception:
		pass
	return _sn


def _offh_notify(event, bind_id=None):
	'''Play one notification event on the player's report queue.'''
	if not event:
		return
	try:
		_sn = _offh_player_notifications()
		if _sn is None:
			return
		_sn.play(event, bind_id)
		LOG_DEBUG('VOICE: %s bind=%s' % (event, bind_id))
	except Exception as _ne:
		LOG_DEBUG('OfflineBattle.voice: play %s failed: %s' % (event, _ne))


def _offh_fire_voice(started):
	'''The fire line named by the CLIENT's own data.

	Avatar.__showDamageIconAndPlaySound reads extrasDict['fire'].sounds and
	plays 'critical' when the tank catches and 'fixed' when it goes out;
	common/vehicle.xml fills those from sounds/fireStarted and
	sounds/fireStopped. Read it the same way, fall back to the shipped names.'''
	_key = 'critical' if started else 'fixed'
	try:
		import BigWorld
		_td = getattr(BigWorld.player(), 'vehicleTypeDescriptor', None)
		_ex = _td.extrasDict.get('fire') if (_td is not None and hasattr(_td, 'extrasDict')) else None
		_snd = getattr(_ex, 'sounds', {}).get(_key) if _ex is not None else None
		if _snd:
			return _snd
	except Exception:
		pass
	return 'fire_started' if started else 'fire_stopped'


def _offh_apply_sound_priority():
	"""Make engine and track noise lose the channel contest, not the voice.

	FMOD has no priority setter exposed to script here - the only lever
	BigWorld hands out is wg_setCategoryVolume, which SoundGroups wraps as
	setVolume(category, ...). That is enough, because the virtual-voice system
	picks which voices stay audible by AUDIBILITY, and a channel's category
	volume feeds straight into it. Turning the vehicle category down therefore
	makes engines and tracks the first thing dropped when the 64 software
	channels are oversubscribed, instead of a crew line or a gun shot.

	The categories line up with what matters (SoundGroups.__categories):
	    voice    -> ingame_voice                      crew, never sacrifice
	    effects  -> hits, weapons, environment, ...   guns and impacts, ditto
	    vehicles -> vehicles                          engines and tracks
	Only 'vehicles' is touched.

	updatePrefs=False, deliberately: this is a mix decision for the battle, not
	an edit to the player's saved sound settings. enableArenaSounds() restores
	the stored value on the next transition either way.

	CAVEAT: the exact stealing policy lives inside FMOD, so treat this as
	weighting the odds rather than a hard guarantee. The hard guarantee is the
	headcount in _offh_sound_budget, which stops the loops claiming the whole
	budget in the first place.
	"""
	try:
		from _constants import CONFIG_OPTIONS as _CFG_SP
		_vol = float(_CFG_SP.get('vehicle_sound_volume', 0.6))
	except Exception:
		_vol = 0.6
	if _vol < 0.0:
		_vol = 0.0
	elif _vol > 1.0:
		_vol = 1.0
	try:
		import SoundGroups as _SGp
		if getattr(_SGp, 'g_instance', None) is None:
			return
		_stored = _SGp.g_instance.getVolume('vehicles')
		_SGp.g_instance.setVolume('vehicles', _stored * _vol, False)
		LOG_DEBUG('OfflineBattle.sound: vehicle category at %.2f of %.2f - engines yield channels to voice/guns'
			% (_stored * _vol, _stored))
	except Exception as _spe:
		LOG_DEBUG('OfflineBattle.sound: category weighting failed:', str(_spe))


def _offh_sound_budget(mocks, player_vid, px, pz, max_loops, prev):
	"""Choose which vehicles may hold looping engine/track events.

	res/engine_config.xml <soundMgr> gives the whole client 64 softwareChannels
	(512 virtual), and every vehicle inside the culling ring holds TWO events
	that never end - engine and tracks. At bots_per_team 15 that is ~62 of the
	64 spent on loops before a single shot, hit or crew line asks for a channel.
	FMOD's answer is not an error: it virtualises the least audible voices, which
	go silent but keep their position and swap back in later - engine loops
	fading in and out, a gun shot you never hear - and it fails the allocation
	outright once even the virtual pool is spent ('Failed to load sound
	.../notifications_VO/...' in the log, and historically a null handle the
	native attach path crashed on).

	Distance culling alone cannot fix it, because bots converge: they all reclaim
	their two events at the same moment, which is also the moment the shooting
	starts. So loops get a HARD headcount, nearest first, and the rest of the
	budget stays free for the transient sounds that actually carry the fight.

	Stability matters more than precision here. A vehicle that already holds its
	loops keeps a 20% distance discount, so a rival has to be clearly closer -
	not a metre closer - before it takes the slot. Without that, two bots at the
	same range trade the slot back and forth and you hear the swap.

	Returns the set of vehicle ids allowed to sound.
	"""
	ranked = []
	for _vid, _m in mocks.iteritems():
		if _vid == player_vid:
			continue
		if not getattr(_m, 'isAlive', False) or (getattr(_m, 'health', 0) or 0) <= 0:
			continue
		try:
			_dx = _m.position.x - px
			_dz = _m.position.z - pz
		except Exception:
			continue
		_d2 = _dx * _dx + _dz * _dz
		if _d2 > 16900.0:
			continue        # past the absolute audibility ring, never a candidate
		# Incumbency discount: cheaper than tracking per-bot hysteresis state.
		ranked.append(((_d2 * 0.8) if _vid in prev else _d2, _vid))
	ranked.sort()
	return set([_vid for _score, _vid in ranked[:max_loops]])


def _offh_battle_music():
	'''Start combat music and the map ambience, as MusicController does.

	Retail runs this from MusicController.__onArenaStateChanged on the
	PREBATTLE -> BATTLE edge. That handler is subscribed by onEnterArena():
	    BigWorld.player().arena.onPeriodChange += self.__onArenaStateChanged
	    self.__isOnArena = True
	and Avatar.__startGUI is its only caller. Offline the player is the account,
	not an Avatar, so onEnterArena never runs: the handler is never subscribed,
	__isOnArena stays False, and neither event was ever requested. That is why
	AMBIENT_EVENT_COMBAT - the map's own soundscape - never played at all, and
	why the one combat-music call in the mod sat at the end of loading, where the
	prebattle MUSIC_EVENT_NONE cancelled it 100 ms later.

	Both resolve through arenaType.music / arenaType.ambientSound. Every 0.8.2
	arena def leaves those unset and ArenaType.__readString falls back to
	arena_defs/_default_.xml, which supplies /music/combat/combat and
	/ambient/wind/wind - so they exist on every map, contrary to the old comment
	that read the arena XML alone and concluded most maps have no music.'''
	try:
		import MusicController as _MC
		_mc = getattr(_MC, 'g_musicController', None)
		if _mc is None:
			return
		_mc.play(_MC.MUSIC_EVENT_COMBAT)
		_mc.play(_MC.AMBIENT_EVENT_COMBAT)
		LOG_DEBUG('OfflineBattle.music: combat music + map ambience started')
	except Exception as _bme:
		LOG_DEBUG('OfflineBattle.music: battle music failed:', str(_bme))


def _offh_stop_battle_music():
	'''Silence the arena ambience on the way out.

	The exit path drops the arena CATEGORY volume to 0 (enableArenaSounds(False))
	and the hangar restores it again with enableLobbySounds(True) - so an ambience
	event left running becomes audible over the garage. Retail unhooks through
	onLeaveArena; offline nothing does, so stop it explicitly.'''
	try:
		import MusicController as _MC
		_mc = getattr(_MC, 'g_musicController', None)
		if _mc is not None:
			_mc.stopAmbient()
	except Exception:
		pass


def _offh_ignite(target_mock, is_player_target, reason, by_player=False):
	'''Set a vehicle alight and tell the damage panel about it.

	by_player: the PLAYER's shell started this fire, so an enemy going up
	earns the retail gunner's call (Avatar.playStartedFire).'''
	target_mock.is_on_fire = True
	try:
		import BigWorld as _bwig
		target_mock._fire_started = _bwig.time()
	except Exception:
		target_mock._fire_started = None
	LOG_DEBUG('FIRE IGNITED ON: %s (%s)' % (getattr(target_mock, 'id', 'PLAYER'), reason))
	if not is_player_target:
		if by_player:
			_offh_notify('enemy_fire_started_by_player', getattr(target_mock, 'id', None))
		return
	try:
		import gui.WindowsManager
		bw = gui.WindowsManager.g_windowsManager.battleWindow
		if bw is not None and hasattr(bw, 'damagePanel'):
			bw.damagePanel.onFireInVehicle(True)
	except Exception as _fe:
		LOG_DEBUG('FIRE UI UPDATE ERR:', str(_fe))
	# 'Fire in the tank!' - the loudest line in the game and the one the mod
	# never played. playRules 2 puts it at the head of the voice queue, so it
	# jumps whatever module report the same shell just produced.
	_offh_notify(_offh_fire_voice(True))


def _offh_extinguish(target_mock, is_player_target, reason):
	'''End a fire and bring the fuel tank back to its regen cap.

	The fuel tank has no repair bar in the game - a destroyed one is red for as
	long as the tank burns and turns orange the moment the fire is out, whether
	the crew smothered it or an extinguisher did. That step is here rather than
	in the repair tick, because it is the FIRE ending that restores it, not time
	spent repairing.'''
	if not getattr(target_mock, 'is_on_fire', False):
		return
	target_mock.is_on_fire = False
	target_mock._fire_started = None
	LOG_DEBUG('FIRE OUT ON: %s (%s)' % (getattr(target_mock, 'id', 'PLAYER'), reason))
	if is_player_target:
		try:
			import gui.WindowsManager
			bw = gui.WindowsManager.g_windowsManager.battleWindow
			if bw is not None and hasattr(bw, 'damagePanel'):
				bw.damagePanel.onFireInVehicle(False)
		except Exception as _xe:
			LOG_DEBUG('FIRE UI CLEAR ERR:', str(_xe))
		_offh_notify(_offh_fire_voice(False))
	# Fuel tank: destroyed -> back at the regen cap, which reads as 'repaired'.
	try:
		from gui.mods.offhangar import device_damage as _DDx
		td = _device_td(target_mock)
		hp_map = getattr(target_mock, 'devices_hp', None)
		if hp_map is None or td is None:
			return
		name = 'fuelTankHealth'
		cap = _DDx.device_regen_hp(td, name)
		if cap is None or hp_map.get(name, cap) >= cap:
			return
		hp_map[name] = cap
		destroyed = getattr(target_mock, '_destroyed_devices', None)
		if destroyed is not None:
			destroyed.discard(name)
		states = getattr(target_mock, '_module_states', None)
		max_hp = _DDx.device_max_hp(td, name)
		new_state = _DDx.device_state(cap, max_hp)
		if states is not None:
			states[name] = new_state
		_push_device_ui(target_mock, is_player_target, name, cap, max_hp, state='repaired')
	except Exception as _fx:
		LOG_DEBUG('fuel tank restore after fire failed:', str(_fx))


def _offh_module_test_mode():
	'''config module_test_mode: a bench for the module model.

	Bot shells still roll every module and crew crit exactly as they normally
	would - same era saving throws, same HP pools, same repair - but they take
	no hull HP off the player, an ammo rack does not detonate the tank, and fire
	does not drain. So a crit can be watched, repaired, re-broken and listened to
	without the run ending after four shells. Nothing about the crit model
	itself is altered; only the consequences that would end the test.'''
	try:
		from _constants import CONFIG_OPTIONS as _TCFG
		return bool(_TCFG.get('module_test_mode', False))
	except Exception:
		return False


def _offh_internal_layout(td):
	'''Per-vehicle interior layout from the adopted profile data, or None.

	None means "fall back to the measured zone model": the feature is switched
	off, the adopted modules are absent, or this tank has no profile. Their
	build_layout() keeps its own cache keyed by type + configuration, so calling
	it per shot is cheap after the first hit on a given tank.'''
	if td is None:
		return None
	try:
		from _constants import CONFIG_OPTIONS as _LCFG
		if not bool(_LCFG.get('internal_layout_profiles', True)):
			return None
	except Exception:
		pass
	try:
		from gui.mods.offhangar import internal_hit_layouts as _IHL
	except Exception as _le:
		if not globals().get('_offh_layout_import_logged'):
			globals()['_offh_layout_import_logged'] = True
			LOG_DEBUG('internal_hit_layouts unavailable, using zone model:', str(_le))
		return None
	try:
		return _IHL.build_layout(td, log_build=False)
	except Exception as _be:
		LOG_DEBUG('build_layout failed:', str(_be))
		return None


def _offh_internal_ray_hits(target_mock, td, start_pos, end_pos, covered=()):
	'''Interior modules and crew the shell REALLY passed through.

	Returns [(entry_distance, extraName)] sorted front to back, or None when no
	layout is available. The profile boxes live in their parent component's own
	space, so the segment goes through exactly the two transforms
	Vehicle.getComponents applies: world -> vehicle -> component, which also
	accounts for the current turret yaw and gun pitch.

	`covered` lists extra names the real collision model already produced for
	this shot. Their layout drops any entity it finds in the per-vehicle
	material table, but surveyingDevice is only in the GLOBAL table, so without
	this the optics would be scored twice - once from geometry, once from the
	profile.'''
	layout = _offh_internal_layout(td)
	if not layout:
		return None
	targets = layout.get('targets') or ()
	if not targets:
		return None
	import Math
	from gui.mods.offhangar import internal_geometry as _IG
	inv = Math.Matrix(target_mock.matrix)
	inv.invert()
	_vs = inv.applyPoint(Math.Vector3(start_pos.x, start_pos.y, start_pos.z))
	_ve = inv.applyPoint(Math.Vector3(end_pos.x, end_pos.y, end_pos.z))
	local = {}
	for compDescr, compMatrix in target_mock.getComponents():
		name = None
		for candidate in ('chassis', 'hull', 'turret', 'gun'):
			if compDescr is getattr(td, candidate, None):
				name = candidate
				break
		if name is None:
			continue
		_ls = compMatrix.applyPoint(_vs)
		_le = compMatrix.applyPoint(_ve)
		local[name] = ((_ls.x, _ls.y, _ls.z), (_le.x, _le.y, _le.z))
	hits = []
	for target in targets:
		seg = local.get(target.get('parent'))
		if seg is None:
			continue
		entity = target.get('entity')
		if not entity:
			continue
		name = str(entity) + 'Health'
		if name in covered:
			continue
		interval = _IG.target_interval(seg[0], seg[1], target)
		if interval is None:
			continue
		hits.append((float(interval[0]), name))
	hits.sort()
	# ONE roll per device, not per box. The profiles model a module as several
	# boxes - an ammo rack is typically three (hull floor left, hull floor right,
	# turret ready rack) - and a shell through the fighting compartment crosses
	# two of them. Scoring both would give that module twice the saving throw WG
	# gives it. The log showed exactly that: 'ammoBayHealth@0.04,
	# ammoBayHealth@0.04' from a single strike. Keep the nearest box per device.
	seen = set()
	unique = []
	for dist, name in hits:
		if name in seen:
			continue
		seen.add(name)
		unique.append((dist, name))
	return unique


# Bumped on every change that ships. Logged once per battle as
#   'OfflineBattle BUILD <stamp>'
# so a log can be checked against the build that produced it instead of
# assuming the client picked the new .pyc up.
_OFFH_BUILD = '1.7.6 (2026-07-29) marker-follows-spotting'


def _offh_hit_sound(path, min_gap=0.10):
	'''Play a hit sound on ONE shared carrier model.
	
	The two bot-shoots-player sites used to build a fresh fake model per impact,
	add it to the world, hang a sound on it and hold both for 3 s. Under fire from
	several bots that is an unbounded rate of new models and events, all parked at
	the camera - which is why the trouble showed up around the tank and only when
	a lot was going on at once.
	
	EVENT PATHS: the group in hits.fev is 'hits_n_impacts', NOT 'hits'. There is
	no group called 'hits' in that project at all, so '/hits/hits/<event>' can only
	ever answer None - which is exactly what it did, 35 times in one session, for
	both of the sounds a bot's shell makes when it strikes the player. The mistake
	is inherited: the client's own VehicleAppearance asks for '/hits/hits/hit_treads'
	and is just as silent about it. Verify a path against res/audio/<project>.fev
	before adding one.

	One carrier is enough: it only positions the sound. Repeats of the SAME sound
	are rate-limited the way IngameSoundNotifications does it with
	minTimeBetweenEvents - several hits in one frame are one bang, not five.'''
	try:
		import BigWorld
		_now = BigWorld.time()
		_last = globals().setdefault('g_offh_hit_snd_t', {})
		if _now - (_last.get(path, 0.0) or 0.0) < min_gap:
			return
		_last[path] = _now
		_fm = globals().get('g_offh_hit_carrier')
		if _fm is None or not getattr(_fm, 'inWorld', False):
			_fm = BigWorld.player().newFakeModel()
			BigWorld.addModel(_fm)
			globals()['g_offh_hit_carrier'] = _fm
		_fm.position = BigWorld.camera().position
		_snd = _fm.getSound(path)
		if _snd:
			_snd.play()
		else:
			# Not a swallowed nothing: getSound answers None when FMOD cannot hand
			# out the event - a full pool in a busy battle is the documented case
			# (see the bot engine-sound culling). Retail's VehicleAppearance._getSound
			# logs the same failure. Without this the impact just went quiet with no
			# trace, which is exactly the 'sometimes missing' report.
			LOG_DEBUG('SOUND UNAVAILABLE (hit):', path)
	except Exception as _hse:
		LOG_DEBUG('hit sound err:', str(_hse))


def _offh_clamp_to_arena(pt):
	'''Pull an aim point back inside the arena bounding box - the red border.
	
	The strategic camera can be scrolled well past the edge of the map, and the
	strategic aim point followed it. A point out there is beyond any ballistic
	solution, and wg_getShotAngles answers an unreachable point with the maximum
	elevation angle - so the barrel swung up and the gun sat pointing at the sky.
	ArenaType.boundingBox is ((minX, minZ), (maxX, maxZ)).'''
	try:
		import BigWorld, Math
		_bb = BigWorld.player().arena.arenaType.boundingBox
		_x0, _z0 = float(_bb[0][0]), float(_bb[0][1])
		_x1, _z1 = float(_bb[1][0]), float(_bb[1][1])
	except Exception:
		return pt
	try:
		_x = pt.x
		_z = pt.z
		if _x < _x0: _x = _x0
		elif _x > _x1: _x = _x1
		if _z < _z0: _z = _z0
		elif _z > _z1: _z = _z1
		if _x == pt.x and _z == pt.z:
			return pt
		# Follow the terrain at the clamped spot, or the point would keep the height
		# it had off-map and the gun would aim at thin air just inside the border.
		_y = pt.y
		try:
			_c = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_x, 1000.0, _z), Math.Vector3(_x, -250.0, _z), 128)
			if _c is not None:
				_y = _c[0].y
		except Exception:
			pass
		return Math.Vector3(_x, _y, _z)
	except Exception:
		return pt


def _offh_water_depth(x, y, z):
	'''Metres of water standing above the hull origin at (x, y, z); -1 when dry.
	
	ONE probe for the player and for every bot. The two used to carry separate
	copies of this call with their own state machines around it, which is exactly
	how their drowning behaviour drifted apart. Ray from 20 m above to 5 m below,
	the same window Avatar.updateVehicleDestroyTimer works in.'''
	try:
		import BigWorld, Math
		_w = BigWorld.wg_collideWater(Math.Vector3(x, y + 20.0, z),
		                              Math.Vector3(x, y - 5.0, z), False)
	except Exception:
		return -1.0
	if _w is None or _w < 0.0:
		return -1.0
	return 20.0 - _w


def _offh_hp_display(mock):
	'''HP to SHOW for a mock, which is not always its .health.
	
	Drowning is not damage: the crew drowns, the hull is untouched, so a drowned
	tank keeps the HP it had when it went under. Its internal health still goes to
	0 because isAlive, the team-wipe check, the repair gate and the wreck swap all
	key off that - only the DISPLAY differs, and every panel push has to read this
	rather than .health or the per-frame spectator push resets the bar to 0.'''
	_d = getattr(mock, '_hp_display', None)
	if _d is not None:
		return max(0, int(_d))
	return max(0, int(getattr(mock, 'health', 0) or 0))


_OFFH_DEATH_DEVICES = ('engineHealth', 'ammoBayHealth', 'fuelTankHealth', 'radioHealth',
                       'gunHealth', 'turretRotatorHealth', 'surveyingDeviceHealth',
                       'leftTrackHealth', 'rightTrackHealth')


def _offh_push_edge_colors():
	'''Push the silhouette palette to the engine.

	BigWorld keeps ONE edge-detect triple, (self, enemy, friend), and it is set by
	EdgeDetectColorController.updateColors() - which retail calls from
	Avatar.onEnterWorld. Offline the player is the ACCOUNT, so that never runs and
	the engine keeps whatever table it had: enemies drew, allies did not. Cheap and
	idempotent, so it is called both at battle start and at every outline change
	rather than being trusted to survive a space clear.'''
	try:
		import BigWorld
		try:
			from helpers import EdgeDetectColorController as _EDC
			if getattr(_EDC, 'g_instance', None) is not None:
				_EDC.g_instance.updateColors()
				return 'controller'
		except Exception:
			pass
		import Math as _Medc
		BigWorld.wgSetEdgeDetectColors((_Medc.Vector4(0.2, 0.2, 0.2, 0.5),
			_Medc.Vector4(1.0, 0.0, 0.0, 0.5), _Medc.Vector4(0.0, 1.0, 0.0, 0.5)))
		return 'defaults'
	except Exception:
		return None


def _offh_repaint_damage_panel(mock):
	'''Push a vehicle's WHOLE module + crew state onto the damage panel.

	The panel is a single widget re-pointed at whatever tank is being shown, and
	retail's onPostmortemVehicleChanged -> switchToVehicle wipes it back to
	all-normal for the new one. Offline nothing then repaints it, so cycling the
	spectator to an ally and back left your own broken modules showing as intact -
	"when switching from an ally to yourself, the icons of broken modules
	disappear". Module level on purpose: the spectator tick cannot see the battle
	closure's per-device helpers.'''
	try:
		import BigWorld
		from gui import WindowsManager as _wmrp
		_bw = getattr(_wmrp.g_windowsManager, 'battleWindow', None)
		_dp = getattr(_bw, 'damagePanel', None) if _bw is not None else None
		if _dp is None or mock is None:
			return
		_td = getattr(mock, 'typeDescriptor', None)
		_destroyed = getattr(mock, '_destroyed_devices', None) or set()
		_hp = getattr(mock, 'devices_hp', None) or {}
		try:
			from gui.mods.offhangar import device_damage as _DDrp
		except Exception:
			_DDrp = None
		# chassis shares the two track pools, so it reads destroyed when either is.
		_groups = [('chassis', ('leftTrackHealth', 'rightTrackHealth'))]
		_groups += [(_module_ui_name(_h), (_h,)) for _h in _OFFH_DEATH_DEVICES]
		for _ui, _healths in _groups:
			_state = 'normal'
			for _h in _healths:
				if _h in _destroyed:
					_state = 'destroyed'
					break
				if _h in _hp and _DDrp is not None:
					try:
						_mx = _DDrp.device_max_hp(_td, _h)
					except Exception:
						_mx = None
					if _mx is not None and _hp[_h] < _mx:
						_state = 'critical'
			try: _dp.updateState(_ui, _state)
			except Exception: pass
		_ko = getattr(mock, '_crew_ko', None) or set()
		try:
			_roster = _crew_roster(_device_td(mock))
		except Exception:
			_roster = list(_ko)
		for _c in _roster:
			try: _dp.updateState(_c, 'destroyed' if _c in _ko else 'normal')
			except Exception: pass
		try: _dp.onFireInVehicle(bool(getattr(mock, 'is_on_fire', False)))
		except Exception: pass
	except Exception:
		pass


def _offh_knock_out_everything(mock, is_player):
	'''A destroyed tank has everything destroyed: every module at 0 HP and every
	crewman down. Used for drowning AND for an ordinary kill - previously only
	drowning called it, so a normal death left most module icons untouched.'''
	try:
		if getattr(mock, 'devices_hp', None) is None:
			mock.devices_hp = {}
		for _n in _OFFH_DEATH_DEVICES:
			mock.devices_hp[_n] = 0
		# The repair tick and the module GUI read the destroyed-SET, not raw HP.
		_ds = getattr(mock, '_destroyed_devices', None)
		if _ds is None:
			_ds = set()
			mock._destroyed_devices = _ds
		for _n in _OFFH_DEATH_DEVICES:
			_ds.add(_n)
	except Exception:
		pass
	# Crew: use the tank's REAL roster ('gunner1', 'loader1', ...). The old generic
	# role names never matched the panel entries, so crew never turned red.
	_roster = []
	try:
		_roster = _crew_roster(_device_td(mock))
		_ko = getattr(mock, '_crew_ko', None)
		if _ko is None:
			_ko = set()
			mock._crew_ko = _ko
		for _c in _roster:
			_ko.add(_c)
		_recompute_crew_impaired(mock)
	except Exception:
		pass
	try:
		_refresh_mobility_flags(mock)
	except Exception:
		pass
	LOG_DEBUG('KNOCKOUT called: is_player=%s devices=%d crew=%d' % (is_player, len(_OFFH_DEATH_DEVICES), len(_roster)))
	if not is_player:
		return
	try:
		import BigWorld
		from gui import WindowsManager as _wmko
		_p = BigWorld.player()
		_bw = getattr(_wmko.g_windowsManager, 'battleWindow', None)
		_dp = getattr(_bw, 'damagePanel', None) if _bw is not None else None
		_ui = [_module_ui_name(_n) for _n in _OFFH_DEATH_DEVICES] + list(_roster)
		LOG_DEBUG('KNOCKOUT: is_player=%s panel=%s names=%s' % (is_player, _dp is not None, _ui))
		for _n in _ui:
			try: _p.guiSessionProvider.invalidateVehicleState(2, _p.playerVehicleID, _n, 'destroyed')
			except Exception: pass
			if _dp is not None:
				try: _dp.updateState(_n, 'destroyed')
				except Exception: pass
		# Retail greys the panel and deactivates the crew from DamagePanel._updateOther
		# the moment the vehicle reads dead. That tick needs a real BigWorld entity, so
		# offline it never runs and the crew icons stayed lit on a destroyed tank.
		if _dp is not None:
			try: _dp.onVehicleDestroyed()
			except Exception: pass
			try: _dp.onCrewDeactivated()
			except Exception: pass
		# onVehicleDestroyed greys the WHOLE panel out, wiping the red module icons we
		# just set, and it can also fire again later. Push them again on the next
		# frames so the destroyed state is what stays visible.
		def _reassert(_names=list(_ui), _panel=_dp, _hp=_offh_hp_display(mock)):
			if _panel is None:
				return
			for _m in _names:
				try: _panel.updateState(_m, 'destroyed')
				except Exception: pass
			# A drowned tank keeps its last HP; anything else is already 0 here.
			try: _panel.updateHealth(_hp)
			except Exception: pass
		try:
			import BigWorld as _bwr
			# Spread over the whole post-mortem: WG's DamagePanel._updateSelf ticks every
			# 30 ms and calls onVehicleDestroyed() the moment the vehicle reads as dead,
			# which greys the panel. Re-push past that point so the red module icons are
			# what remains on screen.
			_bwr.callback(0.1, _reassert)
			_bwr.callback(0.5, _reassert)
			_bwr.callback(1.5, _reassert)
			_bwr.callback(3.0, _reassert)
		except Exception:
			pass
	except Exception:
		pass


def _offh_player_add_model(m):
	'''player.addModel for the offline player. The offline player is the ACCOUNT
	entity, not an Avatar: Entity.addModel parents the model to THAT entity's
	transform/chunk, which sits wherever the account happens to be - not in the
	battle world - so anything routed through it (the ProjectileMover's shell
	tracers being the only user) was built, moved and lit correctly but never
	drawn. Use the global model API instead, exactly like the effects that DO
	work offline (StaticSceneBoundEffects.addNew: addModel + addAlwaysUpdateModel).
	addAlwaysUpdateModel matters for a shell: it crosses several chunks in a few
	frames, and without it the model (and the tracer pixie hanging off its
	'Scene Root' node) is only ticked while its spawn chunk is being drawn.'''
	import BigWorld
	_add_model(m)
	# Always-update ONLY for our own shells. This function is served to every
	# caller of player.addModel through Account.__getattribute__, and pinning all
	# of them (flock, mapactivities, camera and control-mode models) meant a
	# growing set of permanently animated models that are never released - the
	# client died natively during the first battle load. The flag is set around
	# our ProjectileMover.add() calls.
	if not globals().get('g_offh_adding_projectile'):
		return
	try:
		BigWorld.addAlwaysUpdateModel(m)
		# Tracked so the sweep can unregister it: a shell still in flight at battle
		# end is force-deleted without passing through _offh_player_del_model, and a
		# registration left on a deleted model crashes the next space load.
		globals().setdefault('g_offh_always_update_models', []).append(m)
	except Exception:
		pass


def _offh_player_del_model(m):
	'''Symmetric teardown. delAlwaysUpdateModel lives HERE and not in
	_offh_del_model: only projectile models are ever registered for always-update,
	and a failing BigWorld call can leave its C error PENDING - putting one in the
	shared teardown path risks that error surfacing inside an unrelated caller's
	cleanup loop. Same 1-item-loop absorber so it cannot escape this function.'''
	import BigWorld
	try:
		BigWorld.delAlwaysUpdateModel(m)
		for _ in [0]:
			pass
	except:
		pass
	try:
		_aul = globals().get('g_offh_always_update_models')
		if _aul:
			for _i in range(len(_aul) - 1, -1, -1):
				if _aul[_i] is m:
					del _aul[_i]
					break
	except Exception:
		pass
	_offh_del_model(m)


# ---- HE, 0.8.2 ----------------------------------------------------------
# The damage calculator that decides this online lives in the CELL scripts and
# is not shipped with the client, so unlike the penetration model below (which
# comes straight out of items/vehicles.py and physics_shared.py) the blast
# formula is WG's published model of the era, not a decompile:
#
#   damage = nominal * SPLASH_FRACTION * (1 - dist/explosionRadius)
#            - ARMOR_FACTOR * nominal_armour
#
# Both constants are overridable from config.json "physics_tuning"-style under
# "he_tuning", so the feel can be corrected without a recompile.
_OFFH_HE_SPLASH_FRACTION = 0.5
_OFFH_HE_ARMOR_FACTOR = 1.1


def _offh_is_he(shot):
	'''True for a high-explosive round. Reads shell['kind'] - never the name:
	every HEAT shell contains the letters 'HE' too, which is exactly the bug the
	shared penetration model was written to kill.'''
	shell = (shot.get('shell') or {}) if hasattr(shot, 'get') else {}
	return shell.get('kind') == 'HIGH_EXPLOSIVE'


def _offh_he_radius(shot):
	'''explosionRadius of this shot's shell, in metres. items/vehicles.py falls
	back to caliber^2 / 5555 when the shell XML omits it - mirror that rather
	than inventing a number.'''
	shell = (shot.get('shell') or {}) if hasattr(shot, 'get') else {}
	try:
		r = float(shell.get('explosionRadius', 0.0) or 0.0)
	except Exception:
		r = 0.0
	if r > 0.0:
		return r
	try:
		cal = float(shell.get('caliber', 0) or 0)
	except Exception:
		cal = 0.0
	return (cal * cal / 5555.0) if cal > 0.0 else 0.0


def _offh_he_hull_armor(td):
	'''Thinnest STRUCTURAL plate the hull carries, from the descriptor.
	
	Used when the blast ray finds no plate at all. Returning 0 there let the
	blast through untouched; the thinnest plate is the attacker-friendly but
	still bounded assumption - blast looks for the weak facing.'''
	best = None
	try:
		mats = (getattr(td, 'hull', None) or {}).get('materials') or {}
		for m in mats.values():
			if getattr(m, 'vehicleDamageFactor', 1.0) == 0.0:
				continue
			a = float(getattr(m, 'armor', 0.0) or 0.0)
			if a <= 0.0:
				continue
			if best is None or a < best:
				best = a
	except Exception:
		return 0.0
	return best or 0.0


def _offh_he_nominal_armor(all_hits, td=None):
	'''Nominal thickness of the first STRUCTURAL plate on the ray.
	
	The HE reduction uses the plate's NOMINAL thickness, not the angled effective
	value: a sloped plate does not shrug off blast the way it deflects a solid
	shot. Spaced plates (vehicleDamageFactor 0 - tracks, external gear) are
	skipped; HE bursts on them and what has to hold is the hull behind.'''
	best = None
	for _h in (all_hits or []):
		try:
			_d, _mat = _h[0], _h[2]
		except Exception:
			continue
		if _mat is None or getattr(_mat, 'vehicleDamageFactor', 1.0) == 0.0:
			continue
		_a = float(getattr(_mat, 'armor', 0.0) or 0.0)
		if _a <= 0.0:
			continue
		if best is None or _d < best[0]:
			best = (_d, _a)
	if best is not None:
		return best[1]
	# No plate on the ray. Zero would hand the blast a free pass, so fall back to
	# the hull's thinnest structural plate when the descriptor is available.
	return _offh_he_hull_armor(td) if td is not None else 0.0


def _offh_he_damage(base_damage, armor_nominal, dist_frac=0.0):
	'''Damage an HE burst does to a hull it did NOT get through.
	
	dist_frac is 0.0 for the vehicle actually struck and rises to 1.0 at the edge
	of explosionRadius for everything else caught in the blast. Returns 0 when the
	plate eats the whole thing - the normal outcome against heavy armour, and the
	reason a derp gun rewards shooting thin plate.'''
	d = (float(base_damage) * _OFFH_HE_SPLASH_FRACTION * (1.0 - float(dist_frac))
	     - _OFFH_HE_ARMOR_FACTOR * float(armor_nominal or 0.0))
	return int(d) if d > 0.0 else 0


def _offh_he_apply_tuning(overrides):
	'''Overlay config.json "he_tuning" onto the two blast constants.'''
	g = globals()
	applied = []
	if isinstance(overrides, dict):
		for k, gname in (('splash_fraction', '_OFFH_HE_SPLASH_FRACTION'),
		                 ('armor_factor', '_OFFH_HE_ARMOR_FACTOR')):
			if k in overrides:
				try:
					g[gname] = float(overrides[k])
					applied.append('%s=%s' % (k, overrides[k]))
				except (TypeError, ValueError):
					pass
	return applied


def _offh_penetration(shot, dist_m, armor, hit_angle_cos, pierce_loss=0.0):
	'''Armour test shared by the player and by bot-vs-bot fire.

	Returns (result, eff_armor, pierce): 0 ricochet, 1 no penetration, 2 penetration.

	Fixes two faults of the old inline version:
	  * it classified shells with `'HE' not in shell['name']`, a substring test on the
	    NAME. Every HEAT round contains 'HE', so both the ricochet and the
	    no-penetration branch were skipped for it and it always went through.
	    items/vehicles.py stores a proper shell['kind'] - use that.
	  * piercingPower is a Vector2 (value at 100 m, value at maxDistance) and it only
	    ever read [0], so nothing lost penetration with range.
	Randomisation is WG's own g_cache.commonConfig piercingPowerRandomization = 0.25.
	'''
	import math, random
	shell = (shot.get('shell') or {}) if hasattr(shot, 'get') else {}
	kind = shell.get('kind', 'ARMOR_PIERCING')
	# ARMOR_PIERCING_HE (AP with HE filler) belongs in the AP family: same
	# normalisation and the same 70 deg ricochet rule. It was missing, so it fell
	# through to the HEAT branch - no normalisation, no ricochet, no overmatch.
	# 0.8.2 ships five kinds (vehicles.py _shellKinds): HOLLOW_CHARGE,
	# HIGH_EXPLOSIVE, ARMOR_PIERCING, ARMOR_PIERCING_HE, ARMOR_PIERCING_CR.
	is_ap = kind in ('ARMOR_PIERCING', 'ARMOR_PIERCING_HE', 'ARMOR_PIERCING_CR')
	pp = shot.get('piercingPower', (100.0, 100.0))
	try:
		p100 = float(pp[0]); pfar = float(pp[1])
	except Exception:
		p100 = pfar = 100.0
	maxd = 0.0
	try: maxd = float(shot.get('maxDistance', 0.0) or 0.0)
	except Exception: maxd = 0.0
	if maxd <= 100.0: maxd = 400.0
	if dist_m <= 100.0:
		pierce = p100
	else:
		_t = (min(dist_m, maxd) - 100.0) / (maxd - 100.0)
		pierce = p100 + (pfar - p100) * _t
	pierce *= random.uniform(0.75, 1.25)
	# spaced armour already crossed (tracks, external devices) is subtracted here
	pierce -= float(pierce_loss or 0.0)
	if pierce < 0.0:
		pierce = 0.0
	armor = float(armor or 0.0)
	if armor <= 0.0:
		return (2, 0.0, pierce)
	_ac = abs(float(hit_angle_cos))
	if _ac > 1.0: _ac = 1.0
	if _ac < 0.0001: _ac = 0.0001
	ang = math.acos(_ac)                      # 0 = square on the plate
	caliber = float(shell.get('caliber', 100) or 100)
	# shell normalisation pulls the impact towards the normal: AP 5 deg, APCR 2 deg,
	# HEAT/HE none. A calibre over three times the plate overmatches it: normalisation
	# grows and the round can no longer ricochet.
	norm = math.radians(2.0) if kind == 'ARMOR_PIERCING_CR' else (math.radians(5.0) if is_ap else 0.0)
	overmatch = is_ap and caliber > armor * 3.0
	if overmatch:
		norm *= 1.4 * caliber / (armor * 3.0)
	elif is_ap and ang > math.radians(70.0):
		return (0, armor / max(0.087, _ac), pierce)
	ang_eff = ang - norm
	if ang_eff < 0.0: ang_eff = 0.0
	eff = armor / max(0.0001, math.cos(ang_eff))
	if kind == 'HIGH_EXPLOSIVE':
		# HE penetrates or it does not, like everything else - it just gets no
		# normalisation and cannot ricochet (both already handled above). This used
		# to be an unconditional 2, so every HE round dealt FULL damage through any
		# thickness. A non-penetration here is not a miss: the caller runs
		# _offh_he_damage() for the blast.
		return (2 if pierce >= eff else 1, eff, pierce)
	return (2 if pierce >= eff else 1, eff, pierce)


def _offh_del_model(m):
	# BigWorld.delModel can set its C error as PENDING while returning normally;
	# this client only RAISES it at the next list-iteration's exhaustion (the
	# FOR_ITER opcode checks PyErr and finds it). len('') does NOT trip it. So
	# absorb it HERE with our own 1-item loop: the loop's exhaustion FOR_ITER
	# raises the pending error INSIDE this try, where the bare except eats it -
	# it can never surface in the CALLER's cleanup loop (which logged
	# "CRITICAL ERROR IN K KEY: ... Not added as a global model" and skipped
	# the list clear, leaking the battle's models into the next battle).
	import BigWorld
	# Drop it from the sweep list FIRST so add/delete stay symmetric. _add_model
	# records EVERY model, including the ProjectileMover's shell models (they go
	# through the patched player.addModel). Once the projectile chain deletes one
	# mid-battle, a copy left in g_offline_models made the end-of-battle sweep
	# delete it a SECOND time -> dangling native model -> the next arena tripped
	# `MF_ASSERT_DEV FAILED: pSpace_` (chunk_embodiment.cpp:320) and the client died
	# on the second battle. Identity compare: BigWorld.Model equality is not reliable.
	try:
		_gm = g_offline_models
		for _i in range(len(_gm) - 1, -1, -1):
			if _gm[_i] is m:
				del _gm[_i]
				break
	except Exception:
		pass
	try:
		BigWorld.delModel(m)
		for _ in [0]:
			pass
	except:
		pass


def _offh_proc_mem_mb():
	# DEBUG-ONLY memory probe (returns immediately unless debug_logging is on,
	# so it NEVER runs in the live/share build). ctypes is not functional in
	# this client (_ctypes.pyd missing) and tasklist gives only the working
	# set; wmic also exposes VirtualSize - the 32-bit ADDRESS-SPACE figure that
	# hits the ~2 GB fragmentation wall and OOM-crashes map/model loads (that
	# is the real limit; working set/RSS understates it). Returns
	# (rss_mb, virtual_mb, commit_mb) or (-1, -1, -1).
	try:
		from gui.mods.offhangar.logging import _DBG as _mp_dbg
		if not _mp_dbg[0]:
			return (-1, -1, -1)
	except Exception:
		return (-1, -1, -1)
	try:
		import os
		_pid = os.getpid()
		# /value -> one KEY=VALUE per line. VirtualSize/WorkingSetSize are bytes,
		# PageFileUsage (commit/private) is KB.
		_out = os.popen('wmic process where ProcessId=%d get VirtualSize,PageFileUsage,WorkingSetSize /value' % _pid).read()
		_v = {}
		for _ln in _out.splitlines():
			if '=' in _ln:
				_k, _, _val = _ln.partition('=')
				_val = _val.strip()
				if _val.isdigit():
					_v[_k.strip()] = int(_val)
		if _v:
			return (_v.get('WorkingSetSize', 0) // (1024 * 1024),
			        _v.get('VirtualSize', 0) // (1024 * 1024),
			        _v.get('PageFileUsage', 0) // 1024)
	except Exception:
		pass
	return (-1, -1, -1)


def _offh_gc_census_line(tag):
	"""DEBUG-ONLY per-battle object census. Forces a gc.collect first (so we
	count only SURVIVING = truly-retained objects, i.e. the leak), then walks
	gc.get_objects() and logs the top types by count PLUS the top GROWERS vs
	the previous battle's census. Also logs bw_entities + gc.garbage so we can
	tell a Python leak (total/grower type climbs) from a C++ residual
	(python total flat but commit still climbs = map textures / appearances,
	NOT freeable from Python). Returns immediately unless debug_logging is on,
	so it never runs in the live/share build."""
	try:
		from gui.mods.offhangar.logging import _DBG as _c_dbg, LOG_DEBUG as _c_log
		if not _c_dbg[0]:
			return
	except Exception:
		return
	try:
		import gc as _gc
		try:
			_gc.collect()
			_gc.collect(2)
		except Exception:
			pass
		_objs = _gc.get_objects()
		_total = len(_objs)
		_counts = {}
		for _o in _objs:
			try:
				_tn = type(_o).__name__
			except Exception:
				_tn = '?'
			_counts[_tn] = _counts.get(_tn, 0) + 1
		_objs = None
		# per-tag prev so quit->quit and start->start diff cleanly (interleaved
		# quit/start sweeps would otherwise make the grower delta meaningless).
		_prevkey = '_g_offh_prev_census_%s' % tag
		_prev = globals().get(_prevkey) or {}
		_top = sorted(_counts.items(), key=lambda _kv: _kv[1], reverse=True)[:12]
		_grow = []
		if _prev:
			for _k, _v in _counts.items():
				_d = _v - _prev.get(_k, 0)
				if _d:
					_grow.append((_k, _d))
			_grow.sort(key=lambda _kv: _kv[1], reverse=True)
			_grow = _grow[:12]
		globals()[_prevkey] = _counts
		try:
			_ng = len(_gc.garbage)
		except Exception:
			_ng = -1
		_ent = -1
		try:
			import BigWorld as _bw
			_ent = len(getattr(_bw, 'entities', []) or [])
		except Exception:
			pass
		_c_log('OfflineBattle.sweep(%s) census: total=%d garbage=%d bw_entities=%d | top=%s' % (tag, _total, _ng, _ent, _top))
		if _grow:
			_c_log('OfflineBattle.sweep(%s) census GROWERS(vs prev battle): %s' % (tag, _grow))
	except Exception:
		pass


def _offh_bspace():
	"""The space the battle MAP is in and the camera renders. In dedicated
	(full_space_release) mode this is a FRESH space, different from the
	read-only player.spaceID; in reuse mode it equals player.spaceID. Every
	battle collision / physics / destructible query must use THIS, else it
	hits the wrong (empty) space (tank falls through / no terrain)."""
	try:
		_s = globals().get('g_offh_battle_space', 0) or 0
		if _s:
			return _s
	except Exception:
		pass
	try:
		import BigWorld
		return BigWorld.player().spaceID
	except Exception:
		return 0


class _OffhRouteProbe(object):
	'''World queries bot_routes needs, against the BATTLE space.

	bot_routes is deliberately BigWorld-free (it self-tests on the desktop), so
	every terrain question arrives through here.'''

	def ground(self, x, z):
		try:
			import BigWorld, Math
			hit = BigWorld.wg_collideSegment(_offh_bspace(),
				Math.Vector3(x, 1000.0, z), Math.Vector3(x, -1000.0, z), 128)
			return hit[0].y if hit is not None else None
		except Exception:
			return None

	def water_depth(self, x, y, z):
		'''Standing water over the ground at (x, z), or None when unknown.

		The ground probe cannot see water at all - it returns the LAKEBED - so
		without this a node in the middle of a pond validates perfectly and bots
		get sent to drive into it. Shares the mod's single water probe.'''
		try:
			d = _offh_water_depth(x, y, z)
			return None if d is None or d < 0.0 else d
		except Exception:
			return None

	def clear_dist(self, x, y, z, dx, dz, maxd):
		try:
			import BigWorld, Math, math
			hit = BigWorld.wg_collideSegment(_offh_bspace(),
				Math.Vector3(x, y, z),
				Math.Vector3(x + dx * maxd, y, z + dz * maxd), 128)
			if hit is None:
				return maxd
			return min(maxd, math.sqrt((hit[0].x - x) ** 2 + (hit[0].z - z) ** 2))
		except Exception:
			return maxd


def _offh_route_tick():
	'''Load and incrementally validate this map's baked destinations.

	Called once per frame from the bot block. Loading is attempted exactly once
	per battle; validation is sliced so the ~1700 probes never land in a single
	frame. Everything is best-effort: any failure leaves g_offh_routemap None
	and the bots behave exactly as they did before this module existed.'''
	rm = globals().get('g_offh_routemap')
	if rm is not None:
		# Keep stepping until every node has settled, NOT just until the first
		# pass finishes. Terrain chunks stream in by distance, so the far half of
		# the map is not probeable during the opening seconds - the first build
		# of this latched one early pass and logged 0/134 usable on a map that
		# was entirely fine. Once settled this is a no-op.
		if rm.settled():
			return
		try:
			rm.step(_OffhRouteProbe())
			n_live = len(rm.live)
			# Explicit None test, NOT `... or -1`: the stored count is 0 for the
			# whole opening phase and `0 or -1` is -1, so the guard never matched
			# and this logged every single frame while the terrain warmed up.
			_last_live = globals().get('g_offh_routes_last_live')
			if _last_live is None or n_live != _last_live:
				globals()['g_offh_routes_last_live'] = n_live
				_nprobed = len([1 for _b in rm.probed if _b])
				LOG_DEBUG('ROUTES %s: %d/%d usable (%.0f%%) - %d probed, %d provisional; pass=%d rejects=%s'
					% (rm.map_name, n_live, len(rm.pos), rm.pass_rate() * 100.0,
					   _nprobed, n_live - _nprobed, rm.passes, str(rm.reasons)))
			if rm.settled() and not rm.usable():
				LOG_DEBUG('ROUTES: too few nodes survived - falling back to chase-the-nearest-enemy')
		except Exception as e:
			LOG_DEBUG('ROUTES step error:', str(e))
			globals()['g_offh_routemap'] = None
		return
	if globals().get('g_offh_routes_tried'):
		return
	globals()['g_offh_routes_tried'] = True
	try:
		import BigWorld
		from _constants import CONFIG_OPTIONS as _CFG
		if not bool(_CFG.get('bot_routes', True)):
			LOG_DEBUG('ROUTES: disabled by config')
			return
		from gui.mods.offhangar import bot_routes as _BR
		from gui.mods.offhangar import paths as _P
		map_name = getattr(BigWorld.player().arena.arenaType, 'geometryName', '') or ''
		graph = _BR.load_graph(map_name, _P.mod_dir())
		if graph is None:
			LOG_DEBUG('ROUTES: no baked routes for', map_name, '- default bot behaviour')
			return
		globals()['g_offh_routemap'] = _BR.RouteMap(graph, globals().get('g_offline_bounds'))
		LOG_DEBUG('ROUTES: loaded %d baked nodes for %s' % (len(graph['nodes']['pos']), map_name))
	except Exception as e:
		LOG_DEBUG('ROUTES load error:', str(e))


def _offh_battle_message(text, colour='FFCC33'):
	"""Put a line in the battle chat.

	Measuring a map is silent apart from python.log, so a user who has just
	painted a map has no way to tell whether anything is happening - which is
	exactly how a working bake got reported as "generation didn't start".

	`MessengerBattleInterface.addFormattedMessage` takes arbitrary HTML-ish text,
	unlike `vErrorsPanel.showMessage`, which only resolves localisation KEYS.
	Every step is guarded: a missing messenger must never break a battle over a
	status line.
	"""
	try:
		from messenger.gui import MessengerDispatcher as _MD
		_bm = getattr(_MD.g_instance, 'battleMessenger', None)
		if _bm is None:
			return False
		_bm.addFormattedMessage(
			"<font color='#%s'>%s</font>" % (colour, text), False)
		return True
	except Exception as e:
		LOG_DEBUG('battle message failed (%s): %s' % (str(e), text))
		return False


def _offh_nav_note(text, colour='FFCC33'):
	"""Say it in chat if we can, and always say it in the log.

	Chat is not available for the first frames of a battle, so anything said too
	early is simply lost - queued lines are retried by _offh_nav_tick.
	"""
	LOG_DEBUG('NAVMSG: %s' % text)
	if not _offh_battle_message(text, colour):
		globals().setdefault('g_offh_msg_queue', []).append((text, colour))


def _offh_flush_notes():
	"""Retry anything said before the battle chat existed."""
	q = globals().get('g_offh_msg_queue')
	if not q:
		return
	while q:
		text, colour = q[0]
		if not _offh_battle_message(text, colour):
			return
		q.pop(0)


def _offh_nav_tick():
	'''Build this map's navigation grid, then dump it to disk.

	PHASE 1: diagnostics only - nothing steers off this yet. The point is to
	prove the probe budget, the coverage and the passability read correctly on
	a real map before any bot depends on it.

	The dump is the whole economy of this feature: launching the game is the
	expensive step, so with a grid on disk every later question (path quality,
	A* cost, tuning) is answered on the desktop with no client at all.
	Reuses _OffhRouteProbe, which already answers both ground and water.'''
	# One A* per frame across all bots; this tick runs once per frame, so it is
	# where the budget resets.
	globals()['g_offh_astar_used'] = False
	_bt = globals().get('g_offh_bot_ticks', 0) or 0
	if _bt and _bt - (globals().get('g_offh_stat_at', 0) or 0) >= 20000:
		globals()['g_offh_stat_at'] = _bt
		_st = globals().get('g_offh_stuck_ticks', 0) or 0
		LOG_DEBUG('NAVPATH stats: stuck %.1f%% of %d bot-ticks | paths ok=%d fail=%d | '
			'wet-reverses blocked=%d | shots held for no line=%d'
			% (100.0 * _st / _bt, _bt, globals().get('g_offh_astar_ok', 0) or 0,
			   globals().get('g_offh_astar_fail', 0) or 0,
			   globals().get('g_offh_escape_wet', 0) or 0,
			   globals().get('g_offh_los_blocked', 0) or 0))
	g = globals().get('g_offh_navgrid')
	if g is not None:
		# NOT an early return on settled(): a grid LOADED from a dump arrives
		# already settled, and the paint/reach one-shots below still have to run
		# for it. They are each guarded by their own flag, so once all three have
		# fired this whole block is a handful of dict lookups per frame.
		if (g.settled() and globals().get('g_offh_nav_dumped')
				and globals().get('g_offh_nav_painted')):
			return
		try:
			from gui.mods.offhangar import nav_grid as _NG
			_offh_flush_notes()
			if not g.settled():
				g.step(_OffhRouteProbe())
				_cov = g.coverage()
				# Quarter marks only. Per-frame or per-10% would bury the battle
				# chat; the point is one legible progress trail.
				if globals().get('g_offh_nav_announced'):
					_q = int(_cov * 4)
					if _q > (globals().get('g_offh_nav_q', 0) or 0) and _q < 4:
						globals()['g_offh_nav_q'] = _q
						_offh_nav_note('Bot navigation: measuring %s... %d%%'
							% (g.map_name, _q * 25))
				_last = globals().get('g_offh_nav_last_cov')
				if _last is None or abs(_cov - _last) >= 0.10 or (g.settled() and _cov != _last):
					globals()['g_offh_nav_last_cov'] = _cov
					LOG_DEBUG('NAVGRID %s: %dx%d @%.1fm | coverage %.0f%% | %s | pass=%d probes=%d'
						% (g.map_name, g.nx, g.nz, g.cell, _cov * 100.0,
						   str(g.counts()), g.passes, g.probes))
			elif not globals().get('g_offh_nav_dumped'):
				# Loaded from disk: there is nothing to write back.
				globals()['g_offh_nav_dumped'] = True
			if g.settled() and not globals().get('g_offh_nav_dumped'):
				globals()['g_offh_nav_dumped'] = True
				try:
					import os
					from gui.mods.offhangar import paths as _P
					_dst = os.path.join(_P.USER_DIR, 'nav_dump',
					                    '%s.grid' % g.map_name)
					g.dump(_dst)
					LOG_DEBUG('NAVGRID dumped to %s (%dx%d cells)' % (_dst, g.nx, g.nz))
					if globals().get('g_offh_nav_announced'):
						# Coverage below 100% is normal and FINAL - void outside
						# the playable area can never be probed. Say the useful
						# thing (it is saved) rather than the alarming one.
						_offh_nav_note('Bot navigation: %s measured and saved. '
							'Bots use it from here on, and this map is never '
							'measured again.' % g.map_name, '66DD66')
				except Exception as _de:
					LOG_DEBUG('NAVGRID dump error:', str(_de))
					if globals().get('g_offh_nav_announced'):
						_offh_nav_note('Bot navigation: %s was measured but could '
							'not be saved (%s) - it will be measured again next '
							'battle.' % (g.map_name, str(_de)), 'FF6666')
			if g.settled() and not globals().get('g_offh_nav_painted'):
				# Painted keep-out areas become BLOCKED CELLS, which A* already
				# honours - no pathfinding changes at all. Applied after the dump
				# so the dump stays raw terrain.
				globals()['g_offh_nav_painted'] = True
				# Painted areas are the ONLY thing that blocks. The grid states
				# no opinion of its own about where a tank may go.
				_pf = globals().get('g_offh_profile')
				if _pf is not None and _pf.avoid:
					try:
						from gui.mods.offhangar import bot_routes as _BRa
						_nb = g.apply_avoid(_pf.avoid, _BRa.point_in_poly)
						LOG_DEBUG('PROFILE avoid: %d painted areas blocked %d grid cells'
							% (len(_pf.avoid), _nb))
					except Exception as _ae:
						LOG_DEBUG('PROFILE avoid error:', str(_ae))
				# Clearance, so routes run down the middle of a corridor rather
				# than along the staircase edge of a painted area. apply_avoid
				# does this itself, but a map with no paint never calls it.
				try:
					if getattr(g, 'near_wall', None) is None:
						g.build_clearance()
				except Exception as _ce:
					LOG_DEBUG('NAVGRID clearance error:', str(_ce))
				# One flood fill, AFTER the paint, so "is that even reachable?"
				# is a set lookup instead of an exhaustive failed search. Blocks
				# nothing; it only stops the same doomed search running every
				# frame, which is what collapsed the frame rate.
				try:
					_bs = globals().get('g_offline_bases', {}) or {}
					_sd = []
					for _t in (1, 2):
						for _b in (_bs.get(_t, []) or []):
							_sd.append((_b.x, _b.z))
					_nr = g.build_reach(_sd or None)
					LOG_DEBUG('NAVGRID reachable: %d of %d passable cells'
						% (_nr, g.counts().get('passable', 0)))
				except Exception as _re:
					LOG_DEBUG('NAVGRID reach error:', str(_re))
				_offh_orient_profile()
				_offh_autoroutes_tick(g)
		except Exception as e:
			LOG_DEBUG('NAVGRID step error:', str(e))
			globals()['g_offh_navgrid'] = None
		return
	if globals().get('g_offh_nav_tried'):
		return
	globals()['g_offh_nav_tried'] = True
	try:
		import BigWorld
		from _constants import CONFIG_OPTIONS as _CFG
		if not bool(_CFG.get('nav_grid', True)):
			return
		_ab = globals().get('g_offline_bounds')
		if not _ab:
			LOG_DEBUG('NAVGRID: no arena bounds yet - skipping this battle')
			return
		from gui.mods.offhangar import nav_grid as _NG
		_mn = getattr(BigWorld.player().arena.arenaType, 'geometryName', '') or 'map'
		_mn = str(_mn).replace('\\', '/').rstrip('/').split('/')[-1]
		# The arena rectangle IS the playable map - it is exactly what the
		# minimap shows - so the grid covers that and nothing more. Measuring
		# beyond it would spend probes on ground no tank can reach.
		_b = _ab
		# Reuse a grid we already measured on this map. Building one costs ~50k
		# wg_collideSegment rays, and at a few hundred per frame that is minutes
		# of unplayable frame rate - which is exactly what it cost. The terrain
		# does not change between battles, so paying for it twice is pure waste.
		# Painted blocking is NOT baked into the dump, so it still applies fresh
		# each battle and editing a profile takes effect immediately.
		_g = None
		try:
			import os
			from gui.mods.offhangar import paths as _P
			# Two places, in this order:
			#  1. the user's own bake / import, which must always win - it is
			#     either newer than what we shipped or deliberately theirs;
			#  2. a mesh SHIPPED with the mod, so a map we have pre-measured
			#     costs a fresh install nothing at all on its first battle.
			# Only the user location is ever written to; the shipped one is
			# read-only, so a mod update replaces it cleanly.
			_cands = [os.path.join(_P.USER_DIR, 'nav_dump', '%s.grid' % _mn),
			          os.path.join(_P.mod_dir(), 'navmesh', '%s.grid' % _mn)]
			for _src in _cands:
				if not os.path.isfile(_src):
					continue
				try:
					_cand = _NG.NavGrid.load(_src)
				except Exception as _pe:
					LOG_DEBUG('NAVGRID %s unreadable (%s)' % (_src, str(_pe)))
					continue
				if not _cand.fits(_b):
					LOG_DEBUG('NAVGRID %s does not fit arena bounds %s - ignored'
						% (_src, str(_b)))
					continue
				_g = _cand
				globals()['g_offh_nav_shipped'] = (_src is _cands[1])
				LOG_DEBUG('NAVGRID loaded %s (%dx%d @%.1fm) - no probing needed'
					% (_src, _g.nx, _g.nz, _g.cell))
				break
		except Exception as _le:
			LOG_DEBUG('NAVGRID load error (rebuilding):', str(_le))
		# A profile the user PAINTED for this map is itself an opt-in: they have
		# already decided this map matters, and a painted profile without a mesh
		# is half a feature - the destinations apply but the pathfinding and the
		# avoid areas cannot. _offh_profile_tick runs before this one, so a set
		# g_offh_profile here means painted, never auto-generated.
		_painted = globals().get('g_offh_profile') is not None
		if _g is None and not _painted:
			# No mesh, no paint, nobody asked. Measuring costs ~50k raycasts
			# spread over the opening minutes, and a user who never intends to
			# edit this map should not pay that for a map we did not ship a mesh
			# for. Bots behave exactly as they did before any of this existed.
			LOG_DEBUG('NAVGRID: no mesh for %s and nothing painted for it '
				'- bots use the original behaviour on this map' % _mn)
			globals()['g_offh_navgrid'] = None
			return
		if _g is None and _painted:
			LOG_DEBUG('NAVGRID: %s has a painted profile but no mesh - measuring '
				'it so the painted routes can actually be followed' % _mn)
		if _g is None:
			_g = _NG.NavGrid(_b, _mn)
			LOG_DEBUG('NAVGRID: building %dx%d cells @ %.1f m over %s '
				'(this map has a painted profile)' % (_g.nx, _g.nz, _g.cell, str(_b)))
			_offh_nav_note('Bot navigation: measuring %s. This happens once - '
				'later battles on this map load instantly.' % _mn)
			globals()['g_offh_nav_announced'] = True
		globals()['g_offh_navgrid'] = _g
	except Exception as e:
		LOG_DEBUG('NAVGRID init error:', str(e))


def _offh_nav_waypoint(mock, dest):
	'''Turn a destination into the NEXT WAYPOINT along a real path.

	This is the whole point of the nav grid: bots used to steer straight at a
	destination and grind against whatever lay between, which put ~30% of AI
	samples in stuck-escape. Now they follow an A* route and the feelers only
	have to handle what the grid cannot see - other tanks, destructibles.

	Returns dest unchanged whenever pathing is off, the grid is not ready, the
	destination is already close, or no route exists, so every failure mode
	degrades to exactly the old behaviour.
	'''
	try:
		from _constants import CONFIG_OPTIONS as _CFG
		if not bool(_CFG.get('bot_pathfinding', True)):
			return dest
	except Exception:
		return dest
	g = globals().get('g_offh_navgrid')
	if g is None or dest is None:
		return dest
	try:
		import math
		from gui.mods.offhangar import nav_grid as _NG
		# Below this the grid has too many holes to trust for routing.
		if g.coverage() < 0.60:
			return dest
		px, pz = mock.position.x, mock.position.z
		_dx, _dz = dest[0] - px, dest[2] - pz
		_dd = math.sqrt(_dx * _dx + _dz * _dz)
		# Holding position, or near enough to just drive at it. Pathing here
		# would only add wobble.
		if _dd < 25.0:
			mock._nav_wp = None
			mock._nav_dest = None
			return dest
		# A stuck bot's path is, by definition, not working - drop it so the
		# escape manoeuvre is followed by a fresh plan from wherever it ends up.
		if (getattr(mock, '_wall_escape', None) or 0) > 0:
			mock._nav_wp = None
			mock._nav_dest = None
			return dest

		_wp = getattr(mock, '_nav_wp', None)
		_wd = getattr(mock, '_nav_dest', None)
		if _wp and _wd is not None:
			# Same destination as the cached path was built for?
			if (_wd[0] - dest[0]) ** 2 + (_wd[2] - dest[2]) ** 2 < 30.0 * 30.0:
				while _wp:
					wx, wz = _wp[0]
					if (wx - px) ** 2 + (wz - pz) ** 2 <= 18.0 * 18.0:
						_wp.pop(0)
					else:
						break
				mock._nav_wp = _wp
				if _wp:
					return (_wp[0][0], dest[1], _wp[0][1])
				return dest          # path consumed: the destination is next
			mock._nav_wp = None      # destination moved - replan below

		# ONE search per frame across all bots. Measured on the live Malinovka
		# grid: median 4 ms, p90 17 ms under py3, and the client runs 2-3x
		# slower than that, so an unbudgeted stampede at battle start would be
		# a visible stall.
		# A destination this bot already failed on is not worth re-searching every
		# frame. Cheap guard on top of the flood fill, covering the cases the
		# fill cannot answer (a start snapped out of a pocket, say).
		_fd = getattr(mock, '_nav_faildest', None)
		if _fd is not None:
			if (_fd[0] - dest[0]) ** 2 + (_fd[1] - dest[2]) ** 2 < 25.0 * 25.0:
				_ft = (getattr(mock, '_nav_failt', 0) or 0) + 1
				mock._nav_failt = _ft
				if _ft % 240:            # ~4 s at 60 fps before trying again
					return dest
			else:
				mock._nav_faildest = None
				mock._nav_failt = 0
		if globals().get('g_offh_astar_used'):
			return dest
		globals()['g_offh_astar_used'] = True
		_a = g.cell_at(px, pz)
		_b = g.cell_at(dest[0], dest[2])
		# A painted step can sit on a cell the grid calls blocked - a building
		# interior reads as a cliff, and Ruinberg produced 742 failures this way
		# because the profile was painted before the map had a grid to warn
		# against. Snap a DESTINATION harder than the default before giving up.
		if _b is not None and not (g.passable(_b) and g.can_reach(_b)):
			# reachable=True matters: a painted destination often sits INSIDE the
			# area that was painted around it, and snapping to the nearest merely
			# passable cell can land in the same sealed pocket - one certain A*
			# failure traded for another.
			_b2 = g.nearest_passable(_b, radius=10, reachable=True)
			if _b2 is not None:
				_b = _b2
		_p = g.astar(_a, _b)
		if not _p:
			mock._nav_wp = None
			mock._nav_dest = None
			mock._nav_faildest = (dest[0], dest[2])
			mock._nav_failt = 0
			globals()['g_offh_astar_fail'] = (globals().get('g_offh_astar_fail', 0) or 0) + 1
			_nf = globals()['g_offh_astar_fail']
			if _nf in (1, 25, 250):
				LOG_DEBUG('NAVPATH unreachable (#%d): bot=%s at (%.0f,%.0f) -> (%.0f,%.0f) '
					'- a painted step may be inside a building; re-check it in the painter '
					'now this map has a grid'
					% (_nf, getattr(mock, 'id', '?'), px, pz, dest[0], dest[2]))
			return dest
		mock._nav_faildest = None
		_sm = g.smooth(_p)
		_pts = g.path_world(_sm)[1:]     # drop the cell we are standing in
		mock._nav_wp = _pts
		mock._nav_dest = dest
		globals()['g_offh_astar_ok'] = (globals().get('g_offh_astar_ok', 0) or 0) + 1
		if (globals().get('g_offh_astar_ok') or 0) <= 8:
			LOG_DEBUG('NAVPATH bot=%s %d cells -> %d waypoints, %.0f m to go'
				% (getattr(mock, 'id', '?'), len(_p), len(_pts), _dd))
		return (_pts[0][0], dest[1], _pts[0][1]) if _pts else dest
	except Exception as e:
		LOG_DEBUG('NAVPATH error:', str(e))
		return dest


def _offh_orient_profile():
	"""Correct a painted profile whose team numbers are the other way round.

	The editor takes team 1/2 from the map's arena_def; the offline mod assigns
	them its own way, and on some maps the two disagree - measured on
	Prokhorovka, where team 1 spawns at z=+372 but its painted routes start at
	z=-445. Every bot then drives the full length of the map to the ENEMY spawn,
	both teams cross, and they meet in the middle: precisely the behaviour this
	whole feature exists to remove, which is why it was so convincing.

	Decided by comparing painted starts against the real base positions, so it
	fixes profiles already in the wild without anyone repainting, and does
	nothing at all to a correctly-oriented one.
	"""
	if globals().get('g_offh_orient_done'):
		return
	prof = globals().get('g_offh_profile')
	if prof is None:
		return
	globals()['g_offh_orient_done'] = True
	try:
		from gui.mods.offhangar import bot_routes as _BR
		_bs = globals().get('g_offline_bases', {}) or {}

		def _mid(team):
			pts = _bs.get(team) or []
			if not pts:
				return None
			return (sum(p.x for p in pts) / float(len(pts)),
			        sum(p.z for p in pts) / float(len(pts)))

		_b1, _b2 = _mid(1), _mid(2)
		_fl = _BR.orientation_is_flipped(prof, _b1, _b2)
		if _fl is None:
			LOG_DEBUG('PROFILE orientation: not enough data to check - left as painted')
			return
		if not _fl:
			LOG_DEBUG('PROFILE orientation: teams agree with the map')
			return
		_n = _BR.flip_profile_teams(prof)
		globals().pop('g_offh_prof_cache', None)
		globals()['g_offh_prof_taken'] = {}
		LOG_DEBUG('PROFILE orientation: teams were INVERTED for this map - '
			'swapped %d entries so bots go to their own half (team1 base z=%.0f, '
			'team2 base z=%.0f)'
			% (_n, _b1[1] if _b1 else 0.0, _b2[1] if _b2 else 0.0))
		_offh_nav_note('Bot navigation: the painted profile for this map had its '
			'teams the other way round - corrected automatically.', 'FFCC33')
	except Exception as e:
		LOG_DEBUG('PROFILE orientation error:', str(e))


def _offh_autoroutes_tick(g):
	"""Give this map class-appropriate routes even if nobody ever painted it.

	A hand-painted profile always wins - this only fills the gap. The generated
	set is written next to the grid so it is STABLE between battles (bots would
	otherwise re-learn the map every time) and so the editor can import it as a
	starting point rather than making the user begin from a blank map.

	Variety comes from which route a bot picks, not from regenerating: the pool
	holds several per class and _offh_prof_pick spreads bots across them.
	"""
	# NOT g_offh_routes_tried - that belongs to _offh_route_tick (the baked WG
	# node map), which runs FIRST and would have made this return every time.
	if globals().get('g_offh_autoroutes_tried'):
		return
	globals()['g_offh_autoroutes_tried'] = True
	if globals().get('g_offh_profile') is not None:
		return                       # painted by hand; leave it alone
	try:
		import os, json, random
		from gui.mods.offhangar import bot_routes as _BR
		from gui.mods.offhangar import paths as _P
		from _constants import CONFIG_OPTIONS as _CFG
		if not bool(_CFG.get('auto_routes', True)):
			return
		_dst = os.path.join(_P.USER_DIR, 'nav_dump', '%s.routes.json' % g.map_name)
		_doc = None
		if os.path.isfile(_dst):
			try:
				_f = open(_dst, 'rb')
				try:
					_doc = json.loads(_f.read().decode('utf-8'))
				finally:
					_f.close()
			except Exception as _le:
				LOG_DEBUG('AUTOROUTES load error (regenerating):', str(_le))
				_doc = None
		if _doc is None:
			_bs = {}
			for _t in (1, 2):
				_bs[_t] = [(b.x, b.z) for b in (globals().get('g_offline_bases', {}) or {}).get(_t, [])]
			if not (_bs.get(1) and _bs.get(2)):
				LOG_DEBUG('AUTOROUTES: need both bases to know which way is forward - skipped')
				return
			_doc = _BR.generate_profile(g, _bs,
				(g.x0, g.z0, g.x1, g.z1),
				g.map_name, seed=random.randint(0, 1 << 30))
			try:
				_d = os.path.dirname(_dst)
				if _d and not os.path.isdir(_d):
					os.makedirs(_d)
				_f = open(_dst, 'wb')
				try:
					_f.write(json.dumps(_doc, indent=1).encode('utf-8'))
				finally:
					_f.close()
				LOG_DEBUG('AUTOROUTES generated %d routes -> %s' % (len(_doc.get('routes') or []), _dst))
			except Exception as _we:
				LOG_DEBUG('AUTOROUTES save error (using them anyway):', str(_we))
		_pr = _BR.parse_profile(_doc, g.map_name, (g.x0, g.z0, g.x1, g.z1))
		if _pr is None or _pr.is_empty():
			LOG_DEBUG('AUTOROUTES: nothing usable generated for %s' % g.map_name)
			return
		globals()['g_offh_profile'] = _pr
		globals()['g_offh_prof_taken'] = {}
		globals().pop('g_offh_prof_cache', None)
		LOG_DEBUG('AUTOROUTES active for %s: %s' % (g.map_name, _pr.summary()))
	except Exception as e:
		LOG_DEBUG('AUTOROUTES error:', str(e))


def _offh_profile_tick():
	"""Load this map's PAINTED profile once per battle.

	Painted data outranks the WG node pool (see _offh_bot_move_target); the
	absence of either just falls through to the next source down.
	"""
	if globals().get('g_offh_profile_tried'):
		return
	globals()['g_offh_profile_tried'] = True
	try:
		import BigWorld
		from gui.mods.offhangar import bot_routes as _BR
		from gui.mods.offhangar import paths as _P
		_mn = getattr(BigWorld.player().arena.arenaType, 'geometryName', '') or ''
		prof = _BR.load_profile(_mn, _P.mod_dir(), globals().get('g_offline_bounds'))
		if prof is None or prof.is_empty():
			LOG_DEBUG('PROFILE: none painted for %s - using baked WG nodes' % _mn)
			return
		globals()['g_offh_profile'] = prof
		globals()['g_offh_prof_taken'] = {}
		LOG_DEBUG('PROFILE %s: %s' % (prof.map_name, prof.summary()))
		if sum(prof.dropped.values()):
			# A hand-authored file that lost rows must SAY so rather than
			# silently shrinking.
			LOG_DEBUG('PROFILE dropped malformed entries: %s' % str(prof.dropped))
	except Exception as e:
		LOG_DEBUG('PROFILE load error:', str(e))


def _offh_bot_class(mock):
	'''Cached bot_routes class bucket for a mock, from its real descriptor.'''
	c = getattr(mock, '_route_class', None)
	if c is not None:
		return c
	try:
		from gui.mods.offhangar import bot_routes as _BR
		td = getattr(mock, 'typeDescriptor', None)
		c = _BR.class_of(getattr(getattr(td, 'type', None), 'tags', ()) or ())
	except Exception:
		c = 'medium'
	mock._route_class = c
	return c


def _offh_bot_can_see(mock, tx, tz, td):
	"""Can this bot actually SEE the point it is shooting at?

	The bot fire gate checked period, drowning and a destroyed gun - but never
	visibility, so bots happily fired at targets they had no line to, including
	when nothing at all was spotted. Spotting in this mod is only ever simulated
	relative to the PLAYER (`_spot_visible` on enemy mocks), so a bot has no
	notion of what its own team can see; this gives it one.

	Two gates, both cheap:
	  * the tank's own view range from its descriptor, so a scout sees further
	    than a heavy rather than everyone having the same reach;
	  * one line-of-sight ray, sampled at hull and turret height so a target
	    that is merely hull-down behind a crest is not treated as invisible.

	Throttled to ~3x/second per bot and cached, staggered by entity id so the
	whole team does not probe on the same frame.
	"""
	import math
	_now = getattr(mock, '_los_t', None) or 0.0
	_dt = 0.0
	try:
		import BigWorld
		_dt = BigWorld.time()
	except Exception:
		pass
	if _now and (_dt - _now) < 0.33:
		return bool(getattr(mock, '_los_ok', False))
	mock._los_t = _dt

	px, pz = mock.position.x, mock.position.z
	dist = math.sqrt((tx - px) ** 2 + (tz - pz) ** 2)
	# View range from the vehicle's own turret, as the game defines it.
	_vr = 400.0
	try:
		_t = getattr(td, 'turret', None)
		if _t is not None:
			_vr = float(_t.get('circularVisionRadius', 400.0)) or 400.0
	except Exception:
		pass
	if dist > _vr:
		mock._los_ok = False
		return False
	if dist < 15.0:
		mock._los_ok = True          # point blank: never argue with geometry
		return True

	py = mock.position.y
	ok = False
	try:
		import BigWorld, Math
		for _h in (1.5, 2.4):
			_a = Math.Vector3(px, py + _h, pz)
			_b = Math.Vector3(tx, py + _h, tz)
			if BigWorld.wg_collideSegment(_offh_bspace(), _a, _b, 128) is None:
				ok = True
				break
	except Exception:
		ok = True                    # never let a probe failure mute the AI
	mock._los_ok = ok
	return ok


# Frames a bot may make no progress toward its current step before that step is
# treated as reached. ~4-8 s at normal frame rates: long enough that a slow or
# briefly blocked tank is never cut short, short enough that nobody watches a
# bot idle for a whole battle.
_PROF_STALL_FRAMES = 240


def _offh_prof_lists(prof, team, cls):
	"""Cached (routes, points, source_class) for a team+class.

	A class with nothing painted BORROWS from the nearest class by depth rather
	than falling all the way back to chasing the nearest enemy - a TD sent down
	an SPG route is at roughly the right depth, which is most of what the route
	was saying.

	The source class is returned as well, and it is load-bearing: claims must be
	keyed on the list the route actually came from. Keying a borrowing TD under
	'td' while mediums key under 'medium' would let both take "route 0" of the
	same physical list and stack on it - the same fault that had fifteen bots
	sharing three keys before.
	"""
	c = globals().setdefault('g_offh_prof_cache', {})
	k = (team, cls)
	if k not in c:
		_r = prof.routes_for(team, cls)
		_p = prof.destinations_for(team, cls)
		_src = cls
		if not _r and not _p:
			try:
				from gui.mods.offhangar import bot_routes as _BRl
				for _alt in _BRl.similar_classes(cls):
					_r = prof.routes_for(team, _alt)
					_p = prof.destinations_for(team, _alt)
					if _r or _p:
						_src = _alt
						LOG_DEBUG('PROFILE borrow: team %s has nothing painted for '
							'%s - using %s routes instead' % (team, cls, _alt))
						break
			except Exception as _be:
				LOG_DEBUG('PROFILE borrow error:', str(_be))
		c[k] = (_r, _p, _src)
	return c[k]


def _offh_prof_claim(key, delta):
	t = globals().setdefault('g_offh_prof_taken', {})
	n = (t.get(key, 0) or 0) + delta
	if n > 0:
		t[key] = n
	elif key in t:
		del t[key]


def _offh_prof_pick(prof, mock, team, cls):
	import math
	"""Assign this bot a painted route, or failing that a painted point.

	A ROUTE outranks a point: it is a stronger statement of intent - it says
	which way to go, not merely where to end up. Which route is picked is
	spread-weighted and jittered so fifteen bots do not file down one lane and
	successive battles differ.
	"""
	routes, points, _src = _offh_prof_lists(prof, team, cls)
	taken = globals().setdefault('g_offh_prof_taken', {})
	rng = _offh_route_rng()
	best = None
	best_s = None
	if routes:
		for i in range(len(routes)):
			# The key must identify the ROUTE, and route 0 of a heavy is not
			# route 0 of a medium - they come from different per-class lists.
			# Keying on the index alone made a heavy taking lane 0 push mediums
			# off their own lane 0, so fifteen bots spread over three keys
			# instead of fifteen routes.
			sc = -0.6 * (taken.get(('r', team, _src, i), 0) or 0) + rng.random() * 0.3
			if best_s is None or sc > best_s:
				best, best_s = ('r', team, _src, i), sc
	elif points:
		for i in range(len(points)):
			sc = -0.6 * (taken.get(('p', team, _src, i), 0) or 0) + rng.random() * 0.3
			if best_s is None or sc > best_s:
				best, best_s = ('p', team, _src, i), sc
	if best is None:
		return False
	_offh_prof_claim(best, 1)
	mock._prof_kind = best[0]
	mock._prof_idx = best[3]
	mock._prof_key = best
	mock._prof_step = 0
	# A painted step is ONE point, and several bots legitimately share a route -
	# without an offset they all drive at the identical coordinate, collide, and
	# mill about. The offset SCALES with how many are sharing: 8-22 m keeps a
	# pair together as intended, but ten TDs on one of two painted routes need
	# room. Area grows with the number sharing, so the radius grows as its
	# square root rather than linearly.
	_shared = (globals().get('g_offh_prof_taken', {}) or {}).get(best, 1) or 1
	try:
		from gui.mods.offhangar import bot_routes as _BRs
		_lo, _hi = _BRs.spread_radius(_shared)
	except Exception:
		_lo, _hi = 8.0, 22.0
	_ang = rng.random() * 6.28318
	_rad = _lo + rng.random() * max(0.0, _hi - _lo)
	mock._prof_off = (math.cos(_ang) * _rad, math.sin(_ang) * _rad)
	LOG_DEBUG('PROFILE assign: bot=%s team=%s class=%s -> painted %s #%d '
		'(%s list, %d sharing, fan %.0f m)'
		% (getattr(mock, 'id', '?'), team, cls,
		   'route' if best[0] == 'r' else 'point', best[3],
		   _src, _shared, _rad))
	return True


def _offh_prof_release(mock):
	_k = getattr(mock, '_prof_key', None)
	if _k is not None:
		_offh_prof_claim(_k, -1)
	mock._prof_key = None
	mock._prof_kind = None
	mock._prof_idx = None
	mock._prof_step = 0
	mock._prof_off = None
	mock._prof_held = False
	mock._prof_closing = False
	mock._prof_best = None
	mock._prof_stall = 0
	mock._prof_gaveup = False


def _offh_prof_spread(mock, tx, tz):
	'''Apply this bot's fan-out offset, but never off the map or into paint.

	The offset exists so bots sharing a painted step form a small cluster
	instead of piling onto one coordinate. Applied blindly it also pushed
	destinations OUTSIDE the arena bounds (cell_at returns None there) and into
	painted keep-out areas - and since a bot re-derives the same destination
	every frame, each one became a permanent A* failure. That is where all 3350
	failures in the last Ruinberg log came from, against only 308 successes.

	Falls back through half the offset to none, so a bot in a tight painted
	corridor simply shares the exact step rather than losing its destination.
	'''
	_off = getattr(mock, '_prof_off', None)
	if not _off:
		return tx, tz
	g = globals().get('g_offh_navgrid')
	if g is None or not g.settled():
		return tx + _off[0], tz + _off[1]
	for _f in (1.0, 0.5):
		_x, _z = tx + _off[0] * _f, tz + _off[1] * _f
		_c = g.cell_at(_x, _z)
		if _c is not None and g.passable(_c) and g.can_reach(_c):
			return _x, _z
	return tx, tz


def _offh_prof_move_target(mock, enemy_pos, enemy_dist, my_team):
	"""Painted destination for this bot, or None to fall through to WG nodes.

	Mirrors the node path's structure exactly - arrive, hold and fight, move up -
	so there is one set of ranges to reason about rather than two.
	"""
	prof = globals().get('g_offh_profile')
	if prof is None:
		return None
	try:
		import math
		from gui.mods.offhangar import bot_routes as _BR
		cls = _offh_bot_class(mock)
		routes, points, _src = _offh_prof_lists(prof, my_team, cls)
		if not routes and not points:
			return None            # nothing painted for this team+class
		px, pz = mock.position.x, mock.position.z
		here = (px, mock.position.y, pz)

		kind = getattr(mock, '_prof_kind', None)
		if kind is None:
			if not _offh_prof_pick(prof, mock, my_team, cls):
				return None
			kind = mock._prof_kind
		idx = getattr(mock, '_prof_idx', None)
		if idx is None:
			return None

		# Resolve the position this bot is currently heading for.
		if kind == 'r':
			if idx >= len(routes):
				# The class this bot reports can CHANGE between the frame that
				# picked the route and a later frame, and each class has its own
				# route list - so an index valid for one is out of range for
				# another. Name it rather than silently falling through.
				LOG_DEBUG('PROFILE stale index: bot=%s team=%s class=%s idx=%s '
					'but that class has %d routes - releasing'
					% (getattr(mock, 'id', '?'), my_team, cls, idx, len(routes)))
				_offh_prof_release(mock)
				return None
			pts = routes[idx]
			# __getattr__ hands back None for an unset attribute, so the `or 0`
			# is load-bearing - this is the trap that has started four separate
			# bug hunts in this file.
			step = (getattr(mock, '_prof_step', 0) or 0)
			if step >= len(pts):
				step = len(pts) - 1
			tx, tz = pts[step]
			last = (step >= len(pts) - 1)
		else:
			if idx >= len(points):
				_offh_prof_release(mock)
				return None
			tx, tz = points[idx]
			last = True
		tx, tz = _offh_prof_spread(mock, tx, tz)

		_dist = math.sqrt((tx - px) ** 2 + (tz - pz) ** 2)
		# A step can be somewhere the bot physically cannot stand: painted over,
		# up a cliff, inside a building. The pathfinder then drives it to the
		# nearest reachable cell instead, which may be tens of metres short - so
		# the bot parks there, never gets inside ARRIVE_RADIUS of the PAINTED
		# point, never advances, and oscillates on the spot forever. Measured on
		# Mines: TDs sat 33-49 m from their goal with velocity flickering around
		# zero and no arrival ever logged.
		#
		# So arrival is "close enough" OR "stopped getting closer". The watchdog
		# resets whenever real progress is made, so a bot that is merely slow is
		# never cut short.
		_best = getattr(mock, '_prof_best', None)
		if _best is None or _dist < _best - 2.0:
			mock._prof_best = _dist
			mock._prof_stall = 0
		else:
			mock._prof_stall = (getattr(mock, '_prof_stall', 0) or 0) + 1
		_stalled = (getattr(mock, '_prof_stall', 0) or 0) > _PROF_STALL_FRAMES
		if _stalled and not getattr(mock, '_prof_gaveup', False):
			mock._prof_gaveup = True
			LOG_DEBUG('PROFILE unreachable step: bot=%s class=%s stopped %.0f m short '
				'of %s#%s step %s - treating it as reached'
				% (getattr(mock, 'id', '?'), cls, _dist,
				   getattr(mock, '_prof_kind', '?'), getattr(mock, '_prof_idx', '?'),
				   getattr(mock, '_prof_step', 0) or 0))
		if _dist <= _BR.ARRIVE_RADIUS or _stalled:
			if not last:
				mock._prof_step = (getattr(mock, '_prof_step', 0) or 0) + 1
				mock._prof_best = None
				mock._prof_stall = 0
				mock._prof_gaveup = False
				nx, nz = routes[idx][mock._prof_step]
				nx, nz = _offh_prof_spread(mock, nx, nz)
				return (nx, here[1], nz)
			# Arrived at the end of the route: HOLD. This is where the profile
			# said to be, and the gun is aimed by separate code, so standing
			# still is not passivity.
			#
			# It used to hold only while an enemy was within HOLD_RANGE and
			# otherwise release and re-pick. On a big map both teams start ~870 m
			# apart - far outside that range - so every bot abandoned its route
			# the moment it arrived. Tank destroyers suffered worst: their
			# painted spot is deliberately close to spawn, so they got there in
			# seconds and then churned pick -> arrive -> release -> pick on the
			# spot, which reads exactly as "clueless, ignoring its route".
			# Holding a position is not the same as freezing on it. A heavy that
			# can see a target 400 m away should be closing; a TD at 400 m is
			# already where it wants to be. Each class closes to its own
			# engagement distance and fights there.
			_eng = _BR.engage_range(cls)
			if (enemy_pos is not None and enemy_dist is not None
					and enemy_dist <= _BR.HOLD_RANGE and enemy_dist > _eng):
				if not getattr(mock, '_prof_closing', False):
					mock._prof_closing = True
					LOG_DEBUG('PROFILE close: bot=%s class=%s at its painted spot, '
						'enemy %.0f m away and it fights at %.0f m - advancing'
						% (getattr(mock, 'id', '?'), cls, enemy_dist, _eng))
				return enemy_pos
			mock._prof_closing = False
			if not getattr(mock, '_prof_held', False):
				mock._prof_held = True
				LOG_DEBUG('PROFILE hold: bot=%s team=%s class=%s reached the end of '
					'%s#%s - holding position'
					% (getattr(mock, 'id', '?'), my_team, cls,
					   getattr(mock, '_prof_kind', '?'), getattr(mock, '_prof_idx', '?')))
			return here

		# En route: only something in our face is worth diverting for.
		if (enemy_pos is not None and enemy_dist is not None
				and enemy_dist <= _BR.BRAWL_RANGE):
			return enemy_pos
		return (tx, here[1], tz)
	except Exception as e:
		LOG_DEBUG('PROFILE move-target error:', str(e))
		return None


def _offh_bot_move_target(mock, enemy_pos, enemy_dist, my_team):
	'''Where this bot should DRIVE, which is not always what it shoots at.

	An enemy inside engagement range still wins - that keeps the existing
	fighting behaviour intact. Otherwise the bot heads for the destination its
	class picked out of the baked map, which is what stops all 30 tanks
	converging on the midpoint. Returns enemy_pos unchanged whenever routes are
	unavailable, so this is a no-op on unbaked maps.'''
	# Priority: painted routes > painted points > WG node pool > nearest enemy.
	# Each level falls through on absence, so any map works at whatever level it
	# has data for.
	_pp = _offh_prof_move_target(mock, enemy_pos, enemy_dist, my_team)
	if _pp is not None:
		# Log every CHANGE, not just the first call: a bot that starts on a
		# painted step and later falls through to the enemy looks identical to
		# one that never fell through, if you only log once.
		_sig = ('P', getattr(mock, '_prof_kind', None), getattr(mock, '_prof_idx', None),
		        getattr(mock, '_prof_step', 0) or 0)
		if getattr(mock, '_mv_src_sig', None) != _sig:
			mock._mv_src_sig = _sig
			_d = ((_pp[0] - mock.position.x) ** 2 + (_pp[2] - mock.position.z) ** 2) ** 0.5
			LOG_DEBUG('MOVE SRC bot=%s team=%s class=%s -> PAINTED %s#%s step=%s '
				'target=(%.0f,%.0f) %.0fm away'
				% (getattr(mock, 'id', '?'), my_team, _offh_bot_class(mock),
				   _sig[1], _sig[2], _sig[3], _pp[0], _pp[2], _d))
		return _pp
	rm = globals().get('g_offh_routemap')
	if rm is None or not rm.usable():
		# Everything above declined. On a map with a painted profile this should
		# not happen, and it means bots chase the nearest enemy - the original
		# behaviour - so say WHY loudly enough to find it.
		if getattr(mock, '_mv_src_sig', None) != 'E':
			mock._mv_src_sig = 'E'
			_pf = globals().get('g_offh_profile')
			LOG_DEBUG('MOVE SRC bot=%s team=%s class=%s -> ENEMY (no painted target). '
				'profile=%s kind=%s idx=%s step=%s routes_for_class=%d'
				% (getattr(mock, 'id', '?'), my_team, _offh_bot_class(mock),
				   'yes' if _pf is not None else 'NO',
				   getattr(mock, '_prof_kind', None), getattr(mock, '_prof_idx', None),
				   getattr(mock, '_prof_step', 0) or 0,
				   len(_pf.routes_for(my_team, _offh_bot_class(mock))) if _pf is not None else -1))
		return enemy_pos
	try:
		import math
		from gui.mods.offhangar import bot_routes as _BR
		px, pz = mock.position.x, mock.position.z
		here = (px, mock.position.y, pz)

		idx = getattr(mock, '_route_node', None)
		if idx is None:
			bases = globals().get('g_offline_bases', {}) or {}
			own = [(b.x, b.z) for b in (bases.get(my_team, []) or [])]
			foe = [(b.x, b.z) for b in (bases.get(2 if my_team == 1 else 1, []) or [])]
			idx = rm.pick(_offh_bot_class(mock),
			              own[0] if own else None,
			              foe[0] if foe else None,
			              _offh_route_rng())
			mock._route_node = idx
			if idx is not None:
				LOG_DEBUG('ROUTE assign: bot=%s class=%s -> node %d %s'
					% (getattr(mock, 'id', '?'), _offh_bot_class(mock), idx,
					   str(tuple(round(v, 1) for v in rm.position(idx)))))
		if idx is None:
			return enemy_pos

		_nx, _ny, _nz = rm.position(idx)
		_d_node = math.sqrt((_nx - px) ** 2 + (_nz - pz) ** 2)

		if _d_node <= _BR.ARRIVE_RADIUS:
			# We are where we wanted to be. FIGHT FROM HERE - returning our own
			# position makes the driver stop, and the gun is aimed by separate
			# code, so the bot holds the ground it picked instead of walking off
			# it. Only once nothing is left nearby does it move up.
			if enemy_dist is not None and enemy_dist <= _BR.HOLD_RANGE:
				return here
			nxt = rm.next_from(idx, _offh_route_rng())
			rm.release(idx)
			rm.claim(nxt)
			mock._route_node = nxt
			return rm.position(nxt) if nxt is not None else here

		# En route. Only something genuinely in our face is worth diverting for;
		# anything further away gets shot at while we keep driving.
		if (enemy_pos is not None and enemy_dist is not None
				and enemy_dist <= _BR.BRAWL_RANGE):
			return enemy_pos
		return rm.position(idx)
	except Exception as e:
		LOG_DEBUG('ROUTE move-target error:', str(e))
		return enemy_pos


def _offh_route_rng():
	r = globals().get('g_offh_route_rng')
	if r is None:
		import random
		r = random.Random()
		globals()['g_offh_route_rng'] = r
	return r


def _offh_set_render_space(sid):
	"""Make the engine RENDER space `sid` by pointing the camera at it. The
	HSPACE diagnostic proved rendering follows camera.spaceID /
	BigWorld.cameraSpaceID (the hangar renders its own space this way), NOT the
	read-only _offh_bspace(). Tries both; guarded."""
	import BigWorld
	try:
		if hasattr(BigWorld, 'cameraSpaceID'):
			BigWorld.cameraSpaceID(sid)
	except Exception:
		pass
	try:
		_c = BigWorld.camera()
		if _c is not None:
			_c.spaceID = sid
	except Exception:
		pass


def _offh_safe_purge():
	"""WG-style resource wipe in the loading-screen 'no man's land'. Called
	BETWEEN g_hangarSpace.destroy() and init() on battle exit: the battle is
	torn down and the hangar is DESTROYED (not re-inited yet), so NOTHING
	references the map/tank resources -> ResMgr.purge is safe here. Doing it
	while the hangar/tanks were still LIVE (e.g. at battle start) froze the
	engine. Draw is forced off first (as WG does). Gated by resmgr_purge; the
	START line is flushed to disk so a freeze is pinpointed in the log."""
	try:
		from _constants import CONFIG_OPTIONS as _CFG_PG
		_do_purge = bool(_CFG_PG.get('resmgr_purge', False))
		_do_reload = bool(_CFG_PG.get('reload_textures', False))
		_do_deepgc = bool(_CFG_PG.get('deep_gc', True))
	except Exception:
		_do_purge = False
		_do_reload = False
		_do_deepgc = False
	if not (_do_purge or _do_reload or _do_deepgc):
		return
	import BigWorld
	try:
		BigWorld.worldDrawEnabled(False)
	except Exception:
		pass
	try:
		from gui.mods.offhangar.logging import LOG_DEBUG as _pl
	except Exception:
		_pl = lambda *a: None
	# reloadTextures: reloads the GRAPHICS texture cache from LOCAL disk files.
	# ResMgr.purge is the wrong tool (map textures live in Moo, not DataSections)
	# and global purge freezes on the offline reload. reloadTextures is a local
	# graphics op - should free the dead map-texture residual without hanging.
	if _do_reload:
		try:
			if hasattr(BigWorld, 'reloadTextures'):
				_pl('OfflineBattle.reloadTextures START (if this is the LAST log line, it FROZE - set reload_textures:false)')
				try: BigWorld.flushPythonLog()
				except Exception: pass
				BigWorld.reloadTextures()
				_pl('OfflineBattle.reloadTextures done')
		except Exception:
			pass
	if _do_purge:
		try:
			import ResMgr as _rmg
			if hasattr(_rmg, 'purge'):
				_pl('OfflineBattle.safe_purge START (if this is the LAST log line, purge FROZE - set resmgr_purge:false)')
				try: BigWorld.flushPythonLog()
				except Exception: pass
				try:
					if hasattr(BigWorld, 'clearAllSpaces'):
						BigWorld.clearAllSpaces()
				except Exception:
					pass
				_pl('OfflineBattle.safe_purge clearAllSpaces done, now purging')
				try: BigWorld.flushPythonLog()
				except Exception: pass
				try:
					_rmg.purge()
				except TypeError:
					try: _rmg.purge('', True)
					except Exception: pass
				_pl('OfflineBattle.safe_purge done')
		except Exception:
			pass
		pass
	# deep_gc: bypass the broken C++ ResMgr entirely - clearAllSpaces (works)
	# + aggressive Python GC to drop loose model/mock/closure refs that pin
	# C++ objects. Won't touch the Moo texture cache (not Python), but trims
	# the Python overhang between matches. Safe (no freeze).
	if _do_deepgc:
		try:
			if hasattr(BigWorld, 'clearAllSpaces'):
				BigWorld.clearAllSpaces()
		except Exception:
			pass
		try:
			import sys as _sys
			if hasattr(_sys, 'exc_clear'):
				_sys.exc_clear()
		except Exception:
			pass
		try:
			_pl('OfflineBattle.deep_gc START')
		except Exception:
			pass
	try:
		import gc as _gp
		_gp.collect()
		try: _gp.collect(2)
		except Exception: _gp.collect()
	except Exception:
		pass
	# Re-enable drawing so the re-inited hangar renders (the ESC path does not
	# turn it back on itself).
	try:
		BigWorld.worldDrawEnabled(True)
	except Exception:
		pass


def _offh_dump_purge_apis():
	"""DEBUG-ONLY: dump the real docs/signatures of the resource + TEXTURE APIs
	so we can find the RIGHT texture-cache flush (ResMgr.purge is the wrong tool:
	map textures live in the graphics/Moo cache, not ResMgr DataSections, and
	global purge freezes on the offline reload). Grep the log for 'PURGEAPI:'."""
	try:
		from gui.mods.offhangar.logging import _DBG as _d
		if not _d[0]:
			return
	except Exception:
		return
	import BigWorld
	def _L(*a):
		try:
			from gui.mods.offhangar.logging import LOG_DEBUG as _ld
			_ld('PURGEAPI:', *a)
		except Exception:
			pass
	try:
		import ResMgr
		_L('ResMgr attrs', [n for n in dir(ResMgr) if not n.startswith('__')])
		_L('ResMgr.purge doc', repr(getattr(getattr(ResMgr, 'purge', None), '__doc__', None)))
	except Exception as e:
		_L('ResMgr err', e)
	# Every BigWorld attr whose name hints at texture/memory/cache/reload, with docs.
	try:
		_kw = ('texture', 'reload', 'cache', 'memory', 'flush', 'purge', 'release', 'stream', 'mip')
		for _n in dir(BigWorld):
			if any(k in _n.lower() for k in _kw):
				try:
					_f = getattr(BigWorld, _n, None)
					_L('BW.' + _n, repr(getattr(_f, '__doc__', None))[:220])
				except Exception:
					pass
	except Exception as e:
		_L('BW dir err', e)
	try:
		import Moo
		_L('Moo attrs', [n for n in dir(Moo) if not n.startswith('__')])
	except Exception:
		_L('Moo: not importable')


try:
	import BigWorld as _bw_pa
	_bw_pa.callback(14.0, _offh_dump_purge_apis)
except Exception:
	pass


def _offh_dump_hangar_render(_state=[0]):
	"""DEBUG-ONLY: reveal HOW ClientHangarSpace renders its space (the proven
	'render an arbitrary space' pattern) so we can replicate it for battle maps
	without touching read-only _offh_bspace(). Retries every 5s until in the
	hangar, then dumps once: g_hangarSpace, its inner space object, its spaceID
	vs _offh_bspace(), every BigWorld space/render/camera API, and the camera.
	Grep the log for 'HSPACE:'."""
	try:
		from gui.mods.offhangar.logging import _DBG as _d
		if not _d[0]:
			return
	except Exception:
		return
	import BigWorld
	def _L(*a):
		try:
			from gui.mods.offhangar.logging import LOG_DEBUG as _ld
			_ld('HSPACE:', *a)
		except Exception:
			pass
	# In the hangar the ClientHangarSpace inner space object exists. (player.arena
	# is NOT usable to detect hangar - the offline account returns an arena STUB
	# always, never None. That bug made this never fire.) Retry until the hangar
	# space is up.
	try:
		_pl = BigWorld.player()
	except Exception:
		_pl = None
	_hangar_up = False
	try:
		from gui.Scaleform.utils.HangarSpace import g_hangarSpace as _hs0
		_hangar_up = _hs0 is not None and getattr(_hs0, '_HangarSpace__space', None) is not None
	except Exception:
		_hangar_up = False
	if (_pl is None or not _hangar_up) and _state[0] < 20:
		_state[0] += 1
		try: BigWorld.callback(5.0, _offh_dump_hangar_render)
		except Exception: pass
		return
	# In hangar (or gave up waiting after ~100s) -> dump anyway for data.
	_L('hangar_detected', _hangar_up, 'retries', _state[0])
	_L('=== IN HANGAR - dumping render/space mechanism ===')
	try:
		_L('player', _pl.__class__.__name__, '_offh_bspace()', getattr(_pl, 'spaceID', None))
	except Exception as e:
		_L('player err', e)
	try:
		from gui.Scaleform.utils.HangarSpace import g_hangarSpace as _hs
		_L('g_hangarSpace type', _hs.__class__.__name__ if _hs else None)
		try:
			for _k, _v in _hs.__dict__.items():
				_L('  hs.'+str(_k), '=', repr(_v)[:140])
		except Exception as e:
			_L('hs dict err', e)
		_sp = getattr(_hs, '_HangarSpace__space', None)
		for _cand_attr in ('_HangarSpace__space', 'space', '_HangarSpace__spaceInited'):
			try: _L('  hs.'+_cand_attr, repr(getattr(_hs, _cand_attr, 'NONE'))[:140])
			except Exception: pass
		if _sp is not None:
			_L('inner space type', _sp.__class__.__name__)
			try:
				_L('  space dir', [n for n in dir(_sp) if not n.startswith('__')])
			except Exception: pass
			try:
				for _k, _v in _sp.__dict__.items():
					_L('  space.'+str(_k), '=', repr(_v)[:140])
			except Exception as e:
				_L('space dict err', e)
	except Exception as e:
		_L('g_hangarSpace err', e)
	try:
		_kw = ('space', 'world', 'render', 'active', 'camera', 'draw', 'scene', 'geometry')
		_L('BW space/render APIs', [n for n in dir(BigWorld) if any(k in n.lower() for k in _kw)])
	except Exception as e:
		_L('BW dir err', e)
	try:
		_cam = BigWorld.camera()
		_L('camera type', _cam.__class__.__name__ if _cam else None)
		if _cam is not None:
			_L('  camera space-ish attrs', [n for n in dir(_cam) if 'space' in n.lower()])
			for _a2 in ('spaceID', 'space'):
				try: _L('  camera.'+_a2, getattr(_cam, _a2, 'NONE'))
				except Exception: pass
	except Exception as e:
		_L('camera err', e)
	_L('=== dump done ===')


try:
	import BigWorld as _bw_hs_sched
	_bw_hs_sched.callback(8.0, _offh_dump_hangar_render)
except Exception:
	pass


def _offh_bot_pool(cand, tier, max_unique=None):
	"""Return a SMALL, STABLE set of bot vehicle descriptors for this tier,
	cached across battles in g_offh_bot_pool. Bots otherwise pick ~30 fresh
	RANDOM tanks every battle; the process-wide vehicle texture cache never
	frees, so unlimited variety made the hangar baseline RAM climb battle by
	battle until the next map load OOM-crashed the 32-bit client (native
	0xC0000005 read@0x14). Reusing a fixed pool loads those textures ONCE.
	Variety is tunable via config 'bot_variety' (0 = old unlimited)."""
	if not cand:
		return cand
	if max_unique is None:
		try:
			from _constants import CONFIG_OPTIONS as _CFG_BV
			max_unique = int(_CFG_BV.get('bot_variety', 8))
		except Exception:
			max_unique = 8
	if max_unique <= 0:
		return cand
	pool = globals().setdefault('g_offh_bot_pool', {})
	key = int(tier)
	if key not in pool:
		import random as _r
		n = min(max_unique, len(cand))
		try:
			pool[key] = _r.sample(cand, n)
		except Exception:
			pool[key] = [_r.choice(cand) for _x in range(n)]
	return pool[key]


def _offh_veh_excluded(v):
	"""Bots skip removed/hidden tanks: WG tags pulled vehicles 'secret' (e.g.
	usa:T23, removed from the 0.8.2 tree). Data-driven so any future removed
	tank drops out of the bot pool automatically."""
	try:
		_t = v['tags']
	except Exception:
		_t = ()
	if 'secret' in _t:
		return True
	try:
		if v['name'] == 'usa:T23':
			return True
	except Exception:
		pass
	return False


def _offh_battle_sweep(tag='exit'):
	# Full post-battle cleanup. Without it every battle leaves wrecks,
	# global models, FMOD events and the mapped battle space behind;
	# after a few battles the 32-bit client dies out-of-memory while
	# loading a map or the hangar (malloc NULL -> native write@0).
	# v2: staged + ALWAYS logs one line, failures log stage+traceback.
	# First: flush the perf rows still sitting in memory and put the GUI back if
	# the A/B experiment had it hidden. Done before any of the cleanup below, so
	# a failure in there cannot cost the measurement of the battle that just ran.
	try:
		_PROBE.stop()
	except Exception:
		pass
	import BigWorld
	global g_offline_models, g_offline_enemies
	try:
		import gui.mods.offhangar.logging as _swlog
	except Exception:
		_swlog = None
	# Adopted layout caches: one entry per vehicle type and configuration, plus a
	# geometry probe cache. They are keyed by type, not by battle, so on a client
	# with ~2 GB of address space they must not ride along from map to map.
	try:
		import sys as _swsys
		_ihl = _swsys.modules.get('gui.mods.offhangar.internal_hit_layouts')
		if _ihl is not None and hasattr(_ihl, '_LAYOUT_CACHE'):
			_ihl._LAYOUT_CACHE.clear()
		_ig = _swsys.modules.get('gui.mods.offhangar.internal_geometry')
		if _ig is not None and hasattr(_ig, 'clear_cache'):
			_ig.clear_cache()
	except Exception:
		pass
	_stage = 'init'
	_n_models = 0
	_n_mocks = 0
	_fail = None
	_mem_before = _offh_proc_mem_mb()
	try:
		_n_models = len(g_offline_models or [])
		_mvd = globals().get('G_MOCK_VEHICLES', {}) or {}
		_n_mocks = len(_mvd)
		_stage = 'mocks'
		for _m in list(_mvd.values()):
			try:
				# Detach the engine-exhaust Pixie systems (native particles):
				# unreleased they leak past the battle into the hangar.
				try: _stop_engine_exhaust(_m)
				except Exception: pass
				for _sa in ('_snd_engine', '_snd_tracks'):
					try:
						_s = getattr(_m, _sa, None)
						if _s is not None:
							_s.stop()
						setattr(_m, _sa, None)
					except Exception:
						pass
				try:
					if getattr(_m, 'bw_entity', None) is not None:
						_m.bw_entity.model = None
						_m.bw_entity = None
				except Exception:
					pass
				try:
					# entity-owned chassis: ent.model=None above already released it;
					# delModel on it always raised (pending!) 'Not added as a global
					# model' - the very bomb this sweep kept tripping over.
					_m._chassis_model = None
				except Exception:
					pass
			except Exception:
				pass
		_stage = 'mockdict'
		globals()['G_MOCK_VEHICLES'] = {}
		globals()['g_offh_exhaust_owners'] = []
		_stage = 'models'
		try:
			# Unregister always-update FIRST. The list is drained again later, but by
			# then these models are gone - calling delAlwaysUpdateModel on a deleted
			# model is exactly the dangling-reference case that crashes the next load.
			for _aum0 in list(globals().get('g_offh_always_update_models', []) or []):
				try:
					BigWorld.delAlwaysUpdateModel(_aum0)
				except Exception:
					pass
			globals()['g_offh_always_update_models'] = []
			# Clear the list BEFORE the loop: BigWorld.delModel leaves a PENDING
			# C error that this build only raises at the loop's EXHAUSTION (the
			# final FOR_ITER checks PyErr and finds it). If the clear sits AFTER
			# the loop it gets skipped, and the battle's models - including the
			# player's WRECK - leak into the next battle.
			_gm_list = list(g_offline_models or [])
			g_offline_models = []
			for _gm in _gm_list:
				_offh_del_model(_gm)
		except:
			pass
		_stage = 'enemies'
		try:
			g_offline_enemies = []
		except Exception:
			pass
		_stage = 'sounds'
		_es = globals().get('g_offh_engine_state')
		if _es is not None:
			for _k in ('snd1', 'snd2'):
				try:
					if _es.get(_k) is not None:
						_es[_k].stop()
					_es[_k] = None
				except Exception:
					pass
		_offh_stop_battle_music()
		_stage = 'voicenotif'
		# Crew-voice engine: destroy per battle, on EVERY exit path. The
		# instances live on persistent objects (account / module-global AIH);
		# left alive, a voice line active at exit keeps talking into the
		# hangar, and its never-ending 'voice' queue entry mutes all crew
		# voices for the rest of the session.
		try:
			_pl = BigWorld.player()
			_sn = getattr(_pl, 'soundNotifications', None) if _pl is not None else None
			if _sn is not None:
				try:
					_sn.destroy()
				except Exception:
					pass
				try:
					del _pl.soundNotifications
				except Exception:
					pass
		except Exception:
			pass
		try:
			_ga = globals().get('g_offline_aih')
			_sn2 = getattr(_ga, '_snd_notif', None) if _ga is not None else None
			if _sn2 is not None:
				try:
					_sn2.destroy()
				except Exception:
					pass
				try:
					del _ga._snd_notif
				except Exception:
					pass
		except Exception:
			pass
		# Same for the separate crew/module queue. It hangs off the MODULE, so it
		# outlives the battle by definition: battle start only drops the reference
		# (leaving a live instance with a half-spoken line still talking into the
		# hangar), it never stopped the sound.
		try:
			_sn3 = globals().pop('g_offh_crew_notif', None)
			if _sn3 is not None:
				try:
					_sn3.destroy()
				except Exception:
					pass
		except Exception:
			pass
		_stage = 'projectile'
		try:
			_pm = globals().get('g_projectile_mover')
			if _pm is not None:
				try:
					_pm.destroy()
				except Exception:
					pass
				globals()['g_projectile_mover'] = None
		except Exception:
			pass
		_stage = 'destr'
		try:
			import AreaDestructibles
			# Stop the falling-body animator FIRST: a tree mid-fall at battle exit
			# leaves g_destructiblesAnimator's __updateCallback scheduled; it then
			# fires in the HANGAR against the released battle space ->
			# __launchFallEffect -> getDestructibleDesc(self.__spaceID=dead, ...) ->
			# native "argument 1 must be set to an int". clear() -> __stopUpdate()
			# cancels the BigWorld.callback. Manager.clear() alone did NOT (the
			# callback lives on the animator, a SEPARATE global) - that was the
			# "trees stop falling after a certain number of battles" report.
			_an = getattr(AreaDestructibles, 'g_destructiblesAnimator', None)
			if _an is not None and hasattr(_an, 'clear'):
				_an.clear()
			if getattr(AreaDestructibles, 'g_destructiblesManager', None) is not None:
				AreaDestructibles.g_destructiblesManager.clear()
		except Exception:
			pass
		_stage = 'effects'
		try:
			_pl = BigWorld.player()
			if _pl is not None and getattr(_pl, 'terrainEffects', None) is not None:
				try:
					_pl.terrainEffects.destroy()
				except Exception:
					pass
				try:
					_pl.terrainEffects = None
				except Exception:
					pass
		except Exception:
			pass
		_stage = 'muzzle'
		try:
			_pl = BigWorld.player()
			if _pl is not None:
				# Drop the battle descriptor so the hangar's vehicleTypeDescriptor
				# override falls back to its stub instead of the last battle's tank
				# (set at spawn for the native penetration marker).
				try: _pl._offhangar_td = None
				except Exception: pass
				# The swinging fashion is the SAME object as the chassis' wg_fashion, which
				# the sweep deletes with the model. Parking it on the persistent account and
				# leaving it there meant the hangar loaded on top of a dangling native
				# handle: 0xC0000005 Read@0x8 during loadHangarSpaceVehicle, right after a
				# battle that had itself run fine. Drop the reference here.
				try: _pl._offhangar_swinging = None
				except Exception: pass
				# The muzzle EffectsListPlayer lives on the persistent account:
				# unreleased it survives into the hangar and battle 2 would
				# replay battle 1's gun effects.
				_mzp = getattr(_pl, '_offhangar_muzzle_player', None)
				if _mzp is not None:
					try:
						_mzp.stop()
					except Exception:
						pass
					_pl._offhangar_muzzle_player = None
				try:
					_smap = getattr(_pl, '_offhangar_sticker_map', None)
					if _smap:
						_smap.clear()
				except Exception:
					pass
			# Always-update models are pinned by the ENGINE (strong native ref
			# + per-frame animation): without delAlwaysUpdateModel one gun
			# model per battle stays animated forever, hangar included.
			for _aum in list(globals().get('g_offh_always_update_models', []) or []):
				try:
					BigWorld.delAlwaysUpdateModel(_aum)
				except Exception:
					pass
			globals()['g_offh_always_update_models'] = []
		except Exception:
			pass
		_stage = 'snipercam'
		if tag != 'start':
			# Leave sniper/strategic BEFORE the teardown so the control mode's own
			# disable() actually runs. SniperCamera.disable() cancels its auto-update
			# callback, restores the zoomed FOV natively (__applyFOV(self.__fov)) and
			# re-shows the vehicle; skipping it leaves that callback firing forever in
			# the garage, and a display-device recreate - alt-tab, a resolution change -
			# then still has a live SniperCamera to call onRecreateDevice on, which
			# RE-APPLIES the zoom and puts the garage straight back to the stuck-zoom
			# the FOV write below exists to prevent. The player-death path already does
			# exactly this switch, for exactly this reason.
			try:
				_swaih = getattr(BigWorld.player(), 'inputHandler', None)
				_swctrl = getattr(_swaih, 'ctrl', None) if _swaih is not None else None
				if _swctrl is not None and _swctrl.__class__.__name__ != 'ArcadeControlMode':
					_swaih.onControlModeChanged('arcade')
			except Exception:
				pass
		try:
			# Restore the original __cameraUpdate: the per-battle patch closure
			# pins mock_veh/loaded_models (a full tank model set) through the
			# hangar; battle start re-patches with fresh refs anyway.
			import AvatarInputHandler.cameras as _swcams
			_swo = getattr(_swcams.SniperCamera, '_orig_cam_update', None)
			if _swo is not None:
				_swcams.SniperCamera._SniperCamera__cameraUpdate = _swo
		except Exception:
			pass
		try:
			# SniperCamera.disable() is what normally restores the zoomed FOV
			# (cameras.py: __applyFOV(self.__fov)). Our exit path nulls inputHandler and
			# destroys the control modes, so it never runs - and the GARAGE then rendered
			# with the zoomed projection still applied. Put the captured FOV back.
			_fov0 = globals().get('g_offh_base_fov')
			if _fov0:
				BigWorld.projection().fov = _fov0
			# Drop the postmortem colour grading too, or the GARAGE renders desaturated -
			# exactly the mistake the zoomed-FOV bug above already made once.
			try:
				from post_processing import g_postProcessing as _offh_pp2
				_offh_pp2.disable()
			except Exception:
				pass
		except Exception:
			pass
		_stage = 'decals'
		try:
			# shell holes / track marks accumulate in native decal buffers
			if hasattr(BigWorld, 'wg_clearDecals'):
				BigWorld.wg_clearDecals()
		except Exception:
			pass
		# reuse_map_space (OFF by default) KEEPS the battle map mapped between
		# battles so a same-map battle reuses it instead of freeing +
		# re-allocating ~700 MB. That is cheaper for the 32-bit address space,
		# but it does NOT hold up in practice: with the mapping kept alive and
		# re-mapped onto the SAME space id, the next battle was observed to keep
		# showing the PREVIOUS map - so the map roll had no visible effect and
		# every battle looked identical. Map variety wins over the allocation
		# saving, so this ships off and full_space_release (a genuinely fresh
		# space per battle) does the work instead. Dynamics are cleared by the
		# 'models' + 'entcache' stages either way.
		_stage = 'space'
		try:
			from _constants import CONFIG_OPTIONS as _CFG_RM2
			# Fallbacks match config_defaults.json. reuse_map_space is OFF: keeping
			# the mapping alive and re-mapping onto the SAME space id was observed
			# to keep serving the previous map, so the next battle never actually
			# changed maps. A fresh space per battle (full_space_release) is what
			# loads the rolled map reliably.
			_reuse_sp = bool(_CFG_RM2.get('reuse_map_space', False))
			_full_rel = bool(_CFG_RM2.get('full_space_release', True))
		except Exception:
			_reuse_sp = False
			_full_rel = True
		try:
			if _full_rel:
				# Full teardown: unmap + clearSpace + RELEASESPACE (frees the
				# chunk/terrain RAM; clearSpace only clears contents) + forced gc.
				_sid = globals().get('g_offh_battle_space', 0) or 0
				if _sid:
					# Stop the per-frame render pin so the tick stops forcing the
					# camera onto this space. Do NOT move the camera here (setting
					# it to the empty account space crashed on hangar load); the
					# hangar restore points the camera at its own space, and the
					# space is released only LATER (deferred callback) once it is
					# fully orphaned.
					globals()['g_offh_full_release'] = False
					_mh = globals().get('g_offh_battle_mapping')
					if _mh is not None:
						try:
							BigWorld.delSpaceGeometryMapping(_sid, _mh)
							len('')
						except:
							pass
					try:
						BigWorld.clearSpace(_sid)
					except:
						pass
					globals()['g_offh_battle_mapping'] = None
					globals()['g_offh_mapped_handle'] = None
					globals()['g_offh_mapped_name'] = None
					globals()['g_offh_mapped_space'] = 0
					globals()['g_offh_battle_space'] = 0
					# Defer releaseSpace to AFTER entcache destroys this space's
					# OfflineEntity bots + AreaDestructibles: releaseSpace on a
					# space that still holds entities does NOT fully free its
					# chunk/terrain RAM (measured: freed 0 in-sweep, baseline
					# still crept +700 MB/battle).
					globals()['g_offh_pending_release'] = _sid
			elif not _reuse_sp:
				_sid = globals().get('g_offh_battle_space', 0) or 0
				if _sid:
					# clearSpace alone never returns the chunk/terrain resources
					# (+600 MB); the mapping must be deleted explicitly.
					_mh = globals().get('g_offh_battle_mapping')
					if _mh is not None:
						try:
							BigWorld.delSpaceGeometryMapping(_sid, _mh)
							len('')
						except:
							pass
						globals()['g_offh_battle_mapping'] = None
						globals()['g_offh_mapped_handle'] = None
						globals()['g_offh_mapped_name'] = None
						globals()['g_offh_mapped_space'] = 0
					BigWorld.clearSpace(_sid)
					globals()['g_offh_battle_space'] = 0
		except Exception:
			pass
		# NOTE: ResMgr.purge(mapPath) was tried here and MEASURED freeing ~0 MB
		# across battles - it only drops DataSection descriptors (KB), not the
		# loaded chunk textures/geometry (MB) that the async chunk ejection from
		# clearSpace above owns. Removed as dead weight. The residual per-battle
		# baseline climb (405->654->766 MB) is the process-wide vehicle texture
		# cache (~30 RANDOM tanks/battle, each cached and never released); a
		# GLOBAL ResMgr.purge would free it but froze the engine on the next
		# tank load, so it stays. Bot spawn is capped (max_total_bots) so a
		# single battle cannot pile enough tanks to OOM on its own.
		# clearSpace PARKS leaving entities in an engine-side cache instead
		# of destroying them: 30 OfflineEntity + the AreaDestructibles chunk
		# entities pile up there EVERY battle, pinning their resources.
		_stage = 'entcache'
		try:
			# OfflineEntity bots + AreaDestructibles live in BigWorld.entities,
			# NOT cachedEntities() (empty offline: logged "destroyed 0" EVERY
			# battle = the leak, bw_entities climbed 2->270 and never freed).
			# Iterate the real dict (the wrapper delegates .items() to it and
			# excludes injected mocks); the class filter below keeps hangar/
			# account ghosts safe.
			_ce = getattr(BigWorld, 'cachedEntities', None)
			if callable(_ce):
				_ce = _ce()
			if not _ce:
				_ce = getattr(BigWorld, 'entities', None)
			_pid = 0
			try:
				_pid = getattr(BigWorld.player(), 'id', 0) or 0
			except:
				pass
			_cids = []
			try:
				# only OUR battle entity types - never touch hangar/account ghosts
				for _k2, _v2 in list(_ce.items()):
					try:
						if _v2.__class__.__name__ in ('OfflineEntity', 'AreaDestructibles'):
							_cids.append(_k2)
					except:
						pass
			except:
				pass
			_ndest = 0
			for _cid in _cids:
				if not _cid or _cid == _pid:
					continue
				try:
					BigWorld.destroyEntity(_cid)
					len('')
					_ndest += 1
				except:
					pass
			if _swlog is not None:
				try:
					_swlog.LOG_DEBUG('sweep: destroyed %d/%d battle entities (OfflineEntity+AreaDestructibles)' % (_ndest, len(_cids)))
				except:
					pass
		except:
			pass
		# Deferred full_space_release: entcache just destroyed this space's
		# OfflineEntity bots + AreaDestructibles, so it is now EMPTY and
		# releaseSpace can actually return its chunk/terrain RAM (it no-oped
		# earlier when entities were still parked in it).
		# Deferred full_space_release: the emptied battle space is released at
		# the START of the NEXT battle (stable context, space fully orphaned);
		# releasing in-sweep crashed on hangar load and a transition callback
		# never fired. g_offh_pending_release (set in the space stage) holds it.
		_stage = 'release'
		_stage = 'dastate'
		try:
			import gui.mods.offhangar.destructibles_authority as _dam
			# reset() keeps the dict SHAPE (spaceID/chunks/entities keys);
			# _state.clear() removed them and every destructible call the
			# next battle then KeyError'd, killing all destruction.
			_reset = getattr(_dam, 'reset', None)
			if callable(_reset):
				_reset()
			else:
				_dst = getattr(_dam, '_state', None)
				if isinstance(_dst, dict):
					_dst['spaceID'] = None
					_dst['chunks'] = {}
					_dst['entities'] = set()
		except:
			pass
		# ResMgr.purge is UNSAFE here: DataSections are PROCESS-wide shared
		# (items.vehicles g_cache holds refs for the whole session); purging
		# them under its feet froze the engine on the next tank load.
		_stage = 'resmgr_skipped'
		# The dead battle scopes are reference CYCLES (closure<->cell<->
		# function); collect twice to drain cascades that the first pass
		# only unpins.
		_stage = 'gc'
		try:
			import gc as _gc
			_gc.collect()
			_gc.collect()
		except:
			pass
		# Engine memory report into the log (debug only): if anything still
		# holds megabytes after this sweep, the next log names it.
		_stage = 'memstats'
		try:
			if _swlog is not None and getattr(_swlog, '_DBG', [False])[0]:
				BigWorld.outputMemoryStats()
		except:
			pass
		if not globals().get('g_offh_apis_logged'):
			globals()['g_offh_apis_logged'] = True
			try:
				import ResMgr as _rmx
				_kw = ('flush', 'purge', 'cache', 'clear', 'release', 'texture', 'memory', 'reuse')
				_names = [_n for _n in dir(BigWorld) if any((_k in _n.lower()) for _k in _kw)]
				if _swlog is not None:
					_swlog.LOG_DEBUG('BW-APIs: %s' % (_names,))
					_swlog.LOG_DEBUG('ResMgr-APIs: %s' % (dir(_rmx),))
			except:
				pass
		_stage = 'done'
		# bare: BigWorld/FMOD can raise old-style exceptions that do NOT
		# inherit from Exception - 'except Exception' misses them.
	except:
		try:
			import traceback as _swtb
			_fail = _swtb.format_exc()
		except Exception:
			_fail = 'trace unavailable'
	_mem_after = _offh_proc_mem_mb()
	if _swlog is not None:
		try:
			if _fail is not None:
				_swlog.LOG_DEBUG('OfflineBattle.sweep(%s) FAILED at stage %s: %s' % (tag, _stage, _fail))
			_swlog.LOG_DEBUG('OfflineBattle.sweep(%s): freed models=%d mocks=%d stage=%s | rss %d->%d virt %d->%d commit %d->%d MB (freed rss %d virt %d) [virt = 32-bit ~2GB wall]' % (
				tag, _n_models, _n_mocks, _stage,
				_mem_before[0], _mem_after[0], _mem_before[1], _mem_after[1], _mem_before[2], _mem_after[2],
				_mem_before[0] - _mem_after[0], _mem_before[1] - _mem_after[1]))
		except Exception:
			pass
		try:
			_offh_gc_census_line(tag)
		except Exception:
			pass

import BigWorld
try:
	from projectilemover import ProjectileMover
	def _safe_calc(self, r0, v0, gravity, isOwnShoot, tracerCameraPos):
		'''Where the TRACER ends, and how long it takes to get there.

		This used to cast a STRAIGHT ray along v0 and hand that back as the end
		point - no gravity at all. ProjectileMover.add then feeds the result to
		__calcStartVelocity, which solves for the velocity that carries the shell
		from the muzzle to that end point IN THAT TIME under gravity: to reach a
		point that is already too high AND pay for the drop on the way, it has to
		launch higher still. So the visible round climbed well above the true
		trajectory and above the crosshair, while the damage - resolved separately
		on the real parabola - landed correctly. "5/5 went above my crosshair."

		Now walks the same arc the shot does. Note `gravity` arrives as the Vector3
		(0, -g, 0) that add() builds, not the scalar it was given.'''
		import BigWorld, Math
		_n = v0.length
		if _n <= 0.0001:
			return (r0 + Math.Vector3(0.0, 0.0, 100.0), 0.1)
		try:
			_g = abs(float(getattr(gravity, 'y', gravity)))
		except Exception:
			_g = 9.81
		# Skip the first few metres so the walk cannot self-hit at the muzzle
		# (a length-0 result made ProjectileMover.add drop the tracer entirely).
		_skip = 3.0
		_start = r0 + v0.scale(_skip / _n)
		_walk = _offh_shell_path(_offh_bspace(), _start, v0, _g, 2000.0, None, 0.1, 120)
		hitPoint = _walk['pos']
		_d = _skip + _walk['dist']
		try:
			_gn = globals().get('_offh_tcalc_n', 0) + 1
			globals()['_offh_tcalc_n'] = _gn
			if _gn % 10 == 1:
				LOG_DEBUG('TCALC hit=%s dist=%.1f g=%.2f own=%s' % (hitPoint, _d, _g, isOwnShoot))
		except Exception: pass
		if _d < 0.01:
			end = r0 + v0.scale(2000.0 / _n)
			return (end, (end - r0).length / _n)
		return (hitPoint, _d / _n)
	ProjectileMover._ProjectileMover__calcTrajectory = _safe_calc
	# Online the shell is hidden for the first 50 ms because the server's showTracer
	# arrives late and it would otherwise pop out of the barrel. Offline it spawns at
	# the muzzle immediately, so those 50 ms are pure loss - at ~1000 m/s that is the
	# first ~50 m, i.e. most of a bot engagement. Show it from the first tick.
	ProjectileMover._ProjectileMover__PROJECTILE_HIDING_TIME = 0.0
	g_projectile_mover = ProjectileMover()
except Exception as e:
	LOG_DEBUG('Could not init ProjectileMover:', e)
	g_projectile_mover = None

# Decal crash guard. Some effect lists reference decal groups/textures this
# 0.8.2 client never registers ('slow' group, 'explosion' texture). Native
# wg_addDecal RAISES on the unknown group, aborting EffectsList.attachTo
# halfway - the half-built effect state then kills the client with a native
# access violation (crash logs: "addDecal - invalid <groupName>" immediately
# before EXCEPTION_ACCESS_VIOLATION). Unknown textures resolve to index -1.
# Skip such decals silently; every other effect in the list still plays.
try:
	from helpers import EffectsList as _EL0
	from helpers import DecalMap as _DM0
	if not getattr(_EL0._DecalEffectDesc, '_offh_safe_create', False):
		_orig_decal_create = _EL0._DecalEffectDesc.create
		def _offh_safe_decal_create(self, model, list, args, _orig=_orig_decal_create):
			try:
				_texmap = getattr(_DM0.g_instance, '_DecalMap__texMap', None)
				if _texmap is not None and self._texName not in _texmap:
					return  # unknown texture: skip without LOG_ERROR spam
				return _orig(self, model, list, args)
			except Exception:
				return  # unknown decal group: native addDecal rejected it
		_EL0._DecalEffectDesc.create = _offh_safe_decal_create
		_EL0._DecalEffectDesc._offh_safe_create = True
	# Same protection for particle systems: a Pixie can embed a DECAL
	# RENDERER bound to a decal group ('slow' scorch marks). Attaching one
	# validates the group natively and RAISES when it is not registered on
	# this client (crash logs: _PixieEffectDesc.create -> _findTargetNode ->
	# 'addDecal - invalid <groupName>'), aborting attachTo halfway. Skip
	# just that pixie; the rest of the effect list still plays.
	if not getattr(_EL0._PixieEffectDesc, '_offh_safe_create', False):
		_orig_pixie_create = _EL0._PixieEffectDesc.create
		def _offh_safe_pixie_create(self, model, list, args, _orig=_orig_pixie_create):
			try:
				return _orig(self, model, list, args)
			except Exception as _pe:
				# A pixie authored at a hardpoint the target model does not have.
				# The lingering flames of the destruction effect sit at HP_Fire_1,
				# which exists on a vehicle HULL - but _play_death_effect deliberately
				# plays destruction in WORLD space through terrainEffects.addNew (the
				# wreck swap detaches the live hull and would cut the effect short),
				# and StaticSceneBoundEffects.addNew builds a bare newFakeModel() that
				# has only 'Scene Root'. So the explosion played and the fire never
				# could: destroyed tanks simply did not burn. An EMPTY node path makes
				# _findTargetNode fall back to 'Scene Root', which every model has.
				#
				# Restricted to that one error on purpose. The OTHER failure this guard
				# exists for is an unregistered decal group, whose native addDecal must
				# NOT be re-entered - doing so kills the client.
				if 'No node named' in str(_pe):
					try:
						_a2 = dict(args)
						_a2['position'] = ('', None)
						return _orig(self, model, list, _a2)
					except Exception:
						pass
				return
		_EL0._PixieEffectDesc.create = _offh_safe_pixie_create
		_EL0._PixieEffectDesc._offh_safe_create = True
except Exception:
	LOG_DEBUG('Decal guard install failed')


# shotResult -> shotEffects group, exactly as Vehicle.showDamageFromShot maps
# it: 0 ricochet, 1 non-penetration, 2/3 penetration, 4 critical.
_HIT_EFFECT_GROUPS = ('armorRicochet', 'armorResisted', 'armorHit', 'armorHit', 'armorCriticalHit')


def _terrain_hit_material(spaceID, hit_point, dir_vec):
	"""Effect-material name at a terrain/wall impact - one of
	'ground'/'stone'/'wood'/'metal'/'snow'/'sand' - mirroring
	Vehicle.showDamageFromShot: read the surface matKind with
	wg_getMatInfoNearPoint and map it via VehicleAppearance
	.calcEffectMaterialIndex. Falls back to 'ground' (water is handled
	inside ProjectileMover.explode itself). calcEffectMaterialIndex returns
	-1 for untagged terrain offline - the player is an account, not a
	PlayerAvatar, so its arena-default branch is skipped - hence the clamp."""
	try:
		import BigWorld
		from material_kinds import EFFECT_MATERIALS
		seg_start = hit_point - dir_vec.scale(0.5)
		seg_end = hit_point + dir_vec.scale(0.5)
		matInfo = BigWorld.wg_getMatInfoNearPoint(spaceID, seg_start, seg_end, hit_point, lambda *a: False)
		matKind = matInfo[4] if (matInfo is not None and len(matInfo) > 4) else 0
		from VehicleAppearance import VehicleAppearance as _VA
		effIdx = _VA.calcEffectMaterialIndex(matKind)
		if effIdx is None or effIdx < 0 or effIdx >= len(EFFECT_MATERIALS):
			return 'ground'
		return EFFECT_MATERIALS[effIdx]
	except Exception:
		return 'ground'


def _offh_cfg_flag(name, default=True):
	"""One config lookup, cached in globals. Used by the per-frame marker path, so
	it must not re-read the file (or even re-import) every tick."""
	_key = '_offh_cfg_' + name
	_v = globals().get(_key)
	if _v is None:
		try:
			from _constants import CONFIG_OPTIONS as _C
			_v = bool(_C.get(name, default))
		except Exception:
			_v = bool(default)
		globals()[_key] = _v
	return _v


def _offh_shell_drop(dist_m, speed, gravity):
	"""Vertical drop, in metres, a shell suffers over dist_m of flight.
	Used to decide whether a straight ray is a good enough stand-in for the
	real parabola - for a 1000 m/s gun at 100 m it is 0.05 m, for a KV-2's
	howitzer at 400 m it is metres."""
	try:
		if speed <= 0.0:
			return 0.0
		t = float(dist_m) / float(speed)
		return 0.5 * abs(float(gravity)) * t * t
	except Exception:
		return 0.0


def _offh_shell_path(spaceID, start_pos, velocity, gravity, max_dist,
		mock_test=None, step=0.1, max_steps=100, trace=None):
	"""Walk a shell's real, gravity-bent path chord by chord.

	This is the mod's port of VehicleGunRotator.__getGunMarkerPosition. The
	client's own computeProjectileTrajectory splits every `step` seconds of
	flight into chords that stay within SHELL_TRAJECTORY_EPSILON_CLIENT of the
	true parabola; each chord is then collided in turn. A single straight ray
	from the muzzle is only correct for a gun with no drop, and the barrel is
	ELEVATED to compensate for that drop (getShotAngles) - so a straight ray
	along it always ends up above and beyond the point actually aimed at, by
	twice the drop. On a high-velocity gun that is centimetres; on a derp gun
	it is tens of metres.

	`mock_test(p1, p2)` is asked for the nearest mock-vehicle hit on a chord and
	must return (mock, colInfo) with colInfo[0] the distance from p1, else None.

	Returns a dict:
		'pos'   impact point, or the point max_dist along the arc
		'dir'   unit direction of the chord that ended the walk (impact angle)
		'dist'  path length flown to 'pos'
		'world' wg_collideSegment result of the static hit, else None
		'mock'  (mock, colInfo, hitPos, pathDist) of the tank struck, else None
	"""
	import BigWorld, Math
	from projectile_trajectory import computeProjectileTrajectory
	try:
		from constants import SHELL_TRAJECTORY_EPSILON_CLIENT as _EPS
	except Exception:
		_EPS = 0.15
	grav_v = Math.Vector3(0.0, -abs(float(gravity)), 0.0)
	prev_pos = start_pos
	prev_vel = velocity
	travelled = 0.0
	t_acc = 0.0
	last_dir = Math.Vector3(velocity)
	try:
		last_dir.normalise()
	except Exception:
		pass
	res = {'pos': start_pos, 'dir': last_dir, 'dist': 0.0, 'world': None, 'mock': None}
	steps = 0
	while steps < max_steps:
		steps += 1
		t_acc += step
		try:
			points = computeProjectileTrajectory(prev_pos, prev_vel, grav_v, step, _EPS)
		except Exception:
			points = [prev_pos + prev_vel.scale(step) + grav_v.scale(step * step * 0.5)]
		p1 = prev_pos
		for p2 in points:
			seg = p2 - p1
			seg_len = seg.length
			if seg_len <= 1e-06:
				continue
			seg_dir = Math.Vector3(seg)
			seg_dir.normalise()
			last_dir = seg_dir
			# Trim the last chord so the walk never reports a hit past max range -
			# but still collide the part of it that is in range.
			_trimmed = False
			if travelled + seg_len > max_dist:
				_trimmed = True
				p2 = p1 + seg_dir.scale(max(0.0, max_dist - travelled))
				seg_len = max(0.0, max_dist - travelled)
			veh = mock_test(p1, p2) if mock_test is not None else None
			veh_d = veh[1][0] if veh is not None else None
			try:
				wcol = BigWorld.wg_collideSegment(spaceID, p1, p2, 128)
			except Exception:
				wcol = None
			wld_d = (wcol[0] - p1).length if wcol is not None else None
			if trace is not None and len(trace) < 16:
				# Ground height straight under the chord's far end, so a spurious
				# collision (shell metres clear of the terrain, walk stopping anyway)
				# is visible as a hit with a large positive clearance.
				_gy = None
				try:
					_gp = BigWorld.wg_collideSegment(spaceID,
						Math.Vector3(p2.x, p2.y + 300.0, p2.z),
						Math.Vector3(p2.x, p2.y - 300.0, p2.z), 128)
					_gy = _gp[0].y if _gp is not None else None
				except Exception:
					_gy = None
				trace.append((len(trace), p1.y, p2.y, (p2 - start_pos).length, _gy,
					wld_d, veh_d))
			if veh_d is not None and (wld_d is None or veh_d <= wld_d):
				hit_pos = p1 + seg_dir.scale(veh_d)
				res['pos'] = hit_pos
				res['dir'] = seg_dir
				res['dist'] = travelled + veh_d
				res['mock'] = (veh[0], veh[1], hit_pos, travelled + veh_d)
				res['world'] = wcol
				return res
			if wcol is not None:
				res['pos'] = wcol[0]
				res['dir'] = seg_dir
				res['dist'] = travelled + wld_d
				res['world'] = wcol
				return res
			travelled += seg_len
			if _trimmed:
				res['pos'] = p2
				res['dir'] = seg_dir
				res['dist'] = travelled
				return res
			p1 = p2
		prev_pos = start_pos + velocity.scale(t_acc) + grav_v.scale(t_acc * t_acc * 0.5)
		prev_vel = velocity + grav_v.scale(t_acc)
	res['pos'] = prev_pos
	res['dir'] = last_dir
	res['dist'] = travelled
	return res


def _offh_sync_marker_health(mock, player_id=-1):
	"""Push a vehicle's CURRENT hit points onto a freshly created marker.

	A tank's marker is destroyed when it goes dark and built again when it is
	spotted, and a new marker starts at full health. Damage dealt while the tank
	was unspotted was therefore invisible twice over: nothing showed when it
	landed, because there was no marker to hang a number on, and the health bar
	came back reading whatever the tank had before it faded. It looked for all
	the world like the shot had done nothing - the report being "a shell already
	in flight when the target went dark does no damage". The damage was always
	applied; only every trace of it was thrown away."""
	try:
		from gui import WindowsManager
		_bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
		_vm = getattr(_bw, 'vMarkersManager', None) if _bw is not None else None
		_mk = getattr(mock, 'marker', None)
		if _vm is None or _mk in (None, -1):
			return
		_vm.onVehicleHealthChanged(_mk, max(0, getattr(mock, 'health', 0) or 0), player_id, 0)
	except Exception:
		pass


def _stop_engine_exhaust(mock):
	"""Detach + drop the engine-exhaust pixies from a tank (death / cleanup)."""
	try:
		pixies = getattr(mock, '_offhangar_exhaust', None)
		if not pixies:
			return
		for node, pixie in pixies:
			try:
				if node is not None:
					node.detach(pixie)
			except Exception:
				pass
		mock._offhangar_exhaust = None
	except Exception:
		pass


def _sync_engine_exhaust(mock, hull_model, td, speed=0.0):
	"""Engine-exhaust smoke for ANY tank (player or bot), reusing the stock
	Pixie exhaust from hull['exhaust'] (VehicleAppearance.__createExhaust):
	one particle system per exhaust node, attached to the hull, with the
	emission rate driven off speed (idle vs moving) like __changeExhaust.
	Created once per tank and cached on mock._offhangar_exhaust. Owners are
	tracked in g_offh_exhaust_owners so the battle sweep detaches every one
	(else the native Pixie systems leak past the battle)."""
	try:
		import Pixie
		if mock is None or hull_model is None or td is None:
			return
		if getattr(mock, 'health', 1) <= 0:
			_stop_engine_exhaust(mock)
			return
		hull = getattr(td, 'hull', None)
		exhaust = hull.get('exhaust') if isinstance(hull, dict) else None
		if not exhaust:
			return
		nodes = exhaust.get('nodes') or ()
		rates = exhaust.get('rates') or ()
		if not nodes or not rates:
			return
		pixies = getattr(mock, '_offhangar_exhaust', None)
		if pixies is None:
			# Resolve the pixie by engine tag, exactly like the stock appearance.
			engine = getattr(td, 'engine', None)
			etags = (engine.get('tags') if isinstance(engine, dict) else getattr(engine, 'tags', None)) or ()
			pixie_name = None
			for tag in etags:
				pixie_name = exhaust.get('pixie/' + tag, pixie_name)
			if not pixie_name:
				return
			pixies = []
			for i in xrange(len(nodes)):
				try:
					pixie = Pixie.create(pixie_name)
					pixie.drawOrder = 50 + i
					node = hull_model.node(nodes[i])
					node.attach(pixie)
					pixies.append((node, pixie))
				except Exception:
					pass
			mock._offhangar_exhaust = pixies
			# Tracked so the battle sweep can detach every tank's pixies.
			globals().setdefault('g_offh_exhaust_owners', []).append(mock)
		# Emission rate: idle when stopped, higher when moving; clamp to table.
		idx = 1 if abs(speed) < 0.5 else 2
		last = len(rates) - 1
		if idx > last:
			idx = last
		if idx < 0:
			idx = 0
		rate = rates[idx]
		for node, pixie in pixies:
			try:
				for si in xrange(pixie.nSystems()):
					pixie.system(si).action(1).rate = rate
			except Exception:
				pass
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _play_vehicle_hit_effect(shell, hit_pos, hit_dir, shot_result, is_player_target=False, target_mock=None):
	"""Play the armor hit / bounce / ricochet effect at a mock vehicle hit
	point. Mock tanks are not real collision geometry, so the ProjectileMover
	(which only collides against static geometry) never shows an impact on
	them. Replay the same shotEffects group the real Vehicle uses, in world
	space via player.terrainEffects (the same channel destructibles use).

	The fullscreen shockwave/flashbang default to ON in EffectsListPlayer, so
	they are only enabled when the PLAYER's own tank is hit - otherwise every
	bot-on-bot / player-on-bot hit would red-flash the whole screen."""
	try:
		import BigWorld, Math
		from items import vehicles
		player = BigWorld.player()
		te = getattr(player, 'terrainEffects', None)
		if te is None or shell is None or hit_pos is None:
			return
		idx = shell.get('effectsIndex') if isinstance(shell, dict) else getattr(shell, 'effectsIndex', None)
		if idx is None:
			return
		effectsDescr = vehicles.g_cache.shotEffects[idx]
		key = _HIT_EFFECT_GROUPS[max(0, min(int(shot_result), len(_HIT_EFFECT_GROUPS) - 1))]
		stages, effects, _ = effectsDescr[key]
		d = Math.Vector3(hit_dir)
		try:
			d.normalise()
		except Exception:
			d = Math.Vector3(0.0, 1.0, 0.0)
		te.addNew(hit_pos, effects, stages, None, dir=d, start=hit_pos - d, end=hit_pos + d,
		          showShockWave=is_player_target, showFlashBang=is_player_target)
		# Rock the target hull like Vehicle.showDamageFromShot does
		try:
			fashion = getattr(target_mock, '_swinging', None)
			if fashion is None and is_player_target:
				fashion = getattr(player, '_offhangar_swinging', None)
			_trigger_shot_impulse(fashion, d, effectsDescr['targetImpulse'])
		except Exception:
			pass
	except Exception:
		LOG_CURRENT_EXCEPTION()


_DS_NAMES_LOGGED = [False]


def _pick_damage_sticker(shot_result=2):
	"""Return a damage-sticker descr chosen by shot outcome: a penetration
	hole for pens (result >= 2), a scratch/ricochet mark otherwise. Selection
	is deterministic (not random) so a pen never shows a scuff and vice versa.
	Logs the available sticker names once for tuning."""
	try:
		from items import vehicles
		ds = vehicles.g_cache.damageStickers
		descrs = ds.get('descrs') if isinstance(ds, dict) else None
		if not descrs:
			return None
		ids = ds.get('ids', {}) if isinstance(ds, dict) else {}
		if not _DS_NAMES_LOGGED[0]:
			_DS_NAMES_LOGGED[0] = True
			LOG_DEBUG('DecalDBG: damage sticker names:', list(ids.keys()))
		pen = int(shot_result) >= 2
		pen_kw = ('pierc', 'penetr', 'hole', 'through', 'shot')
		nonpen_kw = ('scratch', 'ricochet', 'splash', 'nopen', 'no_pen', 'scuff', 'blast')
		want = pen_kw if pen else nonpen_kw
		# exact name match first, then substring
		for name, sid in ids.iteritems():
			low = str(name).lower()
			if any(k in low for k in want):
				return descrs[sid]
		# deterministic fallback: highest-priority sticker, else the first
		try:
			return sorted(descrs, key=lambda d: -int(d.get('priority', 0)))[0]
		except Exception:
			return descrs[0]
	except Exception:
		return None


def _comp_name_from_hits(td, all_hits):
	"""Map the first hit component descriptor to its name (hull/turret/gun/
	chassis) so the decal lands on the right component model."""
	try:
		for _h in (all_hits or []):
			_hc = _h[3] if len(_h) > 3 else None
			if _hc is None:
				continue
			if _hc is getattr(td, 'turret', None):
				return 'turret'
			if _hc is getattr(td, 'gun', None):
				return 'gun'
			if _hc is getattr(td, 'chassis', None):
				return 'chassis'
			if _hc is getattr(td, 'hull', None):
				return 'hull'
	except Exception:
		pass
	return 'hull'


def _setup_gun_recoil(gun_model, td):
	"""Create a WGGunRecoil fashion on the gun model (node 'G'), configured
	from the gun's recoil descriptor. Returns the recoil object to trigger
	per shot, or None."""
	try:
		import BigWorld
		if gun_model is None or td is None:
			return None
		gun = getattr(td, 'gun', None)
		rd = gun.get('recoil') if isinstance(gun, dict) else getattr(gun, 'recoil', None)
		if not rd:
			return None
		recoil = BigWorld.WGGunRecoil('G')
		try:
			recoil.setLod(rd['lodDist'])
		except Exception:
			pass
		recoil.setDuration(rd['backoffTime'], rd['returnTime'])
		recoil.setDepth(rd['amplitude'])
		gun_model.wg_gunRecoil = recoil
		return recoil
	except Exception:
		LOG_CURRENT_EXCEPTION()
		return None


def _trigger_gun_recoil(recoil):
	"""Play the barrel recoil animation for one shot."""
	try:
		if recoil is not None:
			recoil.recoil()
	except Exception:
		pass


def _setup_swinging(chassis_model, td):
	"""DISABLED: attaching this minimal WGVehicleFashion to a mock chassis
	crashes the engine natively on battle start (EXCEPTION_ACCESS_VIOLATION,
	near-null read) - the C++ fashion update expects the track/wheel/filter
	setup VehicleAppearance always provides. Re-enabling needs the full
	setup (setTracks with the chassis track materials, wheel groups, and a
	movementInfo provider). Kept as a stub so the _trigger_shot_impulse
	call sites stay wired; they no-op on a None fashion.

	Original intent: pitch/roll swinging + shot impulse on root node 'V',
	configured from the hull's swinging descriptor like
	VehicleAppearance._setupVehicleFashion. The
	full fashion also animates tracks/wheels, but that needs the vehicle
	filter's movementInfo which mock vehicles lack - so track/wheel/trace
	LODs are set to 0 (disabled) and only the swinging is active."""
	return None
	try:
		import BigWorld
		if chassis_model is None or td is None:
			return None
		swingingCfg = td.hull['swinging']
		fashion = BigWorld.WGVehicleFashion()
		try:
			fashion.maxMovement = td.physics['speedLimits'][0]
		except Exception:
			pass
		# same pitch modifiers VehicleAppearance applies
		_mods = (0.9, 1.88, 0.3, 4.0, 1.0, 1.0)
		pp = tuple(p * m for (p, m) in zip(swingingCfg['pitchParams'], _mods))
		fashion.setPitchSwinging('V', *pp)
		fashion.setRollSwinging('V', *swingingCfg['rollParams'])
		fashion.setShotSwinging('V', swingingCfg['sensitivityToImpulse'])
		fashion.setLods(0.0, 0.0, 0.0, swingingCfg['lodDist'])
		chassis_model.wg_fashion = fashion
		return fashion
	except Exception:
		LOG_CURRENT_EXCEPTION()
		return None


def _trigger_shot_impulse(fashion, d, impulse):
	"""Rock the hull: on firing (backward along the barrel, gun[impulse])
	and on getting hit (hit direction, shotEffects targetImpulse)."""
	try:
		if fashion is not None and d is not None and impulse:
			fashion.receiveShotImpulse(d, impulse)
	except Exception:
		pass


def _play_death_effect(td, position, is_player=False, ammo_rack=False):
	"""Vehicle destruction effect like VehicleAppearance.__playEffect:
	'explosion' for ammo-rack kills, 'destruction' otherwise, played in
	world space via terrainEffects (the wreck model swap detaches the live
	hull model, so attaching to it would cut the effect short). Fullscreen
	shockwave/flashbang only for the player's own tank, like the game."""
	try:
		import random, BigWorld, Math
		player = BigWorld.player()
		te = getattr(player, 'terrainEffects', None)
		if te is None or td is None or position is None:
			return
		kind = 'explosion' if ammo_rack else 'destruction'
		effs = td.type.effects.get(kind) or td.type.effects.get('destruction')
		if not effs:
			return
		stages, effects, _unused = random.choice(effs)
		pos = Math.Vector3(position)
		te.addNew(pos, effects, stages, None,
		          start=pos + Math.Vector3(0.0, -1.0, 0.0),
		          end=pos + Math.Vector3(0.0, 1.0, 0.0),
		          showShockWave=is_player, showFlashBang=is_player)
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _start_fire_effect(mock, hull_model, td):
	"""Burning-tank flames, mirroring the Fire extra: attach the 'fire'
	stage of a random 'flaming' effects list to the hull model."""
	try:
		import random
		if mock is None or hull_model is None or td is None:
			return
		effs = td.type.effects.get('flaming')
		if not effs:
			return
		stages, effects, _unused = random.choice(effs)
		if len(stages) != 2 or stages[0][0] != 'fire' or stages[1][0] != 'noEmission':
			return
		data = {}
		effects.attachTo(hull_model, data, 'fire')
		mock._fire_fx = {'effects': effects, 'data': data, 'hull': hull_model, 'noEmissionTime': stages[1][1]}
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _stop_fire_effect(mock, died=False):
	"""Stop the flames: on extinguish let the smoke (noEmission stage) fade
	out like the Fire extra does; on death cut everything immediately."""
	try:
		import BigWorld
		fx = getattr(mock, '_fire_fx', None)
		if not fx:
			return
		mock._fire_fx = None
		effects, data, hull = fx['effects'], fx['data'], fx['hull']
		if died:
			effects.detachAllFrom(data)
			return
		effects.detachFrom(data, 'fire')
		effects.attachTo(hull, data, 'noEmission')
		BigWorld.callback(fx['noEmissionTime'], lambda: effects.detachAllFrom(data))
	except Exception:
		pass


def _sync_burn_and_death(mock, hull_model, td):
	"""Per-tick visual sync for ANY tank (player or bot), regardless of what
	killed or ignited it (shells, fire, ammo rack): flames while burning, a
	one-shot destruction/ammo-rack explosion on death."""
	try:
		import BigWorld
		if mock is None:
			return
		alive = getattr(mock, 'health', 1) > 0
		burning = bool(getattr(mock, 'is_on_fire', False)) and alive
		if burning and getattr(mock, '_fire_fx', None) is None:
			_start_fire_effect(mock, hull_model, td)
		elif not burning and getattr(mock, '_fire_fx', None) is not None:
			_stop_fire_effect(mock, died=not alive)
		if not alive and not getattr(mock, '_death_fx_played', False):
			mock._death_fx_played = True
			# One place that catches EVERY death, player and bot, whatever killed them:
			# put every module and every crewman out. Only the drowning path used to.
			try:
				import BigWorld as _bwd
				_is_p = (getattr(mock, 'id', -1) == getattr(_bwd.player(), 'playerVehicleID', -1))
				_offh_knock_out_everything(mock, _is_p)
			except Exception as _kae:
				LOG_DEBUG('death knockout err:', str(_kae))
			# Tracks stop dead. WGVehicleFashion.movementInfo is a SPEED, not a frame:
			# the native scroll keeps running on the last value forever. That never
			# showed before because the wreck swap threw the fashion away with the old
			# models - a drowned tank keeps its intact models, so its tracks rolled on.
			try:
				import Math as _Md
				_fa_d = getattr(mock, '_fashion', None)
				if _fa_d is not None:
					_fa_d.movementInfo = _Md.Vector4(0.0, 0.0, 0.0, 0.0)
			except Exception:
				pass
			mock._veh_velocity = 0.0
			mock._veh_turn_velocity = 0.0
			# Dead engines are silent: the real game stops the engine and
			# movement sounds when the vehicle is destroyed.
			for _sname in ('_snd_engine', '_snd_tracks'):
				_snd = getattr(mock, _sname, None)
				if _snd is not None:
					try:
						_snd.stop()
					except Exception:
						pass
					setattr(mock, _sname, None)
			# Param handles of the stopped events must not outlive them
			for _pname in ('_p_load', '_p_spd'):
				try:
					setattr(mock, _pname, None)
				except Exception:
					pass
			try:
				is_pl = getattr(BigWorld.player(), 'playerVehicleID', -2) == getattr(mock, 'id', -1)
			except Exception:
				is_pl = False
			# drowned tanks sank, they did not explode - skip the destruction effect
			if not getattr(mock, '_drowned', False):
				_play_death_effect(td, getattr(mock, 'position', None), is_player=is_pl,
				                   ammo_rack=bool(getattr(mock, '_ammo_rack_death', False)))
	except Exception:
		pass


def _offh_set_battle_gui_mode(visible):
	"""Single owner of the battle pause-menu input state (cursor visibility +
	AvatarInputHandler input gate). Driven from BOTH the ESC key hook and the
	patched Battle.cursorVisibility flash callback, so every way of opening/
	closing the menu leaves the two consistent (idempotent, not a toggle)."""
	try:
		import BigWorld, GUI
		p = BigWorld.player()
		if p is None:
			return
		p._offhangar_gui_visible = bool(visible)
		aih = getattr(p, 'inputHandler', None)
		if visible:
			BigWorld.setCursor(GUI.mcursor())
			GUI.mcursor().visible = True
			if aih is not None:
				aih._AvatarInputHandler__isStarted = False
		else:
			if aih is not None:
				aih._AvatarInputHandler__isStarted = True
			GUI.mcursor().visible = False
			BigWorld.setCursor(getattr(GUI, 'ccursor', GUI.mcursor)())
	except Exception:
		pass


def _fallback_gun_sound(td, model):
	"""Generic caliber-bucket shot sound. Used ONLY when the gun's own
	effects list could not play: the effects carry the real per-gun sound
	(EffectsList 'sound' element), exactly like the live game, so forcing
	a bucket sound on top doubles it with a generic (often wrong) one."""
	try:
		if model is None:
			return
		caliber = 75
		try:
			gun = getattr(td, 'gun', None)
			if isinstance(gun, dict) and 'shots' in gun:
				caliber = gun['shots'][0]['shell']['caliber']
		except Exception:
			pass
		if caliber > 120:
			sound_event = '/tanks/guns/gun_huge/gun_huge_152mm'
		elif caliber > 100:
			sound_event = '/tanks/guns/gun_large/gun_large_115-152mm'
		elif caliber > 75:
			sound_event = '/tanks/guns/gun_main/gun_main_85-107mm'
		elif caliber > 45:
			sound_event = '/tanks/guns/gun_medium/gun_medium_50-75mm'
		else:
			sound_event = '/tanks/guns/gun_small/gun_small_20-45mm'
		_fb = model.playSound(sound_event)
		if _fb is None:
			LOG_DEBUG('SOUND UNAVAILABLE (fallback gun):', sound_event)
		else:
			# The bucket sound is a LAST resort - reaching it means the gun's own
			# effects list could not play, so the shot is heard with a generic
			# caliber sound instead of this gun's. Worth knowing about.
			LOG_DEBUG('GUN SOUND FALLBACK: caliber bucket %s (gun effects list did not play)' % sound_event)
	except Exception:
		pass


_INPUT_DBG = {'total': 0, 'pre_eaten': 0, 'aih': 0, 'started': '?', 'detach': '?', 'installed': False}


def _install_input_chain_debug():
	"""Temporary diagnostic for the intermittent camera aimlock: counts where
	mouse events die in the chain game.handleMouseEvent ->
	AvatarInputHandler.handleMouseEvent -> control mode, and logs a state
	snapshot every 2s while in battle. Remove once the lock is understood."""
	if _INPUT_DBG['installed']:
		return
	# Debug-only: with logging off (player build) do NOT wrap the mouse-event
	# chain nor start the 2s dump loop - pure overhead for players.
	try:
		from gui.mods.offhangar.logging import _DBG as _in_dbg
		if not _in_dbg[0]:
			return
	except Exception:
		return
	_INPUT_DBG['installed'] = True
	try:
		import game, BigWorld
		import AvatarInputHandler as _AIH_mod

		_orig_game_hme = game.handleMouseEvent
		def _dbg_game_hme(event):
			_INPUT_DBG['total'] += 1
			before = _INPUT_DBG['aih']
			result = _orig_game_hme(event)
			if _INPUT_DBG['aih'] == before:
				_INPUT_DBG['pre_eaten'] += 1
			return result
		game.handleMouseEvent = _dbg_game_hme

		_orig_aih_hme = _AIH_mod.AvatarInputHandler.handleMouseEvent
		def _dbg_aih_hme(self, dx, dy, dz):
			_INPUT_DBG['aih'] += 1
			_INPUT_DBG['started'] = getattr(self, '_AvatarInputHandler__isStarted', '?')
			_INPUT_DBG['detach'] = getattr(self, '_AvatarInputHandler__detachCount', '?')
			return _orig_aih_hme(self, dx, dy, dz)
		_AIH_mod.AvatarInputHandler.handleMouseEvent = _dbg_aih_hme

		def _dump():
			try:
				BigWorld.callback(2.0, _dump)
				p = BigWorld.player()
				if p is None or getattr(p, 'arena', None) is None:
					return
				if not _INPUT_DBG['total']:
					return
				try:
					ih = getattr(p, 'inputHandler', None)
					ctrl = getattr(ih, 'ctrl', None) if ih is not None else None
					ctrl_name = ctrl.__class__.__name__ if ctrl is not None else 'None'
					enabled = '?'
					if ctrl is not None:
						for k, v in ctrl.__dict__.items():
							if k.endswith('__isEnabled'):
								enabled = v
								break
				except Exception:
					ctrl_name, enabled = '?', '?'
				try:
					cam = BigWorld.camera()
					cam_name = cam.__class__.__name__ if cam is not None else 'None'
				except Exception:
					cam_name = '?'
				try:
					import GUI
					cursor_on = GUI.mcursor().visible
				except Exception:
					cursor_on = '?'
				LOG_DEBUG('InputDBG: mouse=%s eaten_before_aih=%s reached_aih=%s isStarted=%s detach=%s ctrl=%s enabled=%s cam=%s mcursor=%s period=%s' % (
					_INPUT_DBG['total'], _INPUT_DBG['pre_eaten'], _INPUT_DBG['aih'],
					_INPUT_DBG['started'], _INPUT_DBG['detach'], ctrl_name, enabled,
					cam_name, cursor_on, getattr(getattr(p, 'arena', None), 'period', '?')))
				_INPUT_DBG['total'] = 0
				_INPUT_DBG['pre_eaten'] = 0
				_INPUT_DBG['aih'] = 0
			except Exception:
				pass
		BigWorld.callback(2.0, _dump)
		LOG_DEBUG('InputDBG: input chain diagnostics installed')
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _play_ground_wave(gun_model, td):
	"""Dust kicked up off the ground under the barrel when firing, mirroring
	vehicle_extras.ShowShooting.__doGroundWaveEffect."""
	try:
		import BigWorld, Math
		if gun_model is None or td is None:
			return
		gun = getattr(td, 'gun', None)
		gwv = gun.get('groundWave') if isinstance(gun, dict) else getattr(gun, 'groundWave', None)
		if not gwv:
			return
		player = BigWorld.player()
		if player is None:
			return
		node = gun_model.node('HP_gunFire')
		gunPos = Math.Matrix(node).translation
		testRes = BigWorld.wg_collideSegment(_offh_bspace(), gunPos + Math.Vector3(0, 0.5, 0), gunPos - Math.Vector3(0, 4.0, 0), 128)
		if testRes is None:
			return
		position = testRes[0]
		stages, effects = gwv[0], gwv[1]
		player.terrainEffects.addNew(position, effects, stages, None, dir=testRes[1], start=position + Math.Vector3(0, 0.5, 0), end=position - Math.Vector3(0, 0.5, 0))
	except Exception:
		pass


def _play_muzzle_flash(owner, gun_model, td, is_player=False):
	"""Play the gun muzzle flash (+ ground dust) for one shot, uniformly for
	the player and bots. Reuses a single EffectsListPlayer per gun, stopped
	before replay, exactly like vehicle_extras.ShowShooting."""
	try:
		import BigWorld
		if gun_model is None or td is None or owner is None:
			return
		gun = getattr(td, 'gun', None)
		ge = gun.get('effects') if isinstance(gun, dict) else getattr(gun, 'effects', None)
		if not isinstance(ge, (tuple, list)) or len(ge) < 2:
			return
		stages, effects = ge[0], ge[1]
		# Keep the gun model animating every frame so recoil + flash play out
		# even when the camera is still (matches the game's isPlayer path).
		if is_player and not getattr(gun_model, '_offhangar_always_update', False):
			try:
				BigWorld.addAlwaysUpdateModel(gun_model)
				gun_model._offhangar_always_update = True
				# engine holds a strong ref + animates it every frame:
				# tracked so the battle sweep can delAlwaysUpdateModel it
				globals().setdefault('g_offh_always_update_models', []).append(gun_model)
			except Exception:
				pass
		# Shooter's models are HIDDEN (player zoomed into sniper / unspotted
		# bot): skip the flash visuals entirely - particles spawned on a hidden
		# model do not animate, so they reappeared FROZEN in the air when the
		# model was shown again ('muzzle flash visible after leaving sniper').
		# Play only the shot sound, using the gun's own EffectsList sound
		# element (the same one attachTo would have played).
		_offh_hidden = (is_player and not getattr(gun_model, 'visible', True)) or \
			((not is_player) and not getattr(owner, '_spot_visible', True))
		if _offh_hidden:
			try:
				for _sdesc in (getattr(effects, '_EffectsList__effectDescList', None) or []):
					_snm = getattr(_sdesc, '_soundName', None)
					if _snm:
						_snd = gun_model.getSound(_snm)
						if _snd is not None:
							_snd.play()
							return True
						break
			except Exception:
				pass
			return None  # caller falls back to the caliber-bucket sound
		from helpers import EffectsList
		mzp = getattr(owner, '_offhangar_muzzle_player', None)
		if mzp is None:
			mzp = EffectsList.EffectsListPlayer(effects, stages)
			owner._offhangar_muzzle_player = mzp
		else:
			try:
				mzp.stop()
			except Exception:
				pass
		mzp.play(gun_model)
		_play_ground_wave(gun_model, td)
		return True
	except Exception:
		pass


def _target_sticker_map(target_mock):
	"""Resolve the per-component VehicleStickers map for ANY hit target,
	uniformly for bots and the player, so decals work the same everywhere."""
	m = getattr(target_mock, '_sticker_map', None)
	if m:
		return m
	# The player's own tank keeps its sticker map on the player object.
	try:
		import BigWorld
		p = BigWorld.player()
		if p is not None and getattr(p, 'playerVehicleID', -2) == getattr(target_mock, 'id', -1):
			return getattr(p, '_offhangar_sticker_map', None)
	except Exception:
		pass
	return None


def _add_impact_decal(sticker_map, comp_name, world_hit_pos, world_dir, shot_result=2):
	"""Add a persistent shell-hole decal to a mock tank at the hit point.
	sticker_map maps component name -> (VehicleStickers, componentModel).
	The sticker projects along the shot segment onto the component surface,
	so the segment must be expressed in that model's LOCAL space."""
	try:
		import Math, math
		if not sticker_map:
			return
		entry = sticker_map.get(comp_name) or sticker_map.get('hull')
		if not entry:
			return
		stickers = entry[0]
		model = entry[1]
		node = entry[2] if len(entry) > 2 else None
		if stickers is None or model is None:
			return
		descr = _pick_damage_sticker(shot_result)
		if descr is None:
			return
		# Component models are attached to nodes and report a local (identity)
		# matrix; the node carries the real world transform. Use it to map the
		# world hit point into the model's local space the decal expects.
		w2m = Math.Matrix(node) if node is not None else Math.Matrix(model.matrix)
		w2m.invert()
		d = Math.Vector3(world_dir)
		try:
			d.normalise()
		except Exception:
			d = Math.Vector3(0.0, -1.0, 0.0)
		# segStart just outside the surface, segEnd just inside, so the
		# projector places the decal on the outer skin at the hit point.
		local_start = w2m.applyPoint(world_hit_pos - d.scale(0.4))
		local_end = w2m.applyPoint(world_hit_pos + d.scale(0.4))
		import random
		ang = random.random() * math.pi * 2.0
		up = Math.Vector3(math.sin(ang), math.cos(ang), 0.0)
		sz = descr.get('modelSizes', (0.3, 0.3))
		sizes = Math.Vector2(sz[0], sz[1])
		stickers.addDamageSticker(descr['texName'], descr.get('bumpTexName', ''), local_start, local_end, sizes, up)
	except Exception:
		LOG_CURRENT_EXCEPTION()

from gui.mods.offhangar.logging import LOG_DEBUG
from gui.mods.offhangar.offline_battle_stack import build_offline_battle_context

_BATTLE_BOOT_DEBOUNCE_SEC = 1.5
OFFLINE_BATTLE_ENABLED = True



def _resolve_real_arena_type(map_id, map_name, gameplay_name):
	"""
	Try to resolve a real ArenaType object from the client's cache.
	This provides minimap + other per-map metadata needed by battle GUI.
	"""
	try:
		try:
			import ArenaType as ArenaTypeModule
		except ImportError:
			# 0.8.2 ships it as `common/arenatype.pyc`
			try:
				from common import arenatype as ArenaTypeModule
			except ImportError:
				import arenatype as ArenaTypeModule
		cache = getattr(ArenaTypeModule, 'g_cache', None)
		# Lazy init on some builds: cache can start as None.
		for init_name in ('init', '_init', 'initialize'):
			init_fn = getattr(ArenaTypeModule, init_name, None)
			if callable(init_fn):
				try:
					init_fn()
					cache = getattr(ArenaTypeModule, 'g_cache', None)
				except Exception:
					LOG_CURRENT_EXCEPTION()
			if cache is not None:
				break

		if cache is None:
			LOG_DEBUG('OfflineBattle.arenaType.cacheMissing', map_name, 'module', getattr(ArenaTypeModule, '__name__', '?'))
			return None

		# Some builds provide module-level getters instead of direct cache access.
		for fn_name in ('getArenaType', 'getByGeometryName', 'getByName', 'getArenaTypeByName'):
			fn = getattr(ArenaTypeModule, fn_name, None)
			if callable(fn):
				for key in (map_name, map_id):
					try:
						at = fn(key)
						if at is not None:
							try:
								at.geometryName = map_name
								at.gameplayName = gameplay_name
							except Exception:
								pass
							return at
					except Exception:
						continue

		def _try_get(key):
			for getter in (
				lambda: cache.get(key),
				lambda: cache[key],
				lambda: cache.getArenaType(key) if hasattr(cache, 'getArenaType') else None,
				lambda: cache.getByID(key) if hasattr(cache, 'getByID') else None,
				lambda: cache.getById(key) if hasattr(cache, 'getById') else None,
			):
				try:
					at = getter()
					if at is not None:
						return at
				except Exception:
					continue
			return None

		# g_cache can be a mapping-like object; try the common access patterns.
		at = _try_get(map_name)
		# If stack provided short name like "himmelsdorf", try to match "04_himmelsdorf".
		if at is None and map_name and '_' not in map_name:
			try:
				keys = cache.keys() if hasattr(cache, 'keys') else []
				for k in keys:
					try:
						if isinstance(k, basestring) and (k == map_name or k.endswith('_' + map_name)):
							at = _try_get(k)
							if at is not None:
								map_name = k
								break
					except Exception:
						continue
			except Exception:
				LOG_CURRENT_EXCEPTION()
		if at is not None:
			try:
				at.geometryName = map_name
				at.gameplayName = gameplay_name
			except Exception:
				pass
			return at

		# 0.8.2: g_cache can be a dict keyed by arenaTypeID (int), with geometryName stored on values.
		try:
			if isinstance(cache, dict):
				for k, v in cache.iteritems():
					try:
						geom = getattr(v, 'geometryName', None) or ''
						if not isinstance(geom, basestring):
							continue
						geom_base = geom.split('/')[-1]
						if geom_base == map_name or map_name.endswith(geom_base) or geom_base.endswith(map_name):
							try:
								v.gameplayName = gameplay_name
							except Exception:
								pass
							return v
					except Exception:
						continue
		except Exception:
			LOG_CURRENT_EXCEPTION()

		# Diagnostics: log cache shape so we can implement the correct lookup for 0.8.2.
		try:
			cache_type = type(cache).__name__
			attrs = [a for a in dir(cache) if 'get' in a.lower() or 'arena' in a.lower() or 'type' in a.lower()]
			if isinstance(cache, dict):
				keys = cache.keys()
				key_types = {}
				for kk in keys[:50]:
					kt = type(kk).__name__
					key_types[kt] = key_types.get(kt, 0) + 1
				# also sample a few geometry names to confirm value shape
				sample_geom = []
				for vv in cache.values()[:10]:
					try:
						g = getattr(vv, 'geometryName', None)
						if g:
							sample_geom.append(g)
					except Exception:
						continue
				LOG_DEBUG(
					'OfflineBattle.arenaType.cacheNoHit',
					map_name, 'mapID', map_id,
					'cacheType', cache_type,
					'keyTypes', key_types,
					'sampleGeom', sample_geom[:5],
					'attrs', attrs[:20]
				)
			else:
				LOG_DEBUG('OfflineBattle.arenaType.cacheNoHit', map_name, 'mapID', map_id, 'cacheType', cache_type, 'attrs', attrs[:25])
		except Exception:
			LOG_CURRENT_EXCEPTION()
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None


def _queue_type_randoms():
	try:
		from constants import QUEUE_TYPE
		return QUEUE_TYPE.RANDOMS
	except Exception:
		# Very old builds: keep a sane default; onEnqueued may still accept an int.
		return 1


def _resolve_vehicle_inv_id(player, int1):
	if int1:
		return int1
	try:
		from CurrentVehicle import g_currentVehicle
		if g_currentVehicle is not None:
			item = getattr(g_currentVehicle, 'item', None)
			if item is not None:
				vid = getattr(item, 'invID', None)
				if vid:
					return vid
	except ImportError:
		pass
	except Exception:
		LOG_CURRENT_EXCEPTION()
	inv = getattr(player, 'inventory', None)
	if inv is None:
		return 0
	for methodName in (
		'getCurrVehicleInvID',
		'getCurrentVehInvID',
		'getVehicleInvID',
		'getCurrentInvID',
	):
		fn = getattr(inv, methodName, None)
		if callable(fn):
			try:
				v = fn()
				if v:
					return v
			except Exception:
				LOG_CURRENT_EXCEPTION()
	for methodName in ('getCurrentVehicle', 'getCurrVehicle'):
		fn = getattr(inv, methodName, None)
		if callable(fn):
			try:
				veh = fn()
				if veh is not None:
					vid = getattr(veh, 'invID', None)
					if vid:
						return vid
			except Exception:
				LOG_CURRENT_EXCEPTION()
	return 0


def _enable_offline_battle_transition(player):
	# Hangar hardening hooks in mod_offhangar must relax while loading an arena.
	player._offhangar_allow_world_clear = True
	# Allow become-non-player only after avatar spawn attempt.
	player._offline_allow_become_non_player = False


def _try_spawn_battle_avatar_stub(player, cmdName):
	import BigWorld
	if player is None or not getattr(player, 'isOffline', False):
		return
	try:
		# Belt: whatever exit path the last battle took, purge its
		# leftovers (wrecks, models, FMOD events, old map) first.
		try:
			_offh_battle_sweep('start')
		except:
			try:
				import traceback as _swtb2
				LOG_DEBUG('Sweep(start) FAILED:', _swtb2.format_exc())
			except Exception:
				pass
		# The sweep destroys the projectile mover on battle exit (it owns
		# the shell models) - recreate it per battle or tracers are gone
		# from the second battle on. The __calcTrajectory patch lives on
		# the class, so a fresh instance keeps it.
		try:
			if globals().get('g_projectile_mover') is None:
				from projectilemover import ProjectileMover as _OffhPM
				globals()['g_projectile_mover'] = _OffhPM()
		except:
			pass
		# Map name from the (matchmaker-rolled) arena.
		map_name = player.arena.arenaType.geometryName
		if not map_name.startswith('spaces/'):
			map_name = 'spaces/' + map_name

		try:
			from _constants import CONFIG_OPTIONS as _CFG_SP
			# See the note in the exit sweep: reuse_map_space stays off because a
			# reused space kept showing the previous map.
			_full_release = bool(_CFG_SP.get('full_space_release', True))
			_reuse_space = bool(_CFG_SP.get('reuse_map_space', False))
		except Exception:
			_full_release = True
			_reuse_space = False
		globals()['g_offh_full_release'] = _full_release

		if _full_release:
			# Dedicated FRESH space per battle. Rendering follows camera.spaceID
			# (the diagnostic proved it; the hangar renders its own space that
			# way), so we point the render camera at this space. The PREVIOUS
			# battle's space is releaseSpace'd HERE (start = stable context, it's
			# fully orphaned) rather than in the exit sweep, which crashed on the
			# hangar load -> map RAM truly returned -> variety WITHOUT fragmentation.
			_prev = globals().get('g_offh_pending_release', 0) or 0
			if _prev:
				globals()['g_offh_pending_release'] = 0
				# That space still has its geometry MAPPED at this point: the mapped_*
				# globals are only overwritten after the new space exists, two lines
				# below. Releasing a space whose mapping is still registered is what
				# leaves the engine reading freed chunk memory one call later. Unmap
				# first, then drop the globals so no stale handle can survive into a
				# REUSED space id - the engine hands the freed id straight back out.
				try:
					_ph = globals().get('g_offh_mapped_handle')
					if _ph is not None and (globals().get('g_offh_mapped_space', 0) or 0) == _prev:
						BigWorld.delSpaceGeometryMapping(_prev, _ph)
						globals()['g_offh_mapped_handle'] = None
						globals()['g_offh_mapped_space'] = 0
						globals()['g_offh_mapped_name'] = None
						LOG_DEBUG('OfflineBattle.unmapped prev space %s' % _prev)
				except Exception, _e_um:
					LOG_DEBUG('OfflineBattle.unmap prev FAILED %s' % _e_um)
				try:
					if hasattr(BigWorld, 'releaseSpace'):
						BigWorld.releaseSpace(_prev)
						len('')
				except Exception:
					pass
				try:
					import gc as _gcp
					_gcp.collect(); _gcp.collect()
				except Exception:
					pass
				LOG_DEBUG('OfflineBattle.released prev space', _prev)
			# The log ends right here on the second battle - 'released prev space'
			# prints, 'dedicated space' never does - so the crash is in one of the
			# next three calls, not in the release. One line each to name which.
			space_id = BigWorld.createSpace()
			LOG_DEBUG('OfflineBattle.createSpace -> %s (prev %s, reusedID=%s)' % (space_id, _prev, space_id == _prev))
			_offh_mh = BigWorld.addSpaceGeometryMapping(space_id, None, map_name)
			LOG_DEBUG('OfflineBattle.addSpaceGeometryMapping ok')
			globals()['g_offh_mapped_handle'] = _offh_mh
			globals()['g_offh_mapped_space'] = space_id
			globals()['g_offh_mapped_name'] = map_name
			_offh_set_render_space(space_id)
			LOG_DEBUG('OfflineBattle.dedicated space', space_id, 'camera render ->', space_id, map_name)
		else:
			space_id = getattr(player, 'spaceID', 0)
			if space_id == 0:
				space_id = BigWorld.createSpace()
			# ANTI-FRAGMENTATION: reuse the already-mapped space when the map is
			# UNCHANGED (reuse_map_space); else unmap old + map new.
			_mapped_name = globals().get('g_offh_mapped_name')
			_mapped_space = globals().get('g_offh_mapped_space', 0) or 0
			_mapped_handle = globals().get('g_offh_mapped_handle')
			if _reuse_space and _mapped_handle is not None and _mapped_space == space_id and _mapped_name == map_name:
				_offh_mh = _mapped_handle
				LOG_DEBUG('OfflineBattle.mappedGeometry REUSED (no realloc)', map_name, 'space', space_id)
			else:
				if _mapped_handle is not None and _mapped_space:
					try: BigWorld.delSpaceGeometryMapping(_mapped_space, _mapped_handle)
					except Exception: pass
				try: BigWorld.clearSpace(space_id)
				except Exception: pass
				_offh_mh = BigWorld.addSpaceGeometryMapping(space_id, None, map_name)
				globals()['g_offh_mapped_handle'] = _offh_mh
				globals()['g_offh_mapped_space'] = space_id
				globals()['g_offh_mapped_name'] = map_name
				LOG_DEBUG('OfflineBattle.mappedGeometry', map_name, 'space', space_id)
		globals()['g_offh_battle_space'] = space_id
		globals()['g_offh_battle_mapping'] = _offh_mh
		globals()['g_offh_battle_mapname'] = map_name
		# The spaceID is reused between battles, so the per-space reset
		# heuristics in the destructibles ledger / tree registry never fire
		# on their own: reset explicitly or the dedup sets grow forever and
		# suppress destruction of fresh objects in later battles.
		try:
			_get_destr_authority().reset()
		except Exception:
			pass
		for _k in ('g_offh_tree_state', 'g_offh_destr_ordered', 'g_offh_destr_chunks', 'g_offh_destr_seen', 'g_offh_ram_cd'):
			globals().pop(_k, None)
		# Start the destructibles manager BEFORE the chunks stream in, so it
		# receives onChunkLoad for every chunk (fences/houses become breakable)
		try:
			import AreaDestructibles
			if getattr(AreaDestructibles, 'g_destructiblesManager', None) is not None:
				AreaDestructibles.g_destructiblesManager.startSpace(space_id)
				LOG_DEBUG('OfflineBattle: destructibles manager started for space', space_id)
		except Exception:
			LOG_CURRENT_EXCEPTION()
	except Exception:
		LOG_CURRENT_EXCEPTION()

	try:
		LOG_DEBUG('OfflineBattle.starting camera manually in space', space_id)
		import AvatarInputHandler
		import Math, ResMgr
		global g_offline_aih

		# Determine spawn position from arena XML
		spawn_pos = Math.Vector3(0, 100.0, 0)
		spawn_dir = Math.Vector3(0, 0, 3.1415926535)
		try:
			at = player.arena.arenaType
			if True:
				xml_path = 'scripts/arena_defs/%s.xml' % at.geometryName.split('/')[-1]
				section = ResMgr.openSection(xml_path)
				LOG_DEBUG('OfflineBattle.XML_LOAD:', xml_path, section is not None)
				if section is not None:
					import debug_utils
					debug_utils.LOG_DEBUG('DUMP ARENA DEFS:', section.keys(), section['gameplayTypes/ctf'].keys() if section.has_key('gameplayTypes/ctf') else 'no_ctf')
					if section.has_key('gameplayTypes/ctf'):
						ctf = section['gameplayTypes/ctf']
						for t in ['team1', 'team2']:
							if ctf.has_key('teamSpawnPoints/%s' % t):
								debug_utils.LOG_DEBUG('SPAWN POINTS %s:' % t, ctf['teamSpawnPoints/%s' % t].keys())
								for k, v in ctf['teamSpawnPoints/%s' % t].items():
									debug_utils.LOG_DEBUG(' - ', k, type(v), v.asVector2)
					gp = section['gameplayTypes/ctf']
					if section is not None:
						try:
							with open('C:\\Games\\World_of_Tanks_0.08.02.00.00_EU_0543_SD\\arena_dump_root.txt', 'w') as f_out:
								f_out.write('ROOT keys: ' + str(section.keys()) + '\n')
								for k, v in section.items():
									if k in ['teamSpawnPoints', 'teamBasePositions'] or 'team' in k:
										f_out.write(' - ' + k + ' : ' + str(type(v)) + '\n')
										if hasattr(v, 'keys'):
											f_out.write('    keys: ' + str(v.keys()) + '\n')
						except Exception as e:
							pass
					if gp is not None:
						try:
							with open('C:\\Games\\World_of_Tanks_0.08.02.00.00_EU_0543_SD\\arena_dump_gp.txt', 'w') as f_out:
								f_out.write('ctf keys: ' + str(gp.keys()) + '\n')
								for k, v in gp.items():
									f_out.write(' - ' + k + ' : ' + str(type(v)) + '\n')
									if hasattr(v, 'keys'):
										f_out.write('    keys: ' + str(v.keys()) + '\n')
										for k2, v2 in v.items():
											f_out.write('    - ' + k2 + ' : ' + str(type(v2)) + '\n')
											if hasattr(v2, 'keys'):
												f_out.write('       keys: ' + str(v2.keys()) + '\n')
												for k3, v3 in v2.items():
													f_out.write('       - ' + k3 + ' asVec2:' + str(getattr(v3, 'asVector2', 'none')) + ' asStr:' + str(getattr(v3, 'asString', 'none')) + '\n')
						except Exception as e:
							import debug_utils
							debug_utils.LOG_DEBUG('DUMP ERROR:', e)
						
						global g_offline_bases
						g_offline_bases = {1: [], 2: []}
						def _add_base(_t, _x, _z, _src):
							'''Accept a base position only if it really is one.
							
							hasattr(section, 'asVector2') is ALWAYS true on a BigWorld DataSection -
							those accessors exist whatever the node holds, so it is not a type test.
							A child that is not a vector read as (0, 0) and became a base at the MAP
							ORIGIN. Driving through the middle of the map then started a capture with
							no circle anywhere near - the reported symptom. No WoT map places a base
							at the origin, so that value is always the bug.'''
							try:
								_x = float(_x); _z = float(_z)
							except Exception:
								return
							if abs(_x) < 1.0 and abs(_z) < 1.0:
								LOG_DEBUG('BASE REJECTED (origin, not a vector node): team=%s src=%s' % (_t, _src))
								return
							g_offline_bases[_t].append(Math.Vector3(_x, 0.0, _z))
							LOG_DEBUG('BASE team=%s at (%.1f, %.1f) src=%s' % (_t, _x, _z, _src))
						import debug_utils
						try:
							bp_node_all = gp['teamBasePositions']
							if bp_node_all is not None:
								debug_utils.LOG_DEBUG('teamBasePositions EXISTS! keys:', bp_node_all.keys())
								for k, v in bp_node_all.items():
									debug_utils.LOG_DEBUG(' - child:', k, v.keys())
						except Exception as e:
							debug_utils.LOG_DEBUG('teamBasePositions error:', e)

						
						for t_id in (1, 2):
							bp_node = gp['teamBasePositions/team%d' % t_id]
							if bp_node is not None:
								items = bp_node.items()
								if items:
									for k, v in items:
										import debug_utils
										debug_utils.LOG_DEBUG('Base node child', t_id, k)
										if v is not None and hasattr(v, 'asVector2'):
											_add_base(t_id, v.asVector2.x, v.asVector2.y, 'child:%s' % k)
										elif v is not None and hasattr(v, 'asVector3'):
											_add_base(t_id, v.asVector3.x, v.asVector3.z, 'child3:%s' % k)
								else:
									import gui.mods.offhangar.logging as __offlog
									__offlog.LOG_DEBUG('LOUD: Base node DIRECT', t_id)
									if hasattr(bp_node, 'asVector2'):
										_add_base(t_id, bp_node.asVector2.x, bp_node.asVector2.y, 'direct2')
									elif hasattr(bp_node, 'asVector3'):
										_add_base(t_id, bp_node.asVector3.x, bp_node.asVector3.z, 'direct3')
									elif hasattr(bp_node, 'asString'):
										try:
											parts = bp_node.asString.split()
											_add_base(t_id, parts[0], parts[1], 'string')
										except Exception as e:
											pass
							import gui.mods.offhangar.logging as __offlog
							__offlog.LOG_DEBUG('LOUD: g_offline_bases is now:', g_offline_bases)
						
						import debug_utils
						debug_utils.LOG_DEBUG('Parsed bases:', g_offline_bases)
						
						# Collect the original spawn points of BOTH teams (for the 15v15 auto-spawn)
						global g_offline_spawns
						g_offline_spawns = {1: [], 2: []}
						for _sp_t in (1, 2):
							try:
								_sp_node = gp['teamSpawnPoints/team%d' % _sp_t]
								if _sp_node is not None:
									for _sp_k, _sp_v in _sp_node.items():
										_sp_v2 = getattr(_sp_v, 'asVector2', None)
										if _sp_v2 is None:
											try:
												_sp_parts = _sp_v.asString.split()
												_sp_v2 = Math.Vector2(float(_sp_parts[0]), float(_sp_parts[1]))
											except Exception:
												_sp_v2 = None
										if _sp_v2 is not None:
											g_offline_spawns[_sp_t].append((float(_sp_v2.x), float(_sp_v2.y)))
							except Exception:
								pass
						LOG_DEBUG('OfflineBattle.spawnPoints (ctf xml):', g_offline_spawns)
						# NOTE: 0.8.2 ctf arena_defs carry NO teamSpawnPoints - only teamBasePositions.
						# (Verified by decoding the packed 04_himmelsdorf.xml: ctf has teamBasePositions
						# only, while domination's teamSpawnPoints sit ~350 m away and its team1 point is
						# beside ctf team2's base.) The server places vehicles in retail, so there is no
						# authentic per-vehicle list to read here: the line-up around the base flag below
						# IS the correct approach. Do not 'fix' this by borrowing another gameplay type.
						# AUTHORITATIVE: ArenaType parsed teamSpawnPoints itself (readVector2s('position')
						# under teamSpawnPoints/teamN) and exposes it via __getattr__. The hand-parse above
						# came back empty on every map (python.log: 'spawnPoints: {1: [], 2: []}'), which
						# dropped every spawn onto the base-flag fallback instead of the real spawn points.
						try:
							_at_sp = getattr(at, 'teamSpawnPoints', None)
							if _at_sp:
								for _ti in (1, 2):
									_lst = _at_sp[_ti - 1] if len(_at_sp) >= _ti else None
									if not _lst: continue
									_acc = []
									for _pv in _lst:
										try: _acc.append((float(_pv.x), float(_pv.y)))
										except Exception: pass
									if _acc: g_offline_spawns[_ti] = _acc
						except Exception as _spe:
							LOG_DEBUG('ArenaType spawn read error:', str(_spe))
						LOG_DEBUG('OfflineBattle.spawnPoints (final):', g_offline_spawns)
						# Map bounds (arena_defs boundingBox: bottomLeft/upperRight as Vector2) - used to
						# reject off-map spawn candidates (the 'spawned left outside the map' bug).
						global g_offline_bounds
						g_offline_bounds = None
						try:
							_bb = getattr(at, 'boundingBox', None)
							if _bb is not None and len(_bb) >= 2:
								g_offline_bounds = (float(_bb[0].x), float(_bb[0].y), float(_bb[1].x), float(_bb[1].y))
								LOG_DEBUG('OfflineBattle.mapBounds:', g_offline_bounds)
						except Exception:
							g_offline_bounds = None
						
						sp = gp['teamSpawnPoints/team1']
						bp = gp['teamBasePositions/team1']
						
						# Validate ALL spawn candidates with a roof/ledge check instead of
						# blindly taking the first one. Real spawn points first (like the
						# original game), base flag positions as fallback.
						_found_spawn = False
						import BigWorld
						
						def _read_vec2(val):
							v2 = getattr(val, 'asVector2', None)
							if v2 is None:
								try:
									parts = val.asString.split()
									v2 = Math.Vector2(float(parts[0]), float(parts[1]))
								except: pass
							return v2
						
						# Collect candidates: real ArenaType spawn points first (original game order),
						# then the XML hand-parse, then the base flag as the last resort.
						_spawn_cands = []
						for _gx, _gz in ((globals().get('g_offline_spawns', {}) or {}).get(1, []) or []):
							try: _spawn_cands.append(Math.Vector2(float(_gx), float(_gz)))
							except Exception: pass
						if sp is not None:
							for key, val in sp.items():
								vec2 = _read_vec2(val)
								if vec2 is not None:
									_spawn_cands.append(vec2)
						if bp is not None:
							for key, val in bp.items():
								if 'position' in key or key.isdigit():
									vec2 = _read_vec2(val)
									if vec2 is not None:
										_spawn_cands.append(vec2)
						LOG_DEBUG('OfflineBattle.spawn candidates:', len(_spawn_cands), map_name)
						
						def _spawn_ground(x, z):
							# Returns (y, ok): ground height + roof/ledge check
							# (neighbouring ground must exist and be at a similar height)
							y = None
							try:
								hit = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(x, 1000.0, z), Math.Vector3(x, -1000.0, z), 128)
								if hit is not None:
									y = hit[0].y
							except: pass
							if y is None:
								return (None, False)
							for _dx, _dz in ((4.0, 0.0), (-4.0, 0.0), (0.0, 4.0), (0.0, -4.0)):
								try:
									c = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(x + _dx, y + 3.0, z + _dz), Math.Vector3(x + _dx, y - 150.0, z + _dz), 128)
								except:
									continue
								if c is None or abs(c[0].y - y) > 3.0:
									return (y, False)
							return (y, True)
						
						for vec2 in _spawn_cands:
							_sy, _sok = _spawn_ground(vec2.x, vec2.y)
							if _sy is not None and _sok:
								spawn_pos = Math.Vector3(vec2.x, _sy, vec2.y)
								LOG_DEBUG('OfflineBattle.spawn validated pos:', spawn_pos)
								_found_spawn = True
								break
						if not _found_spawn and _spawn_cands:
							# Fallback WITHOUT the roof check, but still bounds- and ground-checked. The old
							# code took candidate[0] blind and used Y=100.0 when the ground probe found
							# nothing - a probe from y=1000 to y=-1000 only misses when the point is off the
							# map, so that is exactly the 'spawned outside the map, hanging in the air' bug
							# (python.log: 'spawn fallback pos: (-126.887, 100, -305.909)').
							_bnd = globals().get('g_offline_bounds', None)
							_picked = None
							for _cand in _spawn_cands:
								# 60 m of slack: arena_defs sometimes place valid points just outside their own
								# declared boundingBox (Himmelsdorf: box ends at -300, a point sits at -306.4).
								if _bnd is not None and not (_bnd[0] - 60.0 <= _cand.x <= _bnd[2] + 60.0 and _bnd[1] - 60.0 <= _cand.y <= _bnd[3] + 60.0):
									LOG_DEBUG('OfflineBattle.spawn candidate off-map, skipped:', _cand.x, _cand.y)
									continue
								_cy, _cok = _spawn_ground(_cand.x, _cand.y)
								if _cy is not None:
									_picked = (_cand, _cy)
									break
							if _picked is None:
								# Nothing resolved: search outward from the first in-bounds candidate for real
								# ground rather than dropping the hull at a made-up altitude.
								_seed = None
								for _cand in _spawn_cands:
									if _bnd is None or (_bnd[0] - 60.0 <= _cand.x <= _bnd[2] + 60.0 and _bnd[1] - 60.0 <= _cand.y <= _bnd[3] + 60.0):
										_seed = _cand
										break
								if _seed is None: _seed = _spawn_cands[0]
								for _r in (10.0, 25.0, 50.0, 100.0):
									for _ox, _oz in ((_r, 0.0), (-_r, 0.0), (0.0, _r), (0.0, -_r)):
										_tx, _tz = _seed.x + _ox, _seed.y + _oz
										if _bnd is not None and not (_bnd[0] <= _tx <= _bnd[2] and _bnd[1] <= _tz <= _bnd[3]):
											continue
										_cy, _cok = _spawn_ground(_tx, _tz)
										if _cy is not None:
											_picked = (Math.Vector2(_tx, _tz), _cy)
											break
									if _picked is not None: break
							if _picked is not None:
								spawn_pos = Math.Vector3(_picked[0].x, _picked[1], _picked[0].y)
							else:
								vec2 = _spawn_cands[0]
								spawn_pos = Math.Vector3(vec2.x, 100.0, vec2.y)
								LOG_DEBUG('OfflineBattle.spawn WARNING: no ground found for any candidate')
							LOG_DEBUG('OfflineBattle.spawn fallback pos:', spawn_pos)
						
						# Face the enemy base instead of a fixed 180 degrees
						try:
							import math
							_eb_list = g_offline_bases.get(2, [])
							if _eb_list:
								_ddx = _eb_list[0].x - spawn_pos.x
								_ddz = _eb_list[0].z - spawn_pos.z
								if _ddx * _ddx + _ddz * _ddz > 25.0:
									spawn_dir = Math.Vector3(0, 0, math.atan2(_ddx, _ddz))
						except Exception:
							pass
						
						# Hardcoded spawn hack removed: the candidates above are validated against
						# roofs/ledges, so the real arena_def spawn points are safe to use directly.
						import math
		except Exception as e:
			LOG_DEBUG('OfflineBattle.XML_ERROR:', str(e))

		# Use a MatrixProduct as the live vehicle matrix provider.
		# Math.Matrix is a STATIC snapshot - WGTranslationOnlyMP.source needs a C++ live provider.
		# MatrixProduct(a=identity, b=identity) acts as a live provider and can be .set()-like via its parts.
		veh_matrix_static = Math.Matrix()
		veh_matrix_static.setTranslate(spawn_pos)
		veh_matrix = Math.MatrixProduct()
		veh_matrix.a = veh_matrix_static
		veh_matrix.b = Math.Matrix()  # identity
		
		# Chassis matrix: includes yaw + position, driven by Servo
		# so hull/turret/gun chain stays perfectly in sync
		chassis_m = Math.Matrix()
		chassis_m.setTranslate(spawn_pos)
		chassis_mp = Math.MatrixProduct()
		chassis_mp.a = chassis_m
		chassis_mp.b = Math.Matrix()  # identity

		class _MockFilter(object): pass
		mf = _MockFilter()
		mf.position = Math.Vector3(spawn_pos)
		mf.yaw = 0.0
		mf.pitch = 0.0
		mf.matrix = veh_matrix

		class _Appearance(object):
			def changeVisibility(self, part, visible, lod=True): pass
			def showStickers(self, visible): pass
			def isUnderwater(self): return False
			def __getattr__(self, name):
				if 'turretMatrix' in name:
					return turret_matrix_local
				if 'gunMatrix' in name:
					if self.compoundModel is not None:
						try:
							return self.compoundModel.node('HP_gunJoint')
						except Exception:
							pass
					return turret_matrix
				if 'hullMatrix' in name:
					if self.compoundModel is not None:
						try:
							return self.compoundModel.node('V')
						except Exception:
							pass
					return turret_matrix
				if 'Matrix' in name or 'Prov' in name:
					if self.compoundModel is not None:
						return self.compoundModel.matrix
					return turret_matrix
				if 'Bounds' in name:
					import Math
					return (Math.Vector3(-1,-1,-1), Math.Vector3(1,1,1))
				if name.startswith(('is','on','set','get','update','show','hide','add','remove','play','stop','start')) or name == 'refresh':
					return lambda *a, **k: None
				import Math
				return Math.Matrix()

		ma = _Appearance()

		td = None
		try:
			if hasattr(player, '_offhangar_battle_ctx'):
				ctx = player._offhangar_battle_ctx
				vdict = ctx.get('vehicles', {})
				vid = player.playerVehicleID
				vinfo = vdict.get(vid)
				if not vinfo and vdict:
					vinfo = list(vdict.values())[0]
				if vinfo:
					td = vinfo.get('vehicleType')
			
			from items import vehicles
			if type(td) is int:
				nationID = (td >> 4) & 15
				vehicleID = td >> 8
				td = vehicles.VehicleDescr(typeID=(nationID, vehicleID))
				LOG_DEBUG('PHYSICS_DUMP:', td.physics)
			elif td is None:
				td = vehicles.VehicleDescr(typeName='ussr:MS-1')
			elif type(td).__name__ == 'FakeDesc':
				# If offline_battle_stack gave us FakeDesc, fallback to MS-1 so we don't crash
				td = vehicles.VehicleDescr(typeName='ussr:MS-1')
		except Exception as e:
			LOG_DEBUG('OfflineBattle.td error', str(e))

		LOG_DEBUG('OfflineBattle.td resolved:', td, type(td).__name__ if td else None)
		if td is not None:
			LOG_DEBUG('OfflineBattle.td types:', type(td.chassis), type(td.hull), type(td.turret))
			if hasattr(td.chassis, 'keys'):
				LOG_DEBUG('OfflineBattle.td keys:', td.chassis.keys())

		# Inject into player so the GUI finds it!
		player.vehicleTypeDescriptor = td
		# Publish the battle descriptor via _offhangar_td so the account's
		# vehicleTypeDescriptor getattribute override returns THIS tank (not a
		# fresh tier-1 MS-1); the native penetration marker, tracer ballistics
		# and maxHealth then read the real tank being driven. Cleared in the
		# sweep's 'muzzle' stage so the hangar falls back to its stub.
		player._offhangar_td = td

		loaded_models = {'chassis': None, 'hull': None, 'turret': None, 'gun': None, 'td': td}
		loaded_models['chassis_mp'] = chassis_mp
		if td is not None:
			for part_name in ('chassis', 'hull', 'turret', 'gun'):
				try:
					part_desc = getattr(td, part_name, None)
					if part_desc is not None and 'models' in part_desc and 'undamaged' in part_desc['models']:
						modelName = part_desc['models']['undamaged']
						m = BigWorld.Model(modelName)
						loaded_models[part_name] = m
						LOG_DEBUG('OfflineBattle.model loaded:', part_name, modelName)
				except Exception as e:
					LOG_DEBUG('OfflineBattle load model error:', part_name, str(e))
			
			# BigWorld.Model() is async - the model isn't ready immediately.
			# Use a callback to add them after they've loaded.
			_models_to_add = dict((k, v) for k, v in loaded_models.items() if v is not None)
			_add_attempts = [0]
			
			
			def _add_models_when_ready():
				_add_attempts[0] += 1
				try:
					chassis = _models_to_add.get('chassis')
					hull    = _models_to_add.get('hull')
					turret  = _models_to_add.get('turret')
					gun     = _models_to_add.get('gun')
										
					if chassis is not None:
						chassis.position = Math.Vector3(spawn_pos)
						chassis.yaw = 0.0
						_add_model(chassis)
						try:
							chassis.addMotor(BigWorld.Servo(chassis_mp))
							LOG_DEBUG('OfflineBattle.chassis Servo attached')
						except Exception as e:
							LOG_DEBUG('OfflineBattle.chassis Servo error:', str(e))
						
						if hull is not None:
							try:
								chassis.node('V').attach(hull)
								LOG_DEBUG('OfflineBattle: hull attached to chassis.V')
							except Exception as e:
								LOG_DEBUG('OfflineBattle.attach hull error:', str(e))
								hull.position = Math.Vector3(spawn_pos)
								_add_model(hull)
						
							# Attach turret to hull node 'HP_turretJoint'
							if turret is not None:
								try:
									turret_mat = Math.Matrix()
									turret_mat.setIdentity()
									loaded_models['turret_mat'] = turret_mat
									hull.node('HP_turretJoint', turret_mat).attach(turret)
									LOG_DEBUG('OfflineBattle: turret attached to hull.HP_turretJoint')
								except Exception as e:
									LOG_DEBUG('OfflineBattle.attach turret error:', str(e))
								
								# Apply Camouflage and Emblems
								try:
									import items.vehicles as iv
									cust = iv.g_cache.customization(td.type.id[0])
									camo_kind = getattr(player.arena.arenaType, 'vehicleCamouflageKind', 0) if hasattr(player, 'arena') and hasattr(player.arena, 'arenaType') else 0
									camo_params = td.camouflages[camo_kind] if hasattr(td, 'camouflages') and len(td.camouflages) > camo_kind else None
									LOG_DEBUG('OfflineBattle.customization:', 'kind', camo_kind, 'params', camo_params)
									# Offline QoL: if the map-season slot is empty, fall back to any season
									# the player has painted - otherwise the bought camo never shows here.
									if (camo_params is None or camo_params[0] is None) and hasattr(td, 'camouflages'):
										for _ck in range(len(td.camouflages)):
											if td.camouflages[_ck] is not None and td.camouflages[_ck][0] is not None:
												camo_params = td.camouflages[_ck]
												LOG_DEBUG('OfflineBattle.customization: season fallback ->', _ck, camo_params)
												break
									if camo_params is not None and camo_params[0] is not None:
										camo = cust['camouflages'].get(camo_params[0]) if cust else None
										if camo is not None:
											tex = camo['texture']
											colors = camo['colors']
											defaultTiling = camo['tiling'].get(td.type.compactDescr)
											weights = Math.Vector4((colors[0]>>24)/255.0, (colors[1]>>24)/255.0, (colors[2]>>24)/255.0, (colors[3]>>24)/255.0)
											for p_name, p_mdl in [('chassis', chassis), ('hull', hull), ('turret', turret), ('gun', gun)]:
												if p_mdl is not None:
													excl = td.type.camouflageExclusionMask
													tiling = defaultTiling
													if tiling is None: tiling = td.type.camouflageTiling
													p_desc = getattr(td, p_name, None)
													if p_desc is not None:
														coeff = p_desc.get('camouflageTiling')
														if coeff is not None and tiling is not None:
															tiling = (tiling[0]*coeff[0], tiling[1]*coeff[1], tiling[2]*coeff[2], tiling[3]*coeff[3])
														if 'camouflageExclusionMask' in p_desc:
															excl = p_desc['camouflageExclusionMask']
													if excl != '' and tex != '':
														if p_name == 'chassis':
															# Chassis camo must go THROUGH the track fashion (wg_fashion), like the
															# original __updateCamouflage: a second WGBaseFashion on the chassis
															# detaches the scrolling track material (wheels spin, tracks freeze).
															loaded_models['_camo_args'] = (tex, excl, tiling, colors[0], colors[1], colors[2], colors[3], weights)
														else:
															fashion = getattr(p_mdl, 'wg_baseFashion', None)
															if fashion is None: fashion = p_mdl.wg_baseFashion = BigWorld.WGBaseFashion()
															fashion.setCamouflage(tex, excl, tiling, colors[0], colors[1], colors[2], colors[3], weights)
									
									import VehicleStickers
									emblemPositions = (
										('hull', hull, td.hull['emblemSlots']),
										('gun' if td.turret['showEmblemsOnGun'] else 'turret', gun if td.turret['showEmblemsOnGun'] else turret, td.turret['emblemSlots']),
										('turret' if td.turret['showEmblemsOnGun'] else 'gun', turret if td.turret['showEmblemsOnGun'] else gun, [])
									)
									if not hasattr(player, '_offhangar_stickers'): player._offhangar_stickers = []
									for cName, p_mdl, slots in emblemPositions:
										if p_mdl is not None:
											stickers = VehicleStickers.VehicleStickers(td, slots, cName == 'hull', None)
											try:
												stickers.attachStickers(p_mdl, p_mdl.node(''), False)
											except Exception:
												stickers.attachStickers(p_mdl, p_mdl.root, False)
											player._offhangar_stickers.append(stickers)
								except Exception as e:
									import traceback
									import traceback

									LOG_DEBUG('OfflineBattle.customization error:', str(e), traceback.format_exc())

								# Attach gun to turret node 'HP_gunJoint'
								if gun is not None:
									try:
										gun_mat = Math.Matrix()
										gun_mat.setIdentity()
										loaded_models['gun_mat'] = gun_mat
										turret.node('HP_gunJoint', gun_mat).attach(gun)
										LOG_DEBUG('OfflineBattle: gun attached to turret.HP_gunJoint')
										# Barrel recoil animation on the player's own gun
										player._offhangar_gun_recoil = _setup_gun_recoil(gun, td)
										# Hull rocking (shot impulse / pitch-roll swinging) on the chassis
										player._offhangar_swinging = _setup_swinging(chassis, td)
									except Exception as e:
										LOG_DEBUG('OfflineBattle.attach gun error:', str(e))

								try:
									import VehicleStickers
									_nodes = loaded_models['sticker_nodes'] = {
										'hull': chassis.node('V') if chassis else hull.node(''),
										'turret': hull.node('HP_turretJoint', turret_mat) if hull else turret.node(''),
										'gun': turret.node('HP_gunJoint', gun_mat) if turret else gun.node('')
									}
									_emblemPositions = (
										('hull', hull, td.hull['emblemSlots']),
										('gun' if td.turret['showEmblemsOnGun'] else 'turret', gun if td.turret['showEmblemsOnGun'] else turret, td.turret['emblemSlots']),
										('turret' if td.turret['showEmblemsOnGun'] else 'gun', turret if td.turret['showEmblemsOnGun'] else gun, [])
									)
									if not hasattr(player, '_offhangar_stickers'): player._offhangar_stickers = []
									if not hasattr(player, '_offhangar_sticker_map'): player._offhangar_sticker_map = {}
									for cName, p_mdl, slots in _emblemPositions:
										if p_mdl is not None:
											stickers = VehicleStickers.VehicleStickers(td, slots, cName == 'hull', None)
											p_node = _nodes.get(cName)
											if p_node is not None:
												stickers.attachStickers(p_mdl, p_node, False)
												player._offhangar_stickers.append(stickers)
												# Map by component so shell-hole decals can target the hit
												# part. Store the NODE too: attached component models report
												# a local (identity) matrix, so the node gives the world
												# transform needed to place the decal correctly.
												player._offhangar_sticker_map[cName] = (stickers, p_mdl, p_node)
								except Exception as e:
									import traceback
									LOG_DEBUG('OfflineBattle.stickers error:', str(e), traceback.format_exc())
					elif hull is not None:
						hull.position = Math.Vector3(spawn_pos)
						_add_model(hull)
						LOG_DEBUG('OfflineBattle.addModel OK: hull (no chassis)')
					
					root_model = chassis or hull
					ma.models = [root_model]
					ma.compoundModel = root_model
					LOG_DEBUG('OfflineBattle.compoundModel set, attempt:', _add_attempts[0])


					# Engine sounds are now initialized in _step_offline_physics
				
				except Exception as e:
					import traceback
					LOG_DEBUG('OfflineBattle._add_models_when_ready ERROR:', traceback.format_exc())
					if _add_attempts[0] < 10:
						BigWorld.callback(0.3, _add_models_when_ready)
			
			BigWorld.callback(0.2, _add_models_when_ready)
			
			# Set temporary compoundModel so camera logic doesn't fail
			root_model = loaded_models['chassis'] if loaded_models['chassis'] is not None else loaded_models['hull']
			ma.models = [root_model]
			ma.compoundModel = root_model

		try:
			for hitTester in td.getHitTesters():
				hitTester.loadBspModel()
		except Exception as e:
			LOG_DEBUG("Error loading hitTesters for player:", str(e))

		class _MockVeh(object):
			# Spotting is simulated for ENEMIES ONLY - allies count as always visible, so
			# nothing ever ASSIGNS _spot_visible on an allied mock. __getattr__ below then
			# answers None for it, and `getattr(mock, '_spot_visible', True)` hands back
			# that None instead of the default (the same trap as the `None <= 0` health
			# read in the outline picker). Every consumer evaluates `not None` == True and
			# treats EVERY ALLY AS UNSPOTTED - which silently removed allies from the
			# outline picker (no green silhouette), from the pen indicator (the reticle
			# ignored a friendly tank under it: "I can aim through allied tanks"), from
			# muzzle-flash rendering, and from the entity filter feed.
			# A CLASS attribute is found by normal lookup, so __getattr__ is never
			# consulted and the default can no longer leak through; the enemy spotting
			# block still shadows it per instance exactly as before.
			_spot_visible = True

			def __init__(self):
				self.damage_from_player = 0
				self.damage_from_bots = 0
				self.hits_from_player = 0
				self.matrix = Math.Matrix()
				self.matrix.setIdentity()
				self.position = Math.Vector3(spawn_pos)
				self.yaw = 0.0
				self.pitch = 0.0
				self.roll = 0.0
				self.filter = mf
				self.appearance = ma
				self.isPlayer = True
				self.typeDescriptor = td
				self.health = getattr(td, 'maxHealth', 400)
				self.maxHealth = getattr(td, 'maxHealth', 400)
				self.isStarted = True
				# Callable AND truthy - see _OffhAliveState.
				self.isAlive = _OffhAliveState(True)
				self.id = getattr(player, 'playerVehicleID', 0)
				self.model = getattr(self.appearance, 'compoundModel', None)
				
				class _ModelsDesc(object):
					def __getitem__(self, key):
						if key in loaded_models and loaded_models.get(key) is not None:
							return {'model': loaded_models[key]}
						# Return None model so SniperCamera falls through
						# to the MatrixProduct branch (which uses getOwnVehicleMatrix)
						return {'model': None}
				self.appearance.modelsDesc = _ModelsDesc()
			def getAutorotation(self): return False
			def __getattr__(self, name): return None
			
			def getComponents(self):
				import Math
				res = []
				m = Math.Matrix()
				m.setIdentity()
				res.append((self.typeDescriptor.chassis, m))
				
				hullOffset = self.typeDescriptor.chassis['hullPosition']
				m = Math.Matrix()
				m.setTranslate(-hullOffset)
				res.append((self.typeDescriptor.hull, m))
				
				if getattr(self, 'isPlayer', False):
					tYaw = turret_matrix_local.yaw
					gPitch = gun_matrix.pitch if 'gun_matrix' in globals() else 0.0
				else:
					tYaw = getattr(self, '_t_mat', m).yaw
					gPitch = getattr(self, '_g_mat', m).pitch
					
				turretMatrix = Math.Matrix()
				turretMatrix.setTranslate(-hullOffset - self.typeDescriptor.hull['turretPositions'][0])
				m = Math.Matrix()
				m.setRotateY(-tYaw)
				turretMatrix.postMultiply(m)
				res.append((self.typeDescriptor.turret, turretMatrix))
				
				gunMatrix = Math.Matrix()
				gunMatrix.setTranslate(-self.typeDescriptor.turret['gunPosition'])
				m = Math.Matrix()
				m.setRotateX(-gPitch)
				gunMatrix.postMultiply(m)
				gunMatrix.preMultiply(turretMatrix)
				res.append((self.typeDescriptor.gun, gunMatrix))
				
				return res

			def collideSegment(self, startPoint, endPoint, skipGun=False):
				import Math
				worldToVehMatrix = Math.Matrix(self.matrix)
				worldToVehMatrix.invert()
				startPoint = worldToVehMatrix.applyPoint(startPoint)
				endPoint = worldToVehMatrix.applyPoint(endPoint)
				res_closest = None
				all_hits = []
				for (compDescr, compMatrix) in self.getComponents():
					if skipGun and compDescr.get('itemTypeName') == 'vehicleGun':
						continue
					if not hasattr(compDescr.get('hitTester'), 'localHitTest'):
						continue
					collisions = compDescr['hitTester'].localHitTest(compMatrix.applyPoint(startPoint), compMatrix.applyPoint(endPoint))
					if collisions is None:
						continue
					for (dist, _, hitAngleCos, matKind) in collisions:
						matInfo = compDescr.get('materials', {}).get(matKind)
						if matInfo is None:
							# The mesh DOES carry device geometry the vehicle XML never assigns.
							# Scanned all 1975 collision meshes in the game: surveyingDevice appears
							# on 218 of 252 vehicles and gunBreech on 37, yet _readArmor only ever
							# builds the per-vehicle table from the <armor> section, which names
							# armor_N, the two tracks and the gun. Every optics hit was therefore
							# thrown away. common/vehicle.xml defines those kinds globally, with the
							# right extra and hit chances - fall back to it.
							try:
								from items import vehicles as _vgm
								matInfo = (_vgm.g_cache.commonConfig.get('materials') or {}).get(matKind)
								if matInfo is not None:
									_gm_seen = globals().setdefault('g_offh_global_mat', set())
									if matKind not in _gm_seen:
										_gm_seen.add(matKind)
										LOG_DEBUG('MATKIND from global table: id=%s extra=%s' % (matKind, getattr(getattr(matInfo, 'extra', None), 'name', None)))
							except Exception:
								matInfo = None
						# Does the COLLISION MESH carry material kinds the vehicle XML never
						# defines? The per-vehicle materials dict is built by _readArmor from the
						# <armor> section alone, and that section only ever names armor_N, the two
						# tracks and the gun. But common/vehicle.xml DOES define engine, ammoBay,
						# fuelTank, radio and every crewman as material kinds with their extras and
						# hit chances. If the BSP has triangles tagged with those kinds, the geometry
						# is present and only the per-vehicle LOOKUP is missing - which would be
						# fixable from the global table, no external tool needed. Report each unknown
						# kind once so this is decided by data instead of assumption.
						if matInfo is None:
							try:
								_seen_mk = globals().setdefault('g_offh_unknown_matkinds', set())
								if matKind not in _seen_mk:
									_seen_mk.add(matKind)
									_mkn = matKind
									try:
										import material_kinds as _MK
										for _n, _i in _MK.IDS_BY_NAMES.items():
											if _i == matKind:
												_mkn = _n
												break
									except Exception:
										pass
									LOG_DEBUG('MATKIND unresolved: id=%s name=%s comp=%s' % (matKind, _mkn, compDescr.get('itemTypeName', '?')))
							except Exception:
								pass
						all_hits.append((dist, hitAngleCos, matInfo, compDescr))
						if res_closest is None or res_closest[0] >= dist:
							res_closest = (dist, hitAngleCos, getattr(matInfo, 'armor', 0) if matInfo is not None else 0)
				if res_closest is not None:
					return (res_closest[0], res_closest[1], res_closest[2], all_hits)
				return None

		# Clear persistent data from previous offline battles, BUT keep the player!
		try:
			global G_OFFHANGAR_SHOTS_FIRED
			G_OFFHANGAR_SHOTS_FIRED = 0
			# Fresh who-hit-whom record for this battle. Everything the results
			# screen prints is summed out of it, so a stale one from the previous
			# battle would carry its damage and frags into this one.
			try:
				from gui.mods.offhangar import battle_ledger as _BLED
				_BLED.reset()
			except Exception:
				pass
			player = BigWorld.player()
			if hasattr(player, 'arena') and player.arena is not None:
				p_id = getattr(player, 'playerVehicleID', -1)
				if hasattr(player.arena, 'vehicles') and type(player.arena.vehicles) is dict:
					p_veh = player.arena.vehicles.get(p_id, None)
					player.arena.vehicles.clear()
					if p_veh is not None:
						player.arena.vehicles[p_id] = p_veh
				if hasattr(player.arena, 'statistics') and type(player.arena.statistics) is dict:
					p_stat = player.arena.statistics.get(p_id, None)
					player.arena.statistics.clear()
					if p_stat is not None:
						# Reset frags to 0 for the new battle!
						if 'frags' in p_stat: p_stat['frags'] = 0
						player.arena.statistics[p_id] = p_stat
		except: pass
		
		mock_veh = _MockVeh()

		mock_vehicles = {getattr(BigWorld.player(), 'playerVehicleID', -1): mock_veh}
		global G_MOCK_VEHICLES
		G_MOCK_VEHICLES = mock_vehicles

		# --- X-ray overlay (adopted). OFF by default and not even imported then:
		# it draws interior modules and crew straight through the armour, which is
		# a debug view here but exactly the kind of mod that gets accounts banned
		# on a live server - and this res_mods tree sits in a client that can log
		# in. config internal_xray_overlay=true is the only way it loads, and only
		# then are F8/F9/F10 bound.
		globals()['g_offh_internal_xray'] = None
		try:
			from _constants import CONFIG_OPTIONS as _XCFG
			if bool(_XCFG.get('internal_xray_overlay', False)):
				from gui.mods.offhangar import internal_layout_debug as _ILD
				from gui.mods.offhangar import internal_hit_layouts as _IHL2
				globals()['g_offh_internal_xray'] = _ILD.InternalLayoutDebugController(
					lambda: globals().get('G_MOCK_VEHICLES') or {},
					lambda: BigWorld.player(),
					_IHL2)
				LOG_DEBUG('X-ray overlay armed: F8 toggle, F9 view mode, F10 labels')
		except Exception as _xe:
			globals()['g_offh_internal_xray'] = None
			LOG_DEBUG('X-ray overlay unavailable:', str(_xe))
		# The sticker list lives on the persistent account entity; without this
		# reset it grew by ~6 VehicleStickers objects every battle.
		try: player._offhangar_stickers = []
		except Exception: pass

		# Wrap once and resolve the mock registry at call time: re-wrapping every
		# battle nested the previous wrapper in _orig_entity, so each battle's
		# whole mock_vehicles dict (bots, descriptors, model refs) stayed
		# reachable forever and the lookup chain grew battle by battle.
		if not getattr(BigWorld, '_offh_entity_wrapped', False):
			_orig_entity = BigWorld.entity
			def _mock_entity(eid):
				_mv = globals().get('G_MOCK_VEHICLES', {}) or {}
				if eid == getattr(BigWorld.player(), 'playerVehicleID', -1) and eid in _mv:
					return _mv[eid]
				orig_e = _orig_entity(eid)
				if orig_e is None and eid in _mv:
					return _mv[eid]
				return orig_e
			BigWorld.entity = _mock_entity
			BigWorld._offh_entity_wrapped = True
		# Minimap & friends read BigWorld.entities[id] directly (minimap.pyc:548
		# matrix = BigWorld.entities[id].matrix). Mock bots are not real engine
		# entities -> KeyError on every notifyVehicleStart ('GUI Add error') and
		# no bot ever reached the minimap. Wrap the dict: real entities first,
		# then the mock registry; enumeration stays original-only (engine-safe).
		if not getattr(BigWorld, '_offh_entities_wrapped', False):
			class _OffhEntities(object):
				def __init__(self, orig):
					self._o = orig
				def __getitem__(self, k):
					try:
						return self._o[k]
					except KeyError:
						_mv = globals().get('G_MOCK_VEHICLES', {}) or {}
						if k in _mv:
							return _mv[k]
						raise
				def get(self, k, d=None):
					try:
						return self[k]
					except KeyError:
						return d
				def __contains__(self, k):
					if k in self._o:
						return True
					return k in (globals().get('G_MOCK_VEHICLES', {}) or {})
				def keys(self):
					return self._o.keys()
				def values(self):
					return self._o.values()
				def items(self):
					return self._o.items()
				def iteritems(self):
					return self._o.iteritems()
				def itervalues(self):
					return self._o.itervalues()
				def __iter__(self):
					return iter(self._o)
				def __len__(self):
					return len(self._o)
				def __getattr__(self, n):
					return getattr(self._o, n)
			BigWorld.entities = _OffhEntities(BigWorld.entities)
			BigWorld._offh_entities_wrapped = True

		player.getVehicleAttached = lambda: mock_veh
		player.getOwnVehicleMatrix = lambda: veh_matrix
		player.getOwnVehiclePosition = lambda: mock_veh.position
		player._offhangar_gui_visible = False
		def _mock_handleKey(key, isDown, mods=0):
			aih = getattr(player, 'inputHandler', None)
			if aih is not None and hasattr(aih, 'handleKeyEvent'):
				try: return aih.handleKeyEvent(key, isDown, mods)
				except: pass
			return False
		player.handleKey = _mock_handleKey

		def _offh_mouse_delta(args):
			try:
				if len(args) == 1 and hasattr(args[0], 'dx'):
					return (float(args[0].dx), float(args[0].dy), float(args[0].dz))
				if len(args) >= 3:
					return (float(args[0]), float(args[1]), float(args[2]))
			except Exception:
				pass
			return (0.0, 0.0, 0.0)

		def _offh_apply_zoom_attrs(ctrl, dz):
			# Ported: some 0.8.2 camera handlers silently reject wheel input in the
			# offline shell. Touch camera-only zoom fields (never aiming/ballistics).
			try:
				dz = float(dz)
				if abs(dz) <= 0.0001:
					return False
			except Exception:
				return False
			try:
				candidates = []
				cam = getattr(ctrl, 'camera', None)
				for obj in (cam, BigWorld.camera()):
					if obj is not None and obj not in candidates:
						candidates.append(obj)
				for obj in list(candidates):
					for name in ('_ArcadeCamera__cam', '_SniperCamera__cam', '_StrategicCamera__cam', '_camera', 'camera', 'cam'):
						try:
							sub = getattr(obj, name, None)
							if sub is not None and sub not in candidates:
								candidates.append(sub)
						except Exception:
							pass
				handled = False
				scale = 1.0 - max(-3.0, min(3.0, dz)) * 0.12
				if scale < 0.35:
					scale = 0.35
				if scale > 2.25:
					scale = 2.25
				# NEVER touch SniperCamera/StrategicCamera zoom state here: those
				# modes render their own camera offline and apply dz natively.
				# SniperCamera.__setupZoom walks the discrete zooms list and exits
				# to arcade only on the EXACT check zoom == zooms[0]; a scaled
				# float (2.0 -> 1.76) breaks both the levels and the scroll-out.
				attrs = ('distance', 'dist', 'height', 'curHeight', 'curDistance', '_distance', '_dist', '_height', '_curHeight', '_curDistance', '_ArcadeCamera__distance', '_ArcadeCamera__dist', '_ArcadeCamera__curDist')
				for obj in candidates:
					for name in attrs:
						try:
							old = getattr(obj, name)
						except Exception:
							continue
						try:
							if isinstance(old, (int, long, float)):
								new = float(old) * scale
								if new < 1.0:
									new = 1.0
								if new > 1200.0:
									new = 1200.0
								setattr(obj, name, new)
								handled = True
						except Exception:
							pass
				return handled
			except Exception:
				return False

		def _offh_mouse_cam_fallback(aih, args):
			try:
				ctrl = getattr(aih, 'ctrl', None)
				if ctrl is None:
					return False
				cam = getattr(ctrl, 'camera', None)
				dx, dy, dz = _offh_mouse_delta(args)
				handled = False
				if cam is not None and hasattr(cam, 'update'):
					try:
						# clamp dz like the game's own paths (_clamp(-1, 1, dz))
						# so one notch never jumps several zoom steps
						cam.update(dx, dy, max(-1.0, min(1.0, dz)))
						handled = True
					except Exception:
						pass
				if abs(dz) > 0.0001 and _offh_apply_zoom_attrs(ctrl, dz):
					handled = True
				return handled
			except Exception:
				return False

		def _mock_handleMouse(*args):
			# Ported arty-camera fix: without this hook the wheel never reaches the
			# strategic camera offline, so the SPG view height could not be changed.
			try:
				aih = getattr(player, 'inputHandler', None)
				if aih is not None and hasattr(aih, 'handleMouseEvent'):
					try:
						if len(args) == 1 and hasattr(args[0], 'dx'):
							_ret = aih.handleMouseEvent(args[0].dx, args[0].dy, args[0].dz)
						else:
							_ret = aih.handleMouseEvent(*args)
						if (not _ret) and abs(_offh_mouse_delta(args)[2]) > 0.0001:
							if _offh_mouse_cam_fallback(aih, args):
								return True
						return _ret
					except Exception:
						if _offh_mouse_cam_fallback(aih, args):
							return True
			except Exception:
				pass
			return False
		player.handleMouseEvent = _mock_handleMouse
		
		import game
		if not getattr(game, '_offhangar_hooked', False):
			game._offhangar_hooked = True
			orig_game_handleKeyEvent = game.handleKeyEvent
			def _mock_game_handleKeyEvent(event):
				# NO ESC handling here! The flash menu handles ESC itself (on key
				# DOWN) and fires Battle.cursorVisibility on BOTH open and close -
				# proven by the CursorDBG log - and the patched cursorVisibility
				# below drives the input gate from it. The old key-UP toggle here
				# double-acted on the same press, always leaving the state inverted
				# from the actual menu (aim stuck with cursor shown after closing,
				# or menu open with no cursor).
				return orig_game_handleKeyEvent(event)
			game.handleKeyEvent = _mock_game_handleKeyEvent
			# Wheel: offline the flash GUI often EATS wheel events before they
			# reach the AIH (InputDBG: in sniper the fullscreen binoculars ate
			# 19 of 21 events), so the native zoom path never sees them. The old
			# unconditional fallback masked that but also ran when the native
			# path DID handle the wheel - double zoom in arcade, and its direct
			# _SniperCamera__zoom writes broke the discrete 2/4/8 walk and the
			# scroll-out exit (exact zoom == zooms[0] check in __setupZoom).
			# Correct rule: detect whether the native path consumed this wheel
			# event, and only if not, re-deliver dz through the camera wrapper's
			# own update() so all stock logic (zoom walk, clamps, mode
			# transitions) still runs:
			#  - Arcade/Strategic apply dz SYNCHRONOUSLY to their __camDist, so
			#    an unchanged __camDist right after the orig call means the
			#    event was eaten. (Their __camDist also legitimately stays put
			#    at the range ends - re-delivering there is a no-op, except the
			#    min-end where it correctly (re)fires the mode transition.)
			#  - Sniper stores dz in __dxdydz and applies it next frame, so a
			#    zero stored dz right after the orig call means it was eaten;
			#    re-deliver preserving any stored dx/dy from move events.
			orig_game_handleMouseEvent = game.handleMouseEvent
			def _mock_game_handleMouseEvent(event):
				# no zoom while dead/spectating - postmortem must not sniper-zoom into the wreck.
				# ONLY while a battle is actually on screen: this handler is installed on
				# game.handleMouseEvent globally, so it runs in the hangar and every menu too,
				# and _is_dead / _offh_spectating are cleared at battle START, not on exit.
				# After the first battle you died in, both stayed True all the way back to the
				# hangar and this swallowed every mouse wheel event - scrolling in the menus
				# died out of nowhere and only came back once the next battle started.
				try:
					_pz = BigWorld.player()
					if _pz is not None and (getattr(_pz, '_is_dead', False) or getattr(_pz, '_offh_spectating', False)):
						_in_battle = False
						try:
							from gui import WindowsManager as _wmz
							_in_battle = getattr(_wmz.g_windowsManager, 'battleWindow', None) is not None
						except Exception:
							_in_battle = False
						if _in_battle and abs(float(getattr(event, 'dz', 0.0))) > 0.0001:
							return True
				except Exception:
					pass
				pre = None
				try:
					dz = float(getattr(event, 'dz', 0.0))
					if abs(dz) > 0.0001:
						p = BigWorld.player()
						if p is not None and getattr(p, 'isOffline', False):
							aih = getattr(p, 'inputHandler', None)
							ctrl = getattr(aih, 'ctrl', None)
							cam = getattr(ctrl, 'camera', None)
							# gated input (pause menu open / cursor detached):
							# eaten on purpose, do not zoom
							if cam is not None and getattr(aih, '_AvatarInputHandler__isStarted', False) and getattr(aih, '_AvatarInputHandler__detachCount', 0) >= 0:
								dist = getattr(cam, '_ArcadeCamera__camDist', None)
								if dist is None:
									dist = getattr(cam, '_StrategicCamera__camDist', None)
								pre = (aih, ctrl, cam, None if dist is None else float(dist), dz)
				except Exception:
					pre = None
				result = orig_game_handleMouseEvent(event)
				if pre is not None:
					try:
						aih, ctrl, cam, dist_before, dz = pre
						if getattr(aih, 'ctrl', None) is ctrl:
							dzc = max(-1.0, min(1.0, dz))
							if dist_before is not None:
								dist_after = getattr(cam, '_ArcadeCamera__camDist', None)
								if dist_after is None:
									dist_after = getattr(cam, '_StrategicCamera__camDist', None)
								if dist_after is not None and abs(float(dist_after) - dist_before) < 0.0001:
									cam.update(0, 0, dzc)
							else:
								stored = getattr(cam, '_SniperCamera__dxdydz', None)
								if stored is not None and abs(float(stored[2])) < 0.0001:
									cam.update(float(stored[0]), float(stored[1]), dzc)
					except Exception:
						pass
				return result
			game.handleMouseEvent = _mock_game_handleMouseEvent
			# Route the flash cursor callback through the same state-setter so
			# menu-button closes restore aiming (fixes the stuck-aim repro).
			try:
				from gui.Scaleform.Battle import Battle as _BattleWnd
				if not getattr(_BattleWnd, '_offh_cursor_patched', False):
					_orig_cv = _BattleWnd.cursorVisibility
					def _offh_cursorVisibility(self, callbackId, visible, x=None, y=None, customCall=False, enableAiming=True):
						_orig_cv(self, callbackId, visible, x, y, customCall, enableAiming)
						try:
							import BigWorld
							p = BigWorld.player()
							if p is not None and getattr(p, 'isOffline', False):
								LOG_DEBUG('CursorDBG: flash cursorVisibility ->', visible)
								_offh_set_battle_gui_mode(visible)
						except Exception:
							pass
					_BattleWnd.cursorVisibility = _offh_cursorVisibility
					_BattleWnd._offh_cursor_patched = True
			except Exception:
				LOG_CURRENT_EXCEPTION()
			# Alt-tab fix: offline the aim object can be None during a device recreate;
			# the exception aborted game.onRecreateDevice mid-way so the GUI resetters
			# never ran and HUD/input came back broken after tabbing back in.
			try:
				import AvatarInputHandler as _AIHmod
				if not getattr(_AIHmod.AvatarInputHandler, '_offh_rc_wrapped', False):
					_orig_rc = _AIHmod.AvatarInputHandler._AvatarInputHandler__onRecreateDevice
					def _offh_safe_rc(self, *a, **kw):
						try:
							return _orig_rc(self, *a, **kw)
						except Exception:
							pass
					_AIHmod.AvatarInputHandler._AvatarInputHandler__onRecreateDevice = _offh_safe_rc
					_AIHmod.AvatarInputHandler._offh_rc_wrapped = True
			except Exception:
				pass
		
		def _leaveArena():
			_battle_finished[0] = True
			# Tear the overlay down before the space goes: it holds GUI components
			# and a repeating callback, and both outlive the arena otherwise.
			_xr_stop = globals().get('g_offh_internal_xray')
			if _xr_stop is not None:
				try:
					_xr_stop.stop()
				except Exception:
					pass
				globals()['g_offh_internal_xray'] = None
			# Exactly ONE exit path may tear the battle down. Player death
			# schedules _exit_battle(+3s) AND triggers battle results ->
			# _leaveArena: both ran the full teardown, so the hangar was
			# destroyed + re-inited TWICE, the second init racing the first
			# one's async load -> broken garage return and a leaked
			# half-loaded hangar space per occurrence.
			if _exit_done[0]:
				return
			_exit_done[0] = True
			# ESC runs battle-teardown AND the synchronous hangar load in this
			# ONE call. Free the whole battle (models, sounds, mocks, mapped
			# battle space) FIRST, or g_hangarSpace.init below OOM-crashes.
			try:
				_offh_battle_sweep('quit')
			except:
				pass
			try: player._offhangar_stickers = []
			except Exception: pass
			try:
				import SoundGroups as _SG
				if getattr(_SG, 'g_instance', None) is not None:
					_SG.g_instance.enableArenaSounds(False)
					_SG.g_instance.enableLobbySounds(True)
			except Exception: pass
			try:
				_aih = getattr(player, 'inputHandler', None)
				if _aih is not None:
					try: _aih._AvatarInputHandler__isStarted = False
					except: pass
					for _cm in getattr(_aih, '_AvatarInputHandler__ctrls', {}).values():
						try: _cm.destroy()
						except: pass
					# Parity with the death-exit path: every battle's AIH registers
					# __onRecreateDevice in game.g_guiResetters at construction; left
					# in, one dead resetter per battle piles up and a later device
					# recreate (alt-tab, res change) runs them all against torn-down
					# controls - the GUI never comes back from that.
					try:
						import game
						if hasattr(_aih, '_AvatarInputHandler__onRecreateDevice'):
							game.g_guiResetters.remove(_aih._AvatarInputHandler__onRecreateDevice)
					except: pass
					player.inputHandler = None
			except Exception: pass

			try:
				from gui import WindowsManager
				if hasattr(WindowsManager.g_windowsManager, 'destroyBattle'):
					WindowsManager.g_windowsManager.destroyBattle()
				else:
					WindowsManager.g_windowsManager.hideAll()
				if hasattr(WindowsManager.g_windowsManager, 'showLobby'):
					WindowsManager.g_windowsManager.showLobby()
			except Exception: pass

			try:
				import BigWorld
				BigWorld.camera(None)
				BigWorld.worldDrawEnabled(True)
			except: pass

			try:
				from gui.Scaleform.utils.HangarSpace import g_hangarSpace
				if g_hangarSpace is not None:
					try: g_hangarSpace.destroy()
					except Exception: pass
					
					# Prevent showLobby from destroying the space
					def _mock_refreshSpace(self, isPremium):
						pass
					g_hangarSpace.__class__.refreshSpace = _mock_refreshSpace
					
					# Force premium
					def _mock_getSpacePath(self, isPremium):
						return self._HangarSpace__space.getDefSpacePath(True)
					g_hangarSpace.__class__._HangarSpace__getSpacePath = _mock_getSpacePath
					
					# Init manually
					# WG 'no man's land' purge: hangar destroyed, nothing active, before re-init.
					_offh_safe_purge()
					g_hangarSpace.init(True)
			except Exception: pass


			try:
				global g_offline_models
				# Clear FIRST (see sweep 'models' stage): delModel's pending error
				# raises at loop exhaustion and would skip a post-loop clear,
				# leaking the dead battle's models into the next one.
				_gm_list = list(g_offline_models)
				g_offline_models = []
				for m in _gm_list:
					_offh_del_model(m)
			except Exception: pass
			try:
				import gui.mods.offhangar._constants as _c
				for _e in BigWorld.entities.values():
					if _e.__class__.__name__ in ('PlayerAccount', 'Account'):
						_e._offline_allow_become_non_player = True
						if hasattr(_e, '_offhangar_orig_stats') and _e._offhangar_orig_stats is not None:
							_e.stats = _e._offhangar_orig_stats
						try: _e.showGUI(_c.OFFLINE_GUI_CTX)
						except Exception: pass
			except Exception: pass
			
		player.leaveArena = _leaveArena
		
		def _setGUIVisible(visible):
			aih = getattr(player, 'inputHandler', None)
			if aih is not None:
				try: aih._AvatarInputHandler__isGUIVisible = visible
				except: pass
				if hasattr(aih, 'setGUIVisible'):
					try: aih.setGUIVisible(visible)
					except: pass
		player.setGUIVisible = _setGUIVisible
		
		player.getAutorotation = lambda: False
		player.enableOwnVehicleAutorotation = lambda val: None

		class FakePositionControl(object):
			def bindToVehicle(self, *a, **k): pass
			def followCamera(self, *a, **k): pass
			def moveTo(self, *a, **k): pass
		player.positionControl = FakePositionControl()

		class FakeStats(object):
			def getCache(self, cb): cb(1, {})
			def __getattr__(self, name): return lambda *a, **k: None
			
		if not hasattr(player, '_offhangar_orig_stats'):
			player._offhangar_orig_stats = getattr(player, 'stats', None)
		player.stats = FakeStats()

		class FakeGunRotator(object):
			def __init__(self):
				self.markerInfo = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
				self.dispersionAngle = 0.1
			def getShotParams(self, targetPos, *a, **kw):
				import BigWorld, Math
				try:
					from projectile_trajectory import getShotAngles
					descr = BigWorld.player().vehicleTypeDescriptor
					speed = descr.shot['speed']
					gravity = descr.shot['gravity']
					mat = BigWorld.player().getOwnVehicleMatrix()
					
					# Get exact required gun elevation angle to hit targetPos
					try:
						(shotTurretYaw, shotGunPitch) = getShotAngles(descr, mat, (0, 0), targetPos)
					except Exception:
						shotTurretYaw, shotGunPitch = getattr(self, '_turret_yaw', 0.0), getattr(self, '_gun_pitch', 0.0)
					
					# Clamp to limits so trajectory doesn't draw where gun can't reach
					import math
					try:
						pl = descr.gun['pitchLimits']
						from gun_rotation_shared import calcPitchLimitsFromDesc
						limits = calcPitchLimitsFromDesc(shotTurretYaw, pl)
						if shotGunPitch < limits[0]: shotGunPitch = limits[0]
						elif shotGunPitch > limits[1]: shotGunPitch = limits[1]
					except: pass
					
					try:
						yl = descr.gun.get('turretYawLimits', None)
						if yl is None and descr.turret is not None:
							yl = descr.turret.get('yawLimits', None)
						if yl is not None:
							min_yaw = float(yl[0])
							max_yaw = float(yl[1])
							if abs(min_yaw) > 10.0:
								min_yaw = math.radians(min_yaw)
								max_yaw = math.radians(max_yaw)
							if shotTurretYaw < min_yaw: shotTurretYaw = min_yaw
							elif shotTurretYaw > max_yaw: shotTurretYaw = max_yaw
					except: pass
					
					# Calculate actual world space gun position and velocity vector
					turretOffs = descr.hull['turretPositions'][0] + descr.chassis['hullPosition']
					gunOffs = descr.turret['gunPosition']
					turretWorldMatrix = Math.Matrix()
					turretWorldMatrix.setRotateY(shotTurretYaw)
					turretWorldMatrix.translation = turretOffs
					turretWorldMatrix.postMultiply(mat)
					position = turretWorldMatrix.applyPoint(gunOffs)
					gunWorldMatrix = Math.Matrix()
					gunWorldMatrix.setRotateX(shotGunPitch)
					gunWorldMatrix.postMultiply(turretWorldMatrix)
					vector = gunWorldMatrix.applyVector(Math.Vector3(0, 0, speed))
					
					return (position, vector, Math.Vector3(0, -gravity, 0))
				except Exception as e:
					LOG_DEBUG('OfflineBattle getShotParams ERROR:', str(e))
					# fallback
					try:
						speed = BigWorld.player().vehicleTypeDescriptor.shot['speed']
						gravity = BigWorld.player().vehicleTypeDescriptor.shot['gravity']
					except:
						speed, gravity = 250.0, 9.81
					if hasattr(self, '_gun_pos') and hasattr(self, '_gun_dir'):
						return (self._gun_pos, self._gun_dir.scale(speed), Math.Vector3(0, -gravity, 0))
					startPos = BigWorld.player().getOwnVehiclePosition()
					startPos.y += 2.0
					v0 = BigWorld.camera().direction
					return (startPos, v0.scale(speed), Math.Vector3(0, -gravity, 0))
			def _VehicleGunRotator__getCurShotPosition(self):
				import BigWorld, Math
				try:
					speed = BigWorld.player().vehicleTypeDescriptor.shot['speed']
				except:
					speed = 250.0
				if hasattr(self, '_gun_pos') and hasattr(self, '_gun_dir'):
					return (self._gun_pos, self._gun_dir.scale(speed))
				startPos = BigWorld.player().getOwnVehiclePosition()
				startPos.y += 2.0
				v0 = BigWorld.camera().direction
				return (startPos, v0.scale(speed))
		player.gunRotator = FakeGunRotator()

		# Report real simulated speeds so dispersion reacts to movement
		# (_veh_velocity/_veh_turn_velocity are defined below; resolved at call time)
		player.getOwnVehicleSpeeds = lambda: (_veh_velocity[0], _veh_turn_velocity[0])
		player.autoAim = lambda val: None

		if hasattr(player, 'arena') and player.arena is not None:
			if not hasattr(player.arena, 'collideWithSpaceBB') or not callable(getattr(player.arena, 'collideWithSpaceBB', None)):
				player.arena.collideWithSpaceBB = lambda *a, **kw: None

		veh_yaw     = [spawn_dir.z]
		turret_yaw  = [0.0]   # relative to hull
		gun_pitch   = [0.0]   # gun elevation
		veh_pos = [spawn_pos.x, spawn_pos.y, spawn_pos.z]
		turret_matrix = Math.Matrix()
		turret_matrix.setTranslate(Math.Vector3(spawn_pos.x, spawn_pos.y + 2.0, spawn_pos.z))
		turret_matrix_local = Math.Matrix()

		# Read turret/gun rotation limits from vehicle descriptor
		_turret_rot_speed = 1.5  # rad/s default
		_gun_min_pitch    = -0.35  # ~-20 deg (ELEVATION - UP) default
		_gun_max_pitch    =  0.15  # ~+8.6 deg (DEPRESSION - DOWN) default
		_gun_pitch_desc   = None   # full 0.8.2 pitchLimits descriptor (yaw-dependent)
		_gun_pitch_speed  = 0.75   # rad/s fallback vertical aim speed
		_gun_min_yaw      = -3.14159
		_gun_max_yaw      =  3.14159
		try:
			if td is not None:
				rot = td.turret.get('rotationSpeed', None)
				if rot is not None:
					_turret_rot_speed = float(rot)  # descriptor stores rad/s
				pl = td.gun.get('pitchLimits', None)
				if pl is not None:
					try:
						if isinstance(pl, dict):
							# 0.8.2 descriptor: {'basic': (minRad, maxRad), 'absolute': (...),
							# optional 'front'/'back'/'transition'} - values ALREADY in radians.
							# (The old minPitch/minAngle keys never existed in this format, so
							# every tank silently fell back to the hardcoded default limits.)
							_gun_pitch_desc = pl
							lim = pl.get('basic') or pl.get('absolute')
							if lim:
								_gun_min_pitch = float(lim[0])
								_gun_max_pitch = float(lim[1])
						elif isinstance(pl, (list, tuple)) and len(pl) >= 2:
							_gun_min_pitch = float(pl[0])
							_gun_max_pitch = float(pl[1])
					except Exception as pe:
						LOG_DEBUG('OfflineBattle pitch parsing error:', str(pe))
				# Vertical aim speed from the descriptor (radians/s) instead of the
				# old hardcoded 2.5 rad/s (~143 deg/s - several times too fast).
				try:
					_gs = td.gun.get('rotationSpeed', None)
					if _gs:
						_gun_pitch_speed = float(_gs)
				except Exception:
					pass
				try:
					import math as _math
					LOG_DEBUG('PitchDBG: elevation=%.1f deg (up), depression=%.1f deg (down), yawDependent=%s, aimSpeed=%.1f deg/s' % (
						-_math.degrees(_gun_min_pitch), _math.degrees(_gun_max_pitch),
						_gun_pitch_desc is not None and (('front' in _gun_pitch_desc) or ('back' in _gun_pitch_desc)),
						_math.degrees(_gun_pitch_speed)))
				except Exception:
					pass
				yl = td.gun.get('turretYawLimits', None)
				if yl is None and td.turret is not None:
					yl = td.turret.get('yawLimits', None)
				if yl is not None:
					import math as _math
					_gun_min_yaw = float(yl[0])
					_gun_max_yaw = float(yl[1])
					if abs(_gun_min_yaw) > 10.0 or abs(_gun_max_yaw) > 10.0:
						_gun_min_yaw = _math.radians(_gun_min_yaw)
						_gun_max_yaw = _math.radians(_gun_max_yaw)
		except Exception as e:
			LOG_DEBUG('OfflineBattle.limits error:', str(e))

		_tick_counter = [0]

		# Engine and track sound state
		_sound_state = {
			'engine_sound': None,
			'tread_sound': None,
			'last_engine_event': '',
			'last_tread_event': '',
		}

		# Determine tank class for sound events
		_tank_class = 'medium'
		try:
			if td is not None:
				tags = td.type.tags if hasattr(td, 'type') and hasattr(td.type, 'tags') else set()
				if 'lightTank' in tags: _tank_class = 'light'
				elif 'heavyTank' in tags: _tank_class = 'heavy'
				elif 'SPG' in tags or 'AT-SPG' in tags: _tank_class = 'SAU'
				else: _tank_class = 'medium'
			LOG_DEBUG('OfflineBattle.tank_class:', _tank_class)
		except Exception as e:
			LOG_DEBUG('OfflineBattle.tank_class error:', str(e))

		# Map tank class to FMOD event prefix
		_engine_idle_event = '/tanks/%s/%s/%s' % (
			{'light': 'light', 'heavy': 'heavy', 'medium': 'medium', 'SAU': 'medium'}.get(_tank_class, 'medium'),
			{'light': 'MC-1', 'heavy': 'IS_2', 'medium': 'tiger', 'SAU': 'tiger'}.get(_tank_class, 'tiger'),
			{'light': 'idle', 'heavy': 'IS_2_stand', 'medium': 'tiger_idle', 'SAU': 'tiger_idle'}.get(_tank_class, 'tiger_idle'),
		)
		_engine_run_event = '/tanks/%s/%s/%s' % (
			{'light': 'light', 'heavy': 'heavy', 'medium': 'medium', 'SAU': 'medium'}.get(_tank_class, 'medium'),
			{'light': 'MC-1', 'heavy': 'IS_2', 'medium': 'tiger', 'SAU': 'tiger'}.get(_tank_class, 'tiger'),
			{'light': 'run', 'heavy': 'heavy_tank_run_state2', 'medium': 'medium_tank_state2', 'SAU': 'medium_tank_state2'}.get(_tank_class, 'medium_tank_state2'),
		)
		_tread_prefix = '/tanks/tanks_treads/%s_tank' % ({'SAU': 'SAU'}.get(_tank_class, _tank_class))

# --- GUN MECHANICS STATE ---
		_gun_state = {
			'base_dispersion': 0.1,
			'after_shot': 1.5,
			'aim_time': 2.0,
			'clip_size': 1,
			'clip_reload': 2.0,
			'reload': 5.0,
			'ammo': 100,
			'clip': 1,
			'reloadTime': 0.0,
			'dispersion': 0.1,
			'initialized': False,
			'shot_index': 0
		}

		_engine_state = {'init': False, 'snd1': None, 'snd2': None}
		globals()['g_offh_engine_state'] = _engine_state
		
		_veh_velocity = [0.0]        # m/s, forward speed
		_veh_turn_velocity = [0.0]   # rad/s, current hull rotation speed
		_last_tick_time = [BigWorld.time()]
		_veh_vert_vel = [0.0]        # m/s, vertical (falling) speed
		_veh_airborne = [False]      # True while the hull has left the ground
		_veh_fall_armed = [False]    # fall damage arms only after the FIRST real ground
		                             # contact: the spawn drop (collide-miss fallback puts
		                             # the hull well above ground) must land free, or the
		                             # tank spawns damaged/dead (WZ-111 report)
		
		# === WoT-style physics parameters ===
		import math
		# ONE source of physics laws + parameters for player AND bots:
		# gui.mods.offhangar.physics (see its module docstring for units).
		from gui.mods.offhangar import physics as _PHY
		# Live tuning: config.json "physics_tuning" overrides the WG constants
		# (cohesion, power, brake, slide thresholds...) - restart, no recompile.
		# MUST run before derive_params so the new values reach the params.
		try:
			from _constants import CONFIG_OPTIONS as _CFG_PHY
			_applied_tuning = _PHY.apply_tuning(_CFG_PHY.get('physics_tuning'))
			if _applied_tuning:
				LOG_DEBUG('OfflineBattle.PHYSICS tuning: ' + ', '.join(_applied_tuning))
			# Same idea for the two HE blast constants, under "he_tuning". Those are a
			# reconstruction (the damage calculator is cell-side and is not shipped), so
			# dialling them without a recompile matters more here than for the physics.
			_applied_he = _offh_he_apply_tuning(_CFG_PHY.get('he_tuning'))
			if _applied_he:
				LOG_DEBUG('OfflineBattle.HE tuning: ' + ', '.join(_applied_he))
			# Same again for the earning coefficients under "economy_tuning".
			# Those are the most reconstructed numbers in the mod - WG never
			# published them - so being able to re-tune without a recompile is
			# the point rather than a convenience.
			try:
				from gui.mods.offhangar import battle_economy as _BECO_T
				_applied_eco = _BECO_T.apply_tuning(_CFG_PHY.get('economy_tuning'))
				if _applied_eco:
					LOG_DEBUG('OfflineBattle.economy tuning: ' + ', '.join(_applied_eco))
			except Exception as _ecte:
				LOG_DEBUG('economy tuning skipped:', str(_ecte))
			_offh_phys_debug = [bool(_CFG_PHY.get('physics_debug', False))]
		except Exception:
			_offh_phys_debug = [False]
		if _offh_phys_debug[0]:
			try:
				import gui.mods.offhangar.physics_monitor as _offh_mon
				_offh_mon.reset()
				LOG_DEBUG('OfflineBattle.PHYSICS telemetry ON -> offhangar_user/physics_telemetry.csv')
			except Exception:
				_offh_phys_debug[0] = False
		_pparams = _PHY.derive_params(td)
		# Local aliases: the tick code below and several helpers (tank_resolve,
		# sounds, scroll caps) read these names.
		_phys_mass           = _pparams['mass']
		_phys_enginePowerW   = _pparams['powerW']
		_phys_speedFwd       = _pparams['speedFwd']
		_phys_speedBwd       = _pparams['speedBwd']
		_phys_chassisRotSpd  = _pparams['rotSpd']
		_phys_terrainResist  = _pparams['terrainResist']
		_phys_specificFriction = _pparams['specificFriction']
		_phys_terrainCoeff   = _pparams['terrainResist'][0]
		_phys_gravity        = _PHY.GRAVITY
		_phys_brakeDecel     = _pparams['brakeDecel']
		_phys_trackCenter    = _pparams['trackCenter']
		LOG_DEBUG('OfflineBattle.PHYSICS: mass=%.0f, power=%.0fW, fwd=%.1f m/s, bwd=%.1f m/s, rot=%.1f deg/s, terrain=(%.2f,%.2f,%.2f), friction=%.4f, brake=%.2f m/s2, halfGauge=%.2f' % (
			_phys_mass, _phys_enginePowerW, _phys_speedFwd, _phys_speedBwd,
			math.degrees(_phys_chassisRotSpd), _phys_terrainResist[0], _phys_terrainResist[1], _phys_terrainResist[2],
			_phys_specificFriction, _phys_brakeDecel, _phys_trackCenter))
		_battle_finished = [False]
		_exit_done = [False]  # once-guard shared by ALL exit paths (leaveArena / death / K)
		# One battle = one generation: loops of an older battle see the bump
		# and stop instead of stacking up (every stale loop pins its whole
		# battle graph -> the 32-bit client runs out of memory on start #3).
		globals()['g_offh_battle_gen'] = (globals().get('g_offh_battle_gen', 0) or 0) + 1
		_offh_my_gen = [globals()['g_offh_battle_gen']]
		_offh_seen_arena = [False]
		_offh_seen_bw = [False]
		
		global g_base_capture
		g_base_capture = {1: {'points': 0}, 2: {'points': 0}}
		globals().pop('G_OFFH_FORCED_WINNER', None)  # stale capture-win flag from a crashed exit
		globals().pop('g_offh_capture_won', None)
		globals().pop('g_offh_battle_over', None)
		globals().pop('_offh_kill_msgs', None)
		# Baked bot destinations are per-map AND per-battle: the RouteMap holds
		# validated node state and which nodes are already claimed, so carrying
		# one into the next battle would hand out stale slots on a different map.
		globals().pop('g_offh_routemap', None)
		globals().pop('g_offh_routes_tried', None)
		globals().pop('g_offh_routes_last_live', None)
		# The nav grid is per-map AND holds ~10k cells; carrying one into the
		# next battle would both path against the wrong map and keep the memory
		# alive across a space teardown (CLAUDE.md #14).
		globals().pop('g_offh_navgrid', None)
		globals().pop('g_offh_nav_tried', None)
		globals().pop('g_offh_nav_dumped', None)
		globals().pop('g_offh_nav_last_cov', None)
		globals().pop('g_offh_nav_painted', None)
		# A painted profile is per-map; carrying one over would hand out
		# positions from the wrong map. The claim dict is a GLOBAL (the mocks are
		# fresh each battle, this is not), so it must be popped too.
		globals().pop('g_offh_profile', None)
		globals().pop('g_offh_profile_tried', None)
		globals().pop('g_offh_prof_taken', None)
		globals().pop('g_offh_prof_cache', None)
		globals().pop('g_offh_autoroutes_tried', None)
		globals().pop('g_offh_orient_done', None)
		globals().pop('g_offh_nav_announced', None)
		globals().pop('g_offh_nav_q', None)
		globals().pop('g_offh_msg_queue', None)
		globals().pop('g_offh_astar_ok', None)
		globals().pop('g_offh_astar_fail', None)
		globals().pop('g_offh_astar_used', None)
		globals().pop('g_offh_bot_ticks', None)
		globals().pop('g_offh_stuck_ticks', None)
		globals().pop('g_offh_stat_at', None)
		globals().pop('g_offh_escape_wet', None)
		globals().pop('g_offh_los_blocked', None)
		# Drop the hit-sound carrier reference. It is added with BigWorld.addModel,
		# NOT through _add_model, so the end-of-battle sweep never saw it - carrying
		# a model reference across a space teardown is the dangling-reference case
		# that has crashed the next space load before.
		globals().pop('g_offh_hit_carrier', None)
		globals().pop('g_offh_hit_snd_t', None)
		# New battle, new queue - the old one belongs to the finished arena.
		globals().pop('g_offh_crew_notif', None)
		globals().pop('g_offh_roster_ready', None)
		# Looping-sound headcount: those ids belong to the finished arena, and a
		# stale set would gate the new battle's bots by the old battle's numbering.
		globals().pop('g_offh_snd_keep', None)
		globals().pop('g_offh_snd_budget_t', None)
		# Re-arm the outline roll call. Both counters are session-lived (one a module
		# global, one an attribute of the account, which IS the player and survives
		# every battle), so they only ever reported on the first battle after a client
		# launch - and a report that goes quiet by battle 2 reads as "no candidates".
		globals().pop('_offh_outl_diag_n', None)
		try:
			player._dbg_outl_block = False
		except Exception:
			pass

		global g_capture_tick_ref
		def trigger_battle_results(winnerTeam=1):
			import BigWorld
			player = BigWorld.player()
			if player is None: return
			try:
				from gui.SystemMessages import SM_TYPE, pushMessage
				pushMessage('Offline battle finished. Returning to Hangar...'.encode('utf-8'), SM_TYPE.Information)
			except Exception as e: pass
			
			try:
				import MusicController
				if hasattr(MusicController, 'g_musicController') and MusicController.g_musicController:
					_mc = MusicController.g_musicController
					try: _mc.stop()
					except: pass
					evt = None
					p_team = getattr(player, 'team', 1)
					if winnerTeam == p_team:
						evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_VICTORY', getattr(MusicController, 'MUSIC_EVENT_VICTORY', 'music_victory'))
					elif winnerTeam != 0:
						evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_LOSE', getattr(MusicController, 'MUSIC_EVENT_LOSE', 'music_lose'))
					else:
						evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_DRAW', getattr(MusicController, 'MUSIC_EVENT_DRAW', 'music_draw'))
					try: _mc.play(evt)
					except: pass
			except Exception as e: pass
			
			try:
				import battle_results_shared
				mock_arena_id = 999
				
				v_id = getattr(player, 'playerVehicleID', 1)
				p_max_health = getattr(getattr(player, 'vehicleTypeDescriptor', None), 'maxHealth', 1000)
				p_health = getattr(getattr(player, 'vehicle', None), 'health', p_max_health)
				
				_player_mock = globals().get('G_MOCK_VEHICLES', {}).get(getattr(player, 'playerVehicleID', -1))
				_p_killer_id = getattr(_player_mock, 'last_killer_id', 255) if p_health <= 0 else 0
				
				p_team = getattr(player, 'team', 1)
				p_dbid = getattr(player, 'databaseID', 1)
				p_name = getattr(player, 'name', 'Player')
				p_cd = getattr(getattr(getattr(player, 'vehicleTypeDescriptor', None), 'type', None), 'compactDescr', 0)
				
				players_dict = {p_dbid: {'name': p_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': p_team, 'igrType': 0}}
				vehicles_dict = {v_id: {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': 10, 'hits': 8, 'he_hits': 0, 'pierced': 8, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': max(0, p_max_health - p_health), 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': {}, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0}}
				
				for vid, vinfo in getattr(player.arena, 'vehicles', {}).items():
					if vid == v_id: continue
					bot_team = vinfo.get('team', 2)
					bot_name = vinfo.get('name', 'Bot')
					bot_dbid = vid
					td = vinfo.get('vehicleType', None)
					td_type = getattr(td, 'type', None)
					bot_cd = getattr(td_type, 'compactDescr', 0)
					
					players_dict[bot_dbid] = {'name': bot_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': bot_team, 'igrType': 0}
					
					is_killed = not vinfo.get('isAlive', True)
					bot_hp = getattr(td, 'maxHealth', 1000)
					if is_killed: bot_hp = 0
					
					vehicles_dict[vid] = {'health': bot_hp, 'credits': 0, 'xp': 0, 'shots': 0, 'hits': 0, 'he_hits': 0, 'pierced': 0, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': getattr(td, 'maxHealth', 1000) - bot_hp, 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 10, 'lifeTime': 300, 'killerID': v_id if is_killed else 0, 'achievements': [], 'repair': 0, 'freeXP': 0, 'details': {}, 'accountDBID': bot_dbid, 'team': bot_team, 'typeCompDescr': bot_cd, 'gold': 0}
				
				mock_res = {
					'arenaUniqueID': mock_arena_id,
					'personal': {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': 10, 'hits': 8, 'he_hits': 0, 'pierced': 8, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': 0, 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': {}, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0, 'xpPenalty': 0, 'creditsPenalty': 0, 'creditsContributionIn': 0, 'creditsContributionOut': 0, 'tmenXP': 0, 'eventCredits': 0, 'eventGold': 0, 'eventXP': 0, 'eventFreeXP': 0, 'eventTMenXP': 0, 'autoRepairCost': 0, 'autoLoadCost': (0, 0), 'autoEquipCost': (0, 0), 'isPremium': True, 'premiumXPFactor10': 15, 'premiumCreditsFactor10': 15, 'dailyXPFactor10': 10, 'aogasFactor10': 10, 'markOfMastery': 0, 'dossierPopUps': []},
					'common': {'arenaTypeID': getattr(player.arena, 'arenaTypeID', 1), 'arenaCreateTime': __import__('time').time(), 'winnerTeam': winnerTeam, 'finishReason': 1, 'duration': 300, 'bonusType': 1, 'guiType': 1, 'vehLockMode': 0},
					'players': players_dict,
					'vehicles': vehicles_dict
				}
				
				
				
				try:
					from gui import WindowsManager
					if hasattr(WindowsManager.g_windowsManager, 'showBattleResults'):
						WindowsManager.g_windowsManager.showBattleResults(mock_arena_id)
				except: pass
				
			except Exception as e:
				import traceback
				import gui.mods.offhangar.logging as __offlog
				__offlog.LOG_DEBUG('CRITICAL ERROR IN TRIGGER BATTLE RESULTS:', e)
				__offlog.LOG_DEBUG(traceback.format_exc())
				
			# Now clean up and leave arena!
			# Restore original stats object which was replaced with FakeStats
			if hasattr(player, '_offhangar_orig_stats') and player._offhangar_orig_stats is not None:
				player.stats = player._offhangar_orig_stats
			
			_leaveArena()
			player.onBecomeNonPlayer()
			
			# HACK: Because we triggered onBecomeNonPlayer manually but never call
			# onBecomePlayer to avoid crashing the offline mock state, we must manually
			# re-bind the requester modules and un-ignore them!
			for helper in ('syncData', 'inventory', 'stats', 'trader', 'shop', 'dossierCache', 'battleResultsCache', 'questProgress'):
				h = getattr(player, helper, None)
				if hasattr(h, 'setAccount'):
					try: h.setAccount(player)
					except: pass
				if hasattr(h, 'onAccountBecomePlayer'):
					try: h.onAccountBecomePlayer()
					except: pass

		def _offh_finish_battle(winner, reason):
			'''End the battle through the one flow that is known to work: force the
			outcome, switch the arena to AFTERBATTLE, then replay a K keypress after the
			5 s window - the same route base capture already takes.'''
			import BigWorld
			if globals().get('g_offh_battle_over'):
				return
			globals()['g_offh_battle_over'] = True
			globals()['G_OFFH_FORCED_WINNER'] = winner
			LOG_DEBUG('BATTLE OVER: %s -> winnerTeam=%s' % (reason, winner))
			try:
				BigWorld.player().arena.onPeriodChange(4, BigWorld.serverTime() + 5.0, 5.0, {})
			except Exception:
				pass
			def _end_now():
				try:
					if _exit_done[0]:
						return
					import Keys as _EK
					class _EndKeyEvent(object):
						key = _EK.KEY_K
						def isKeyDown(self): return True
						def isRepeatedEvent(self): return False
						def isShiftDown(self): return False
						def isCtrlDown(self): return False
						def isAltDown(self): return False
					_mock_handleKeyEvent(_EndKeyEvent())
				except Exception:
					import traceback
					LOG_DEBUG('battle end error:', traceback.format_exc())
			BigWorld.callback(5.0, _end_now)
		
		def _offh_check_battle_end():
			'''Team wipe and timer expiry. Base capture handles itself further down.'''
			import BigWorld
			if globals().get('g_offh_battle_over'):
				return
			player = BigWorld.player()
			if player is None or getattr(player, 'arena', None) is None:
				return
			# Only once the battle proper is running - period 3. Checking during the
			# countdown would call a wipe before the bots have even spawned.
			if getattr(player.arena, 'period', 0) != 3:
				return
			p_team = getattr(player, '_offhangar_team', 1)
			# --- timer expiry -> draw ---
			try:
				_end_t = getattr(player.arena, 'periodEndTime', 0) or 0
				if _end_t and BigWorld.serverTime() >= _end_t:
					_offh_finish_battle(0, 'battle timer expired')
					return
			except Exception:
				pass
			# --- team wipe ---
			try:
				_mv = globals().get('G_MOCK_VEHICLES', {}) or {}
				if not _mv:
					return
				_alive = {1: 0, 2: 0}
				for _vid, _m in _mv.items():
					if (getattr(_m, 'health', 0) or 0) <= 0:
						continue
					_t = getattr(_m, '_bot_team', None)
					if _t is None:
						if _vid == getattr(player, 'playerVehicleID', -1):
							_t = p_team
						else:
							_pi = getattr(_m, 'publicInfo', None)
							_t = _pi.get('team', 2) if _pi else 2
					if _t in _alive:
						_alive[_t] += 1
				# Both sides must have HAD vehicles, or a half-spawned line-up reads as a
				# wipe. Requires the roster to be complete on both sides first.
				if not globals().get('g_offh_roster_ready'):
					if _alive[1] > 0 and _alive[2] > 0:
						globals()['g_offh_roster_ready'] = True
					return
				if _alive[1] <= 0 and _alive[2] <= 0:
					_offh_finish_battle(0, 'both teams wiped out')
				elif _alive[2] <= 0:
					_offh_finish_battle(1, 'team 2 wiped out')
				elif _alive[1] <= 0:
					_offh_finish_battle(2, 'team 1 wiped out')
			except Exception as _bee:
				LOG_DEBUG('battle end check error:', str(_bee))
		
		def _capture_tick():
			import gui.mods.offhangar.logging as __offlog
			__offlog.LOG_DEBUG('LOUD: Capture tick started running!')
			try:
				if _battle_finished[0]: return
				import BigWorld
				player = BigWorld.player()
				if player is None or _battle_finished[0]:
					return
				if globals().get('g_offh_battle_gen', 0) != _offh_my_gen[0]:
					return  # a newer battle owns the globals - stop this stale loop
				if getattr(player, 'arena', None) is not None:
					_offh_seen_arena[0] = True
				elif _offh_seen_arena[0]:
					return  # battle left (hangar) - stop, let the battle graph free
				# The battle GUI dies on EVERY exit path (ESC quit has no hook of
				# its own). Once it existed and is gone: clean up NOW and stop -
				# ticking into the teardown/hangar load crashed the client, and
				# the leaked battle OOM-crashed the hangar load itself.
				try:
					from gui import WindowsManager as _gwm
					_bwref = getattr(_gwm.g_windowsManager, 'battleWindow', None)
				except Exception:
					_bwref = None
				if _bwref is not None:
					_offh_seen_bw[0] = True
				elif _offh_seen_bw[0]:
					try:
						_offh_battle_sweep('esc')
					except:
						pass
					_battle_finished[0] = True
					return

				# Get alive vehicles per team
				vehs_by_team = {1: [], 2: []}
				_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
				# The player has no isVehicleAlive offline (the account object
				# always answered True, so a DEAD player kept capturing); the
				# player's mock carries the real health.
				_pm = _mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
				if _pm is None or getattr(_pm, 'health', 1) > 0:
					vehs_by_team[1].append(player) # player is always team 1

				for e_mock in _mock_vehicles.values():
					# Bots carry _bot_team/isAlive; the old check probed the
					# nonexistent _team (mock __getattr__ -> None), so no bot was
					# ever counted and bases could only be captured by the player.
					_bt = getattr(e_mock, '_bot_team', None)
					if _bt in vehs_by_team and getattr(e_mock, 'isAlive', False):
						vehs_by_team[_bt].append(e_mock)
				
				# Check base distances
				for base_team, bases in g_offline_bases.items():
					if not bases: continue
					
					invading_team = 2 if base_team == 1 else 1
					
					invaders_count = 0
					invaders_here = []   # (id, current hp) of everything inside the circle
					for invader in vehs_by_team[invading_team]:
						for base_pos in bases:
							import BigWorld
							if invader == BigWorld.player():
								inv_x = veh_pos[0]
								inv_z = veh_pos[2]
								_inv_id = getattr(player, 'playerVehicleID', -1)
								# The account answers None for anything it does not carry, so the
								# player's HP has to come off his mock (same reason as the alive
								# test above).
								_inv_hp = (getattr(_pm, 'health', 0) or 0) if _pm is not None else 1
							else:
								inv_x = invader.position.x
								inv_z = invader.position.z
								_inv_id = getattr(invader, 'id', None) or id(invader)
								_inv_hp = getattr(invader, 'health', 0) or 0
							dx = inv_x - base_pos.x
							dz = inv_z - base_pos.z
							_d2 = dx*dx + dz*dz
							# Was logged for EVERY vehicle against EVERY base every second -
							# 1494 lines of a 4027-line session, all of them about tanks
							# hundreds of metres away. Only the approach tells us anything.
							if _d2 <= 22500.0:   # 150 m
								import gui.mods.offhangar.logging as __offlog
								__offlog.LOG_DEBUG('LOUD: Distance to base', base_team, 'is', _d2, 'pos:', inv_x, inv_z, 'base:', base_pos.x, base_pos.z)
							if _d2 <= 2500.0: # 50m radius
								invaders_count += 1
								invaders_here.append((_inv_id, _inv_hp))
								break
					
					defenders_count = 0
					for defender in vehs_by_team[base_team]:
						for base_pos in bases:
							import BigWorld
							if defender == BigWorld.player():
								def_x = veh_pos[0]
								def_z = veh_pos[2]
							else:
								def_x = defender.position.x
								def_z = defender.position.z
							dx = def_x - base_pos.x
							dz = def_z - base_pos.z
							if dx*dx + dz*dz <= 2500.0:
								defenders_count += 1
								break
					
					state = g_base_capture[base_team]
					old_points = state['points']
					
					# Handle transition from PREBATTLE to BATTLE
					if getattr(player.arena, 'period', 0) == 2 and BigWorld.serverTime() >= getattr(player.arena, 'periodEndTime', 0):
						import gui.mods.offhangar.logging as __offlog
						__offlog.LOG_DEBUG('LOUD: TRANSITION TO BATTLE PERIOD')
						player.arena.period = 3
						player.arena.periodLength = 900
						player.arena.periodEndTime = BigWorld.serverTime() + 900
						player.arena.onPeriodChange(3, player.arena.periodEndTime, 900, {}) # dict, not int: UI handlers call has_key() on it
						# 'Battle begins!' - retail plays it here, in Avatar.__onArenaPeriodChange,
						# on the same PREBATTLE -> BATTLE edge. playRules 0, so it goes straight to
						# BigWorld.playSound with no queue and no binding.
						_offh_notify('start_battle')
						_offh_battle_music()
					
					import debug_utils
					if state['points'] != old_points or invaders_count > 0:
						debug_utils.LOG_DEBUG('Capture tick: team', base_team, 'invaders:', invaders_count, 'defenders:', defenders_count, 'points:', state['points'], 'serverTime:', BigWorld.serverTime())
					
					# Retail rule: every vehicle inside the circle accumulates its OWN capture
					# points at 1/s, the base shows the SUM, and a capturer that TAKES DAMAGE
					# loses what it had accumulated. Standing on your own base does NOT stop a
					# capture - only shooting the capturers does.
					#
					# The old gate was `defenders_count == 0`, which made a base uncapturable
					# while any defender was within 50 m of it - and that is where the player
					# spawns. Measured in the 2026-08-05 log: 3-4 enemies sat inside the
					# player's circle for 27 consecutive ticks with the counter frozen at 0,
					# because the player was parked 34 m from his own base. Offline that also
					# disabled the whole capture-win path in most battles.
					#
					# Damage is detected by watching each capturer's HP across ticks rather
					# than by hooking the damage paths: there are a dozen of those (shell,
					# splash, fire, ramming, drowning, falls) and every one of them writes
					# health, so one comparison covers them all.
					_cap_pts = state.setdefault('veh', {})   # vehicle id -> its own points
					_cap_hp = state.setdefault('hp', {})     # vehicle id -> hp at the last tick
					_cap_broken = False
					_here_ids = set()
					for _iid, _ihp in invaders_here:
						_here_ids.add(_iid)
						_prev_hp = _cap_hp.get(_iid)
						_cap_hp[_iid] = _ihp
						if _prev_hp is not None and _ihp < _prev_hp:
							if _cap_pts.get(_iid):
								_cap_broken = True   # drives the UI's "capture stopped" flag
							_cap_pts[_iid] = 0
						else:
							_cap_pts[_iid] = _cap_pts.get(_iid, 0) + 1
					# Left the circle, or died: its contribution goes with it. This is also
					# what zeroes the bar when the last invader leaves.
					for _gid in list(_cap_pts.keys()):
						if _gid not in _here_ids:
							del _cap_pts[_gid]
							_cap_hp.pop(_gid, None)
					state['points'] = min(100, sum(_cap_pts.values()))

					if state['points'] != old_points or invaders_count > 0:
						import gui.mods.offhangar.logging as __offlog
						__offlog.LOG_DEBUG('LOUD: PERIOD:', getattr(player.arena, 'period', None), 'SERVERTIME:', BigWorld.serverTime(), 'PERIODENDTIME:', getattr(player.arena, 'periodEndTime', None))
						__offlog.LOG_DEBUG('Capture UI updating points! base:', base_team, 'points:', state['points'], 'invaders:', invaders_count)
						try:
							import gui.Scaleform.Battle
							if not hasattr(gui.Scaleform.Battle.TeamBasesPanel, '_patched_update'):
								orig = gui.Scaleform.Battle.TeamBasesPanel._TeamBasesPanel__onTeamBasePointsUpdate
								def _hook(self, team, baseID, points, capturingStopped):
									import gui.mods.offhangar.logging as __offlog
									__offlog.LOG_DEBUG('LOUD: UI HOOK! team', team, 'base', baseID, 'pts', points, 'stop', capturingStopped)
									try:
										orig(self, team, baseID, points, capturingStopped)
										__offlog.LOG_DEBUG('LOUD: UI HOOK orig executed successfully!')
									except Exception as e:
										__offlog.LOG_DEBUG('LOUD: UI HOOK EXCEPTION:', e)
								gui.Scaleform.Battle.TeamBasesPanel._TeamBasesPanel__onTeamBasePointsUpdate = _hook
								gui.Scaleform.Battle.TeamBasesPanel._patched_update = True
						except Exception as e:
							__offlog.LOG_DEBUG('LOUD: UI HOOK INIT ERROR:', e)
						try:
							# capturingStopped is retail's "the capture was INTERRUPTED" flag
							# (a capturer got hit), not "someone is defending" - the panel
							# used to show a permanently blocked capture that was never
							# actually being blocked.
							player.arena.onTeamBasePointsUpdate(base_team, 0, state['points'], _cap_broken)
						except Exception as e:
							__offlog.LOG_DEBUG('LOUD: Capture UI Error:', e)
					
					if state['points'] >= 100 and not globals().get('g_offh_capture_won'):
						globals()['g_offh_capture_won'] = True  # once-guard; popped at battle start
						try:
							player.arena.onTeamBaseCaptured(1, base_team)
						except: pass
						# _battle_finished intentionally NOT set here: it stops the AIH
						# tick (camera/input/physics), which froze the whole game for
						# the 5 s afterbattle window ('game freezes at 99 cap, then
						# returns to garage'). Shooting is blocked via period 4 in the
						# fire gates; leaveArena sets the flag when the exit runs.

						# Stop the battle!
						try: player.arena.onPeriodChange(4, BigWorld.serverTime() + 5.0, 5.0, {}) # ArenaPeriod.AFTERBATTLE; dict, not int: UI handlers call has_key() on it
						except: pass

						# End through the SAME flow as the K key. The old
						# trigger_battle_results path never hooked
						# battleResultsCache.get (so the results screen fetched
						# mock arena 999 from the fake server) and ran an
						# onBecomeNonPlayer teardown no other exit path uses ->
						# 'capture the zone -> you win, but the game crashes'.
						globals()['G_OFFH_FORCED_WINNER'] = 3 - base_team
						def _capture_end():
							try:
								if _exit_done[0]:
									return  # player already left (K/death/ESC) during the countdown
								import Keys as _K
								class _CaptureKeyEvent(object):
									# Full PyKeyEvent surface: the K branch falls through to the
									# native AIH handler, whose game.convertKeyEvent probes all of
									# these (a bare key/isKeyDown object raised AttributeError).
									key = _K.KEY_K
									def isKeyDown(self):
										return True
									def isRepeatedEvent(self):
										return False
									def isShiftDown(self):
										return False
									def isCtrlDown(self):
										return False
									def isAltDown(self):
										return False
								_mock_handleKeyEvent(_CaptureKeyEvent())
							except Exception:
								import traceback
								__offlog.LOG_DEBUG('Capture end error:', traceback.format_exc())
						import BigWorld
						BigWorld.callback(5.0, _capture_end)
						
				# Team wipe / timer expiry, checked on the same 1 s cadence.
				_offh_check_battle_end()
			except Exception as e:
				import gui.mods.offhangar.logging as __offlog
				__offlog.LOG_DEBUG('LOUD: Capture Tick Error:', e)
			finally:
				# Reschedule ONLY while this battle is alive and owns the globals;
				# unconditional rescheduling kept whole old battles in memory.
				try:
					import BigWorld as _cbw
					_cpl = _cbw.player()
					_cok = (not _battle_finished[0]) and globals().get('g_offh_battle_gen', 0) == _offh_my_gen[0]
					if _cok and _offh_seen_arena[0] and (_cpl is None or getattr(_cpl, 'arena', None) is None):
						_cok = False
					if _cok:
						_cbw.callback(1.0, _capture_tick)
				except Exception:
					pass
					
		g_capture_tick_ref = _capture_tick
		BigWorld.callback(5.0, _capture_tick)
		
		global g_aih_tick_ref
		def _aih_tick():
			try:
				import BigWorld, Math, Keys, math
				_PROBE.begin()
				player = BigWorld.player()
				
				# Stop the loop if battle is over
				if _battle_finished[0] or player is None:
					return
				# Stale-loop guard: each battle start bumps the generation. A stale
				# per-frame loop pins its whole battle (models/mocks) in the 32-bit
				# client - three battles piled up = OOM crash while loading #3.
				if globals().get('g_offh_battle_gen', 0) != _offh_my_gen[0]:
					return
				if getattr(player, 'arena', None) is not None:
					_offh_seen_arena[0] = True
				elif _offh_seen_arena[0]:
					return  # back in the hangar - stop and release the battle
				# The battle GUI dies on EVERY exit path (ESC quit has no hook of
				# its own). Once it existed and is gone: clean up NOW and stop -
				# ticking into the teardown/hangar load crashed the client, and
				# the leaked battle OOM-crashed the hangar load itself.
				try:
					from gui import WindowsManager as _gwm
					_bwref = getattr(_gwm.g_windowsManager, 'battleWindow', None)
				except Exception:
					_bwref = None
				if _bwref is not None:
					_offh_seen_bw[0] = True
				elif _offh_seen_bw[0]:
					try:
						_offh_battle_sweep('esc')
					except:
						pass
					_battle_finished[0] = True
					return
				# (The identical guard block that used to be duplicated right here ran
				# twice per frame - two globals() lookups, two player.arena reads and two
				# WindowsManager imports for the same answer. Second copy removed.)

				current_time = BigWorld.time()
				dt = current_time - _last_tick_time[0]
				_last_tick_time[0] = current_time
				# full_space_release: pin the render camera to the dedicated
				# battle space each frame (camera-mode switches recreate cameras
				# that would revert rendering to the empty account space -> black).
				if globals().get('g_offh_full_release', False):
					_offh_set_render_space(_offh_bspace())
				if dt <= 0.0 or dt > 0.5:
					dt = 0.016 # fallback to 60fps
				_frame_dt = dt # real per-frame delta (dt is reused by the bot section below)
				# Bot AI think rate. The obstacle feelers are by far the most
				# expensive thing a bot does - about 25 wg_collideSegment calls each,
				# 750 per frame at 30 bots, the largest single item in the profile.
				# Deciding 'left or right around this rock' does not need to happen
				# 30 times a second. Bots are split into phases by id and only the
				# phase whose turn it is casts rays; the rest keep steering on the
				# hysteresis memory that block already maintains for 15 frames.
				# DRIVING still runs every frame for every bot - only the deciding
				# is staggered, so nothing stutters.
				if globals().get('g_offh_ai_phases') is None:
					try:
						from _constants import CONFIG_OPTIONS as _CFG_AI
						globals()['g_offh_ai_phases'] = max(1, min(6, int(_CFG_AI.get('bot_ai_phases', 3))))
					except Exception:
						globals()['g_offh_ai_phases'] = 3
				globals()['g_offh_ai_phase'] = (globals().get('g_offh_ai_phase', 0) + 1) % globals()['g_offh_ai_phases']

				# --- One-time spawn correction once the terrain has streamed in ---
				# The initial spawn runs before the space is loaded (all ground rays
				# miss -> y=100 fallback, sometimes onto/inside buildings). As soon
				# as the ground answers, snap the player onto formation slot 0 at his
				# team base, facing the enemy base - the original line-up position.
				if not getattr(player, '_offh_spawn_fixed', False):
					try:
						_bases_fix = globals().get('g_offline_bases', {}) or {}
						_my_bl = _bases_fix.get(getattr(player, '_offhangar_team', 1) or 1) or []
						_my_b = _my_bl[0] if _my_bl else None
						if _my_b is not None:
							_probe = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_my_b.x, 800.0, _my_b.z), Math.Vector3(_my_b.x, -500.0, _my_b.z), 128)
							if _probe is not None:
								# Terrain is ready: take the player's line-up slot
								_fs = globals().get('g_offline_formation_slot')
								if _fs is not None:
									_sx, _sz, _syaw = _fs(getattr(player, '_offhangar_team', 1) or 1, 0)
								else:
									_sx, _sz, _syaw = _my_b.x, _my_b.z, 0.0
								# Roof-safe ground probe: while something substantially
								# lower exists below the hit (roof/bridge), keep going down
								_gy = None
								_from_y = 800.0
								for _ri in range(4):
									_c1 = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_sx, _from_y, _sz), Math.Vector3(_sx, -500.0, _sz), 128)
									if _c1 is None: break
									_gy = _c1[0].y
									_c2 = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_sx, _gy - 0.4, _sz), Math.Vector3(_sx, -500.0, _sz), 128)
									if _c2 is None or (_gy - _c2[0].y) < 2.5: break
									_from_y = _gy - 0.4
								if _gy is not None:
									player._offh_spawn_fixed = True
									veh_pos[0] = _sx
									veh_pos[1] = _gy
									veh_pos[2] = _sz
									veh_yaw[0] = _syaw
									_veh_velocity[0] = 0.0
									_veh_turn_velocity[0] = 0.0
									_veh_vert_vel[0] = 0.0
									_veh_airborne[0] = False
									_veh_fall_armed[0] = False  # teleported: next touchdown is free
									LOG_DEBUG('OfflineBattle: spawn corrected to line-up slot:', _sx, _gy, _sz)
					except Exception as _sce:
						LOG_DEBUG('Spawn correction error:', str(_sce))
				
				import debug_utils
				if not hasattr(player, '_debug_dump_done_6'):
					player._debug_dump_done_6 = True
					debug_utils.LOG_DEBUG('AIH_TICK DUMP AT START!')
					_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
					debug_utils.LOG_DEBUG('AIH_TICK keys:', _mock_vehicles.keys())
					
				def _get_terrain_ypr(spaceID, pos, yaw, length=5.0, width=3.0):
					import math, BigWorld, Math
					cos_y = math.cos(yaw)
					sin_y = math.sin(yaw)
					
					hl = length / 2.0
					hw = width / 2.0
					
					# 4 body na podvozku
					fx = pos.x + sin_y * hl
					fz = pos.z + cos_y * hl
					bx = pos.x - sin_y * hl
					bz = pos.z - cos_y * hl
					
					rx = pos.x + cos_y * hw
					rz = pos.z - sin_y * hw
					lx = pos.x - cos_y * hw
					lz = pos.z + sin_y * hw
					
					def get_y(x, z):
						try:
							# Ray from well above: the old +1.5 start sat BELOW steep uphill
							# ground so the hull stayed flat. Accept ground within a hull-height
							# window; reject walls/roofs far above and holes/cliffs far below.
							c = BigWorld.wg_collideSegment(spaceID, Math.Vector3(x, pos.y + 8.0, z), Math.Vector3(x, pos.y - 30.0, z), 128)
							if c is not None and -14.0 < (c[0].y - pos.y) < 6.0:
								return c[0].y
						except: pass
						return pos.y
					
					fy = get_y(fx, fz)
					by = get_y(bx, bz)
					ry = get_y(rx, rz)
					ly = get_y(lx, lz)
					
					pitch = -math.atan2(fy - by, length)
					roll = math.atan2(ry - ly, width)

					# Suspension + tip guard. The hull tilts toward the true downhill but
					# the TOTAL lean is capped as one magnitude - clamping pitch and roll
					# INDEPENDENTLY let a diagonal slope combine them into a ~44 deg
					# tip-over (tank looked laid on its side). A single magnitude clamp
					# keeps the tip DIRECTION honest and caps how far it leans, so the hull
					# lies flush on real 30-35 deg slopes without floating a side, yet never
					# tips over on a steep diagonal. Light damp mimics suspension give; the
					# per-tick blend (dt*8) already absorbs 1-frame spikes.
					pitch *= 0.9
					roll *= 0.9
					_tilt = math.sqrt(pitch * pitch + roll * roll)
					_max_tilt = 0.61                       # ~35 deg total hull lean
					if _tilt > _max_tilt:
						_s = _max_tilt / _tilt
						pitch *= _s
						roll *= _s
					
					# --- Slope gradient: unit downhill dir + magnitude (caller integrates slide) ---
					slide_x = 0.0
					slide_z = 0.0
					slope = 0.0
					try:
						grad_f = (by - fy) / length   # + = downhill toward hull front
						grad_l = (ly - ry) / width    # + = downhill toward hull right
						slope = math.sqrt(grad_f * grad_f + grad_l * grad_l)
						if slope > 0.001:
							dh_x = grad_f * sin_y + grad_l * cos_y
							dh_z = grad_f * cos_y - grad_l * sin_y
							dl = math.sqrt(dh_x * dh_x + dh_z * dh_z)
							if dl > 0.001:
								slide_x = dh_x / dl
								slide_z = dh_z / dl
					except: pass
					# telemetry: height spread of the 4 footprint samples = how edgy/uneven
					# the ground under the hull is (a sharp edge/step reads high here).
					_spread = max(fy, by, ry, ly) - min(fy, by, ry, ly)
					return (yaw, pitch, roll, slide_x, slide_z, slope, _spread)

				def _terrain_support(spaceID, px, py, pz, yaw, hl=2.5):
					# Returns (supportMax, centreY):
					#   supportMax = HIGHEST ground under the fore-aft track footprint
					#     (front/centre/back). A grounded tracked hull rests on the
					#     highest ground it touches, belly hanging - use this for the
					#     rest height so climbing a bank and cresting a ridge stay smooth
					#     (nose does not clip in, hull does not dive early).
					#   centreY = ground directly under the hull centre - the centre of
					#     mass. Drives the airborne trigger and the landing height: once
					#     the CoM clears the ledge the hull tips and FALLS, even if the
					#     tail still overhangs the crest (supportMax would hang it there).
					# Either is None when that probe finds no ground (map edge / void).
					import BigWorld, Math
					_sy = math.sin(yaw); _cy = math.cos(yaw)
					best = None
					centre = None
					for _d in (hl, 0.0, -hl):
						_x = px + _sy * _d
						_z = pz + _cy * _d
						try:
							_c = BigWorld.wg_collideSegment(spaceID, Math.Vector3(_x, py + 2.0, _z), Math.Vector3(_x, py - 1000.0, _z), 128)
						except Exception:
							_c = None
						if _c is not None:
							_yv = _c[0].y
							if best is None or _yv > best:
								best = _yv
							if _d == 0.0:
								centre = _yv
					return (best, centre)

				def _try_destroy_destructible(spaceID, matInfo, yaw, vel):
					import AreaDestructibles, BigWorld, constants
					try:
						if not hasattr(AreaDestructibles, 'g_destructiblesManager') or not AreaDestructibles.g_destructiblesManager:
							return False
							
						hitPt, surfNormal, chunkID, itemIndex, matKind, fname = matInfo
						_dseen = globals().setdefault('g_offh_destr_seen', set())
						_dkey = (matKind, fname)
						if _dkey not in _dseen:
							_dseen.add(_dkey); LOG_DEBUG('Destr hit: matKind=', matKind, 'fname=', repr(fname), 'chunk=', chunkID, 'idx=', itemIndex)
						# Widened band: the strict 71-100 range rejected spawn barriers/props at
						# matKind 102. getDescByFilename below is the real filter, so a wider band
						# only lets more candidates reach the authoritative desc check.
						if matKind < 71 or matKind > 130:
							return False
						desc = AreaDestructibles.g_cache.getDescByFilename(fname)
						if not desc:
							_dnd = globals().setdefault('g_offh_destr_nodesc', set())
							if _dkey not in _dnd:
								_dnd.add(_dkey); LOG_DEBUG('Destr no desc: matKind=', matKind, 'fname=', repr(fname), 'chunk=', chunkID, 'idx=', itemIndex)
							return False
						
						# Data-driven vegetation gate: soft vegetation (bush/shrub/fern)
						# ships with health <= 5; real fallable trees start at 10.
						if desc['type'] in (AreaDestructibles.DESTR_TYPE_TREE, AreaDestructibles.DESTR_TYPE_FALLING_ATOM):
							_hp_gate = desc.get('health', 0)
							if _hp_gate < 10 or _hp_gate > 1000:
								return False
						# All bookkeeping (chunk bootstrap, dedup, encoding) lives in
						# the authority - this path is now just a contact sensor.
						_auth = _get_destr_authority()
						
						typ = desc['type']
						# STRUCTURE (buildings) now falls through to the module-destroy
						# path: online, small buildings crumble module by module as the
						# tank pushes through. Requires the working effects pipeline
						# (terrainEffects + real fake_model), else it raises mid-destroy.
						if _auth.is_destroyed(chunkID, itemIndex, matKind):
							LOG_DEBUG('Destr: already broken')
							return True
							
						if typ == AreaDestructibles.DESTR_TYPE_TREE:
							_destr_ok = _auth.destroy_tree(spaceID, chunkID, itemIndex, yaw, vel, hitPt)
						elif typ == AreaDestructibles.DESTR_TYPE_FALLING_ATOM:
							_destr_ok = _auth.destroy_column(spaceID, chunkID, itemIndex, yaw, vel, hitPt)
						elif typ == AreaDestructibles.DESTR_TYPE_FRAGILE:
							_destr_ok = _auth.destroy_fragile(spaceID, chunkID, itemIndex, hitPt)
						else:
							# STRUCTURE: buildings crumble module by module
							_destr_ok = _auth.destroy_module(spaceID, chunkID, itemIndex, matKind, hitPt, False)
							
						if _destr_ok:
							LOG_DEBUG('Destr SUCCESS!', typ)
						return True
					except Exception as e:
						LOG_DEBUG('Destr Exception:', str(e))
					return False
					
				# The destructible effect pipeline (fall dust, decay effects) calls
				# player.terrainEffects.addNew(); only the real battle Avatar has it.
				# Without it __launchFallEffect raises and trees never start falling.
				try:
					from helpers import bound_effects
					if getattr(player, 'terrainEffects', None) is None:
						player.terrainEffects = bound_effects.StaticSceneBoundEffects()
					# Effects attach to player.newFakeModel(); the offline stub
					# returned BigWorld.Model('') and Model.node() rejects blank
					# models. Use the real fake model like Avatar does.
					def _offh_new_fake_model():
						try:
							return BigWorld.Model('objects/fake_model.model')
						except Exception:
							return BigWorld.Model('')
					player.newFakeModel = _offh_new_fake_model
				except Exception:
					LOG_CURRENT_EXCEPTION()
				
				# Export for _mock_shoot (different function scope); resolved at call time
				loaded_models['_destr_fn'] = _try_destroy_destructible

				def _fell_trees_near(spaceID, pos, yaw, vel, td=None):
					# Offline tree/pole felling. Online the SERVER detected tank-vs-tree
					# contact; the client-side collision probes never return tree/column
					# materials, so trees could never fall offline. Instead: enumerate
					# each chunk's destructibles once (filename + world matrix), then
					# fell TREE / FALLING_ATOM items that intersect the moving hull.
					import math
					import AreaDestructibles
					try:
						if abs(vel) < 1.0:
							return
						mgr = getattr(AreaDestructibles, 'g_destructiblesManager', None)
						if not mgr:
							return
						if mgr.getSpaceID() is None:
							mgr.startSpace(spaceID)
						_st = globals().setdefault('g_offh_tree_state', {'chunks': {}, 'felled': set(), 'spaceID': None})
						if _st.get('spaceID') != spaceID:
							# New battle/space: chunk IDs collide between maps and the
							# dedup sets would suppress destruction of fresh objects.
							_st['chunks'] = {}
							_st['felled'] = set()
							_st['spaceID'] = spaceID
							globals()['g_offh_destr_ordered'] = set()
							globals()['g_offh_destr_chunks'] = set()
							globals()['g_offh_destr_seen'] = set()
						cos_y = math.cos(yaw); sin_y = math.sin(yaw)
						cids = set()
						for _pf in (0.0, 6.0 if vel >= 0 else -6.0):
							try:
								cids.add(AreaDestructibles.chunkIDFromPosition(Math.Vector3(pos.x + sin_y * _pf, pos.y, pos.z + cos_y * _pf)))
							except Exception:
								pass
						hw = 1.6; hl_f = 3.6; hl_b = 3.6
						try:
							if td is not None and hasattr(td, 'hull') and 'hitTester' in td.hull:
								bbox = td.hull['hitTester'].bbox
								hw = max(abs(bbox[0][0]), abs(bbox[1][0]))
								hl_b = abs(bbox[0][2])
								hl_f = abs(bbox[1][2])
						except Exception:
							pass
						for cid in cids:
							trees = _st['chunks'].get(cid)
							if trees is None:
								_dfn = None
								try:
									_dfn = BigWorld.wg_getChunkDestrFilenames(spaceID, cid)
								except Exception:
									pass
								if _dfn is None:
									continue # chunk not streamed in yet; retry next tick
								trees = []
								_cm_t = None
								try:
									_cm_t = BigWorld.wg_getChunkMatrix(spaceID, cid).translation
								except Exception:
									pass
								if _cm_t is None:
									continue
								for _ti in xrange(len(_dfn)):
									try:
										desc = AreaDestructibles.g_cache.getDescByFilename(_dfn[_ti])
										if desc is None:
											continue
										if desc['type'] not in (AreaDestructibles.DESTR_TYPE_TREE, AreaDestructibles.DESTR_TYPE_FALLING_ATOM, AreaDestructibles.DESTR_TYPE_FRAGILE):
											continue
										# Data-driven vegetation gate: destructibles.xml gives
										# soft vegetation (bushes/shrubs/ferns/weeds) health<=5
										# (or -2); real fallable trees start at health 10.
										# ChristmasTree sentinels use 40000 = unrammable.
										if desc['type'] != AreaDestructibles.DESTR_TYPE_FRAGILE:
											_hp_gate = desc.get('health', 0)
											if _hp_gate < 10 or _hp_gate > 1000:
												continue
										# Destructible matrices are CHUNK-LOCAL: world pos =
										# chunk translation + destructible translation
										# (see AreaDestructibles.__launchEffect)
										_m = Math.Matrix(BigWorld.wg_getDestructibleMatrix(spaceID, cid, _ti))
										trees.append((_ti, _cm_t.x + _m.translation.x, _cm_t.z + _m.translation.z, desc['type'], _dfn[_ti], desc.get('health', 0), desc.get('mass', 0)))
									except Exception:
										continue
								_st['chunks'][cid] = trees
								LOG_DEBUG('DestrTree: chunk registry', cid, len(trees), 'trees/poles')
								if trees:
									LOG_DEBUG('DestrTree: sample world pos', trees[0][1], trees[0][2], 'tank at', pos.x, pos.z)
							if not trees:
								continue
							reach_f = hl_f + 0.8 + min(abs(vel) * 0.25, 1.2)
							for (_ti, _tx, _tz, _ttyp, _tfn, _thp, _tmass) in trees:
								dx = _tx - pos.x; dz = _tz - pos.z
								if dx * dx + dz * dz > 64.0:
									continue
								fwd = dx * sin_y + dz * cos_y
								lat = dx * cos_y - dz * sin_y
								if vel < 0:
									in_reach = -(hl_b + 0.8) <= fwd <= hl_f
								else:
									in_reach = -hl_b <= fwd <= reach_f
								if abs(lat) > hw + 0.5 or not in_reach:
									continue
								_key = (cid, _ti)
								if _key in _st['felled']:
									continue
								_st['felled'].add(_key)
								fall_yaw = yaw if vel >= 0 else (yaw + math.pi)
								_auth = _get_destr_authority()
								if _ttyp == AreaDestructibles.DESTR_TYPE_FRAGILE:
									# Haybales, barrels, wire fences: collision skins often
									# resolve to no item in the probes; crush by proximity.
									_ok = _auth.destroy_fragile(spaceID, cid, _ti, pos)
								elif _ttyp == AreaDestructibles.DESTR_TYPE_TREE:
									_ok = _auth.destroy_tree(spaceID, cid, _ti, fall_yaw, vel, pos)
								else:
									_ok = _auth.destroy_column(spaceID, cid, _ti, fall_yaw, vel, pos)
								if _ok:
									LOG_DEBUG('DestrTree: FELLED', cid, _ti, 'type', _ttyp, 'hp', _thp, 'mass', _tmass, _tfn)
					except Exception:
						import traceback
						LOG_DEBUG('DestrTree error:', traceback.format_exc())
				
				def _try_destroy_solid_hit(spaceID, seg_start, hit_pt, yaw, vel):
					# wg_collideSegment returns no material info: probe the hit point for a
					# destructible (fence/wall segment) before treating it as solid
					import BigWorld
					try:
						# Probe along the SURFACE NORMAL like Vehicle.onStaticCollision: the
						# forward probe grazed the solid collision skin (matKind 101/109, empty
						# fname); crossing the surface perpendicular resolves the destructible
						# mesh's real chunk/index/fname. dir points into the surface; normal = -dir,
						# so segStart = point - normal*3 = point + dir*3, segStop = point - dir*2.
						_dirv = hit_pt - seg_start
						if _dirv.length > 0.001:
							_dirv.normalise()
						else:
							return False
						_seg_a = hit_pt + _dirv.scale(3.0)
						_seg_b = hit_pt - _dirv.scale(2.0)
						_mi = BigWorld.wg_getMatInfoNearPoint(spaceID, _seg_a, _seg_b, hit_pt, lambda *a: False)
						if _mi is not None:
							return _try_destroy_destructible(spaceID, _mi, yaw, vel)
					except Exception:
						pass
					return False
				
				def _collision_damage(victim, dmg, attacker_id):
					# Ported ram-damage sink: HP, damage panel / marker feedback, kill.
					if dmg <= 0 or victim is None:
						return
					try:
						if getattr(victim, 'health', 0) <= 0:
							return
						victim.health = max(0, int(getattr(victim, 'health', 0)) - int(dmg))
						victim.last_killer_id = attacker_id
						v_id = getattr(victim, 'id', -1)
						is_player_victim = (v_id == getattr(player, 'playerVehicleID', -1))
						if is_player_victim:
							try:
								if hasattr(player, 'vehicle') and player.vehicle:
									player.vehicle.health = victim.health
							except Exception:
								pass
							try:
								from gui import WindowsManager as _rwm
								bw = getattr(_rwm.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, 'damagePanel'):
									bw.damagePanel.updateHealth(victim.health)
							except Exception:
								pass
						else:
							try:
								if hasattr(player.arena, 'onVehicleStatisticsUpdate'):
									player.arena.onVehicleStatisticsUpdate(v_id)
								from gui import WindowsManager as _rwm
								bw = getattr(_rwm.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, 'vMarkersManager'):
									marker = getattr(victim, 'marker', None)
									if marker is not None:
										# max(0, ...) is not cosmetic. VehicleMarkersManager.swf, VehicleMarker.updateHealth
										# (curHealth, flag, damageType) starts with:  if (curHealth < 0) damageType = 'explosion'
										# and VehicleMarkerFlags.ALLOW_ATTACK_REASONS = ['fire','explosion'] is exactly the set
										# that makes hitExplosion.setFlag() draw the red blow-up symbol next to the damage
										# number. A killing shot drives health negative, so EVERY kill lit that icon up. Retail
										# never sends a negative value here.
										bw.vMarkersManager.onVehicleHealthChanged(marker, max(0, victim.health), attacker_id, 0)
										try:
											bw.vMarkersManager.showVehicleDamageInfo(marker, dmg, 0, 0, 0)
										except Exception:
											pass
							except Exception:
								pass
						if victim.health <= 0 and not is_player_victim:
							_offh_set_alive(victim, False)
							try:
								if v_id in player.arena.vehicles:
									player.arena.vehicles[v_id]['isAlive'] = False
								if hasattr(player.arena, 'onVehicleKilled'):
									player.arena.onVehicleKilled(v_id, attacker_id, 3)  # reason 3 = ram (wrapper swaps wreck)
							except Exception:
								pass
					except Exception as e:
						LOG_DEBUG('Collision damage error:', str(e))

				def _tank_hull_dims(td):
					# (half_width, front_len, back_len) from the hull hit-tester bbox.
					# Cached per descriptor: this sits inside the per-frame tank-pair loop.
					_c = globals().setdefault('g_offh_dims', {})
					_k = id(td)
					_v = _c.get(_k)
					if _v is not None:
						return _v
					hw = 1.5; hlf = 3.5; hlb = 3.5
					try:
						if td is not None and hasattr(td, 'hull') and 'hitTester' in td.hull:
							bbox = td.hull['hitTester'].bbox
							hw = max(abs(bbox[0][0]), abs(bbox[1][0]))
							hlb = abs(bbox[0][2])
							hlf = abs(bbox[1][2])
					except: pass
					_v = (hw, hlf, hlb)
					_c[_k] = _v
					return _v

				def _tank_circles(x, z, yaw, td):
					# Approximate the rectangular hull by a chain of circles along the
					# forward axis (circle radius = hull half-width). Cheap tank-vs-tank.
					import math
					hw, hlf, hlb = _tank_hull_dims(td)
					r = hw if hw > 0.8 else 0.8
					fx = math.sin(yaw); fz = math.cos(yaw)
					start = -hlb + r
					end = hlf - r
					out = []
					if end <= start:
						out.append((x, z, r))
						return out
					n = int((end - start) / (r * 1.5))
					if n < 1: n = 1
					step = (end - start) / n
					i = 0
					while i <= n:
						o = start + step * i
						out.append((x + fx * o, z + fz * o, r))
						i += 1
					return out

				def _tank_resolve(self_id, x, z, yaw, td, inv_self, svx, svz, y=None):
					# Velocity-relative impulse (e=0) + Baumgarte push-apart vs every
					# other living tank. (svx, svz) = self's world velocity. Returns
					# (corr_x, corr_z, dvx, dvz): positional correction + velocity
					# impulse for self's push velocity. Movement is NEVER blocked ->
					# no deadlock; inverse-mass weighting shoves the lighter tank aside.
					import BigWorld, math
					_mv = globals().get('G_MOCK_VEHICLES', {}) or {}
					_plobj = BigWorld.player()
					_pid = getattr(_plobj, 'playerVehicleID', -1)
					my_c = _tank_circles(x, z, yaw, td)
					corr_x = 0.0; corr_z = 0.0; dvx = 0.0; dvz = 0.0
					_SLOP = 0.02; _PCT = 0.4
					# PERF: this runs once per living tank and walks every other one, so
					# at 30 bots it is 900 pair iterations per frame - the single most
					# executed inner loop in the tick. Two things were wasteful:
					#   - .items() built a fresh 30-tuple list on every one of the 30
					#     calls per frame. iteritems() walks the same dict for free.
					#   - the yaw, descriptor, velocity and push reads plus two trig
					#     calls per pair ran BEFORE the height and distance gates, and
					#     the gates then threw almost all of it away: out of 900 pairs
					#     only the handful actually within 12 m can ever matter.
					# Now each pair reads position only, gets culled, and the expensive
					# part happens for survivors. The gates and their order are
					# unchanged, so the physics result is identical.
					for oid, ov in _mv.iteritems():
						if oid == self_id or ov is None:
							continue
						_is_player = (oid == _pid)
						if _is_player:
							ox = veh_pos[0]; oy = veh_pos[1]; oz = veh_pos[2]
						else:
							op = getattr(ov, 'position', None)
							if op is None:
								continue
							ox = op.x; oy = op.y; oz = op.z
						# Height gate: the circles are 2D, so a hull FALLING past a tank
						# (cliff drop next to it) collided in x/z despite metres of air
						# between them - phantom rams and corrections mid-flight, and a
						# flipped push normal on touchdown that ejected the hull through
						# the other tank ('glitched through after the fall').
						if y is not None and abs(y - oy) > 3.0:
							continue
						dcx = x - ox; dcz = z - oz
						if dcx * dcx + dcz * dcz > 144.0:
							continue
						# Survived both gates - now the expensive reads are worth doing.
						if _is_player:
							oyaw = veh_yaw[0]; otd = loaded_models.get('td'); inv_o = 1.0 / max(_phys_mass, 1.0)
							ovx = math.sin(oyaw) * _veh_velocity[0] + (getattr(_plobj, '_push_x', 0.0) or 0.0)
							ovz = math.cos(oyaw) * _veh_velocity[0] + (getattr(_plobj, '_push_z', 0.0) or 0.0)
						else:
							oyaw = getattr(ov, 'yaw', 0.0); otd = getattr(ov, 'typeDescriptor', None); inv_o = 1.0 / 25000.0
							_ovv = getattr(ov, '_veh_velocity', 0.0) or 0.0
							ovx = math.sin(oyaw) * _ovv + (getattr(ov, '_push_x', 0.0) or 0.0)
							ovz = math.cos(oyaw) * _ovv + (getattr(ov, '_push_z', 0.0) or 0.0)
						_isum = inv_self + inv_o
						if _isum <= 0.0:
							continue
						_best = 0.0; _bnx = 0.0; _bnz = 0.0
						_ocl = _tank_circles(ox, oz, oyaw, otd)  # hoisted: was rebuilt per self-circle
						for _cc in my_c:
							for _oc in _ocl:
								ddx = _cc[0] - _oc[0]; ddz = _cc[1] - _oc[1]
								rr = _cc[2] + _oc[2]
								_d2 = ddx * ddx + ddz * ddz
								if _d2 < rr * rr and _d2 > 1e-06:
									_dist = math.sqrt(_d2)
									_pen = rr - _dist
									if _pen > _best:
										_best = _pen; _bnx = ddx / _dist; _bnz = ddz / _dist
						if _best > 0.0:
							_corr = max(_best - _SLOP, 0.0) / _isum * _PCT * inv_self
							corr_x += _bnx * _corr; corr_z += _bnz * _corr
							_vn = (svx - ovx) * _bnx + (svz - ovz) * _bnz
							if _vn < 0.0:
								_j = -_vn / _isum
								dvx += _j * inv_self * _bnx; dvz += _j * inv_self * _bnz
								# Ram damage (ported): approach speed beyond 3.5 m/s hurts both hulls
								if _vn < -3.5:
									_now = BigWorld.time()
									_rcd = globals().setdefault('g_offh_ram_cd', {})
									_rkey = (min(self_id, oid), max(self_id, oid))
									if _now - _rcd.get(_rkey, 0.0) > 0.75:
										_rcd[_rkey] = _now
										_rel = -_vn
										# physics.ram_damage: mass-ratio weighted, same law everywhere
										_dmo, _dms = _PHY.ram_damage(_rel, 1.0 / max(inv_self, 1e-09), 1.0 / max(inv_o, 1e-09))
										_rsv = _mv.get(self_id)
										if _dmo > 0:
											_collision_damage(ov, _dmo, self_id)
										if _dms > 0 and _rsv is not None:
											_collision_damage(_rsv, _dms, oid)
										LOG_DEBUG('RAM:', self_id, '<->', oid, 'rel=%.1f' % _rel, 'dmg', _dmo, _dms)
					return (corr_x, corr_z, dvx, dvz)

				def _drive_pitch(spaceID, x, z, yaw, y):
					# Fore/aft GROUND slope under the hull (nose-up = negative, BigWorld
					# convention) for the drive/slide physics. Sampled close to the hull
					# (L = track half-length) so a WALL a few metres ahead is not read as
					# a 69 deg 'slope' that injects phantom gravity. A sample that rises
					# more than a drivable step over L is a wall/cliff face, not ground -
					# it is clamped to the drivable ceiling (the collision code, not
					# gravity, handles walls). Final pitch clamped to +/-40 deg: steeper
					# than that no tank drives, so it must never drive the engine/hold maths.
					import math, BigWorld, Math
					fx = math.sin(yaw); fz = math.cos(yaw)
					L = 2.0
					_WALL_RISE = L * 1.43   # ~55 deg over L: real steep slopes register
					                        # (so they slide); only near-vertical walls clamp
					def _gy(px, pz):
						# Skip geometry ABOVE the hull. The probe starts 15 m up so an uphill sample
						# ahead is still caught, but that also made it hit a BRIDGE DECK when driving
						# underneath: front sample = bridge, back sample = road, which reads as a
						# near-vertical rise, gets clamped to the wall ceiling and cuts the engine -
						# the tank simply stopped at the underpass. Anything more than 3.5 m above the
						# hull cannot be ground we drive on (the drivable band over L is 2.86 m), so
						# drop below it and probe again.
						_from = y + 15.0
						for _ in range(3):
							try:
								c = BigWorld.wg_collideSegment(spaceID, Math.Vector3(px, _from, pz), Math.Vector3(px, y - 60.0, pz), 128)
							except:
								return None
							if c is None:
								return None
							_yv = c[0].y
							if _yv > y + 3.5:
								_from = _yv - 0.5
								continue
							return _yv
						return None
					fy = _gy(x + fx * L, z + fz * L)
					by = _gy(x - fx * L, z - fz * L)
					if fy is None or by is None:
						return 0.0
					# Clamp each side's height delta to the drivable band: a wall ahead
					# (huge rise) or a cliff (huge drop) must not tilt the physics.
					_fd = fy - y
					if _fd > _WALL_RISE: _fd = _WALL_RISE
					elif _fd < -_WALL_RISE: _fd = -_WALL_RISE
					_bd = by - y
					if _bd > _WALL_RISE: _bd = _WALL_RISE
					elif _bd < -_WALL_RISE: _bd = -_WALL_RISE
					p = -math.atan2(_fd - _bd, 2.0 * L)
					if p > 0.96: p = 0.96      # ~55 deg: real slopes keep full gravity/slide
					if p < -0.96: p = -0.96
					return p

				def _check_horizontal_collision(spaceID, pos, yaw, vel, td=None, airborne=False, dt=0.04):
					import math, BigWorld, Math
					try:
						hw = 1.5
						hl_front = 3.5
						hl_back = 3.5

						if td and hasattr(td, 'hull') and 'hitTester' in td.hull:
							try:
								bbox = td.hull['hitTester'].bbox
								hw = max(abs(bbox[0][0]), abs(bbox[1][0])) - 0.1
								hl_back = abs(bbox[0][2])
								hl_front = abs(bbox[1][2])
							except: pass

						# Look-ahead beyond the hull. The old flat +2.0 m made an invisible
						# wall 2 m before every obstacle, and DURING A FALL it saw the cliff
						# face below-ahead and zeroed the speed mid-air - the tank then hugged
						# the wall and trickled down instead of flying a ballistic arc.
						# Grounded: just enough to not tunnel at speed. Airborne: only the
						# distance actually travelled this tick - contact stops, proximity not.
						if airborne:
							_ahead = abs(vel) * dt + 0.2
						else:
							_ahead = min(1.2, max(0.4, abs(vel) * dt * 2.0))
						back_margin = -0.5 if vel > 0 else 0.5
						front_margin = (hl_front + _ahead) if vel > 0 else -(hl_back + _ahead)
						
						cos_y = math.cos(yaw)
						sin_y = math.sin(yaw)

						# DRIVABLE-SLOPE GUARD: a rising HILL is not a wall. Sample the ground under
						# the hull and at the hull front along the heading; if it rises as a climbable
						# slope (avg gradient below ~52 deg) the hull CLIMBS it - report no collision.
						# Wall-stopping on steep hills slammed the model speed 60->9 km/h (the oversteer).
						try:
							_fw = 1.0 if vel >= 0 else -1.0
							_look = (hl_front if vel > 0 else hl_back) + _ahead
							# Walk the ground along the heading in small steps. A drivable HILL rises
							# gradually at EVERY step -> climb it (no collision). A big rock / step /
							# wall SPIKES one step (rises more than a climbable amount over its short
							# length) -> leave it to the ray wall-check so the hull is BLOCKED.
							_seg_n = 6
							_seg = _look / _seg_n
							_smooth = True
							_prev_y = None
							for _si in range(_seg_n + 1):
								_dd = _seg * _si
								_px = pos.x + sin_y * _dd * _fw
								_pz = pos.z + cos_y * _dd * _fw
								_gg = BigWorld.wg_collideSegment(spaceID, Math.Vector3(_px, pos.y + 12.0, _pz), Math.Vector3(_px, pos.y - 5.0, _pz), 128)
								if _gg is None:
									_smooth = False
									break
								if _prev_y is not None and (_gg[0].y - _prev_y) > _seg * 1.28:
									_smooth = False   # step rises steeper than ~52 deg = rock/step, not a hill
									break
								_prev_y = _gg[0].y
							if _smooth:
								return False
						except: pass

						for offset_x in (-hw, 0, hw):
							sx = pos.x + cos_y * offset_x
							sz = pos.z - sin_y * offset_x
							
							x1 = sx + sin_y * back_margin
							z1 = sz + cos_y * back_margin
							x2 = sx + sin_y * front_margin
							z2 = sz + cos_y * front_margin
							
							# Nezávislý scan na stromy a ploty před tankem
							try:
								if offset_x != 0:
									raise StopIteration  # perf: look-ahead only on centre column
								seg_start = Math.Vector3(sx, pos.y + 0.5, sz)
								seg_stop = Math.Vector3(x2, pos.y + 0.5, z2)
								matInfo = BigWorld.wg_getMatInfoNearPoint(spaceID, seg_start, seg_stop, seg_stop, lambda *a: False)
								if matInfo:
									if _try_destroy_destructible(spaceID, matInfo, yaw, vel):
										# Pokud jsme rozbili strom/plot, můžeme ignorovat pevnou kolizi, která na něj případně navazuje (nebo i když žádná není)
										pass
							except: pass
							
							# Spodní paprsek pro pevnou geometrii (0.6m nad zemí)
							start_bot = Math.Vector3(x1, pos.y + 0.6, z1)
							end_bot = Math.Vector3(x2, pos.y + 0.6, z2)
							col_bot = BigWorld.wg_collideSegment(spaceID, start_bot, end_bot, 128)
							
							if col_bot is not None:
								d_bot = (col_bot[0] - start_bot).length
								target_len = abs(back_margin) + (hl_front if vel > 0 else hl_back) + _ahead
								if d_bot < target_len:
									# Něco jsme trefili, zkontrolujeme horní paprsek (1.6m nad zemí)
									start_top = Math.Vector3(x1, pos.y + 1.6, z1)
									end_top = Math.Vector3(x2, pos.y + 1.6, z2)
									col_top = BigWorld.wg_collideSegment(spaceID, start_top, end_top, 128)
									
									if col_top is not None:
										d_top = (col_top[0] - start_top).length
										if (d_top - d_bot) < 0.5:
											if _try_destroy_solid_hit(spaceID, start_bot, col_bot[0], yaw, vel): pass
											else: return True
									else:
										start_mid = Math.Vector3(x1, pos.y + 1.1, z1)
										end_mid = Math.Vector3(x2, pos.y + 1.1, z2)
										col_mid = BigWorld.wg_collideSegment(spaceID, start_mid, end_mid, 128)
										if col_mid is not None:
											d_mid = (col_mid[0] - start_mid).length
											if (d_mid - d_bot) < 0.25:
												if _try_destroy_solid_hit(spaceID, start_bot, col_bot[0], yaw, vel): pass
												else: return True
										else:
											# Low object (<1.1m): only the bottom ray caught it. Crush it if
											# it's a destructible (fence / small prop) so the tank drives
											# THROUGH, not over it. Non-destructibles (low rocks) stay drivable.
											_try_destroy_solid_hit(spaceID, start_bot, col_bot[0], yaw, vel)
					except: pass
					return False

				def _offh_land_impact(vy):
					# 0.8.x landing: fall damage from the COMBINED slam speed (vertical
					# fall + carried lateral drift - a hull that slid sideways off a
					# slope hits harder). The residual lateral becomes ground-slide speed
					# so the tank skids on after touchdown, then the air-lateral clears.
					try:
						_alx = getattr(player, '_air_lat_vx', 0.0) or 0.0
						_alz = getattr(player, '_air_lat_vz', 0.0) or 0.0
						_lat = math.sqrt(_alx * _alx + _alz * _alz)
						if _lat > 0.01:
							player._slide_spd = max(getattr(player, '_slide_spd', 0.0) or 0.0, _lat)
						player._air_lat_vx = 0.0
						player._air_lat_vz = 0.0
						if getattr(mock_veh, 'health', 0) <= 0:
							return
						_iv = math.sqrt(vy * vy + _lat * _lat)   # combined slam speed
						# physics.fall_damage: free below ~4 m-equivalent, then linear
						_dmg = _PHY.fall_damage(getattr(mock_veh, 'maxHealth', 400), _iv)
						if _dmg <= 0:
							return
						mock_veh.health -= _dmg
						LOG_DEBUG('OfflineBattle: landing impact %.1f m/s (lat %.1f) -> %d damage' % (_iv, _lat, _dmg))
						try:
							import gui.WindowsManager
							_bwli = gui.WindowsManager.g_windowsManager.battleWindow
							if _bwli and hasattr(_bwli, 'damagePanel'):
								_bwli.damagePanel.updateHealth(max(0, mock_veh.health))
						except Exception:
							pass
						if mock_veh.health <= 0:
							mock_veh.health = 0
							mock_veh.last_killer_id = -1
							try:
								player.arena.onVehicleKilled(getattr(mock_veh, 'id', player.playerVehicleID), -1, 2)
							except Exception:
								pass
						try:
							if hasattr(player, 'vehicle') and player.vehicle:
								player.vehicle.health = mock_veh.health
								player.guiSessionProvider.invalidateVehicleState(1, player.playerVehicleID, mock_veh.health, mock_veh.health)
						except Exception:
							pass
					except Exception:
						pass

				if not _engine_state['init']:
					try:
						td = loaded_models.get('td')
						root_model = loaded_models.get('chassis') or loaded_models.get('hull') or loaded_models.get('turret') or loaded_models.get('gun')
						engine_dict = getattr(td, 'engine', None)
						chassis_dict = getattr(td, 'chassis', None)
						if td and engine_dict and chassis_dict and root_model is not None and root_model.inWorld:
							_engine_state['snd1'] = root_model.playSound(engine_dict['sound'])
							_engine_state['snd2'] = root_model.playSound(chassis_dict['sound'])
							# Latch only on a REAL attach. playSound answers None when the event
							# cannot be created - which the FMOD pool makes a live possibility in a
							# 30-bot battle (see the range culling in the bot loop) - and setting
							# init anyway meant the player's own engine and tracks stayed silent for
							# the WHOLE battle with no retry, while the param handles below could
							# never resolve either. Unlike bots, the player has no culling pass to
							# reset the flag, so this was permanent.
							# Refs on the mock so _sync_burn_and_death can stop them on death
							mock_veh._snd_engine = _engine_state['snd1']
							mock_veh._snd_tracks = _engine_state['snd2']
							if _engine_state['snd1'] is None or _engine_state['snd2'] is None:
								# Bounded retry, not a per-frame one: if the event name is simply wrong
								# no amount of asking helps, and asking FMOD every frame forever is its
								# own bug. ~20 frames is plenty for a transiently full pool.
								_engine_state['tries'] = (_engine_state.get('tries', 0) or 0) + 1
								if _engine_state['tries'] >= 20:
									_engine_state['init'] = True   # give up quietly
									LOG_DEBUG('OfflineBattle: engine sound attach gave up after %d tries (%s / %s)' % (
										_engine_state['tries'], engine_dict['sound'], chassis_dict['sound']))
							else:
								_engine_state['init'] = True
								LOG_DEBUG('OfflineBattle: Engine sounds attached!', engine_dict['sound'], chassis_dict['sound'])
					except Exception as e:
						LOG_DEBUG('OfflineBattle: Engine sounds failed:', str(e))

				# --- Track animation (one-time): WGVehicleFashion drives the scrolling
				# track materials and wheels, exactly like the original VehicleAppearance ---
				if not loaded_models.get('_fashion_done'):
					try:
						_f_ch = loaded_models.get('chassis')
						_f_td = loaded_models.get('td')
						if _f_ch is not None and _f_td is not None and getattr(_f_ch, 'inWorld', False):
							loaded_models['_fashion_done'] = True
							_fash = BigWorld.WGVehicleFashion()
							try:
								_fash.maxMovement = _f_td.physics['speedLimits'][0]
							except Exception:
								pass
							# Swinging setup is mandatory: without a swinging node ('V' =
							# vehicle root) the fashion refuses to attach / stays inert
							try:
								_f_sw = _f_td.hull['swinging']
								# VehicleAppearance scales pitchParams by _PITCH_SWINGING_MODIFIERS before
								# handing them over; feeding the raw descriptor values gave the hull a
								# noticeably different pitch response from retail (the 2nd term is x1.88).
								_f_pp = tuple(_p * _m for (_p, _m) in zip(_f_sw['pitchParams'], (0.9, 1.88, 0.3, 4.0, 1.0, 1.0)))
								_fash.setPitchSwinging('V', *_f_pp)
								_fash.setRollSwinging('V', *_f_sw['rollParams'])
								_fash.setShotSwinging('V', _f_sw['sensitivityToImpulse'])
							except Exception as _swe:
								LOG_DEBUG('Fashion swinging setup failed:', str(_swe))
							_f_tr = _f_td.chassis['tracks']
							try:
								_fash.setLods(_f_td.chassis['traces']['lodDist'], _f_td.chassis['wheels']['lodDist'], _f_tr['lodDist'], _f_td.hull['swinging']['lodDist'])
							except Exception:
								pass
							_fash.setTracks(_f_tr['leftMaterial'], _f_tr['rightMaterial'], _f_tr['textureScale'])
							# (setTrackTraces intentionally NOT called - see crash note in the sweep)
							# Road wheels spin with movement: replicate _setupVehicleFashion's
							# addWheelGroup/addWheel. nodes = '<template><i>' from chassis['wheels'].
							try:
								_wcfg = _f_td.chassis['wheels']
								for _grp in _wcfg['groups']:
									_wnodes = ['%s%d' % (_grp[1], _wi) for _wi in range(_grp[3], _grp[3] + _grp[2])]
									_fash.addWheelGroup(_grp[0], _grp[4], _wnodes)
								for _wh in _wcfg['wheels']:
									_fash.addWheel(_wh[0], _wh[2], _wh[1])
								LOG_DEBUG('Fashion road wheels added')
							except Exception as _we:
								LOG_DEBUG('Fashion road wheels failed:', str(_we))
							# EXPERIMENT: real game sources fashion.movementInfo from WGVehicleFilter2;
							# the kinematic mock has none, so attach a settable Vector4 provider and
							# drive it per frame from speed. Log the available classes for diagnosis.
							try:
								LOG_DEBUG('Math Vector4 classes:', [ _n for _n in dir(Math) if 'Vector4' in _n ])
								_fash.movementInfo = Math.Vector4(0.0, 0.0, 0.0, 0.0)
								loaded_models['_fashion_mv'] = _fash
								LOG_DEBUG('Fashion movementInfo set Vector4; readback=', repr(_fash.movementInfo))
							except Exception as _mve:
								LOG_DEBUG('Fashion movementInfo attach failed:', str(_mve))
							_f_ch.wg_fashion = _fash
							# Chassis camo goes through THIS fashion (stashed at model setup);
							# a separate WGBaseFashion would detach the scrolling track material.
							try:
								_ca = loaded_models.get('_camo_args')
								if _ca is not None:
									_fash.setCamouflage(_ca[0], _ca[1], _ca[2], _ca[3], _ca[4], _ca[5], _ca[6], _ca[7])
									LOG_DEBUG('Chassis camo applied via track fashion')
							except Exception as _cae:
								LOG_DEBUG('Chassis camo via fashion failed:', str(_cae))
							loaded_models['_fashion'] = _fash
							# DO NOT point this at the real fashion. Doing so makes _trigger_shot_impulse
							# actually call fashion.receiveShotImpulse(), and the client then died with
							# EXCEPTION_ACCESS_VIOLATION 0xC0000005 Read@0x8 at loadHangarSpaceVehicle -
							# twice, same address. WG feeds a fashion placingCompensationMatrix and
							# physicsInfo from WGVehicleFilter2 (VehicleAppearance._setupVehicleFashion);
							# a mock has no such filter, so the shot-swinging path dereferences null.
							# Hull rocking needs those two set first - until then it stays a no-op.
							LOG_DEBUG('OfflineBattle: track fashion attached (player)')
							try:
								LOG_DEBUG('WGVehicleFashion dir:', [ _n for _n in dir(_fash) if not _n.startswith('__') ])
							except Exception:
								pass
					except Exception as _fe:
						loaded_models['_fashion_done'] = True
						LOG_DEBUG('Track fashion failed:', str(_fe))

				# --- WoT-style Hull Physics ---
				# Determine input direction
				throttle = 0
				steer = 0
				
				# Allow WASD to move the tank even in Arty Mode, because offline edge-panning is broken
				# and the user needs to be able to rotate the hull to bring targets into the gun arc!
				if getattr(player, '_is_dead', False) is True:
					throttle = 0
					steer = 0
				else:
					# Honor Controls->Movement rebinds from the settings screen: the
					# raw W/A/S/D polls ignored CommandMapping, so rebinding movement
					# keys saved fine but changed nothing in the offline battle.
					try:
						import CommandMapping as _CMap
						_cmg = _CMap.g_instance
						_k_fwd = _cmg.get('CMD_MOVE_FORWARD') or Keys.KEY_W
						_k_bwd = _cmg.get('CMD_MOVE_BACKWARD') or Keys.KEY_S
						_k_lft = _cmg.get('CMD_ROTATE_LEFT') or Keys.KEY_A
						_k_rgt = _cmg.get('CMD_ROTATE_RIGHT') or Keys.KEY_D
					except Exception:
						_k_fwd, _k_bwd, _k_lft, _k_rgt = Keys.KEY_W, Keys.KEY_S, Keys.KEY_A, Keys.KEY_D
					if BigWorld.isKeyDown(_k_fwd): throttle = 1
					elif BigWorld.isKeyDown(_k_bwd): throttle = -1

					if BigWorld.isKeyDown(_k_lft): steer = -1
					elif BigWorld.isKeyDown(_k_rgt): steer = 1
					
					# Auto-hull rotation if aiming outside limits
					# Only auto-rotate if not manually steering
					if steer == 0:
						steer = _gun_state.get('auto_steer', 0)
				
				# Freeze tank movement if battle hasn't started yet (Prebattle Countdown)
				arena = getattr(BigWorld.player(), 'arena', None)
				if arena is not None and getattr(arena, 'period', 3) < 3:
					throttle = 0
					steer = 0
				
				cur_vel = _veh_velocity[0]
				# Longitudinal law (engine / rolling resist / brake / slope / clamps):
				# physics.longitudinal_step - the SAME function every bot integrates
				# with, parameterized by this tank's real descriptor values.
				_slope_p = 0.0
				# Probe ground slope on EVERY grounded tick, incl. parked/idle: a hull
				# parked on a slope must feel gravity along the hull (standstill slide),
				# so longitudinal_step needs the real pitch, not 0.0. Old gate on motion
				# fed 0.0 at rest and killed the documented stand-still slide.
				if not _veh_airborne[0]:
					_raw_sp = _drive_pitch(_offh_bspace(), veh_pos[0], veh_pos[2], veh_yaw[0], veh_pos[1])
					# The ground probe throws isolated garbage (LOD seams produced single-frame
					# 78 deg spikes), but the old cure was worse than the disease: rate-limiting
					# the TRACKED pitch to 0.3 * 1.2 * dt rad capped it at 20.6 deg/s at ANY
					# framerate. Driving onto a hill at 13 m/s the physics then needed ~1.5 s to
					# perceive a 30 deg slope - by which time the hull had already covered ~19 m
					# of it. THAT lag, not a weak slip drag, is what let momentum carry a tank up
					# slopes the engine flatly refuses; the slip term was fed a far too shallow
					# grade the whole way. A median rejects the spikes outright (they are isolated
					# by nature, and 3 of 5 samples would have to be bad to shift it) while a
					# genuine slope now registers within a couple of frames.
					_hist = getattr(player, '_offh_pitch_hist', None)
					if _hist is None:
						_hist = [_raw_sp] * 5
						player._offh_pitch_hist = _hist
					_hist.append(_raw_sp)
					del _hist[:-5]
					_med = sorted(_hist)[2]
					_prev_sp = getattr(player, '_offh_smooth_pitch', _med)
					_slope_p = _prev_sp + (_med - _prev_sp) * 0.5
					player._offh_smooth_pitch = _slope_p
					player._offh_last_pitch = _slope_p  # launch seed for ramp jumps (see airborne start)
				else:
					player._offh_smooth_pitch = 0.0
				# Handbrake (SPACE): locks the tracks. Held down it overrides the throttle,
				# so it also works as an emergency stop, not just a parking brake.
				_hb_on = False
				try:
					import Keys as _hbK
					_hb_on = bool(BigWorld.isKeyDown(_hbK.KEY_SPACE)) and not _offh_cursor_shown()
				except Exception:
					_hb_on = False
				player._offh_handbrake = _hb_on
				# Crew + module effects on mobility: a downed driver halves the throttle, and
				# a destroyed engine or track stops the hull outright (the repair tick clears
				# those flags again once the module is functional).
				_p_locked = False
				try:
					_pm_mob = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					if _pm_mob is not None:
						# A thrown track is a LOCKED track, not a released throttle: cutting the
						# throttle alone left the hull coasting another 10-15 m after it was
						# immobilised. Feed it through the handbrake branch, which is grip-limited
						# like every other brake here, so a cliff still slides the hull off.
						# A dead ENGINE is different and stays a coast - the hull still rolls.
						_p_locked = bool(getattr(_pm_mob, 'is_tracked', False))
						if _p_locked or getattr(_pm_mob, 'is_engine_dead', False):
							throttle = 0
						elif throttle != 0:
							# A downed driver and a DAMAGED engine both cost throttle; a destroyed
							# engine is the hard gate above (that is all avatar.py ever knew about).
							_mf = _crew_factor(_pm_mob, 'mobility') * _module_factor(_pm_mob, 'mobility')
							if _mf < 1.0:
								throttle = throttle * _mf
				except Exception: pass
				_veh_velocity[0] = _PHY.longitudinal_step(_pparams, cur_vel, throttle, steer != 0, _slope_p, dt, _veh_airborne[0], 0, _hb_on or _p_locked)
				# --- offhangar slope diagnostic (physics_debug): steepest grade the PLAYER
				# climbs. Resets on flat so each hill reports its own peak. Drive up Drachenpass
				# then the Serene mountain and read the two 'SLOPE grade=' peaks from the log.
				try:
					from _constants import CONFIG_OPTIONS as _CFG_SLP
					if _CFG_SLP.get('physics_debug', False):
						_sp_deg = math.degrees(abs(_slope_p))
						if _sp_deg < 3.0:
							player._offh_climb_max = 0.0
						elif cur_vel > 0.5 and throttle > 0:
							if _sp_deg > getattr(player, '_offh_climb_max', 0.0) + 0.5:
								player._offh_climb_max = _sp_deg
								LOG_DEBUG('SLOPE grade=%.1f deg %s  v=%.1f m/s' % (_sp_deg, 'UP' if _slope_p < 0 else 'DOWN', cur_vel))
				except Exception:
					pass

				# Track scroll: feed movementInfo from current speed so WGVehicleFashion
				# scrolls the track texture (experimental; real game uses WGVehicleFilter2).
				_mvp = loaded_models.get('_fashion_mv')
				if _mvp is not None:
					try:
						# physics.track_scroll: v -/+ omega*halfGauge, clamped strictly
						# below fashion.maxMovement (native scroll wraps to zero at the
						# exact boundary and the tracks freeze at top speed).
						if (getattr(mock_veh, 'health', 1) or 0) <= 0:
							# Dead: hold the tracks still. movementInfo is a speed the native
							# scroll keeps applying, so the last value would roll forever.
							_tls = _trs = 0.0
						else:
							_tls, _trs = _PHY.track_scroll(_pparams, _veh_velocity[0], _veh_turn_velocity[0])
						_mvp.movementInfo = Math.Vector4(0.0, _tls, _trs, 0.0)
					except Exception:
						pass
				# Update engine sounds
				try:
					cur_speed = abs(_veh_velocity[0])
					max_speed = _phys_speedFwd
					# Continuous load blend: discrete 1/2/3 mode values flip rapidly around
					# their thresholds and retrigger the FMOD engine loop (audible resets).
					power_fraction = min(1.0, (cur_speed / max_speed) + (abs(throttle) * 0.3))
					load = 1.0 + (power_fraction * 2.0)
					if _engine_state['snd1']:
						p = _engine_state.get('p_load')
						if p is None:
							p = _engine_state['snd1'].param('load')  # resolve once, not every frame
							_engine_state['p_load'] = p
						if p: p.value = load
					if _engine_state['snd2']:
						p = _engine_state.get('p_speed')
						if p is None:
							p = _engine_state['snd2'].param('speed')
							_engine_state['p_speed'] = p
						if p: p.value = cur_speed / max_speed
				except:
					pass
				# Apply position
				if _veh_velocity[0] != 0.0:
					_p_td = loaded_models.get('td')
					_fell_trees_near(_offh_bspace(), Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2]), veh_yaw[0], _veh_velocity[0], _p_td)
					if _check_horizontal_collision(_offh_bspace(), Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2]), veh_yaw[0], _veh_velocity[0], _p_td, _veh_airborne[0], dt):
						# A WALL only pushes laterally - it must NOT brake a fall. Airborne:
						# keep momentum, just don't advance into it (slide down the face).
						if not _veh_airborne[0]:
							# WALL-SLIDE: instead of dead-sticking (the '2.6 s pinned against
							# a rock until you steer' bug), probe angled directions and grind
							# along the obstacle at reduced speed. Only a true head-on into a
							# flat wall / inside corner (every angle blocked) stops the hull.
							_deflected = False
							for _da in (0.55, -0.55, 1.0, -1.0):   # ~32 then ~57 deg, both sides
								_ty = veh_yaw[0] + _da
								if not _check_horizontal_collision(_offh_bspace(), Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2]), _ty, _veh_velocity[0], _p_td, False, dt):
									# First tick of wall contact loses the into-wall velocity component
									# (~40%); continuing to grind only applies friction. Storing the realized
									# slide speed back into v stops the model booking phantom forward progress
									# while the hull actually moves along _ty (fps-independent).
									if getattr(player, '_offh_grind', 0) <= 0:
										_veh_velocity[0] *= 0.6
									_gs = _veh_velocity[0] * (0.85 ** (dt * 60.0))
									veh_pos[0] += math.sin(_ty) * _gs * dt
									veh_pos[2] += math.cos(_ty) * _gs * dt
									_veh_velocity[0] = _gs
									player._offh_grind = 4   # in-contact latch; survives 1-tick convex-wall separations
									_deflected = True
									break
							if not _deflected:
								# Head-on into a flat wall / inside corner (no angle clears): bleed the
								# speed out over ~0.1 s (fps-independent) instead of a 1-frame hard stop -
								# no velocity discontinuity. The wall already blocks the advance (no
								# veh_pos update here), so the hull holds against it.
								_veh_velocity[0] *= 0.35 ** (dt * 60.0)
								if abs(_veh_velocity[0]) < 0.05:
									_veh_velocity[0] = 0.0
								player._offh_grind = 4
							player._offh_deflected = _deflected
					else:
						player._offh_deflected = False
						player._offh_grind = max(0, getattr(player, '_offh_grind', 0) - 1)
						veh_pos[0] += math.sin(veh_yaw[0]) * _veh_velocity[0] * dt
						veh_pos[2] += math.cos(veh_yaw[0]) * _veh_velocity[0] * dt
				# Tank-vs-tank: velocity-relative impulse (e=0) + Baumgarte push-apart.
				# Never blocks movement -> no deadlock; heavier tank shoves lighter aside.
				try:
					_psvx = math.sin(veh_yaw[0]) * _veh_velocity[0] + (getattr(player, '_push_x', 0.0) or 0.0)
					_psvz = math.cos(veh_yaw[0]) * _veh_velocity[0] + (getattr(player, '_push_z', 0.0) or 0.0)
					_ptr = _tank_resolve(getattr(player, 'playerVehicleID', -1), veh_pos[0], veh_pos[2], veh_yaw[0], loaded_models.get('td'), 1.0 / max(_phys_mass, 1.0), _psvx, _psvz, veh_pos[1])
					# e=0: the FORWARD share of the impulse must hit the real drive
					# speed - a ram stops the hull. Before, it went only into the
					# 0.90-decay push velocity, so the tracks kept feeding into the
					# other tank until the centres crossed and the hull popped out the
					# far side ('glitched through a tank after the cliff drop').
					_fimp = _ptr[2] * math.sin(veh_yaw[0]) + _ptr[3] * math.cos(veh_yaw[0])
					_fabs = 0.0
					if _fimp * _veh_velocity[0] < 0.0:
						_fabs = -_veh_velocity[0] if abs(_fimp) >= abs(_veh_velocity[0]) else _fimp
						_veh_velocity[0] += _fabs
					player._push_x = (getattr(player, '_push_x', 0.0) or 0.0) + _ptr[2] - _fabs * math.sin(veh_yaw[0])
					player._push_z = (getattr(player, '_push_z', 0.0) or 0.0) + _ptr[3] - _fabs * math.cos(veh_yaw[0])
					veh_pos[0] += _ptr[0] + player._push_x * dt
					veh_pos[2] += _ptr[1] + player._push_z * dt
					player._push_x *= 0.90
					player._push_z *= 0.90
				except Exception:
					pass
				
				# --- Hull Rotation: physics.traverse_step (same law as the bots) ---
				turn_dir = steer
				# A destroyed track (or engine) stops the hull turning as well. Gating only the
				# throttle left a tracked tank free to pivot on the spot.
				try:
					_pm_trn = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					if _pm_trn is not None and (getattr(_pm_trn, 'is_tracked', False) or getattr(_pm_trn, 'is_engine_dead', False)):
						turn_dir = 0
						_veh_turn_velocity[0] = 0.0
				except Exception: pass
				_veh_turn_velocity[0] = _PHY.traverse_step(_pparams, _veh_turn_velocity[0], turn_dir, _veh_velocity[0], dt)
				# A damaged (not thrown) ENGINE slows the hull traverse - turning is engine
				# work. A damaged track costs nothing here; a THROWN track blocks the turn
				# outright a few lines above, through is_tracked.
				try:
					_ptf = _module_factor(mock_veh, 'traverse')
					if _ptf < 1.0:
						_veh_turn_velocity[0] = _veh_turn_velocity[0] * _ptf
				except Exception: pass
				
				if _veh_turn_velocity[0] != 0.0:
					veh_yaw[0] += _veh_turn_velocity[0] * dt
					while veh_yaw[0] > math.pi: veh_yaw[0] -= 2*math.pi
					while veh_yaw[0] < -math.pi: veh_yaw[0] += 2*math.pi

				# --- Ground contact: stick to terrain on slopes, fall with gravity off ledges ---
				# Ray starts just above the hull (not +100) so bridges/roofs overhead are ignored
				try:
					# Rest on the HIGHEST ground under the fore-aft track footprint
					# (front/centre/back), not a lone centre ray - couples the vertical
					# follow to the pitch probe so climbs and crests are smooth.
					_hl_sup = 2.5
					try:
						_tdc = loaded_models.get('td')
						if _tdc is not None and hasattr(_tdc, 'hull') and 'hitTester' in _tdc.hull:
							_bbs = _tdc.hull['hitTester'].bbox
							_hl_sup = max(1.5, abs(_bbs[1][2]))
					except Exception:
						pass
					_sup = _terrain_support(_offh_bspace(), veh_pos[0], veh_pos[1], veh_pos[2], veh_yaw[0], _hl_sup)
					_centre_y = _sup[1]     # ground under the hull CENTRE (chassis origin sits here)
					player._y_snap = None   # telemetry: tag which path sets veh_pos.y this tick
					# Rest on the CENTRE ground, not the highest footprint point: the latter
					# lifted the hull by the half-slope rise, so every tank floated. Fall back
					# to the footprint max only when the centre probe missed (centre over a gap).
					ground_y = _centre_y if _centre_y is not None else _sup[0]
					if ground_y is not None:
						snap_gap = max(0.8, min(2.5, abs(_veh_velocity[0]) * dt * 2.0 + 0.6))
						max_climb = max(0.6, abs(_veh_velocity[0]) * dt * 2.5)
						# CoM has left the ground when the CENTRE probe drops away (or finds
						# nothing): THEN the hull tips and falls, even if the tail still
						# overhangs the crest. Landing height is that same centre ground.
						_com_gap = snap_gap if _centre_y is None else (veh_pos[1] - _centre_y)
						_land_y = ground_y if _centre_y is None else _centre_y
						if not _veh_fall_armed[0]:
							# First ground acquisition after spawn: the hull starts at the y=100
							# fallback far above terrain. SNAP straight down instead of a ~100 m
							# ballistic plummet (telemetry reached -46 m/s). Spawn touchdown is
							# free and instant - never a fall.
							player._offh_buried = 0
							veh_pos[1] = _land_y if _land_y is not None else ground_y
							_veh_vert_vel[0] = 0.0
							_veh_airborne[0] = False
							_veh_fall_armed[0] = True
						elif _centre_y is not None and veh_pos[1] < _centre_y and (_centre_y - veh_pos[1]) > max_climb:
							# Buried deeper than any climbable step: a diagonal slip past the
							# wall probes left the hull INSIDE the slope, where it stuck
							# forever. Two consecutive buried ticks = terrain, never a fence
							# (fences fit inside max_climb) -> pop back to the surface.
							player._offh_buried = getattr(player, '_offh_buried', 0) + 1
							if player._offh_buried >= 2 and (_centre_y - veh_pos[1]) > 0.5:
								veh_pos[1] = _centre_y
								player._offh_buried = 0
						elif veh_pos[1] <= ground_y or (_com_gap <= snap_gap and not _veh_airborne[0]):
							player._offh_buried = 0
							# Soft ground-follow: below the surface snaps up hard (never sink,
							# tracks stay planted); above eases down (no teleport) but is capped
							# 0.12 m over ground so the hull never visibly floats on a downhill.
							if veh_pos[1] < ground_y:
								_rise = ground_y - veh_pos[1]
								veh_pos[1] += _rise if _rise <= max_climb else max_climb
							else:
								veh_pos[1] += (ground_y - veh_pos[1]) * min(1.0, dt * 15.0)
								if veh_pos[1] > ground_y + 0.12:
									veh_pos[1] = ground_y + 0.12
							_veh_vert_vel[0] = 0.0
							_veh_airborne[0] = False
							_veh_fall_armed[0] = True
						else:
							# Ledge/cliff: ballistic fall, substepped so a fast drop can't clip/tunnel
							player._offh_buried = 0
							if not _veh_airborne[0]:
								# Launch: leaving a ramp/crest inherits the vertical component
								# of the ground slope (v*sin(-pitch); nose-up pitch is
								# negative). Starting every jump at v_y=0 made ramps feel
								# dead - the hull dropped like a brick the moment the ray
								# lost the ground.
								try:
									# Upward launches only (nose-up pitch, i.e. ramps/crests).
									# Seeding DOWNWARD on steep descents inflated the landing
									# speed and charged phantom fall damage for ordinary
									# downhill driving.
									_lp = getattr(player, '_offh_last_pitch', 0.0) or 0.0
									_veh_vert_vel[0] = _veh_velocity[0] * math.sin(-_lp) if _lp < 0.0 else 0.0
								except Exception:
									_veh_vert_vel[0] = 0.0
								LOG_DEBUG('OfflineBattle: player airborne, %.1fm to ground' % (veh_pos[1] - ground_y))
							_veh_airborne[0] = True
							_fall_n = 1
							if abs(_veh_vert_vel[0] * dt) > 0.5:
								_fall_n = min(8, int(abs(_veh_vert_vel[0] * dt) / 0.5) + 1)
							_fall_sdt = dt / _fall_n
							_fall_i = 0
							while _fall_i < _fall_n:
								_veh_vert_vel[0] -= _phys_gravity * _fall_sdt
								veh_pos[1] += _veh_vert_vel[0] * _fall_sdt
								if _land_y is not None and veh_pos[1] <= _land_y:
									veh_pos[1] = _land_y
									# Fall damage only once armed (first spawn touchdown is
									# free) and only in the running battle - never during the
									# countdown while the spawn drop settles.
									if _veh_fall_armed[0] and getattr(getattr(player, 'arena', None), 'period', 3) == 3:
										_offh_land_impact(_veh_vert_vel[0])
									_veh_fall_armed[0] = True
									_veh_vert_vel[0] = 0.0   # kill vertical only; horizontal momentum kept
									_veh_airborne[0] = False
									break
								_fall_i += 1
					else:
						# No terrain below. Told apart by whether we have EVER been grounded
						# (fall_armed): at SPAWN the space may simply not be streamed in yet -
						# HOLD instead of plummeting off the y=100 fallback (the ~4 s, -46 m/s
						# spawn drop). Once a ray hits, the spawn snap above sets the hull on
						# the ground. A genuine map-edge void AFTER driving is a real free fall.
						if not _veh_fall_armed[0]:
							_veh_vert_vel[0] = 0.0
							_veh_airborne[0] = False
						else:
							if not _veh_airborne[0]:
								LOG_DEBUG('OfflineBattle: player off map-edge, free fall')
							_veh_airborne[0] = True
							_veh_vert_vel[0] -= _phys_gravity * dt
							veh_pos[1] += _veh_vert_vel[0] * dt
				except Exception:
					pass

				# --- Drowning: WG 1:1 (Avatar.updateVehicleDestroyTimer). Three DROWN_WARNING_LEVEL
				# states: SAFE=hide, CAUTION='warning' (standing in water), DANGER='critical' countdown.
				# Whole seconds only - WG's flash floors the value, no sub-seconds. ---
				try:
					player._offh_dchk = getattr(player, '_offh_dchk', 0.0) + dt
					if player._offh_dchk >= 0.3:
						_dcel = min(player._offh_dchk, 0.5)
						player._offh_dchk = 0.0
						_depth = _offh_water_depth(veh_pos[0], veh_pos[1], veh_pos[2])
						# level: 0=SAFE(dry) 1=CAUTION(in water) 2=DANGER(drowning countdown)
						if _depth > 1.6:
							player._offh_drown_t = getattr(player, '_offh_drown_t', 0.0) + _dcel
							_rem = int(round(max(0.0, 10.0 - player._offh_drown_t)))
							_lvl = 2
						elif _depth > 0.5:
							player._offh_drown_t = 0.0
							_rem = 0
							_lvl = 1
						else:
							player._offh_drown_t = 0.0
							_rem = 0
							_lvl = 0
						if (getattr(mock_veh, 'health', 1) or 0) <= 0:
							_lvl = 0
							_rem = 0
						# Submerged: the crew is fighting the water, not the gun. Read by the shoot
						# gate and by the turret traverse below.
						player._offh_drowning = (_lvl == 2)
						# push ONCE per level change: flash then animates the countdown ring itself.
						# re-pushing every second restarts that animation -> stutter (WG pushes on level change only)
						if getattr(player, '_offh_drown_state', None) != _lvl:
							player._offh_drown_state = _lvl
							try:
								from gui import WindowsManager as _dwm
								_dbw = getattr(_dwm.g_windowsManager, 'battleWindow', None)
								if _dbw is not None:
									try:
										import constants as _dcst
										_dcode = _dcst.VEHICLE_MISC_STATUS.VEHICLE_DROWN_WARNING
									except Exception:
										_dcode = 4
									# mirror Avatar.updateVehicleDestroyTimer exactly
									if _lvl == 2:
										try: _dbw.showVehicleTimer(_dcode, _rem, 'critical')
										except TypeError: _dbw.showVehicleTimer(_dcode, _rem)
									elif _lvl == 1:
										try: _dbw.showVehicleTimer(_dcode, 0, 'warning')
										except TypeError: _dbw.showVehicleTimer(_dcode, 0)
									else:
										try: _dbw.hideVehicleTimer(_dcode)
										except TypeError: _dbw.hideVehicleTimer()
							except Exception:
								pass
						# death after 10 s fully submerged
						if _depth > 1.6 and player._offh_drown_t > 10.0 and (getattr(mock_veh, 'health', 1) or 0) > 0:
							# Drowning is not damage: the crew drowns, the hull is untouched. Keep the HP
							# the tank had when it went under for the DISPLAY, while the internal health
							# still goes to 0 - everything else (isAlive, the wipe check, the repair gate)
							# keys off that to treat the tank as dead.
							_hp_at_drown = getattr(mock_veh, 'health', 0) or 0
							mock_veh._hp_display = _hp_at_drown
							mock_veh.health = 0
							mock_veh._drowned = True
							mock_veh.last_killer_id = -1
							# the crew drowns with the tank - every module and every crew member is out
							_offh_knock_out_everything(mock_veh, True)
							# crew_deactivated used to be bound to some living ally, because binding it
							# to the player - who has just drowned - makes __playFirstFromQueue discard
							# it. The instance no longer binds anything implicitly, so send it unbound:
							# a report about the player's OWN crew must not depend on who else is alive.
							_offh_notify('crew_deactivated')
							# reason 5 = drowning attackReasonID; wrapper greys panel + dead marker
							try:
								player.arena.onVehicleKilled(getattr(mock_veh, 'id', player.playerVehicleID), -1, 5)
							except Exception:
								pass
							try:
								if hasattr(player, 'vehicle') and player.vehicle:
									player.vehicle.health = _hp_at_drown
									player.guiSessionProvider.invalidateVehicleState(1, player.playerVehicleID, _hp_at_drown, _hp_at_drown)
							except Exception:
								pass
							LOG_DEBUG('OfflineBattle: player drowned')
				except Exception:
					pass

				# --- Dead-state sync: grey out EVERY destroyed tank in the players panel,
				# no matter which path killed it (player shot, bot, fire, ram, fall) ---
				try:
					_greyed = globals().setdefault('_offh_greyed_ids', set())
					_pl2 = BigWorld.player()
					_arena_v = getattr(getattr(_pl2, 'arena', None), 'vehicles', None)
					# perf: sweep runs 4x/s, not every frame
					_ds = globals().get('g_offh_ds_acc', 0.0) + dt
					globals()['g_offh_ds_acc'] = 0.0 if _ds >= 0.25 else _ds
					_fresh = []
					for _kid, _kv in ((globals().get('G_MOCK_VEHICLES', {}) or {}).items() if _ds >= 0.25 else ()):
						if _kid in _greyed:
							continue
						if _kid == getattr(_pl2, 'playerVehicleID', -1):
							_dead = getattr(_pl2, '_is_dead', False)
						else:
							_dead = (not getattr(_kv, 'isAlive', True)) or ((getattr(_kv, 'health', 1) or 0) <= 0)
						if _dead:
							_greyed.add(_kid)
							_fresh.append(_kid)
							if _arena_v is not None and _kid in _arena_v:
								try:
									_arena_v[_kid]['isAlive'] = False
									_arena_v[_kid]['isAvatarReady'] = False  # else panel keeps it 'alive' (vState=2)
								except Exception: pass
					if _fresh:
						try:
							from gui import WindowsManager
							_bw2 = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
							if _bw2 is not None:
								if hasattr(_bw2, '_Battle__updatePlayers'):
									_bw2._Battle__updatePlayers()
								if getattr(_bw2, 'minimap', None):
									for _dk in _fresh:
										try: _bw2.minimap.notifyVehicleStop(_dk)
										except Exception: pass
						except Exception: pass
				except Exception: pass

				# --- Turret & Gun Mouse Aiming ---
				# Safe default: the aiming code below only assigns 'mat' on its happy
				# path; without this the gun-marker block crashes every frame.
				mat = Math.Matrix(mock_veh.matrix)
				try:
					is_sniper = False
					is_arty = False
					aih = getattr(BigWorld.player(), 'inputHandler', None)
					if aih and getattr(aih, '_AvatarInputHandler__isStarted', False):
						ctrl = getattr(aih, 'ctrl', None)
						if ctrl is not None:
							name = ctrl.__class__.__name__
							if name == 'SniperControlMode': is_sniper = True
							if name == 'StrategicControlMode': is_arty = True
					# SPG strategic (bird's-eye) view support for offline battles:
					# (A) StrategicCamera.enable seeds its pan anchor (__totalMove) from the map
					#     origin when no server position is available, so recentre it on the tank
					#     for a few frames after the strategic mode is entered.
					# (B) the strategic trajectory drawer (the green/red shot line) is driven by
					#     AvatarInputHandler from getDesiredShotPoint, which returns None offline,
					#     so it never updates. Feed it here: R = ground under the camera look-at,
					#     r0/v0 = gun muzzle params from the gun rotator.
					try:
						_aih_c = getattr(BigWorld.player(), 'inputHandler', None)
						_ctrl_c = getattr(_aih_c, 'ctrl', None) if _aih_c is not None else None
						_plr = BigWorld.player()
						_cur = _ctrl_c.__class__.__name__ if _ctrl_c is not None else None
						_last = getattr(_plr, '_offh_last_ctrl', None)
						_plr._offh_last_ctrl = _cur
						if _cur == 'StrategicControlMode':
							_sc2 = getattr(_ctrl_c, 'camera', None)
							if _last != 'StrategicControlMode':
								_plr._offh_arty_seedn = 12
							_sn = getattr(_plr, '_offh_arty_seedn', 0)
							if _sn > 0:
								_plr._offh_arty_seedn = _sn - 1
								try:
									_tm = getattr(_sc2, '_StrategicCamera__totalMove', None)
									if _tm is not None:
										_tm[0] = veh_pos[0]
										_tm[2] = veh_pos[2]
										try: _sc2.update(0.0, 0.0, 0.0)
										except Exception: pass
								except Exception: pass
							_tn = globals().get('_offh_traj_n', 0) + 1
							globals()['_offh_traj_n'] = _tn
							if _tn % 3 == 0:
								try:
									_tm2 = getattr(_sc2, '_StrategicCamera__totalMove', None)
									_tdrw = getattr(_ctrl_c, '_StrategicControlMode__trajectoryDrawer', None)
									if _tm2 is not None and _tdrw is not None:
										_ry = float(veh_pos[1])
										try:
											_rc = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_tm2[0], 1000.0, _tm2[2]), Math.Vector3(_tm2[0], -250.0, _tm2[2]), 3)
											if _rc is not None: _ry = _rc[0][1]
										except Exception: pass
										_R = Math.Vector3(_tm2[0], _ry, _tm2[2])
										_r0, _v0, _g0 = BigWorld.player().gunRotator.getShotParams(_R)
										_tdrw.update(_R, _r0, _v0, 0.1)
								except Exception:
									pass
						else:
							_plr._offh_arty_seedn = 0
					except Exception:
						pass
					# 1. First compute previous exact gun position
					try:
						td = loaded_models.get('td')
						turretOffs = td.hull['turretPositions'][0] + td.chassis['hullPosition']
						gunOffs = td.turret['gunPosition']
					except:
						turretOffs = Math.Vector3(0, 1.5, 0)
						gunOffs = Math.Vector3(0, 0.4, 1.0)

					turretWorldMatrix = Math.Matrix()
					turretWorldMatrix.setRotateY(turret_yaw[0])
					turretWorldMatrix.translation = turretOffs
					turretWorldMatrix.postMultiply(mock_veh.matrix)
					last_true_gun_pos = turretWorldMatrix.applyPoint(gunOffs)

					# 2. Get exact 3D point the crosshair is looking at.
					#
					# CAST IT OURSELVES, in the battle space. The client's own
					# getDesiredShotPoint goes through collideDynamicAndStatic, which raycasts
					# BigWorld.player().spaceID - offline that is the ACCOUNT's space, not the
					# space the battle geometry is mapped into (which is why this file has
					# _offh_bspace() at all). It therefore hits nothing, ever, and falls
					# through to _shootInSkyPoint, which returns a point at the shell's
					# maxDistance along the view ray.
					#
					# That is the 718-720 m aim point in EVERY log line of this investigation,
					# whatever was actually under the reticle: the gun was being aimed at the
					# sky at maximum range. So it elevated far more than the real target
					# needed, the round sailed over anything close, and the marker - which
					# correctly shows where the shell truly lands - sat well above the
					# crosshair. One bug behind "the aiming point is much higher than it
					# should be", "the projectile flies higher", and "crosshair still
					# incorrect when I aim at terrain".
					shot_point = None
					cam_pos = None
					cam_dir = None
					try:
						# Through the screen point the RETICLE IS DRAWN AT - the client's own
						# _getDesiredShotPoint casts through exactly aim.offset(), and
						# ArcadeControlMode even seeds its camera with it, so the camera's bare
						# forward axis is a different pixel from the one being aimed through.
						from AvatarInputHandler import cameras as _offh_cams
						_aim_o = getattr(g_offline_aih, 'aim', None)
						_off = _aim_o.offset() if _aim_o is not None else (0.0, 0.0)
						_r_dir, _r_start = _offh_cams.getWorldRayAndPoint(_off[0], _off[1])
						cam_dir = Math.Vector3(_r_dir)
						cam_dir.normalise()
						cam_pos = Math.Vector3(_r_start)
					except Exception:
						cam_pos = None
					if cam_pos is None:
						cam_mat = Math.Matrix(BigWorld.camera().matrix)
						cam_pos = cam_mat.translation
						cam_dir = cam_mat.applyToAxis(2)
						cam_dir.normalise()
					try:
						_aim_end = cam_pos + cam_dir.scale(1000.0)
						_col_aim = BigWorld.wg_collideSegment(_offh_bspace(), cam_pos, _aim_end, 128)
						_aim_d = (_col_aim[0] - cam_pos).length if _col_aim is not None else 1000.0
						# Tanks are not in the engine's collision scene offline, so a reticle on
						# an enemy would otherwise aim at the ground BEHIND it and the gun would
						# elevate for the wrong range. Test the mocks along the same ray.
						_aim_veh = None
						for _ae, _am in mock_vehicles.iteritems():
							if _ae == getattr(player, 'playerVehicleID', -1):
								continue
							if not getattr(_am, 'isAlive', False) or (getattr(_am, 'health', 0) or 0) <= 0:
								continue
							_ac = _am.collideSegment(cam_pos, _aim_end)
							if _ac is not None and _ac[0] < _aim_d:
								_aim_d = _ac[0]
								_aim_veh = _am
						if _aim_veh is not None:
							shot_point = cam_pos + cam_dir.scale(_aim_d)
							_gun_state['_aim_pt_src'] = 'reticleRay(tank)'
						elif _col_aim is not None:
							shot_point = _col_aim[0]
							_gun_state['_aim_pt_src'] = 'reticleRay'
					except Exception:
						shot_point = None
					if shot_point is None:
						try:
							if aih and getattr(aih, '_AvatarInputHandler__isStarted', False):
								shot_point = aih.getDesiredShotPoint()
								_gun_state['_aim_pt_src'] = 'aih'
						except Exception:
							pass
					# (POS-CHECK debug removed: ran every frame and referenced an undefined var)

					if shot_point is None:
						_gun_state['_aim_pt_src'] = 'rayEnd'
						end_pos = cam_pos + cam_dir.scale(1000.0)
						col = BigWorld.wg_collideSegment(_offh_bspace(), cam_pos, end_pos, 128)
						if col is not None:
							shot_point = col[0]
						else:
							shot_point = end_pos
							# Strategic/SPG looks straight down: if the engine ray misses for a
							# frame, intersect the view ray with the last aim height instead of
							# letting the reticle jump to the ray end (ported arty-view fix).
							if is_arty and abs(cam_dir.y) > 0.0001:
								try:
									_ply = float(_gun_state.get('last_aim_y', 0.0) or 0.0)
									_pt = (_ply - cam_pos.y) / cam_dir.y
									if 0.0 < _pt <= 2000.0:
										shot_point = cam_pos + cam_dir.scale(_pt)
								except Exception:
									pass
					try:
						_gun_state['last_aim_y'] = float(shot_point.y)
					except Exception:
						pass

				# 3. Calculate target yaw and pitch
					# Vector from mathematical gun to the target					
					dx = shot_point.x - last_true_gun_pos.x
					dy = shot_point.y - last_true_gun_pos.y
					dz = shot_point.z - last_true_gun_pos.z
					dist = math.sqrt(dx*dx + dz*dz)
					
					try:
						if _gun_state.get('rmb_down', False) and not getattr(player, '_autoaim_target', None) and 'locked_local_yaw' in _gun_state:
							local_target_yaw = _gun_state['locked_local_yaw']
							target_pitch = _gun_state['locked_local_pitch']
							target_yaw = veh_yaw[0] + local_target_yaw
						else:
							_aat = getattr(player, '_autoaim_target', None)
							if _aat is not None and not getattr(_aat, '_spot_visible', True):
								# Target lost from view -> release the lock (like online):
								# the barrel silently tracking an invisible tank both
								# reveals it and looks broken.
								player._autoaim_target = None
								_aat = None
							if _aat is not None and getattr(_aat, 'health', 0) > 0:
								t_pos = Math.Vector3(_aat.position)
								t_pos.y += 1.0
								shot_point = t_pos
							# wg_getShotAngles elevates the barrel to drop the shell onto
							# shot_point, and it reads td.shot - which is gun['shots'][
							# activeGunShotIndex]. Nothing kept that index on the battle
							# descriptor in step with the shell actually chambered (only the
							# pen-indicator block set it, and only in arcade/sniper), so a
							# tank firing anything other than its FIRST shell type was aimed
							# with the wrong muzzle velocity and gravity. Sync it here, where
							# every mode passes.
							try:
								_si_aim = _gun_state.get('shot_index', 0)
								if getattr(td, 'activeGunShotIndex', 0) != _si_aim:
									td.activeGunShotIndex = _si_aim
							except Exception:
								pass
							from projectile_trajectory import getShotAngles
							mat = BigWorld.player().getOwnVehicleMatrix()
							tYaw, gPitch = getShotAngles(td, mat, (turret_yaw[0], gun_pitch[0]), shot_point)
							local_target_yaw = tYaw
							target_pitch = gPitch
							target_yaw = veh_yaw[0] + local_target_yaw
							_gun_state['_aim_src'] = 'wg_getShotAngles'
					except Exception as e:
						# Fallback k jednoduche trigonometrii (nepresne). NOTE for direct fire
						# this fallback has NO gravity compensation at all - it aims the barrel
						# straight at the target, so every shell lands short by its full drop.
						_gun_state['_aim_src'] = 'FALLBACK(%s)' % str(e)
						target_yaw = math.atan2(dx, dz)
						local_target_yaw = target_yaw - veh_yaw[0]
						
						if is_arty:
							try:
								shots = td.gun['shots'] if isinstance(td.gun, dict) else getattr(td.gun, 'shots')
								shot = shots[0]
								v = shot['speed'] if isinstance(shot, dict) else getattr(shot, 'speed')
								g = shot['gravity'] if isinstance(shot, dict) else getattr(shot, 'gravity', 9.81)
								g = abs(g)
								if g < 0.1: g = 9.81
								root = v**4 - g * (g * dist**2 + 2 * dy * v**2)
								if root > 0:
									target_pitch = -math.atan((v**2 - math.sqrt(root)) / (g * dist))
								else:
									# No ballistic solution: hold the gun at ITS OWN maximum elevation rather
									# than a hardcoded 45 degrees, which was the sky for anything else.
									target_pitch = _gun_min_pitch
							except Exception as ex:
								target_pitch = math.atan2(-dy, dist) # direct fire fallback
						else:
							target_pitch = math.atan2(-dy, dist)
					
					# Normalize angleses
					while local_target_yaw > math.pi: local_target_yaw -= 2*math.pi
					while local_target_yaw < -math.pi: local_target_yaw += 2*math.pi
					while turret_yaw[0] > math.pi: turret_yaw[0] -= 2*math.pi
					while turret_yaw[0] < -math.pi: turret_yaw[0] += 2*math.pi
					
					_gun_state['auto_steer'] = 0
					if not BigWorld.isKeyDown(Keys.KEY_RIGHTMOUSE):
						if _gun_min_yaw is not None and _gun_max_yaw is not None:
							# Check if aiming outside bounds
							if local_target_yaw < _gun_min_yaw - 0.02: _gun_state['auto_steer'] = -1
							elif local_target_yaw > _gun_max_yaw + 0.02: _gun_state['auto_steer'] = 1
					
					# Clamp to max traverse limits (for SPGs and TDs)
					local_target_yaw = max(_gun_min_yaw, min(_gun_max_yaw, local_target_yaw))
					
					diff_yaw = local_target_yaw - turret_yaw[0]
					if diff_yaw > math.pi: diff_yaw -= 2*math.pi
					if diff_yaw < -math.pi: diff_yaw += 2*math.pi
					
					# (turret_speed for the dispersion model is measured from the rotation
					# the turret ACTUALLY performs, below, over a 0.1 s window - see there.)

					# Stashed for the per-shot SHOT DIAG line: what the gun was ASKED to do
					# and where the aim point was. If the elevation the shell actually leaves
					# at disagrees with the one getShotAngles asked for, the round lands short
					# and no amount of correct trajectory maths will put it on the target.
					_gun_state['_aim_pt'] = shot_point
					_gun_state['_aim_req_pitch'] = target_pitch
					# The hull matrix getShotAngles was handed. Its pitch is the difference
					# between a hull-relative gun pitch and the elevation the shell actually
					# leaves at, so it is the one number that separates "the gun did not go
					# where it was told" from "getShotAngles never saw the hull attitude".
					try:
						_gun_state['_aim_hull_pitch'] = Math.Matrix(mat).pitch
					except Exception:
						_gun_state['_aim_hull_pitch'] = 0.0

					# perf: outline scan ~8x/s (was every frame over all bots)
					player._outl_acc = (getattr(player, '_outl_acc', 9.0) or 9.0) + dt
					try:
						gun_dir = shot_point - last_true_gun_pos
						if player._outl_acc >= 0.12 and gun_dir.length > 0.001:
							gun_dir.normalise()
							_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
							
							import debug_utils
							if not hasattr(player, '_debug_dump_done_5'):
								player._debug_dump_done_5 = True
								debug_utils.LOG_DEBUG('AIH_TICK DUMP keys:', _mock_vehicles.keys())
								for _k, _v in _mock_vehicles.items():
									debug_utils.LOG_DEBUG(' - Veh', _k, getattr(_v, '_bot_team', 'N/A'))
							
							player._outl_acc = 0.0
							# Periodic roll call - the first 10 scans of a battle, logged even when NOTHING
							# passes, so a short session still says why. Inferring from silence cost a round.
							_outl_n = globals().get('_offh_outl_diag_n', 0)
							_outl_dbg = [] if _outl_n < 10 else None
							_outl_skip = [0, 0, 0]   # self, dead, unspotted
							closest_bot = None
							min_dist = 9999.0
							for eid, m_veh in _mock_vehicles.iteritems():
								if eid == getattr(player, 'playerVehicleID', -1):
									_outl_skip[0] += 1; continue
								# `or 0` MATTERS. The mocks define __getattr__ returning None, so
								# getattr(mock, 'health', 0) hands back None rather than the default
								# whenever the attribute is not set - and in python 2 `None <= 0` is
								# True, so the tank was silently dropped from the picker. Every other
								# health test in this file already uses this idiom; only the outline
								# scan had the bare form, which is why allies never got a silhouette.
								if (getattr(m_veh, 'health', 0) or 0) <= 0:
									_outl_skip[1] += 1; continue
								# Unspotted (hidden) bots must not be outline/autoaim targets
								if not getattr(m_veh, '_spot_visible', True):
									_outl_skip[2] += 1; continue
								if _outl_dbg is not None and len(_outl_dbg) < 8:
									_outl_dbg.append('%s(team=%s,hp=%s)' % (eid, getattr(m_veh, '_bot_team', '?'), getattr(m_veh, 'health', '?')))
								b_pos = Math.Vector3(m_veh.position)
								b_vec = b_pos - last_true_gun_pos
								proj_len = b_vec.dot(gun_dir)
								# (per-frame log removed: file I/O for every bot every frame)
								if proj_len > 0:
									proj_pt = last_true_gun_pos + gun_dir.scale(proj_len)
									dist_to_ray = (b_pos - proj_pt).length
									# (per-frame log removed)
									if dist_to_ray < 2.5:
										if proj_len < min_dist:
											min_dist = proj_len
											closest_bot = m_veh
							if _outl_dbg is not None:
								globals()['_offh_outl_diag_n'] = _outl_n + 1
								LOG_DEBUG('OUTLINE scan: mocks=%d skipped(self=%d dead=%d unspotted=%d) cands=[%s] picked=%s'
									% (len(_mock_vehicles), _outl_skip[0], _outl_skip[1], _outl_skip[2],
									', '.join(_outl_dbg), getattr(closest_bot, '_bot_team', None)))
							# No outline through terrain/buildings: the picker only projects
							# onto the gun ray, so tanks BEHIND rocks/walls got the border
							# (and could be autoaim-locked). Two sample points (mid-hull +
							# turret top): a hull-down tank whose centre ray grazes the
							# crest still gets its silhouette off the turret ray - only a
							# tank with BOTH points behind statics loses the outline.
							if closest_bot is not None:
								try:
									_ob_base = closest_bot.position
									_blocked = True
									_self_id = getattr(player, 'playerVehicleID', -1)
									for _oby in (1.5, 2.2):
										_ob_pos = Math.Vector3(_ob_base.x, _ob_base.y + _oby, _ob_base.z)
										_ob_len = (_ob_pos - last_true_gun_pos).length
										_oc = BigWorld.wg_collideSegment(_offh_bspace(), last_true_gun_pos, _ob_pos, 128)
										if _oc is not None and ((_oc[0] - last_true_gun_pos).length + 2.0) < _ob_len:
											continue   # this sample is behind terrain or a building
										# ...and behind another TANK? wg_collideSegment sees STATIC
										# geometry only - the mocks are not in the engine's collision
										# scene - so the ray sailed straight through an ally parked in
										# front and the enemy behind him kept his silhouette.
										_veh_block = False
										for _oe2, _om2 in _mock_vehicles.iteritems():
											if _om2 is closest_bot or _oe2 == _self_id:
												continue
											if (getattr(_om2, 'health', 0) or 0) <= 0:
												continue
											try:
												if (_om2.position - last_true_gun_pos).dot(gun_dir) >= _ob_len:
													continue   # not between us and the target
											except Exception:
												pass
											_oc2 = _om2.collideSegment(last_true_gun_pos, _ob_pos)
											if _oc2 is not None and (_oc2[0] + 2.0) < _ob_len:
												_veh_block = True
												if not getattr(player, '_dbg_outl_block', False):
													player._dbg_outl_block = True
													LOG_DEBUG('OUTLINE blocked by vehicle %s at %.1f m (target at %.1f m)'
														% (_oe2, _oc2[0], _ob_len))
												break
										if not _veh_block:
											_blocked = False
											break
									if _blocked:
										closest_bot = None
								except Exception:
									pass
							prev_bot = getattr(player, '_outlined_bot', None)
							if prev_bot and prev_bot != closest_bot:
								try:
									if hasattr(prev_bot, 'bw_entity') and prev_bot.bw_entity:
										BigWorld.wgDelEdgeDetectEntity(prev_bot.bw_entity)
								except Exception as e:
									pass
								player._outlined_bot = None
							if closest_bot and prev_bot != closest_bot:
								_myteam = getattr(player, '_offhangar_team', 1)
								_histeam = getattr(closest_bot, '_bot_team', 2)
								color = 2 if _histeam == _myteam else 1
								# Re-push the palette HERE, not just once at battle start. BigWorld
								# holds a single (self, enemy, friend) triple and the battle-start
								# push happens around a space clear/remap, so index 2 could be gone
								# by the time anything asks for a friendly outline. The call is
								# trivial and idempotent, so ordering stops mattering.
								_offh_push_edge_colors()
								try:
									if hasattr(closest_bot, 'bw_entity') and closest_bot.bw_entity:
										BigWorld.wgAddEdgeDetectEntity(closest_bot.bw_entity, color)
										LOG_DEBUG('OUTLINE applied: id=%s colour=%d (his team=%s, mine=%s)' % (
											closest_bot.bw_entity.id, color, _histeam, _myteam))
									else:
										LOG_DEBUG('REAL_RAYCAST bot has no bw_entity!')
								except Exception as e:
									LOG_DEBUG('Outline dummy err:', str(e))
								player._outlined_bot = closest_bot
					except Exception as e:
						import debug_utils
						debug_utils.LOG_DEBUG('Outline error:', str(e))
					
					# Countdown: turret + gun frozen until the prebattle timer hits 0 (period 3)
					if getattr(getattr(BigWorld.player(), 'arena', None), 'period', 3) < 3:
						local_target_yaw = turret_yaw[0]; diff_yaw = 0.0; target_pitch = gun_pitch[0]
					_t_step = _turret_rot_speed * dt  # rad this frame (framerate independent)
					_pm_tr = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					_turret_locked = _pm_tr is not None and getattr(_pm_tr, 'is_turret_locked', False)
					# A DAMAGED turret ring still traverses, only slowly. Destroyed is the freeze
					# below (_turret_locked), which is the state the real client knew about.
					try:
						_tsf = _module_factor(_pm_tr, 'turret_speed')
						if _tsf < 1.0:
							_t_step = _t_step * _tsf
					except Exception: pass
					if getattr(player, '_offh_drowning', False) or getattr(player, '_is_dead', False) or _turret_locked:
						# Frozen while submerged AND once dead. The damage-panel tank indicator is
						# bound to this turret matrix, so a dead tank kept turning its turret there.
						_t_step = 0.0
						diff_yaw = 0.0
					_ty_before = turret_yaw[0]
					if abs(diff_yaw) < _t_step:
						turret_yaw[0] = local_target_yaw
					else:
						turret_yaw[0] += _t_step * (1 if diff_yaw > 0 else -1)

					# Turret speed for the dispersion model, measured the way the real
					# VehicleGunRotator measures it: |actual yaw change| over the rotator's
					# 0.1 s tick, NOT the remaining angle to target over one frame. The old
					# reading was wrong twice over - it used the distance still to travel
					# (so a locked or limit-clamped turret still reported motion), and it
					# divided by a 16 ms frame, so a single mouse flick larger than
					# rotationSpeed*dt (well under a degree) pinned it at max traverse for
					# that frame. With turretRotation factors stored per rad/s, max traverse
					# is a ~6x circle: that is why a flick of the camera used to blow the
					# reticle wide open.
					_ty_d = turret_yaw[0] - _ty_before
					if _ty_d > math.pi: _ty_d -= 2 * math.pi
					elif _ty_d < -math.pi: _ty_d += 2 * math.pi
					_gun_state['_turr_acc'] = _gun_state.get('_turr_acc', 0.0) + abs(_ty_d)
					_gun_state['_turr_t'] = _gun_state.get('_turr_t', 0.0) + dt
					if _gun_state['_turr_t'] >= 0.1:
						_gun_state['turret_speed'] = min(_gun_state['_turr_acc'] / _gun_state['_turr_t'], _turret_rot_speed)
						_gun_state['_turr_acc'] = 0.0
						_gun_state['_turr_t'] = 0.0

					# Update pitch
					# Yaw-dependent pitch limits (extraPitchLimits tanks have e.g. less
					# depression over the engine deck), same as the real VehicleGunRotator
					try:
						if _gun_pitch_desc is not None:
							from gun_rotation_shared import calcPitchLimitsFromDesc as _cpl
							_lim_now = _cpl(turret_yaw[0], _gun_pitch_desc)
							target_pitch = max(_lim_now[0], min(_lim_now[1], target_pitch))
						else:
							target_pitch = max(_gun_min_pitch, min(_gun_max_pitch, target_pitch))
					except Exception:
						target_pitch = max(_gun_min_pitch, min(_gun_max_pitch, target_pitch))
					
					diff_pitch = target_pitch - gun_pitch[0]
					_p_step = _gun_pitch_speed * dt  # vertical aim speed from gun descriptor (rad/s)
					if abs(diff_pitch) < _p_step:
						gun_pitch[0] = target_pitch
					else:
						gun_pitch[0] += _p_step * (1 if diff_pitch > 0 else -1)
					# Post-slew, post-clamp: what the barrel is REALLY set to this frame.
					_gun_state['_aim_cur_pitch'] = gun_pitch[0]

					player = BigWorld.player()
					# Force the mod model funcs (once): the native ProjectileMover draws shot
					# tracers via player.addModel/delModel; route them through _add_model so the
					# shell models land in the BATTLE space. An account-native addModel targets
					# the empty read-only space and left tracers invisible (the old not-hasattr
					# guard never replaced it).
					# NOTE: do NOT install addModel/delModel by assignment. PlayerAccount is a real
					# BigWorld.Entity and its method slots are READ-ONLY - the assignment raises
					# "Sorry, that method attribute in PlayerAccount is read-only", so this never
					# took effect and every shell model went to the account's own chunk instead of
					# the battle world. Served from Account.__getattribute__ in mod_offhangar.py.
					# (delModel is served the same way, see _offh_player_del_model.)

					# Mock appearance for SniperCamera to find HP_gunJoint
					if 'gun_node_matrix' not in loaded_models:
						loaded_models['gun_node_matrix'] = Math.Matrix()
					if not hasattr(mock_veh, 'appearance'):
						class FakeAppearance(object):
							def __init__(self):
								class FakeCompound(object):
									def node(self, name):
										if name == 'HP_gunJoint': return loaded_models['gun_node_matrix']
										if name == 'HP_turretJoint': return loaded_models.get('hull').node(name) if loaded_models.get('hull') else None
										return mock_veh.model.node(name)
									@property
									def position(self): return mock_veh.position
									@property
									def matrix(self): return mock_veh.matrix
								self.compoundModel = FakeCompound()
								self.modelsDesc = {'gun': {'model': loaded_models.get('gun')}}
							def changeVisibility(self, modelName, modelVisible, attachmentsVisible):
								is_sniper = not modelVisible
								c_mdl = loaded_models.get('chassis')
								h_mdl = loaded_models.get('hull')
								t_mdl = loaded_models.get('turret')
								g_mdl = loaded_models.get('gun')
								if hasattr(c_mdl, 'visible'): c_mdl.visible = not is_sniper
								if hasattr(h_mdl, 'visible'): h_mdl.visible = not is_sniper
								if hasattr(t_mdl, 'visible'): t_mdl.visible = not is_sniper
								if hasattr(g_mdl, 'visible'): g_mdl.visible = not is_sniper
							def hideIfExistFor(self, vehicle):
								pass
						mock_veh.appearance = FakeAppearance()
					
					# Debug log every 50 ticks (1 sec)
					_tick_counter[0] += 1
					if _tick_counter[0] % 50 == 0:
						try:
							cur_cam = Math.Matrix(BigWorld.camera().matrix)
							c_ptc = -cur_cam.pitch
						except:
							c_ptc = 0.0
						LOG_DEBUG('OfflineBattle.aim: cam_yaw=%.2f, veh_yaw=%.2f, loc_tgt=%.2f, tur_yaw=%.2f, cam_ptc=%.2f, gun_ptc=%.2f' % (
							target_yaw, veh_yaw[0], local_target_yaw, turret_yaw[0], c_ptc, gun_pitch[0]))
						
				except Exception as e:
					LOG_DEBUG('OfflineBattle.aim error:', str(e))


				# --- Update mock vehicle and camera matrix ---
				mock_veh.position = Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2])
				mock_veh.yaw   = veh_yaw[0]
				
				# DEBUG CHASSIS KEYS
				try:
					if getattr(mock_veh, '_dbg_keys_logged', None) is None:
						_td_dbg = loaded_models.get('td')
						if _td_dbg and hasattr(_td_dbg, 'chassis'):
							try: LOG_DEBUG('CHASSIS BBOX:', _td_dbg.chassis['hitTester'].bbox)
							except: pass
						import AreaDestructibles, inspect, constants
						if hasattr(AreaDestructibles, 'g_destructiblesManager'):
							if getattr(AreaDestructibles.g_destructiblesManager, 'getSpaceID', lambda: -1)() != _offh_bspace():
								AreaDestructibles.g_destructiblesManager.startSpace(_offh_bspace())
							try: LOG_DEBUG('DESTRUCTIBLE_MATKIND MIN/MAX:', constants.DESTRUCTIBLE_MATKIND.MIN, constants.DESTRUCTIBLE_MATKIND.MAX)
							except: pass
							try: LOG_DEBUG('BW collide doc:', BigWorld.collide.__doc__)
							except: pass
							try:
								# Log DestructiblesController methods!
								import AreaDestructibles
								if getattr(AreaDestructibles.g_destructiblesManager, 'getSpaceID', lambda: -1)() != _offh_bspace():
									AreaDestructibles.g_destructiblesManager.startSpace(_offh_bspace())
								chunkID = AreaDestructibles.chunkIDFromPosition(BigWorld.player().position)
								ctrl = AreaDestructibles.g_destructiblesManager.getController(chunkID)
								if ctrl:
									LOG_DEBUG('DestructiblesController dir:', dir(ctrl))
								else:
									LOG_DEBUG('DestructiblesController ctrl is NONE')
							except Exception as e:
								LOG_DEBUG('DestructiblesController EXCEPTION:', str(e))
							try: LOG_DEBUG('encodeDestructibleModule argspec:', inspect.getargspec(AreaDestructibles.encodeDestructibleModule))
							except: pass
							try: LOG_DEBUG('encodeFallenTree argspec:', inspect.getargspec(AreaDestructibles.encodeFallenTree))
							except: pass
							try: LOG_DEBUG('encodeFallenColumn argspec:', inspect.getargspec(AreaDestructibles.encodeFallenColumn))
							except: pass
							try: LOG_DEBUG('wg_getMatInfoNearPoint doc:', BigWorld.wg_getMatInfoNearPoint.__doc__)
							except: pass
							try: LOG_DEBUG('onChunkLoad argspec:', inspect.getargspec(AreaDestructibles.g_destructiblesManager.onChunkLoad))
							except: pass
						mock_veh._dbg_keys_logged = True
				except: pass
				
				# Vypočítat náklon tanku hráče podle terénu
				_p_ypr = _get_terrain_ypr(_offh_bspace(), mock_veh.position, veh_yaw[0])
				# --- Slope slide: physics.slope_slide_speed (WG track-cohesion hold). The
				# hull holds on any drivable hill; past the grip limit it slips down the
				# fall line. Only the CROSS-heading component is applied here - the along-
				# heading part is already in longitudinal_step (engine vs slope gravity),
				# so climbing/stalling and lateral slip never double-count. ---
				_pss = getattr(player, '_slide_spd', 0.0) or 0.0
				if _veh_airborne[0]:
					_pss = 0.0   # no fresh ground slide while flying (carried drift below)
				else:
					_pss = _PHY.slope_slide_speed(_pss, _p_ypr[5], dt)
				player._slide_spd = _pss
				# Physics telemetry (~5 Hz) when config physics_debug is on: one CSV row
				# per sample to offhangar_user/physics_telemetry.csv for tuning vs original.
				if _offh_phys_debug[0]:
					player._offh_tel_acc = (getattr(player, '_offh_tel_acc', 0.0) or 0.0) + dt
					if player._offh_tel_acc >= 0.2:
						player._offh_tel_acc = 0.0
						try:
							import gui.mods.offhangar.physics_monitor as _offh_mon
							# observable extras the force numbers miss:
							_now = BigWorld.time()
							_pel = _now - (getattr(player, '_tel_pt', _now) or _now)
							_gkm = 0.0; _dyv = 0.0
							if _pel > 0.01:
								_ddx = veh_pos[0] - (getattr(player, '_tel_px', veh_pos[0]) or veh_pos[0])
								_ddz = veh_pos[2] - (getattr(player, '_tel_pz', veh_pos[2]) or veh_pos[2])
								_gkm = (math.sqrt(_ddx*_ddx + _ddz*_ddz) / _pel) * 3.6
								_dyv = veh_pos[1] - (getattr(player, '_tel_py', veh_pos[1]) or veh_pos[1])
							player._tel_px = veh_pos[0]; player._tel_pz = veh_pos[2]; player._tel_py = veh_pos[1]; player._tel_pt = _now
							_offh_mon.log(_PHY.snapshot(_pparams, _veh_velocity[0], _veh_turn_velocity[0], throttle, _slope_p, _veh_airborne[0], _pss, 'player', ground_kmh=_gkm, pitch_deg=math.degrees(getattr(mock_veh, 'pitch', 0.0) or 0.0), roll_deg=math.degrees(getattr(mock_veh, 'roll', 0.0) or 0.0), vert_ms=_veh_vert_vel[0], deflect=getattr(player, '_offh_deflected', False), dy=_dyv, slide_slope=_p_ypr[5], dy_tick=(getattr(player, '_dy_tick_max', 0.0) or 0.0), y_src=getattr(player, '_dy_tick_src', None), terr_spread=(_p_ypr[6] if len(_p_ypr) > 6 else None)), _now)
							player._dy_tick_max = 0.0
						except Exception:
							pass
				# fall line projected onto the cross-heading axis (perp to hull yaw)
				_cross_x = math.cos(veh_yaw[0]); _cross_z = -math.sin(veh_yaw[0])
				_slide_dot = _p_ypr[3] * _cross_x + _p_ypr[4] * _cross_z
				_slide_dx = _cross_x * _slide_dot
				_slide_dz = _cross_z * _slide_dot
				try:
					player._slide_dbg_t = getattr(player, '_slide_dbg_t', 0.0) + dt
					if _p_ypr[5] > 0.35 and player._slide_dbg_t > 1.0:
						player._slide_dbg_t = 0.0
						LOG_DEBUG('SLIDE dbg slope=%.2f deg=%.0f pss=%.2f air=%s vvel=%.2f' % (_p_ypr[5], math.degrees(math.atan(_p_ypr[5])), _pss, _veh_airborne[0], _veh_velocity[0]))
				except: pass
				if _veh_airborne[0]:
					# Carry the lateral drift frozen at take-off through the fall: no ground
					# contact = no friction, so sideways momentum is conserved (light air
					# drag). Longitudinal v and the vertical fall are integrated elsewhere.
					_alx = getattr(player, '_air_lat_vx', 0.0) or 0.0
					_alz = getattr(player, '_air_lat_vz', 0.0) or 0.0
					if abs(_alx) > 1e-04 or abs(_alz) > 1e-04:
						veh_pos[0] += _alx * dt
						veh_pos[2] += _alz * dt
						player._air_lat_vx = _alx * 0.995
						player._air_lat_vz = _alz * 0.995
						mock_veh.position = Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2])
				else:
					# Grounded: freeze the CURRENT world lateral velocity so a take-off next
					# frame conserves it (zero when not sliding).
					player._air_lat_vx = _slide_dx * _pss
					player._air_lat_vz = _slide_dz * _pss
				if not _veh_airborne[0] and _pss > 0.01 and (abs(_slide_dx) > 1e-04 or abs(_slide_dz) > 1e-04):
					_slp_x = veh_pos[0] + _slide_dx * _pss * dt
					_slp_z = veh_pos[2] + _slide_dz * _pss * dt
					try:
						_slp_c = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_slp_x, veh_pos[1] + 8.0, _slp_z), Math.Vector3(_slp_x, veh_pos[1] - 30.0, _slp_z), 128)
					except Exception:
						_slp_c = None
					if _slp_c is not None and (veh_pos[1] - _slp_c[0].y) < 4.0:
						veh_pos[0] = _slp_x
						veh_pos[2] = _slp_z
						_sdy = _slp_c[0].y - veh_pos[1]
						if _sdy > 0.35:
							_sdy = 0.35
						elif _sdy < -0.35:
							_sdy = -0.35   # ease off/onto an edge under the slide; don't teleport the
							               # hull (a sharp spot below the slid position was a 3.5 m pop).
						veh_pos[1] += _sdy
						player._y_snap = 'slide'
						# Anti-penetration: lift so the uphill hull edge clears the rising bank
						try:
							_up_x = _slp_x - _p_ypr[3] * 3.0
							_up_z = _slp_z - _p_ypr[4] * 3.0
							_upc = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_up_x, veh_pos[1] + 8.0, _up_z), Math.Vector3(_up_x, veh_pos[1] - 30.0, _up_z), 128)
							if _upc is not None:
								_pexp = veh_pos[1] + 3.0 * _p_ypr[5]
								# Only lift for a GENUINE rising bank/step: the uphill ground must sit a
								# clear margin ABOVE the linear slope. The gentle concave base of a hill
								# sits only just above it - lifting the whole hull there made the tank
								# ride high driving onto a slope. Lift only the excess over the margin,
								# small cap, sharing the 0.35 budget with the slide-snap (_sdy).
								if _upc[0].y > _pexp + 0.30:
									_lift = _upc[0].y - _pexp - 0.30
									_lift_cap = 0.20 - (_sdy if _sdy > 0.0 else 0.0)
									if _lift_cap < 0.0:
										_lift_cap = 0.0
									if _lift > _lift_cap:
										_lift = _lift_cap
									veh_pos[1] += _lift
									player._y_snap = 'antipen'
						except Exception:
							pass
						_veh_vert_vel[0] = 0.0
						_veh_airborne[0] = False
						mock_veh.position = Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2])
				# Smooth pitch/roll so bumps and landings don't snap the hull instantly
				_pr_blend = min(1.0, dt * 8.0)
				_pr_p0 = getattr(mock_veh, 'pitch', 0.0)
				_pr_r0 = getattr(mock_veh, 'roll', 0.0)
				_p_ypr = (_p_ypr[0], _pr_p0 + (_p_ypr[1] - _pr_p0) * _pr_blend, _pr_r0 + (_p_ypr[2] - _pr_r0) * _pr_blend)
				mock_veh.pitch = _p_ypr[1]
				mock_veh.roll = _p_ypr[2]
				# edge-pop telemetry: track the largest single physics-tick height jump
				# across the 5 Hz window (the 0.2 s dy smears sub-sample pops at edges).
				if _offh_phys_debug[0]:
					_cy = veh_pos[1]
					_tdy = _cy - (getattr(player, '_prev_ty', _cy) or _cy)
					player._prev_ty = _cy
					if abs(_tdy) > abs(getattr(player, '_dy_tick_max', 0.0) or 0.0):
						player._dy_tick_max = _tdy
						player._dy_tick_src = getattr(player, '_y_snap', None) or ('air' if _veh_airborne[0] else 'follow')
				
				# Update base matrix IN PLACE so AvatarInputHandler doesn't lose the reference
				mock_veh.matrix.setRotateYPR(_p_ypr)
				mock_veh.matrix.translation = mock_veh.position
				
				if hasattr(mock_veh, 'filter'):
					mock_veh.filter.position = mock_veh.position
					mock_veh.filter.yaw = veh_yaw[0]
					
				# Update camera matrix (needs both translation AND yaw for SniperCamera offsets to work)
				# (Arcade camera strips yaw using WGTranslationOnlyMP later)
				new_m = Math.Matrix()
				new_m.setRotateYPR(_p_ypr)
				new_m.translation = mock_veh.position
				veh_matrix.a = new_m

				# Update chassis matrix (position + yaw) - Servo drives the model
				# Always update the chassis matrix, INCLUDING sniper mode. Sniper
				# hiding is done via model.visible=False nowadays (not the old
				# push-underground trick this skip was written for), and freezing
				# the matrix left the chassis/hull/gun models - and everything
				# attached to them: engine sound, gun shot sound, muzzle flash -
				# stuck at the position where the player scoped in, so audio and
				# shot effects played from the wrong place after driving scoped.
				chassis_new = Math.Matrix()
				chassis_new.setRotateYPR(_p_ypr)
				chassis_new.translation = mock_veh.position
				chassis_mp.a = chassis_new

				# Engine sounds are handled in _step_offline_physics


										
						

				# --- Update Gun Mechanics (Dispersion & Reload) ---
				if not _gun_state['initialized']:
					td = loaded_models.get('td')
					if td is not None and hasattr(td, 'gun'):
						try:
							_gun_state['base_dispersion'] = td.gun.get('shotDispersionAngle', 0.1) if isinstance(td.gun, dict) else getattr(td.gun, 'shotDispersionAngle', 0.1)
							if 'shotDispersionFactors' in td.gun if isinstance(td.gun, dict) else hasattr(td.gun, 'shotDispersionFactors'):
								_gun_state['after_shot'] = td.gun['shotDispersionFactors'].get('afterShot', 1.5) if isinstance(td.gun, dict) else td.gun.shotDispersionFactors.get('afterShot', 1.5)
							_gun_state['aim_time'] = td.gun.get('aimingTime', 2.0) if isinstance(td.gun, dict) else getattr(td.gun, 'aimingTime', 2.0)
							if 'clip' in td.gun if isinstance(td.gun, dict) else hasattr(td.gun, 'clip'):
								_clip = td.gun['clip'] if isinstance(td.gun, dict) else td.gun.clip
								_gun_state['clip_size'] = _clip[0]
								_gun_state['clip_reload'] = _clip[1]
							_gun_state['reload'] = td.gun.get('reloadTime', 5.0) if isinstance(td.gun, dict) else getattr(td.gun, 'reloadTime', 5.0)
							
							_gun_state['ammo'] = 45
							if hasattr(td, 'maxAmmo'): _gun_state['ammo'] = td.maxAmmo
							elif isinstance(td.gun, dict) and 'maxAmmo' in td.gun: _gun_state['ammo'] = td.gun['maxAmmo']
							elif hasattr(td.gun, 'maxAmmo'): _gun_state['ammo'] = td.gun.maxAmmo
							elif hasattr(td, 'turret') and hasattr(td.turret, 'maxAmmo'): _gun_state['ammo'] = td.turret.maxAmmo
							
							# Equipment & Crew Modifiers
							has_rammer, has_egld, has_vents, has_vstab, has_rations = False, False, False, False, False
							has_bia, has_snapshot, has_smooth_ride = True, False, False
							
							# Hardcode consumables if none found or to guarantee they exist in offline mode
							_gun_state['consumables'] = [
								{'slot': 3, 'tag': 'repairkit', 'name': 'smallrepairkit', 'icon': '../maps/icons/artefact/smallRepairkit.png', 'used': False},
								{'slot': 4, 'tag': 'medkit', 'name': 'smallmedkit', 'icon': '../maps/icons/artefact/smallMedkit.png', 'used': False},
								{'slot': 5, 'tag': 'extinguisher', 'name': 'handextinguishers', 'icon': '../maps/icons/artefact/handExtinguishers.png', 'used': False}
							]
							
							try:
								from CurrentVehicle import g_currentVehicle
								if g_currentVehicle and hasattr(g_currentVehicle, 'item') and g_currentVehicle.item:
									v_item = g_currentVehicle.item
									
									try:
										import debug_utils
										debug_utils.LOG_DEBUG('DEBUG STATS COMP: td.gun.aimingTime=', getattr(td.gun, 'aimingTime', None), 'v_item.descriptor.gun.aimingTime=', getattr(v_item.descriptor.gun, 'aimingTime', None))
									except: pass
									
									# Parse Equipment
									for dev in getattr(v_item, 'optDevices', []):
										if not dev: continue
										name = getattr(dev, 'name', '') or getattr(getattr(dev, 'descriptor', None), 'name', '') or str(dev)
										name = str(name).lower()
										import debug_utils
										debug_utils.LOG_DEBUG('Parsed Equipment Name:', name)
										if 'rammer' in name: has_rammer = True
										if 'aimdrives' in name: has_egld = True
										if 'ventilation' in name: has_vents = True
										if 'stabilizer' in name: has_vstab = True
									# Parse Consumables from g_currentVehicle if available
									# (We already hardcoded them above, but we can override if needed)
									
									_eqs_list = list(getattr(v_item, 'eqs', []))
									if any(_eqs_list):
										_gun_state['consumables'] = []
									
									for idx, eq in enumerate(_eqs_list):
										if not eq: continue
										name = getattr(eq, 'name', '') or getattr(getattr(eq, 'descriptor', None), 'name', '') or str(eq)
										name = str(name).lower()
										if any(x in name for x in ('ration', 'chocolate', 'cola', 'coffee', 'pudding')): has_rations = True
										icon = getattr(eq, 'icon', None) or getattr(getattr(eq, 'descriptor', None), 'icon', None)
										icon_path = icon[0] if icon and isinstance(icon, tuple) else ''
										if not icon_path:
											# Every variant has its OWN icon shipped in res (smallRepairkit/largeRepairkit,
											# smallMedkit/largeMedkit, handExtinguishers/autoExtinguishers). The old fallback
											# matched only 'medkit'/'repair'/'extinguisher' and always handed back the small
											# one, so a large kit sat in the slot wearing the small kit's picture.
											_big = ('large' in name) or ('big' in name)
											if 'medkit' in name:
												icon_path = '../maps/icons/artefact/%sMedkit.png' % ('large' if _big else 'small')
											elif 'repair' in name:
												icon_path = '../maps/icons/artefact/%sRepairkit.png' % ('large' if _big else 'small')
											elif 'extinguisher' in name:
												# automatic extinguishers are the 'auto' variant, the hand ones the default
												icon_path = '../maps/icons/artefact/%sExtinguishers.png' % ('auto' if 'auto' in name else 'hand')
										
										import debug_utils
										debug_utils.LOG_DEBUG('DUMP CONSUMABLE:', name, icon, icon_path)
										tag_name = 'extinguisher' if 'extinguisher' in name else ('medkit' if 'medkit' in name else ('repairkit' if 'repair' in name else ''))
										if tag_name:
											_gun_state['consumables'].append({
												'slot': idx + 3,
												'tag': tag_name,
												'name': name,
												'icon': icon_path,
												'used': False
											})
										
									# Parse Crew Perks
									crew = getattr(v_item, 'crew', [])
									import debug_utils
									debug_utils.LOG_DEBUG('CREW OBJECT IS:', len(crew), crew)
									if not crew: has_bia = False
									for idx, item in enumerate(crew):
										try:
											tman = item[1] if isinstance(item, tuple) and len(item) == 2 else item
											
											if tman is None:
												has_bia = False
												continue
											
											tman_skills = []
											if hasattr(tman, 'skills'):
												for sk in tman.skills:
													name = getattr(sk, 'name', '') or str(sk)
													tman_skills.append(str(name).lower())
											elif hasattr(tman, 'descriptor') and hasattr(tman.descriptor, 'skills'):
												tman_skills = [str(sk).lower() for sk in tman.descriptor.skills]
											
											if 'brotherhood' not in tman_skills: has_bia = False
											if 'smoothturret' in tman_skills or 'snapshot' in tman_skills: has_snapshot = True
											if 'smoothdriving' in tman_skills or 'smoothride' in tman_skills: has_smooth_ride = True
										except Exception as ce:
											import debug_utils
											debug_utils.LOG_DEBUG('Crew member parsing error:', str(ce))
											has_bia = False
							except Exception as e:
								import debug_utils
								debug_utils.LOG_DEBUG('Equipment/Crew parsing error:', str(e))
								has_bia = False
							
							# Calculate crew multiplier (Base 100% crew + Commander 10% bonus)
							crew_skill, commander_skill = 100.0, 100.0
							if has_vents:
								crew_skill += 5.0
								commander_skill += 5.0
							if has_bia:
								crew_skill += 5.0
								commander_skill += 5.0
							if has_rations:
								crew_skill += 10.0
								commander_skill += 10.0
							effective_skill = crew_skill + (commander_skill * 0.1)
							crew_mult = 1.0 / (0.5 + 0.005 * effective_skill)
							
							_gun_state['base_dispersion'] *= crew_mult
							_gun_state['aim_time'] *= crew_mult
							_gun_state['reload'] *= crew_mult
							_gun_state['clip_reload'] *= crew_mult
							
							if has_rammer:
								_gun_state['reload'] *= 0.9
								_gun_state['clip_reload'] *= 0.9
							if has_egld:
								_gun_state['aim_time'] /= 1.1
							_gun_state['has_vstab'] = has_vstab
							_gun_state['has_snapshot'] = has_snapshot
							_gun_state['has_smooth_ride'] = has_smooth_ride
							
						except Exception as e:
							LOG_DEBUG('OfflineBattle: Gun State Init ERROR:', str(e))
						# Empty gun at battle start, like the original: nothing is chambered and no
						# magazine is in. The reload tick below refuses to run until the arena reaches
						# period 3, so the first round only starts going in when the countdown ends.
						_gun_state['clip'] = 0
						_gun_state['reloadTime'] = _gun_state['reload']
						_gun_state['load_started'] = False
						# A knocked-out gunner widens the aiming circle (commander a bit more).
						try:
							_pm_cd = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
							_gun_state['dispersion'] = _gun_state['base_dispersion'] * (_crew_factor(_pm_cd, 'dispersion') if _pm_cd is not None else 1.0)
						except Exception:
							_gun_state['dispersion'] = _gun_state['base_dispersion']
						_gun_state['next_shot_index'] = _gun_state.get('shot_index', 0)
						_gun_state['initialized'] = True
						LOG_DEBUG('OfflineBattle: Gun State initialized from TD: dispersion=%.3f, aim_time=%.2f, reload=%.2f, clip_size=%d' % (
							_gun_state['base_dispersion'], _gun_state['aim_time'], _gun_state['reload'], _gun_state['clip_size']))

				if _gun_state['initialized']:
					try:
						# 1. Dispersion shrinkage
	
						if 'GUI_INIT' not in _gun_state:
							try:
								from gui import WindowsManager
								panel = getattr(WindowsManager.g_windowsManager.battleWindow, 'consumablesPanel', None) if getattr(WindowsManager.g_windowsManager, 'battleWindow', None) else None
								if panel:
									try:
										td = loaded_models.get('td')
										shots = td.gun['shots'] if isinstance(td.gun, dict) else getattr(td.gun, 'shots', [])
										
										# Distribute maxAmmo across available shells
										ammo_pool = _gun_state['ammo']
										try:
											from CurrentVehicle import g_currentVehicle
											v_shells = []
											if g_currentVehicle and g_currentVehicle.item:
												shells = getattr(g_currentVehicle.item, 'shells', [])
												for sh in shells:
													if hasattr(sh, 'count'): v_shells.append(sh.count)
													elif isinstance(sh, tuple) and len(sh) >= 2: v_shells.append(sh[1])
										except:
											v_shells = []
											
										for i, shot in enumerate(shots):
											try: shell = shot['shell']
											except: shell = getattr(shot, 'shell', None)
											try: piercing_val = shot['piercingPower']
											except: piercing_val = getattr(shot, 'piercingPower', 100)
											if isinstance(piercing_val, (tuple, list)): piercing_val = piercing_val[0]
											
											if v_shells and i < len(v_shells):
												qty = v_shells[i]
											else:
												qty = int(ammo_pool * 0.6) if i == 0 else (int(ammo_pool * 0.3) if i == 1 else int(ammo_pool * 0.1))
												if qty == 0 and ammo_pool > 0: qty = 1
											
											_gun_state['ammo_%d' % i] = qty
											panel.addShellSlot(i, qty, _gun_state['clip_size'], _gun_state['clip_size'], shell, piercing_val)
											
										# Find first shell with > 0 ammo
										first_active = 0
										for i in xrange(len(shots)):
											if _gun_state.get('ammo_%d' % i, 0) > 0:
												first_active = i
												break
										_gun_state['shot_index'] = first_active
										
										# Select the first shell as active to show clip UI
										panel.setCurrentShell(first_active)
										panel.setShellQuantityInSlot(first_active, _gun_state['ammo_%d' % first_active], _gun_state['clip'])
									except Exception as ex: LOG_DEBUG('SHELL SLOT FAIL:', str(ex))
									
									try:
										import AvatarInputHandler.aims as aim
										aim.setClipParams(_gun_state['clip_size'], 1)
										aim.setAmmoStock(_gun_state['ammo_%d' % first_active], _gun_state['clip'], False)
										
										# Vynutit reset ukazatele zdraví v GUI!
										from gui import WindowsManager
										bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
										if bw is not None:
											_mh = getattr(td, 'maxHealth', 400)
											if hasattr(bw, 'damagePanel'):
												try: bw.damagePanel._DamagePanel__callFlash('setMaxHealth', [_mh])
												except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
												bw.damagePanel.updateHealth(_mh)
												# Everything the panel remembers from the LAST battle has to go. Offline it
												# is not rebuilt per battle, and the only 'normal' pushes anywhere are the
												# repair-kit paths - so a module destroyed by the killing shot last time
												# stayed red on a fresh, undamaged tank, and the new mock never pushes
												# 'normal' because it has nothing damaged to report. Very often that is the
												# tracks, which is what the killing shot usually breaks. Once per battle:
												# doing it per frame would erase live crits as they happen.
												if not _gun_state.get('_dmg_panel_reset'):
													_gun_state['_dmg_panel_reset'] = True
													_n_reset = 0
													_ui_reset = ['engine', 'ammoBay', 'gun', 'turretRotator', 'chassis',
														'surveyingDevice', 'radio', 'fuelTank', 'leftTrack', 'rightTrack']
													try:
														from gui.mods.offhangar import device_damage as _dd_reset
														_ui_reset += [(_c[:-6] if _c.endswith('Health') else _c) for _c in _dd_reset.CREW_HEALTH_NAMES]
													except Exception:
														_ui_reset += ['commander', 'driver', 'gunner1', 'gunner2',
															'loader1', 'loader2', 'radioman1', 'radioman2']
													for _dv in _ui_reset:
														try:
															bw.damagePanel.updateState(_dv, 'normal')
															_n_reset += 1
														except Exception:
															pass
													try: bw.damagePanel.onFireInVehicle(False)
													except Exception: pass
													LOG_DEBUG('DAMAGE PANEL reset for new battle (%d entries cleared)' % _n_reset)
											if hasattr(bw, 'vMarkersManager'):
												pass # bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, _mh, 1, 0)
									except Exception as e: pass
									
									# Add Consumables to UI
									if not _gun_state.get('consumables_added_to_ui'):
										_gun_state['consumables_added_to_ui'] = True
										import debug_utils
										debug_utils.LOG_DEBUG('ADDING CONSUMABLES TO UI:', _gun_state.get('consumables', []))
										
										class FakeEqDescr(object):
											def __init__(self, tag, icon, name):
												self.tags = set([tag])
												self.icon = [icon]
												self.userString = name
												self.description = ''
										
										for cons in _gun_state.get('consumables', []):
											idx = cons['slot']
											tag = cons['tag']
											icon = cons['icon']
											name = cons['name']
											try:
												panel.addEquipmentSlot(idx, 1, FakeEqDescr(tag, icon, name))
											except Exception as e:
												import debug_utils
												debug_utils.LOG_DEBUG('Failed to addEquipmentSlot:', str(e))
									
									# Route Flash slot clicks and damage-panel icon clicks into the offline
									# equipment activation: small kit -> selector, large -> repair all, and a
									# click on a damaged module icon repairs exactly that module.
									try:
										player.onEquipmentButtonPressed = (lambda _idx, deviceName=None: _offh_activate_equipment(_idx, deviceName))
										player.onDamageIconButtonPressed = (lambda _tag, _dev=None: _offh_damage_icon(_tag, _dev))
									except Exception as _wire_e:
										LOG_DEBUG('wire equipment methods err:', str(_wire_e))
									_gun_state['GUI_INIT'] = True
									LOG_DEBUG('OfflineBattle: GUI panel initialized!')
							except Exception as e:
								LOG_DEBUG('OfflineBattle GUI Init Error:', str(e))
						cur_time = BigWorld.time()
						if 'last_time' not in _gun_state: _gun_state['last_time'] = cur_time
						dt = cur_time - _gun_state['last_time']
						_gun_state['last_time'] = cur_time
						
						# Dispersion, ported line for line from the client's own
						# Avatar.getOwnVehicleShotDispersionAngle:
						#   ideal  = sqrt(1 + (move^2 + rot^2 + turret^2 + shot^2) * additive^2)
						#   aiming = startFactor * exp((startTime - now) / aimingTime)
						#   if aiming < ideal: aiming = ideal, and the clock restarts from there
						#   dispersion = shotDispersionAngle * aiming
						# So the circle DECAYS TOWARDS ZERO and is caught by the ideal floor,
						# reaching it in aimingTime*ln(start/ideal) seconds; and it grows into a
						# larger ideal INSTANTLY, in the frame the ideal changes.
						#
						# The old code did neither. It relaxed asymptotically towards the ideal,
						# which never actually arrives. Measured on the last tank in python.log
						# (base 0.003 rad, aimingTime 2.19 s, afterShot 4.0): closing the 4.12x
						# post-shot bloom back to within 2% of base took 11.0 s, against the 3.07 s
						# this formula gives and the 3.06 s aimingTime*ln(4.12/1.02) predicts. The
						# gun aimed five times its own aiming time instead of one and a half, and
						# never read as fully aimed at all. And bloom GREW at 20% of the gap PER
						# FRAME, so a new ideal faded in over ~0.3 s at 60 fps - slower on a weaker
						# machine - instead of landing in the frame it happened.
						#
						# Both halves are why a turret traverse reads as "the spread goes to its
						# maximum": full traverse really is a ~6x circle (turretRotation factors
						# are stored per rad/s), the spike crept in, and then it sat there for the
						# best part of eleven seconds.
						import math
						_base_eff = _gun_state['base_dispersion']
						_aim_time = _gun_state['aim_time']
						try:
							_pm_ds = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
							if _pm_ds is not None:
								_base_eff = _base_eff * _crew_factor(_pm_ds, 'dispersion') * _module_factor(_pm_ds, 'dispersion')
								_aim_time = _aim_time * _module_factor(_pm_ds, 'aim_time')
						except Exception:
							pass
						_aim_time = max(_aim_time, 0.1)
						_terms = 0.0
						_add = 1.0
						try:
							_d_td = loaded_models.get('td')
							_cm, _cr = _d_td.chassis['shotDispersionFactors']
							_gdf = _d_td.gun['shotDispersionFactors'] if isinstance(_d_td.gun, dict) else _d_td.gun.shotDispersionFactors
							_gt = _gdf['turretRotation']
							v_speed, r_speed = player.getOwnVehicleSpeeds()
							_mv = v_speed * _cm
							_rv = r_speed * _cr
							_tv = _gun_state.get('turret_speed', 0.0) * _gt
							# Equipment rides on miscAttrs['additiveShotDispersionFactor'] - a
							# vertical stabiliser sets it to 0.8 - and the real formula scales
							# EVERY movement term by it, including the after-shot one. The fitted
							# descriptor already carries it, so read it instead of hardcoding a
							# vStab discount that would otherwise be counted twice.
							try:
								_add = float(_d_td.miscAttrs['additiveShotDispersionFactor'])
							except Exception:
								_add = 0.8 if _gun_state.get('has_vstab', False) else 1.0
							if _gun_state.get('has_snapshot', False):
								_tv *= 0.925
							if _gun_state.get('has_smooth_ride', False):
								_mv *= 0.96
							_terms = (_mv * _mv + _rv * _rv + _tv * _tv) * _add * _add
						except Exception:
							try:
								v_speed, r_speed = player.getOwnVehicleSpeeds()
								_ex = (abs(v_speed) * 0.015 + abs(r_speed) * 0.015) / max(_gun_state['base_dispersion'], 1e-06)
								_terms = (1.0 + _ex) ** 2 - 1.0
							except Exception:
								_terms = 0.0
						# Cached for the after-shot bloom in _mock_shoot, which has to rebuild the
						# same ideal factor with the afterShot term folded in.
						_gun_state['_disp_terms'] = _terms
						_gun_state['_disp_add2'] = _add * _add
						_gun_state['_base_eff'] = _base_eff
						_ideal_f = math.sqrt(1.0 + _terms)
						_start_f = _gun_state.get('aim_start_f', _ideal_f)
						_start_t = _gun_state.get('aim_start_t', cur_time)
						_aim_f = _start_f * math.exp((_start_t - cur_time) / _aim_time)
						if _aim_f < _ideal_f:
							_aim_f = _ideal_f
							_gun_state['aim_start_f'] = _ideal_f
							_gun_state['aim_start_t'] = cur_time
						_gun_state['dispersion'] = _base_eff * _aim_f

						# 2. Reload logic
						# The crew does not load during the countdown - the gun is empty until the
						# battle actually starts. On the frame period turns 3 the pending reload is
						# announced to the UI so the bar animates from full instead of appearing
						# half-way through.
						_period_g = getattr(getattr(player, 'arena', None), 'period', 3)
						if _period_g != 3:
							_gun_state['load_started'] = False
						elif not _gun_state.get('load_started'):
							_gun_state['load_started'] = True
							if _gun_state['reloadTime'] > 0:
								try:
									_si_g = _gun_state.get('shot_index', 0)
									from gui import WindowsManager as _wmg
									_bwg = getattr(_wmg.g_windowsManager, 'battleWindow', None)
									_pg = getattr(_bwg, 'consumablesPanel', None) if _bwg is not None else None
									if _pg is not None:
										_pg.setCoolDownTime(_si_g, _gun_state['reloadTime'])
									_aimg = getattr(g_offline_aih, 'aim', None)
									if _aimg is not None:
										_aimg.setReloading(_gun_state['reloadTime'], None)
										_aimg.setAmmoStock(_gun_state['ammo_%d' % _si_g], 0, False)
								except Exception as _lse:
									LOG_DEBUG('initial load UI err:', str(_lse))
						if _gun_state['reloadTime'] > 0 and _period_g == 3:
							_gun_state['reloadTime'] -= dt
							if _gun_state['reloadTime'] <= 0:
								_gun_state['reloadTime'] = 0.0
								# The queued shell type is what actually goes in the breech. A single
								# press only ever selects it (see the key handler); this is where it
								# is honoured, so the swap costs the reload you were already serving
								# rather than throwing away a loaded round.
								_nsi = _gun_state.get('next_shot_index', None)
								if _nsi is not None and _nsi != _gun_state.get('shot_index', 0) and (_gun_state.get('ammo_%d' % _nsi, 0) or 0) > 0:
									_gun_state['shot_index'] = _nsi
									_gun_state['clip'] = 0
									LOG_DEBUG('AMMO loaded queued type: slot %d' % _nsi)
									try:
										from gui import WindowsManager as _WMq
										_pq = getattr(getattr(_WMq.g_windowsManager, 'battleWindow', None), 'consumablesPanel', None)
										if _pq is not None:
											_pq.setCurrentShell(_nsi)
									except Exception as _nse:
										LOG_DEBUG('setCurrentShell (queued) error:', str(_nse))
								if _gun_state['clip'] == 0:
									# Never more rounds than are actually carried - a near-empty ammo type
									# used to refill to the full magazine size.
									_si_r = _gun_state.get('shot_index', 0)
									_gun_state['clip'] = min(_gun_state['clip_size'], _gun_state.get('ammo_%d' % _si_r, 0) or 0)
								
								# Reset UI cooldown and refresh ammo count when reload finishes
								try:
									from gui import WindowsManager
									panel = WindowsManager.g_windowsManager.battleWindow.consumablesPanel
									if panel:
										shot_idx = _gun_state.get('shot_index', 0)
										panel.setShellQuantityInSlot(shot_idx, _gun_state['ammo_%d' % shot_idx], _gun_state['clip'])
										panel.setCoolDownTime(shot_idx, 0.0)
									aim = getattr(g_offline_aih, 'aim', None)
									if aim:
										aim.setReloading(0.0, None)
										shot_idx = _gun_state.get('shot_index', 0)
										aim.setAmmoStock(_gun_state['ammo_%d' % shot_idx], _gun_state['clip'], True if _gun_state['clip'] == _gun_state['clip_size'] else False)
									
									_offh_notify('gun_reloaded')
								except Exception:
									pass
					except Exception as e:
						LOG_DEBUG('OfflineBattle dispersion error:', str(e))

					# 3. Update Crosshair + AIH
					try:
						# Let the engine update the aim crosshair
						# Compute where the gun is actually pointing (offset start pos by 4.0m to avoid hitting our own tank hull!)
						try:
							td = loaded_models.get('td')
							turretOffs = td.hull['turretPositions'][0] + td.chassis['hullPosition']
							gunOffs = td.turret['gunPosition']
						except:
							turretOffs = Math.Vector3(0, 1.5, 0)
							gunOffs = Math.Vector3(0, 0.4, 1.0)

						turretWorldMatrix = Math.Matrix()
						turretWorldMatrix.setRotateY(turret_yaw[0])
						turretWorldMatrix.translation = turretOffs
						turretWorldMatrix.postMultiply(mock_veh.matrix)

						true_gun_pos = turretWorldMatrix.applyPoint(gunOffs)

						gunWorldMatrix = Math.Matrix()
						gunWorldMatrix.setRotateX(gun_pitch[0])
						gunWorldMatrix.translation = gunOffs
						gunWorldMatrix.postMultiply(turretWorldMatrix)
						
						gun_dir = gunWorldMatrix.applyToAxis(2)
						gun_dir.normalise()
						
						if 'gun_node_matrix' in loaded_models:
							# Store ONLY the true_gun_pos (pivot). NO rotation.
							# SniperCamera applies its own pitch/yaw from mouse input,
							# and then automatically applies the tank's configured pivotPos.
							_cam_m = Math.Matrix()  # identity = no rotation
							_cam_m.translation = true_gun_pos
							loaded_models['gun_node_matrix'].set(_cam_m)
						
						# Pass gun pos to rotator for Arty/Arcade raycasts
						if hasattr(player, 'gunRotator'):
							player.gunRotator._gun_pos = true_gun_pos
							player.gunRotator._gun_dir = gun_dir
							
						is_arty = False
						try: is_arty = 'SPG' in td.type.tags
						except: pass
						# Calculate exact terrain intersection for the green marker (perfectly simulates server)
						_end_gun = true_gun_pos + gun_dir.scale(10000.0)
						_col_gun = None
						try:
							_col_gun = BigWorld.wg_collideSegment(_offh_bspace(), true_gun_pos, _end_gun, 128)
						except Exception:
							pass
						gun_target_pos = _col_gun[0] if _col_gun is not None else _end_gun
						
						if hasattr(player, 'gunRotator') and len(player.gunRotator.markerInfo) >= 2:
							mtp = player.gunRotator.markerInfo[0]
							mdir = player.gunRotator.markerInfo[1]
							
							if isinstance(mtp, tuple) and mtp == (0.0, 0.0, 0.0):
								pass # Offline stub, ignore
							elif isinstance(mtp, Math.Vector3) and mtp.lengthSquared == 0.0:
								pass # Offline stub, ignore
							else:
								if isinstance(mtp, tuple):
									gun_target_pos = Math.Vector3(mtp[0], mtp[1], mtp[2])
								else:
									gun_target_pos = mtp
									
								if isinstance(mdir, tuple):
									gun_dir = Math.Vector3(mdir[0], mdir[1], mdir[2])
								else:
									gun_dir = mdir
							
						if _tick_counter[0] % 50 == 0:
							LOG_DEBUG('OfflineBattle.gun: target_pos=', gun_target_pos, 'dir=', gun_dir, 'pos=', true_gun_pos)
							
						# Hide vehicle in sniper mode using model.visible
						if hasattr(g_offline_aih, 'ctrl'):
							is_sniper = g_offline_aih.ctrl.__class__.__name__ == 'SniperControlMode'
							was_sniper = getattr(g_offline_aih, '_was_sniper', None)
							if is_sniper != was_sniper:
								g_offline_aih._was_sniper = is_sniper
								# (removed) This used to _offhangar_muzzle_player.stop() to kill a
								# muzzle flash left frozen when the hidden models reappeared - but
								# the gun's EffectsList also carries the SHOT SOUND, so zooming
								# right after firing (either direction) cut the bang off mid-play.
								# The freeze itself is gone: _play_muzzle_flash puts the player's
								# gun model on BigWorld.addAlwaysUpdateModel, so the flash animates
								# out even while the models are hidden/unrendered.
								for _part in ('chassis', 'hull', 'turret', 'gun'):
									_mdl = loaded_models.get(_part)
									if _mdl is not None:
										try: _mdl.visible = not is_sniper
										except: pass
								# Tank is hidden via .visible=False, so no need to push underground.
								# Keeping it at real position ensures 3D sounds (engine, gun) remain audible!

						# Calculate perfectly synchronous math_gun_world for raycast
						math_turret_pos = td.chassis['hullPosition'] + td.hull['turretPositions'][0]
						math_gun_world = Math.Matrix(mat).applyPoint(math_turret_pos)
						yaw_mat = Math.Matrix()
						yaw_mat.setRotateY(turret_yaw[0])
						math_gun_world += Math.Matrix(mat).applyVector(yaw_mat.applyVector(td.turret['gunPosition']))

						_end_gun = math_gun_world + gun_dir.scale(10000.0)
						if not is_arty:
							dist_to_target = (shot_point - math_gun_world).length
							# The barrel is ELEVATED to drop the shell onto the aim point
							# (getShotAngles), so a straight ray along it always ends up high and
							# long - by roughly twice the drop. On a 1000 m/s gun that is a few
							# centimetres and nobody notices; on a KV-2's 152 mm howitzer at
							# 400 m it is metres, which is exactly the reported "the aiming point
							# is much higher than it should be, and the projectile flies higher".
							# Walk the real parabola instead, the way VehicleGunRotator does.
							_shot_now = None
							try:
								_shots_m = td.gun.get('shots', []) or []
								if _shots_m:
									_shot_now = _shots_m[min(_gun_state.get('shot_index', 0), len(_shots_m) - 1)]
							except Exception:
								_shot_now = None
							_col_gun = BigWorld.wg_collideSegment(_offh_bspace(), math_gun_world, _end_gun, 128)
							_straight_d = (_col_gun[0] - math_gun_world).length if _col_gun is not None else dist_to_target
							_marker_drop = _offh_shell_drop(min(_straight_d, dist_to_target),
								_shot_now['speed'], _shot_now['gravity']) if _shot_now is not None else 0.0
							_marker_branch = 'straight(drop=%.2f shot=%s cfg=%s)' % (
								_marker_drop, _shot_now is not None, _offh_cfg_flag('ballistic_shells', True))
							# ALWAYS walk - no drop gate. The straight fallback below places the marker
							# along the BARREL at the CAMERA's aim distance, and those two rays start
							# metres apart, so at short range the parallax dominates: measured 135 deg of
							# separation between reticle and circle at 16 m, while every ballistic frame
							# in the same log sat at 0.00 circle radii. The walk is correct at all ranges
							# and costs one or two chords up close, so the gate only ever bought a
							# correctness cliff. The straight branch stays for ballistic_shells=false.
							if _shot_now is not None and _offh_cfg_flag('ballistic_shells', True):
								# Walk until the shell actually hits something, exactly like
								# VehicleGunRotator - do NOT stop it at the camera's aim
								# distance. Capping it there pinned the marker to the aim point
								# whatever the barrel was doing, so the reticle always looked
								# right while the shell went wherever the gun was really
								# pointing: "reticle is at the correct location but the shell
								# goes way below the target". The marker's whole job is to show
								# where THIS barrel sends THIS shell, including while the gun is
								# still slewing or is up against an elevation limit.
								_walk_max = min(float(_shot_now.get('maxDistance', 1000.0)), 2000.0)
								# Same 0.1 s step as the shot walk in _mock_shoot: a marker
								# stepped more coarsely than the round it is predicting can
								# pick a different chord to cross the ground on.
								# Tanks were not tested at all here, so a reticle held on an enemy
								# watched its own shell pass THROUGH him and stop on the ground
								# behind - the marker dropping off the target that Bence reported.
								# The shot walk has always tested them; the marker has to agree
								# with it or it is predicting a different round.
								def _mk_mock_test(p1, p2, _pid=getattr(player, 'playerVehicleID', -1)):
									_best, _bv = None, None
									_seg = p2 - p1
									_sl = _seg.length
									if _sl <= 1e-06:
										return None
									for _me, _mm in mock_vehicles.iteritems():
										if _me == _pid or not getattr(_mm, 'isAlive', False):
											continue
										if (getattr(_mm, 'health', 0) or 0) <= 0:
											continue
										try:
											_to = _mm.position - p1
											_t = max(0.0, min(1.0, (_to.x * _seg.x + _to.y * _seg.y + _to.z * _seg.z) / (_sl * _sl)))
											if (_to - _seg.scale(_t)).length > 12.0:
												continue
										except Exception:
											pass
										_c = _mm.collideSegment(p1, p2)
										if _c is not None and (_best is None or _c[0] < _best[0]):
											_best, _bv = _c, _mm
									return (_bv, _best) if _best is not None else None
								_walk = _offh_shell_path(_offh_bspace(), math_gun_world,
									gun_dir.scale(_shot_now['speed']), _shot_now['gravity'],
									_walk_max, _mk_mock_test, 0.1, 160)
								gun_target_pos = _walk['pos']
								gun_dir = _walk['dir']
								if _walk['world'] is not None:
									_col_gun = _walk['world']
								_marker_branch = 'ballistic'
							elif _col_gun is not None:
								gun_hit = _col_gun
								if gun_hit[1] < dist_to_target:
									dist_to_static = (gun_hit[0] - math_gun_world).length
									if dist_to_target - dist_to_static > 1.0:
										gun_target_pos = math_gun_world + gun_dir.scale(dist_to_target)
									else:
										gun_target_pos = gun_hit[0]
								else:
									gun_target_pos = math_gun_world + gun_dir.scale(dist_to_target)
							else:
								gun_target_pos = math_gun_world + gun_dir.scale(10000.0)
						else:
							gun_target_pos = math_gun_world + gun_dir.scale(10000.0)

						# Is the drawn reticle where the crosshair is pointing? shot_point is the point
						# under the reticle centre (SniperControlMode -> _getDesiredShotPoint casts a ray
						# through the aim offset) and the gun is aimed AT it, so a correct marker sits on
						# it. Log the angle between the two as seen from the muzzle, expressed in units of
						# the dispersion circle's own radius - that IS the offset visible on screen.
						if _tick_counter[0] % 50 == 0:
							try:
								_mv1 = gun_target_pos - math_gun_world
								_mv2 = shot_point - math_gun_world
								_l1, _l2 = _mv1.length, _mv2.length
								_ang = 0.0
								if _l1 > 0.001 and _l2 > 0.001:
									_cosv = max(-1.0, min(1.0, _mv1.dot(_mv2) / (_l1 * _l2)))
									_ang = math.degrees(math.acos(_cosv))
								_rad = math.degrees(_gun_state.get('dispersion', 0.006) or 0.006)
								LOG_DEBUG('MARKER DIAG: branch=%s | marker %.0fm %s | crosshair %.0fm %s | offset %.3fdeg = %.2f circle radii' % (
									_marker_branch, _l1, gun_target_pos, _l2, shot_point, _ang,
									(_ang / _rad) if _rad > 0.0 else -1.0))
							except Exception as _mde:
								LOG_DEBUG('MARKER DIAG err:', str(_mde))

						# UPDATE CROSSHAIR
						# dead/spectating -> skip the dynamic gun-marker (dispersion reticle) refresh;
						# leaving it on re-shows it every frame + fights the post-mortem hide below.
						if hasattr(g_offline_aih, 'ctrl') and not getattr(player, '_is_dead', False):
							try:
								if hasattr(player, 'gunRotator'):
									player.gunRotator.dispersionAngle = _gun_state['dispersion']
								
								dist_m = (gun_target_pos - math_gun_world).length
								size_m = _gun_state['dispersion'] * dist_m * 2.0
								
								# Native penetration marker: feed collData so the stock gun
								# marker colours itself green/orange/red by whether the loaded
								# shell pierces the plate under the reticle. Only direct-fire
								# modes own a _FlashGunMarker; others stay None (raycast skipped).
								# pen_indicator is pure math (no models/effects/state) = leak-free.
								_pen_coll = None
								try:
									_pen_cn = g_offline_aih.ctrl.__class__.__name__
									if _pen_cn in ('SniperControlMode', 'ArcadeControlMode'):
										try:
											player.vehicleTypeDescriptor.activeGunShotIndex = _gun_state.get('shot_index', 0)
										except Exception:
											pass
										# Sample armor along the ACTUAL shot ray (muzzle pos +
										# gun dir), not the marker vectors - else the ray grazes
										# the hull and reads a bogus near-infinite armor.
										_pen_start = math_gun_world
										_pen_dir = gun_dir
										try:
											_sp, _sd = player.gunRotator._VehicleGunRotator__getCurShotPosition()
											_pen_start = _sp
											_pen_dir = Math.Vector3(_sd)
											_pen_dir.normalise()
										except Exception:
											pass
										_pen_wd = None
										try:
											if _col_gun is not None:
												_pen_wd = (_col_gun[0] - _pen_start).length
										except Exception:
											_pen_wd = None
										from gui.mods.offhangar import pen_indicator as _peni
										_pen_coll = _peni.build_coll_data(
											globals().get('G_MOCK_VEHICLES', {}) or {},
											getattr(player, 'playerVehicleID', -1),
											getattr(player, 'team', getattr(player, '_offhangar_team', 1)),
											_pen_start, _pen_dir, _pen_wd)
								except Exception as _pie:
									LOG_DEBUG('OfflineBattle pen-colldata error:', str(_pie))
								g_offline_aih.ctrl.updateGunMarker(gun_target_pos, gun_dir, size_m, 0.0, _pen_coll)
							except Exception as e:
								LOG_DEBUG('OfflineBattle updateGunMarker error:', str(e), 'pos:', true_gun_pos, 'dir:', gun_dir)
							try:
								g_offline_aih.ctrl.updateGunMarker2(gun_target_pos, gun_dir, size_m, 0.0, _pen_coll)
							except Exception as e:
								pass
								
							if _gun_state.get('tick_counter', 0) % 60 == 0:
								import debug_utils
								try:
									cam_m_debug = Math.Matrix(BigWorld.camera().matrix)
									debug_utils.LOG_DEBUG("DEBUG DIR", "cam_pos:", cam_m_debug.translation, "gun_pos:", true_gun_pos)
									debug_utils.LOG_DEBUG("DEBUG DIR", "cam_dir:", cam_m_debug.applyToAxis(2), "gun_dir:", gun_dir)
									debug_utils.LOG_DEBUG("DEBUG DIR", "tYaw:", tYaw, "gPitch:", gPitch)
								except: pass
							_gun_state['tick_counter'] = _gun_state.get('tick_counter', 0) + 1
								
							# Synchronize ammo UI when switching control modes
							aim = getattr(g_offline_aih, 'aim', None)
							if aim and aim != _gun_state.get('last_aim'):
								_gun_state['last_aim'] = aim
								try:
									if hasattr(aim, 'setClipParams'): aim.setClipParams(_gun_state['clip_size'], 1)
									if hasattr(aim, 'setAmmoStock'): aim.setAmmoStock(_gun_state['ammo_%d' % _gun_state.get('shot_index', 0)], _gun_state['clip'], False)
									# setReloading hands Flash a DURATION and Flash animates it locally, so
									# pushing the pending reload here started the bar running during the
									# countdown - the crew is not loading yet. Only announce it once the
									# battle is live; the period-3 handler above starts the bar on that frame.
									if hasattr(aim, 'setReloading'):
										if _gun_state['reloadTime'] > 0 and getattr(getattr(player, 'arena', None), 'period', 3) == 3:
											aim.setReloading(_gun_state['reloadTime'], None)
										else:
											aim.setReloading(0.0, None)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
					except Exception as e:
						import traceback
						LOG_DEBUG('OfflineBattle fatal gun error:', traceback.format_exc())
						
				# Update turret rotation (via node matrix)
				turret_mat = loaded_models.get('turret_mat')
				if turret_mat is not None:
					turret_mat.setRotateYPR((turret_yaw[0], 0, 0))

				# Update gun pitch (via node matrix)
				gun_mat = loaded_models.get('gun_mat')
				if gun_mat is not None:
					gun_mat.setRotateYPR((0, gun_pitch[0], 0))

				
						
				# --- Update turret_matrix for camera/AIH ---
				tm = Math.Matrix()
				tm.setRotateYPR((veh_yaw[0] + turret_yaw[0], gun_pitch[0], 0))
				try:
					td = loaded_models.get('td')
					turret_offs = td.hull['turretPositions'][0] + td.chassis['hullPosition']
					tm.translation = mock_veh.matrix.applyPoint(turret_offs)
				except:
					tm.translation = Math.Vector3(veh_pos[0], veh_pos[1] + 2.0, veh_pos[2])
				turret_matrix.set(tm)
				
				tm_local = Math.Matrix()
				tm_local.setRotateYPR((turret_yaw[0], gun_pitch[0], 0))
				turret_matrix_local.set(tm_local)

				# --- PLAYER FIRE LOGIC ---
				try:
					_player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					_sync_burn_and_death(_player_mock, loaded_models.get('hull'), loaded_models.get('td'))
					# Crew auto-repair: destroyed modules climb back to functional over the
					# repair time, damaged ones regen to the same cap. Without this the player
					# would stay crippled for the rest of the battle after a single crit.
					try:
						if _player_mock is not None and getattr(_player_mock, 'health', 0) > 0:
							_tick_module_repair(_player_mock, loaded_models.get('td'), dt, True)
					except Exception as _mre:
						LOG_DEBUG('player module repair err:', str(_mre))
					_sync_engine_exhaust(_player_mock, loaded_models.get('hull'), loaded_models.get('td'), _veh_velocity[0])
					if _player_mock and getattr(_player_mock, 'is_on_fire', False) and getattr(_player_mock, 'health', 0) > 0:
						# Fires burn out on their own. device_damage.FIRE_DURATION_SECONDS.
						try:
							import BigWorld as _bwf
							from gui.mods.offhangar import device_damage as _DDf
							_fs = getattr(_player_mock, '_fire_started', None)
							if _fs is not None and (_bwf.time() - _fs) >= _DDf.FIRE_DURATION_SECONDS:
								_offh_extinguish(_player_mock, True, 'burnt out')
						except Exception:
							pass
						cur_timer = getattr(_player_mock, '_fire_timer', 0.0)
						if cur_timer is None: cur_timer = 0.0
						# dt, not a hardcoded 0.02. This runs once per FRAME, so a fixed
						# step made the burn rate track the framerate: at 100 fps the
						# player took his 5% tick twice a second, at 25 fps once every
						# two. The bot fire tick has always used dt.
						_player_mock._fire_timer = float(cur_timer) + float(dt if dt is not None else 0.02)
						if _player_mock._fire_timer >= 1.0:
							_player_mock._fire_timer -= 1.0
							fire_dmg = max(1, int(_player_mock.maxHealth * 0.05))
							# The flames, the panel and the extinguisher all still work in
							# test mode; only the HP drain is held back.
							if not _offh_module_test_mode():
								_player_mock.health -= fire_dmg
							
							try:
								import gui.WindowsManager
								bw = gui.WindowsManager.g_windowsManager.battleWindow
								import debug_utils
								debug_utils.LOG_DEBUG("PLAYER_FIRE_TICK! bw: ", bw)
								if bw:
									debug_utils.LOG_DEBUG("BW_DIR: ", dir(bw))
									if hasattr(bw, 'damagePanel'):
										debug_utils.LOG_DEBUG("DAMAGE_PANEL_DIR: ", dir(bw.damagePanel))
										bw.damagePanel.updateHealth(_player_mock.health)
							except: pass
							
							if _player_mock.health <= 0:
								_player_mock.health = 0
								# `or -1`: the mock answers None for a player who was never shot
								# by anyone (burning to death after a self-inflicted fire), and
								# the arena handler wants an id, not None. Every other
								# onVehicleKilled site here already guards it this way.
								player.arena.onVehicleKilled(getattr(_player_mock, 'id', player.playerVehicleID), (getattr(_player_mock, 'last_killer_id', None) or -1), 2)
								_player_mock.is_on_fire = False
								try:
									import gui.WindowsManager
									bw = gui.WindowsManager.g_windowsManager.battleWindow
									if hasattr(bw, 'damagePanel'):
										bw.damagePanel._DamagePanel__callFlash('onFireInVehicle', [False])
								except: pass
							
							if hasattr(player, 'vehicle') and player.vehicle:
								player.vehicle.health = _player_mock.health
								try: player.guiSessionProvider.invalidateVehicleState(1, player.playerVehicleID, _player_mock.health, _player_mock.health)
								except: pass
				except: pass

				# --- Looping-sound headcount (see _offh_sound_budget) ----------------
				# Recomputed a few times a second, not per frame: it ranks every bot, and at
				# 15v15 that is not work worth doing 60 times a second when the answer only
				# changes as tanks drive.
				try:
					_sb_now = BigWorld.time()
					if _sb_now - (globals().get('g_offh_snd_budget_t') or 0.0) > 0.4:
						globals()['g_offh_snd_budget_t'] = _sb_now
						from _constants import CONFIG_OPTIONS as _CFG_SB
						_sb_max = int(_CFG_SB.get('max_engine_sound_loops', 10) or 0)
						if _sb_max <= 0:
							globals()['g_offh_snd_keep'] = None   # 0 or less = no cap, old behaviour
						else:
							globals()['g_offh_snd_keep'] = _offh_sound_budget(
								mock_vehicles, getattr(player, 'playerVehicleID', -1),
								veh_pos[0], veh_pos[2], _sb_max,
								globals().get('g_offh_snd_keep') or ())
				except Exception as _sbe:
					LOG_DEBUG('sound budget err:', str(_sbe))
				
				# --- BOT AI (Advanced Physics) ---
				import math, random
				dt = _frame_dt # real frame delta: bot speed/reload no longer depends on FPS
				# PERF: both of these are constant for the whole battle, yet they were read
				# 12x and 7x per bot INSIDE this loop - and one of the playerVehicleID reads
				# sits in the inner enemy scan below, so at 30 bots that single line made
				# ~900 trips through the offline account __getattribute__ every frame.
				# Read once per tick. Deferred callbacks further down keep reading the live
				# player on purpose, so they are left untouched.
				_p_vid = getattr(player, 'playerVehicleID', -1)
				_p_team = getattr(player, '_offhangar_team', 1)
				# PERF: the battle space cannot change inside one tick, but _offh_bspace()
				# was called 16x per bot down there - 169282 calls per 300 ticks, i.e. a
				# globals() lookup plus a try/except for an answer that is fixed for the
				# whole battle. The deferred callbacks further down still call it live.
				_tick_space = _offh_bspace()
				# Baked per-map destinations: loads once, then validates a slice of
				# nodes per frame against the LIVE 0.8.2 terrain. No-op on maps we
				# have not baked.
				_offh_route_tick()
				# Painted profile: per-team/per-class points, routes, avoid areas.
				_offh_profile_tick()
				# Nav grid: builds, dumps, then takes painted avoid areas.
				_offh_nav_tick()
				for eid, m_veh in mock_vehicles.iteritems():
					# A tank at zero hit points must END UP DEAD, whatever route took it there:
					# fire, drowning, HE splash, ramming, a fall, or a shell that landed while
					# another kill was already in flight. Several of those paths zero the health
					# and then lean on a handler that can be skipped, leaving a 0 HP tank still
					# driving with its tracks scrolling - 'their track will keep spinning and
					# won't be destroyed'. Route it through the same onVehicleKilled every real
					# kill uses (_KillEventWrapper clears isAlive, greys the marker and posts the
					# kill feed). Once per tank.
					try:
						if ((getattr(m_veh, 'health', 1) or 0) <= 0 and getattr(m_veh, 'isAlive', False)
							and not getattr(m_veh, '_zero_hp_swept', False)):
							m_veh._zero_hp_swept = True
							LOG_DEBUG('ZERO HP SWEEP: %s was alive at 0 hp - forcing the kill path' % eid)
							try:
								BigWorld.player().arena.onVehicleKilled(m_veh.id, getattr(m_veh, 'last_killer_id', -1) or -1, 0)
							except Exception as _zke:
								LOG_DEBUG('zero-hp sweep err:', str(_zke))
					except Exception:
						pass
					if eid != _p_vid and getattr(m_veh, 'isAlive', False):
						try:
							my_team = getattr(m_veh, '_bot_team', m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2)
							closest_dist = 99999.0
							target_pos = None
							# INIT BOT STATES
							if getattr(m_veh, '_veh_velocity', None) is None: m_veh._veh_velocity = 0.0
							if getattr(m_veh, '_veh_turn_velocity', None) is None: m_veh._veh_turn_velocity = 0.0
							
							# perf: full enemy scan only every ~0.4 s per bot (staggered); between
							# scans the cached target is tracked LIVE by id - aiming stays frame-exact,
							# only the re-pick of a new target is throttled. Dead/gone target -> rescan.
							m_veh._tgt_t = (getattr(m_veh, '_tgt_t', 9.0) or 9.0) + dt
							_tref = getattr(m_veh, '_tgt_ref', None)
							player_team = _p_team
							_p_ok = my_team != player_team and _p_vid != -1 and getattr(player, 'health', 1) > 0 and not getattr(player, '_is_dead', False)
							if _tref == 'P':
								if not _p_ok:
									_tref = None
							elif _tref is not None:
								_tv = mock_vehicles.get(_tref)
								if _tv is None or not getattr(_tv, 'isAlive', False):
									_tref = None
							if _tref is not None and m_veh._tgt_t < 0.4:
								if _tref == 'P':
									target_pos = veh_pos
								else:
									_tv = mock_vehicles.get(_tref)
									target_pos = (_tv.position.x, _tv.position.y, _tv.position.z)
							else:
								m_veh._tgt_t = (eid % 8) * 0.05
								m_veh._tgt_ref = None
								if _p_ok:
									dx = veh_pos[0] - m_veh.position.x
									dz = veh_pos[2] - m_veh.position.z
									dist = math.sqrt(dx*dx + dz*dz)
									if dist < closest_dist:
										closest_dist = dist
										target_pos = veh_pos
										m_veh._tgt_ref = 'P'
								for oeid, omeh in mock_vehicles.iteritems():
									if oeid == _p_vid: continue
									if oeid != eid and getattr(omeh, 'isAlive', False):
										oteam = getattr(omeh, '_bot_team', omeh.publicInfo.get('team', 2) if getattr(omeh, 'publicInfo', None) is not None else 2)
										if my_team != oteam:
											dx = omeh.position.x - m_veh.position.x
											dz = omeh.position.z - m_veh.position.z
											dist = math.sqrt(dx*dx + dz*dz)
											if dist < closest_dist:
												closest_dist = dist
												target_pos = (omeh.position.x, omeh.position.y, omeh.position.z)
												m_veh._tgt_ref = oeid
							if target_pos is None:
								# NO ENEMIES! STOP!
								m_veh._veh_velocity = max(0.0, m_veh._veh_velocity - 20.0 * dt)
								m_veh._veh_turn_velocity = 0.0
								target_pos = (m_veh.position.x, m_veh.position.y, m_veh.position.z)
							# Bearing and range to the ENEMY. The gun works off these and
							# nothing else: fix #16a gave _raw_target_yaw precisely the meaning
							# 'bearing to the target', and the movement destination introduced
							# below must not blur it again.
							_gdx = target_pos[0] - m_veh.position.x
							_gdz = target_pos[2] - m_veh.position.z
							_enemy_dist = math.sqrt(_gdx*_gdx + _gdz*_gdz)
							# Where to DRIVE. An enemy close enough to fight still wins, so combat
							# behaviour is untouched; otherwise the bot heads for the destination
							# its CLASS picked out of the baked map. This is the line that stops
							# all 30 tanks converging on the midpoint.
							_move_pos = _offh_bot_move_target(m_veh, target_pos, _enemy_dist, my_team)
							# Route the destination through the nav grid: steer at the next
							# WAYPOINT, not straight at a place that may be behind a hill.
							_move_pos = _offh_nav_waypoint(m_veh, _move_pos)
							dx = _move_pos[0] - m_veh.position.x
							dz = _move_pos[2] - m_veh.position.z
							dist = math.sqrt(dx*dx + dz*dz)
							_dc = (getattr(m_veh, '_dbg_ctr', 0) or 0)
							if _dc % 200 == 0:
								LOG_DEBUG('BOT_AI eid=%s cls=%s vel=%.2f enemy=%.0fm goal=%.0fm node=%s escape=%s' % (
									str(eid), _offh_bot_class(m_veh), m_veh._veh_velocity, _enemy_dist, dist,
									str(getattr(m_veh, '_route_node', None)), str(getattr(m_veh, '_wall_escape', 0))))

							_td = getattr(m_veh, 'typeDescriptor', None) or loaded_models.get('td')

							# PHYSICS PARAMS: same law module as the player, derived ONCE
							# per bot from its real descriptor (the old inline block
							# re-read td.physics for every bot on every tick).
							_bphys = getattr(m_veh, '_phys_params', None)
							if _bphys is None:
								_bphys = _PHY.derive_params(_td)
								m_veh._phys_params = _bphys
							bot_mass = _bphys['mass']
							bot_enginePowerW = _bphys['powerW']
							bot_speedFwd = _bphys['speedFwd']
							bot_speedBwd = _bphys['speedBwd']
							bot_terrainCoeff = _bphys['terrainResist'][0]
							bot_specificFriction = _bphys['specificFriction']
							bot_chassisRotSpd = _bphys['rotSpd']
							
							# VIRTUAL DRIVER
							throttle = 0.0
							turn_dir = 0

							# Bearing to the ENEMY - consumed by the turret aim and the fire gate.
							_raw_target_yaw = math.atan2(_gdx, _gdz)
							_raw_diff_yaw = _raw_target_yaw - m_veh.yaw
							while _raw_diff_yaw > math.pi:  _raw_diff_yaw -= 2*math.pi
							while _raw_diff_yaw < -math.pi: _raw_diff_yaw += 2*math.pi
							# Bearing to where we are DRIVING - consumed by the feelers, the stuck
							# escape and the steering blend. Same as _raw_* whenever the bot is
							# chasing an enemy, different once it is following a route leg.
							_mv_target_yaw = math.atan2(dx, dz)
							_mv_diff_yaw = _mv_target_yaw - m_veh.yaw
							while _mv_diff_yaw > math.pi:  _mv_diff_yaw -= 2*math.pi
							while _mv_diff_yaw < -math.pi: _mv_diff_yaw += 2*math.pi

							# --- STUCK DETECTOR ---
							# Track last position; if not moved >0.5m in 100 ticks (2 sec), force reverse escape
							_last_p = getattr(m_veh, '_last_pos', None)
							_cur_escape = getattr(m_veh, '_wall_escape', None) or 0
							_stuck_ctr = getattr(m_veh, '_stuck_ctr', 0) or 0
							
							if _cur_escape > 0:
								_stuck_ctr = 0
								m_veh._last_pos = (m_veh.position.x, m_veh.position.z)
							else:
								_stuck_ctr += 1
								if _stuck_ctr >= 100:
									if _last_p is not None:
										_moved = math.sqrt((m_veh.position.x-_last_p[0])**2 + (m_veh.position.z-_last_p[1])**2)
										if _moved < 0.5:
											m_veh._wall_escape = 60
											m_veh._wall_turn = 1 if _mv_diff_yaw > 0 else -1
									m_veh._last_pos = (m_veh.position.x, m_veh.position.z)
									_stuck_ctr = 0
							m_veh._stuck_ctr = _stuck_ctr
							
							_escape = getattr(m_veh, '_wall_escape', None) or 0
							if _escape > 0:
								# The escape reverses BLIND: this branch runs no feelers at all, so a
								# bot stuck near a shoreline backs straight into the lake. Three bots
								# drowned at 5 m depth in the pathfinding run while their PATHS avoided
								# water perfectly - reversing is the one place a path has no say.
								# Cancelling hands control back to normal steering, whose feelers do
								# check water. One probe per escaping bot, and only ~5% of bot-ticks
								# are escaping.
								try:
									_rx = m_veh.position.x - math.sin(m_veh.yaw) * 10.0
									_rz = m_veh.position.z - math.cos(m_veh.yaw) * 10.0
									if _offh_water_depth(_rx, m_veh.position.y, _rz) > 0.8:
										m_veh._wall_escape = 0
										_escape = 0
										globals()['g_offh_escape_wet'] = (globals().get('g_offh_escape_wet', 0) or 0) + 1
								except Exception:
									pass
							if _escape > 0:
								m_veh._wall_escape = _escape - 1
								# Reversing escape: drive backwards and turn
								throttle = -0.7
								turn_dir = getattr(m_veh, '_wall_turn', 1)
								diff_yaw = _mv_diff_yaw
								target_yaw = _mv_target_yaw
							else:
								# --- SEPARATION: repulsion from nearby bots ---
								sep_x = 0.0
								sep_z = 0.0
								for _seid, _smeh in mock_vehicles.iteritems():
									if _seid == eid: continue
									_sdx = m_veh.position.x - _smeh.position.x
									_sdz = m_veh.position.z - _smeh.position.z
									_sd = math.sqrt(_sdx*_sdx + _sdz*_sdz)
									if 0.5 < _sd < 12.0:
										_w = (12.0 - _sd) / 12.0
										sep_x += (_sdx / _sd) * _w
										sep_z += (_sdz / _sd) * _w

								# --- ADVANCED MULTI-RAY SENSORS (Local Avoidance) ---
								_feeler_steer_yaw = None
								# Only this frame's phase casts feelers (see the think-rate note at
								# the top of the tick). Skipping leaves _feeler_steer_yaw None,
								# which drops straight into the hysteresis branch below - it already
								# replays the last clear heading for 15 frames, so a skipped bot
								# keeps the heading it chose instead of losing its steering.
								_ai_phases = globals().get('g_offh_ai_phases', 1) or 1
								if _ai_phases <= 1 or (eid % _ai_phases) == globals().get('g_offh_ai_phase', 0):
									_ray_angles = [0.0]
									_step = 0.25
									for i in range(1, 6): # up to 1.25 rad (~71 degrees)
										if _mv_diff_yaw > 0:
											_ray_angles.extend([i * _step, -i * _step])
										else:
											_ray_angles.extend([-i * _step, i * _step])
									
									# Two passes: first try a wide safe margin (2.2m), if boxed in, try a tighter margin (1.6m)
									for _hw in (2.2, 1.6):
										_center_blocked = False
										_best_clear_angle = None
										
										for _fyo in _ray_angles:
											_fy = m_veh.yaw + _fyo
											_hit = False
											
											# Width-aware sensors: Left track, Center, Right track
											_cos_fy = math.cos(_fy)
											_sin_fy = math.sin(_fy)
											
											# Dual-height rays: catch low rocks (0.7m) and tall buildings (1.5m)
											_ray_profiles = [(0.7, 7.0), (1.5, 12.0)]
											
											for _h, _dist in _ray_profiles:
												if _hit: break
												
												# 1. Terrain elevation check (Cliffs and High Hills)
												_dest_x = m_veh.position.x + _sin_fy * _dist
												_dest_z = m_veh.position.z + _cos_fy * _dist
												_dest_y = m_veh.position.y
												
												try:
													_g_hit = BigWorld.wg_collideSegment(_tick_space, 
														Math.Vector3(_dest_x, m_veh.position.y + 4.0, _dest_z), 
														Math.Vector3(_dest_x, m_veh.position.y - 15.0, _dest_z), 128)
													if _g_hit:
														_dest_y = _g_hit[0].y
													else:
														_hit = True # Abyss / out of bounds
												except: pass
												
												if _hit: break
												
												_y_diff = _dest_y - m_veh.position.y
												if _y_diff > _dist * 0.45 or _y_diff < -_dist * 0.7:
													_hit = True
													break
												
												# 1b. WATER. The probe above returns the LAKEBED, so a lake reads as
												# perfectly flat, unobstructed, ideal terrain - which is exactly why
												# bots have always been happy to drive into one and drown. One extra
												# probe per ray, against the four this block already spends.
												try:
													if _offh_water_depth(_dest_x, _dest_y, _dest_z) > 0.8:
														_hit = True
														break
												except Exception: pass
													
												# 2. Obstacle check (parallel to slope)
												for _ox in (-_hw, 0.0, _hw):
													_sx = m_veh.position.x + _cos_fy * _ox
													_sz = m_veh.position.z - _sin_fy * _ox
													try:
														_fs = Math.Vector3(_sx, m_veh.position.y + _h, _sz)
														_fe = Math.Vector3(_sx + _sin_fy*_dist, _dest_y + _h, _sz + _cos_fy*_dist)
														if BigWorld.wg_collideSegment(_tick_space, _fs, _fe, 128):
															_hit = True
															break
													except: pass
											
											if _fyo == 0.0:
												if _hit: 
													_center_blocked = True
												else:
													break # Center is clear
											else:
												if not _hit:
													_best_clear_angle = _fyo
													break
													
										if not _center_blocked:
											break # Center is clear on this margin
										if _best_clear_angle is not None:
											_feeler_steer_yaw = m_veh.yaw + _best_clear_angle
											break # Found a clear path
											
									# If even tight margin fails, we just keep current steering and let stuck detector handle reversing if we crash
								
								# Hysteresis: keep steering into the clear path for a moment
								if _feeler_steer_yaw is not None:
									m_veh._feeler_timer = 15
									m_veh._feeler_mem = _feeler_steer_yaw
								else:
									_ft = getattr(m_veh, '_feeler_timer', 0) or 0
									if _ft > 0:
										m_veh._feeler_timer = _ft - 1
										_feeler_steer_yaw = getattr(m_veh, '_feeler_mem', None)
								
								if _feeler_steer_yaw is not None:
									target_yaw = _feeler_steer_yaw
								else:
									# Blend target dir + separation
									_ndx = dx / dist if dist > 0.1 else 0.0
									_ndz = dz / dist if dist > 0.1 else 0.0
									_rdx = _ndx + sep_x * 1.5
									_rdz = _ndz + sep_z * 1.5
									target_yaw = math.atan2(_rdx, _rdz)
								diff_yaw = target_yaw - m_veh.yaw
								while diff_yaw > math.pi:  diff_yaw -= 2*math.pi
								while diff_yaw < -math.pi: diff_yaw += 2*math.pi

								if dist > 15.0:
									if abs(diff_yaw) < 0.5: throttle = 1.0
									elif abs(diff_yaw) > 2.0: throttle = -0.5
									else: throttle = 0.5

								# Only steer while there is somewhere to go. A bot HOLDING its position
								# has dx=dz=0, which collapses the blend above to atan2(0,0) = 0 - so
								# without this guard every stationary bot slowly rotates to face north.
								if dist > 2.0:
									if diff_yaw > 0.05: turn_dir = 1
									elif diff_yaw < -0.05: turn_dir = -1

							m_veh._dbg_ctr = (getattr(m_veh, '_dbg_ctr', 0) or 0) + 1
							# THE metric for pathfinding: how much of a bot's life is spent in the
							# stuck-escape reverse. Counted exactly rather than sampled off the
							# throttled BOT_AI line, so the flag-on/flag-off comparison is clean.
							globals()['g_offh_bot_ticks'] = (globals().get('g_offh_bot_ticks', 0) or 0) + 1
							if (getattr(m_veh, '_wall_escape', None) or 0) > 0:
								globals()['g_offh_stuck_ticks'] = (globals().get('g_offh_stuck_ticks', 0) or 0) + 1
							
							# IMMOBILIZATION CHECK
							_dev_hp = getattr(m_veh, 'devices_hp', None)
							# is_tracked = locked tracks (handbrake below), a dead engine only coasts.
							_b_locked = bool(getattr(m_veh, 'is_tracked', False))
							if _b_locked or (_dev_hp is not None and _dev_hp.get('engineHealth', 1) <= 0):
								throttle = 0.0
								turn_dir = 0.0
								# The player path zeroed this; the bot path did not, so a tracked bot kept
								# pivoting on its residual angular velocity.
								m_veh._veh_turn_velocity = 0.0
							elif throttle:
								# Same cost the player pays: a downed driver and a DAMAGED engine
								# both eat throttle (destruction is the hard gate above).
								_bmf = _crew_factor(m_veh, 'mobility') * _module_factor(m_veh, 'mobility')
								if _bmf < 1.0:
									throttle = throttle * _bmf
								
							# FIRE LOGIC (Damage Over Time)
							_sync_burn_and_death(m_veh, getattr(m_veh, '_hull_model', None), getattr(m_veh, 'typeDescriptor', None))
							try:
								_tick_module_repair(m_veh, getattr(m_veh, 'typeDescriptor', None), dt, False)
							except Exception: pass
							_sync_engine_exhaust(m_veh, getattr(m_veh, '_hull_model', None), getattr(m_veh, 'typeDescriptor', None), getattr(m_veh, '_veh_velocity', 0.0) or 0.0)
							if getattr(m_veh, 'is_on_fire', False) and m_veh.health > 0:
								try:
									from gui.mods.offhangar import device_damage as _DDf2
									_fs2 = getattr(m_veh, '_fire_started', None)
									if _fs2 is not None and (BigWorld.time() - _fs2) >= _DDf2.FIRE_DURATION_SECONDS:
										_offh_extinguish(m_veh, False, 'burnt out')
								except Exception:
									pass
								cur_timer = getattr(m_veh, '_fire_timer', 0.0)
								if cur_timer is None: cur_timer = 0.0
								m_veh._fire_timer = float(cur_timer) + float(dt if dt is not None else 0.02)
								if m_veh._fire_timer >= 1.0: # Tick every 1 second
									m_veh._fire_timer -= 1.0
									fire_dmg = max(1, int(m_veh.maxHealth * 0.05)) # 5% max HP per sec
									m_veh.health -= fire_dmg
									
									try:
										import BigWorld
										from gui import WindowsManager
										bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
										
										if m_veh.health <= 0:
											m_veh.health = 0
											BigWorld.player().arena.onVehicleKilled(m_veh.id, (getattr(m_veh, 'last_killer_id', None) or -1), 2)
										elif bw and hasattr(bw, 'vMarkersManager'):
											player_id = getattr(BigWorld.player(), 'playerVehicleID', -1)
											if m_veh.id == player_id:
												player = BigWorld.player()
												if hasattr(player, 'vehicle') and player.vehicle:
													player.vehicle.health = m_veh.health
													try: player.guiSessionProvider.invalidateVehicleState(1, player_id, m_veh.health, m_veh.health)
													except: pass
											else:
												marker = getattr(m_veh, 'marker', None)
												if marker is not None:
													bw.vMarkersManager.onVehicleHealthChanged(marker, max(0, m_veh.health), (getattr(m_veh, 'last_killer_id', -1) or -1), 0)
													try:
														bw.vMarkersManager.showVehicleDamageInfo(marker, fire_dmg, 0, 0, 1)
													except:
														pass
													LOG_DEBUG('Fire HP updated via marker, HP=%d' % m_veh.health)
									except: pass

							# Pre-battle countdown: the line-up holds position (like the original)
							if getattr(getattr(player, 'arena', None), 'period', 3) < 3:
								throttle = 0.0
								turn_dir = 0
								m_veh._veh_velocity = 0.0
								m_veh._veh_turn_velocity = 0.0

							# ACCELERATION & MOVEMENT: physics.longitudinal_step - identical
							# law to the player (engine curve, grip-limited brake, coast
							# auto-brake, slope, clamps). Bot throttle is fractional (0.5,
							# -0.5): the law scales engine force by it, like a part-pressed
							# key. Slope probe stays rate-limited (~7x/s per bot).
							bot_gravity = _PHY.GRAVITY
							cur_vel = m_veh._veh_velocity
							if not getattr(m_veh, '_airborne', False) and (throttle != 0 or abs(cur_vel) > 0.01):
								m_veh._dp_acc = (getattr(m_veh, '_dp_acc', 9.0) or 9.0) + dt
								if m_veh._dp_acc >= 0.15:
									m_veh._dp_acc = 0.0
									_braw = _drive_pitch(_tick_space, m_veh.position.x, m_veh.position.z, m_veh.yaw, m_veh.position.y)
									# smooth probe spikes (same reason as the player)
									_bprev = getattr(m_veh, '_dp_v', _braw) or 0.0
									_bd = _braw - _bprev
									if _bd > 0.35: _bd = 0.35
									elif _bd < -0.35: _bd = -0.35
									m_veh._dp_v = _bprev + _bd * 0.6
							m_veh._veh_velocity = _PHY.longitudinal_step(
								_bphys, cur_vel, throttle, turn_dir != 0,
								getattr(m_veh, '_dp_v', 0.0) or 0.0, dt,
								getattr(m_veh, '_airborne', False), 0, _b_locked)
							
							try:
								# FMOD channel budget: bots beyond earshot must not hold
								# engine+track events. Every bot kept 2 events alive for the
								# whole battle; with 30+ bots the pool ran dry mid-battle, so
								# NEW events - crew voices included - failed to create
								# ('Failed to load sound .../notifications_VO/...') and the
								# native attach path then crashed on the null handle
								# (ACCESS_VIOLATION read 0xC). 130 m release / 115 m
								# re-create hysteresis; sounds come back on approach.
								_sdx2 = m_veh.position.x - veh_pos[0]
								_sdz2 = m_veh.position.z - veh_pos[2]
								_sd2v = _sdx2 * _sdx2 + _sdz2 * _sdz2
								# Two gates now: the absolute audibility ring AND the headcount. Without
								# the second one the loops alone spend the whole 64-channel budget the
								# moment the teams converge.
								_snd_keep = globals().get('g_offh_snd_keep')
								if _sd2v > 16900.0 or not ((_snd_keep is None) or (eid in _snd_keep)):
									if getattr(m_veh, '_snd_engine', None) is not None or getattr(m_veh, '_snd_tracks', None) is not None:
										for _sa2 in ('_snd_engine', '_snd_tracks'):
											_so2 = getattr(m_veh, _sa2, None)
											if _so2 is not None:
												try: _so2.stop()
												except Exception: pass
											setattr(m_veh, _sa2, None)
										m_veh._p_load = None
										m_veh._p_spd = None
									m_veh._snd_init = False
								elif not getattr(m_veh, '_snd_init', False) and _sd2v < 13225.0 and getattr(m_veh, 'isAlive', False):
									# isAlive gate: without it the range culling re-created
									# engine sounds on WRECKS when the player drove back near
									# (death stops them once; culling resets _snd_init).
									_engine_d = getattr(_td, 'engine', None) if _td else None
									_chassis_d = getattr(_td, 'chassis', None) if _td else None
									if hasattr(_td, 'engine') and isinstance(_td.engine, dict): _engine_d = _td.engine
									if hasattr(_td, 'chassis') and isinstance(_td.chassis, dict): _chassis_d = _td.chassis
									if _engine_d and hasattr(m_veh, '_chassis_model') and getattr(m_veh._chassis_model, 'inWorld', False):
										m_veh._snd_engine = m_veh._chassis_model.playSound(_engine_d['sound'])
									if _chassis_d and hasattr(m_veh, '_chassis_model') and getattr(m_veh._chassis_model, 'inWorld', False):
										m_veh._snd_tracks = m_veh._chassis_model.playSound(_chassis_d['sound'])
									# Latch only when the events really exist: a None from playSound (FMOD
									# pool dry) used to count as attached, so that bot stayed mute until it
									# left the 130 m ring and came back.
									if getattr(m_veh, '_snd_engine', None) is not None and getattr(m_veh, '_snd_tracks', None) is not None:
										m_veh._snd_init = True
									if m_veh._snd_init and getattr(m_veh, '_snd_tracks', None):
										# VehicleAppearance zeroes these for all non-player vehicles;
										# left at event defaults they can add wrong terrain flavour.
										for _pn in ('ground', 'stone', 'wood', 'snow', 'sand', 'water', 'hardness', 'friction', 'roughness', 'flying'):
											try:
												_pp = m_veh._snd_tracks.param(_pn)
												if _pp is not None: _pp.value = 0.0
											except Exception: pass
								
								cur_speed = abs(m_veh._veh_velocity)
								# Continuous load blend (see player path: discrete values retrigger FMOD)
								power_fraction = min(1.0, (cur_speed / bot_speedFwd) + (abs(throttle) * 0.3))
								load = 1.0 + (power_fraction * 2.0)
								
								if getattr(m_veh, '_snd_engine', None):
									p = getattr(m_veh, '_p_load', None)
									if p is None:
										p = m_veh._snd_engine.param('load')  # resolve once per bot
										m_veh._p_load = p
									if p: p.value = load
								if getattr(m_veh, '_snd_tracks', None):
									p = getattr(m_veh, '_p_spd', None)
									if p is None:
										p = m_veh._snd_tracks.param('speed')
										m_veh._p_spd = p
									if p: p.value = cur_speed / bot_speedFwd
							except Exception as _e: pass
							
							# COLLISION - always checked when moving
							if m_veh._veh_velocity != 0.0:
								_hit_wall = False
								m_veh._cw_fc = (getattr(m_veh, '_cw_fc', 0) or 0) + 1
								if abs(m_veh._veh_velocity) > 0.5:
									try:
										_fell_trees_near(_tick_space, m_veh.position, m_veh.yaw, m_veh._veh_velocity, _td)
									except: pass
								# perf: wall scan alternates frames per bot (<0.5 m travel between checks)
								if abs(m_veh._veh_velocity) > 0.5 and ((m_veh._cw_fc + eid) & 1) == 0:
									try:
										_hit_wall = _check_horizontal_collision(_tick_space, m_veh.position, m_veh.yaw, m_veh._veh_velocity, _td, getattr(m_veh, '_airborne', False), dt)
									except: pass
								_bnx = m_veh.position.x + math.sin(m_veh.yaw) * m_veh._veh_velocity * dt
								_bnz = m_veh.position.z + math.cos(m_veh.yaw) * m_veh._veh_velocity * dt
								if _hit_wall:
									# Airborne: a wall must not brake the fall - keep momentum,
									# just don't advance into it. Grounded: bleed forward drive.
									if not getattr(m_veh, '_airborne', False):
										m_veh._veh_velocity *= 0.2
								else:
									m_veh.position = Math.Vector3(_bnx, m_veh.position.y, _bnz)
								# Tank-vs-tank: velocity-relative impulse (e=0) + Baumgarte push-apart
								try:
									_bsvx = math.sin(m_veh.yaw) * m_veh._veh_velocity + (getattr(m_veh, '_push_x', 0.0) or 0.0)
									_bsvz = math.cos(m_veh.yaw) * m_veh._veh_velocity + (getattr(m_veh, '_push_z', 0.0) or 0.0)
									_btr = _tank_resolve(eid, m_veh.position.x, m_veh.position.z, m_veh.yaw, _td, 1.0 / max(bot_mass, 1.0), _bsvx, _bsvz, m_veh.position.y)
									# Forward impulse share hits the bot's drive speed too (see player)
									_bfimp = _btr[2] * math.sin(m_veh.yaw) + _btr[3] * math.cos(m_veh.yaw)
									_bfabs = 0.0
									if _bfimp * m_veh._veh_velocity < 0.0:
										_bfabs = -m_veh._veh_velocity if abs(_bfimp) >= abs(m_veh._veh_velocity) else _bfimp
										m_veh._veh_velocity += _bfabs
									_bpx = (getattr(m_veh, '_push_x', 0.0) or 0.0) + _btr[2] - _bfabs * math.sin(m_veh.yaw)
									_bpz = (getattr(m_veh, '_push_z', 0.0) or 0.0) + _btr[3] - _bfabs * math.cos(m_veh.yaw)
									m_veh.position = Math.Vector3(m_veh.position.x + _btr[0] + _bpx * dt, m_veh.position.y, m_veh.position.z + _btr[1] + _bpz * dt)
									m_veh._push_x = _bpx * 0.90
									m_veh._push_z = _bpz * 0.90
								except: pass
							
							# ROTATION: physics.traverse_step (same law as the player)
							m_veh._veh_turn_velocity = _PHY.traverse_step(_bphys, m_veh._veh_turn_velocity, turn_dir, m_veh._veh_velocity, dt)
							try:
								_btf = _module_factor(m_veh, 'traverse')
								if _btf < 1.0:
									m_veh._veh_turn_velocity = m_veh._veh_turn_velocity * _btf
							except Exception: pass
							
							if m_veh._veh_turn_velocity != 0.0:
								m_veh.yaw += m_veh._veh_turn_velocity * dt
								while m_veh.yaw > math.pi: m_veh.yaw -= 2*math.pi
								while m_veh.yaw < -math.pi: m_veh.yaw += 2*math.pi
							
							# TERRAIN SNAP (ray starts just above the hull so bridges overhead are ignored)
							try:
								# Highest ground under the fore-aft footprint (same law as the player)
								_bhl = 2.5
								try:
									if _td is not None and hasattr(_td, 'hull') and 'hitTester' in _td.hull:
										_bhl = max(1.5, abs(_td.hull['hitTester'].bbox[1][2]))
								except Exception:
									pass
								_bsup = _terrain_support(_tick_space, m_veh.position.x, m_veh.position.y, m_veh.position.z, m_veh.yaw, _bhl)
								_bc_y = _bsup[1]        # ground under the hull centre (chassis origin)
								_bg_y = _bc_y if _bc_y is not None else _bsup[0]  # rest on centre, not float
								if _bg_y is not None:
									_b_snap = max(0.8, min(2.5, abs(m_veh._veh_velocity) * dt * 2.0 + 0.6))
									_b_climb = max(0.6, abs(m_veh._veh_velocity) * dt * 2.5)
									_bcom_gap = _b_snap if _bc_y is None else (m_veh.position.y - _bc_y)
									_bland_y = _bg_y if _bc_y is None else _bc_y
									if _bc_y is not None and m_veh.position.y < _bc_y and (_bc_y - m_veh.position.y) > _b_climb:
										m_veh.position = Math.Vector3(m_veh.position.x, _bc_y, m_veh.position.z)
									elif m_veh.position.y <= _bg_y or (_bcom_gap <= _b_snap and not getattr(m_veh, '_airborne', False)):
										# Soft ground-follow: below snaps up hard, above eases down (cap 0.12 m)
										if m_veh.position.y < _bg_y:
											_brise = _bg_y - m_veh.position.y
											_bfy = m_veh.position.y + (_brise if _brise <= _b_climb else _b_climb)
										else:
											_bfy = m_veh.position.y + (_bg_y - m_veh.position.y) * min(1.0, dt * 15.0)
											if _bfy > _bg_y + 0.12:
												_bfy = _bg_y + 0.12
										m_veh.position = Math.Vector3(m_veh.position.x, _bfy, m_veh.position.z)
										m_veh._vert_vel = 0.0
										m_veh._airborne = False
									else:
										m_veh._airborne = True
										_bvv = (getattr(m_veh, '_vert_vel', 0.0) or 0.0)
										_bfall_n = 1
										if abs(_bvv * dt) > 0.5:
											_bfall_n = min(8, int(abs(_bvv * dt) / 0.5) + 1)
										_bfall_sdt = dt / _bfall_n
										_by = m_veh.position.y
										_bfall_i = 0
										while _bfall_i < _bfall_n:
											_bvv -= bot_gravity * _bfall_sdt
											_by += _bvv * _bfall_sdt
											if _bland_y is not None and _by <= _bland_y:
												_by = _bland_y
												_bvv = 0.0
												m_veh._airborne = False
												break
											_bfall_i += 1
										m_veh._vert_vel = _bvv
										m_veh.position = Math.Vector3(m_veh.position.x, _by, m_veh.position.z)
							except: pass
							
							# perf: 4-ray tilt sampling alternates frames per bot; yaw stays live,
							# the pitch/roll smoothing below hides the halved sample rate
							m_veh._ypr_fc = (getattr(m_veh, '_ypr_fc', 0) or 0) + 1
							if getattr(m_veh, '_ypr_c', None) is None or ((m_veh._ypr_fc + eid) & (1 if getattr(m_veh, '_spot_visible', True) else 3)) == 0:
								m_veh._ypr_c = _get_terrain_ypr(_tick_space, m_veh.position, m_veh.yaw)
							_b_ypr = (m_veh.yaw, m_veh._ypr_c[1], m_veh._ypr_c[2], m_veh._ypr_c[3], m_veh._ypr_c[4], m_veh._ypr_c[5])
							# --- Slope slide (bot): same WG law + cross-heading projection as player ---
							_bss = getattr(m_veh, '_slide_spd', 0.0) or 0.0
							if getattr(m_veh, '_airborne', False):
								_bss = 0.0   # airborne = pure ballistic fall, no slide
							else:
								_bss = _PHY.slope_slide_speed(_bss, _b_ypr[5], dt)
							m_veh._slide_spd = _bss
							_bcross_x = math.cos(m_veh.yaw); _bcross_z = -math.sin(m_veh.yaw)
							_bsl_dot = _b_ypr[3] * _bcross_x + _b_ypr[4] * _bcross_z
							_bsl_dx = _bcross_x * _bsl_dot; _bsl_dz = _bcross_z * _bsl_dot
							if getattr(m_veh, '_airborne', False):
								# carry the frozen lateral drift through the fall (see player)
								_balx = getattr(m_veh, '_air_lat_vx', 0.0) or 0.0
								_balz = getattr(m_veh, '_air_lat_vz', 0.0) or 0.0
								if abs(_balx) > 1e-04 or abs(_balz) > 1e-04:
									m_veh.position = Math.Vector3(m_veh.position.x + _balx * dt, m_veh.position.y, m_veh.position.z + _balz * dt)
									m_veh._air_lat_vx = _balx * 0.995
									m_veh._air_lat_vz = _balz * 0.995
							else:
								m_veh._air_lat_vx = _bsl_dx * _bss
								m_veh._air_lat_vz = _bsl_dz * _bss
							if not getattr(m_veh, '_airborne', False) and _bss > 0.01 and (abs(_bsl_dx) > 1e-04 or abs(_bsl_dz) > 1e-04):
								_slb_x = m_veh.position.x + _bsl_dx * _bss * dt
								_slb_z = m_veh.position.z + _bsl_dz * _bss * dt
								try:
									_slb_c = BigWorld.wg_collideSegment(_tick_space, Math.Vector3(_slb_x, m_veh.position.y + 8.0, _slb_z), Math.Vector3(_slb_x, m_veh.position.y - 30.0, _slb_z), 128)
								except Exception:
									_slb_c = None
								if _slb_c is not None and (m_veh.position.y - _slb_c[0].y) < 4.0:
									m_veh.position = Math.Vector3(_slb_x, _slb_c[0].y, _slb_z)
									m_veh._vert_vel = 0.0
									m_veh._airborne = False
							# Smooth pitch/roll so bots don't jitter on rough terrain
							_b_blend = min(1.0, dt * 8.0)
							_b_p0 = getattr(m_veh, 'pitch', 0.0) or 0.0
							_b_r0 = getattr(m_veh, 'roll', 0.0) or 0.0
							m_veh.pitch = _b_p0 + (_b_ypr[1] - _b_p0) * _b_blend
							m_veh.roll = _b_r0 + (_b_ypr[2] - _b_r0) * _b_blend
							_b_ypr = (_b_ypr[0], m_veh.pitch, m_veh.roll)
							
							m_veh.matrix.setRotateYPR(_b_ypr)
							m_veh.matrix.translation = m_veh.position
							# --- Spotting: unspotted ENEMY tanks are hidden like the real game.
							# Simulation keeps running; only rendering/markers/minimap are culled.
							try:
								_sen = globals().get('g_offh_spotting')
								if _sen is None:
									try:
										from _constants import CONFIG_OPTIONS as _SCFG
										_sen = bool(_SCFG.get('spotting_enabled', True))
									except Exception:
										_sen = True
									globals()['g_offh_spotting'] = _sen
								if _sen and getattr(m_veh, 'isAlive', True) and getattr(m_veh, '_bot_team', 2) != (_p_team or 1):
									m_veh._spot_chk = (getattr(m_veh, '_spot_chk', 9.0) or 9.0) + dt
									if m_veh._spot_chk >= 0.5:
										m_veh._spot_chk = (eid % 10) * 0.05  # stagger re-checks across bots
										_svr = globals().get('g_offh_viewrange', 0.0)
										if not _svr:
											try:
												_svr = float(loaded_models['td'].turret.get('circularVisionRadius', 400.0))
											except Exception:
												_svr = 400.0
											globals()['g_offh_viewrange'] = _svr
										# Damaged optics and a downed commander/radioman cut the range.
										# Only the BASE radius stays cached; the factors are read on every
										# check, so view range follows the crew and the module state
										# instead of freezing at what the tank was worth on spawn.
										try:
											_pm_vis = mock_vehicles.get(_p_vid)
											if _pm_vis is not None:
												from gui.mods.offhangar import device_damage as _DDv
												_svr = _svr * _DDv.clamp_vision_factor(
													_crew_factor(_pm_vis, 'vision') * _module_factor(_pm_vis, 'vision'))
										except Exception:
											pass
										_sdx = m_veh.position.x - veh_pos[0]
										_sdz = m_veh.position.z - veh_pos[2]
										_sd2 = _sdx * _sdx + _sdz * _sdz
										_seen = False
										if _sd2 <= 2500.0:
											_seen = True  # 50 m proximity spot
										elif _sd2 <= _svr * _svr:
											_slos = BigWorld.wg_collideSegment(_tick_space, Math.Vector3(veh_pos[0], veh_pos[1] + 2.5, veh_pos[2]), Math.Vector3(m_veh.position.x, m_veh.position.y + 1.5, m_veh.position.z), 128)
											_seen = _slos is None
											if not _seen:
												# Second sample at turret height: a single mid-hull
												# ray grazing a crest could keep a plainly exposed
												# (and firing) tank unspotted; real spotting checks
												# several points on the target.
												_slos = BigWorld.wg_collideSegment(_tick_space, Math.Vector3(veh_pos[0], veh_pos[1] + 2.5, veh_pos[2]), Math.Vector3(m_veh.position.x, m_veh.position.y + 2.2, m_veh.position.z), 128)
												_seen = _slos is None
										if not _seen:
											# Team vision: living allied bots relay spots to the player (radio).
											# Cheap distance pass over all allies, then ONE ray to the nearest.
											_tvb = None
											_tvd = 1e18
											_tpid = _p_vid
											# The relay runs over the RADIO, so an ally outside comms range
											# reports nothing. A damaged set shortens the range, a destroyed
											# one shortens it further (device_damage 'signal'). Without a
											# radio distance on the descriptor the gate stays open.
											# ONLY gate when the radio is actually hurt. Gating on an intact
											# set made every ally beyond the nominal signal range stop
											# relaying, which on a big map silently removed most of the
											# team vision the player had before - and that reads as the
											# old "enemies are invisible" bug, not as a radio mechanic.
											_radio_r2 = None
											try:
												_pm_rad = mock_vehicles.get(_tpid)
												_sig = _module_factor(_pm_rad, 'signal')
												if _sig < 1.0:
													_rd = float(loaded_models['td'].radio.get('distance', 0.0) or 0.0)
													if _rd > 0.0:
														_rd = _rd * _sig
														_radio_r2 = _rd * _rd
											except Exception:
												_radio_r2 = None
											for _tvm in (globals().get('G_MOCK_VEHICLES', {}) or {}).values():
												if _tvm is m_veh or getattr(_tvm, 'id', -1) == _tpid:
													continue
												if not getattr(_tvm, 'isAlive', True):
													continue
												if (getattr(_tvm, '_bot_team', 2) or 2) != (_p_team or 1):
													continue
												if _radio_r2 is not None:
													_rdx = _tvm.position.x - veh_pos[0]
													_rdz = _tvm.position.z - veh_pos[2]
													if (_rdx * _rdx + _rdz * _rdz) > _radio_r2:
														continue      # out of radio range: no relay
												_tdx = m_veh.position.x - _tvm.position.x
												_tdz = m_veh.position.z - _tvm.position.z
												_td2 = _tdx * _tdx + _tdz * _tdz
												if _td2 <= 2500.0:
													_seen = True  # 50 m proximity spot by an ally
													_tvb = None
													break
												if _td2 <= _svr * _svr and _td2 < _tvd:
													_tvd = _td2
													_tvb = _tvm
											if (not _seen) and _tvb is not None:
												_tlos = BigWorld.wg_collideSegment(_tick_space, Math.Vector3(_tvb.position.x, _tvb.position.y + 2.5, _tvb.position.z), Math.Vector3(m_veh.position.x, m_veh.position.y + 1.5, m_veh.position.z), 128)
												_seen = _tlos is None
										if _seen:
											m_veh._spot_until = BigWorld.time() + 5.0  # spot memory
										# Re-apply the model state on every check (idempotent): a
										# show that failed or raced the async model load left the
										# bot invisible-while-spotted FOREVER (the change-only flip
										# below never re-fires once the flag matches) - the
										# 'invisible tank keeps firing until destroyed' report.
										_schk = getattr(m_veh, '_chassis_model', None)
										if _schk is not None:
											# spot memory alone kept DEAD bots marked and shown - gate on alive too
											_svchk = BigWorld.time() < (getattr(m_veh, '_spot_until', 0.0) or 0.0) and (getattr(m_veh, 'health', 0) or 0) > 0
											try:
												_schk.visible = _svchk
												_schk.visibleAttachments = _svchk
											except Exception:
												pass
											# The MARKER has to follow the model. Hiding an unspotted bot but leaving its
											# marker up is what the 'invisible tank' screenshots actually show: name, HP
											# bar and direction arrow floating over empty ground. Retail shows nothing at
											# all for an unspotted vehicle, because the marker only exists while it is
											# spotted. Create and destroy it alongside the model - both branches are
											# idempotent, so this cannot churn on every check.
											try:
												from gui import WindowsManager as _spwm
												_spbw = getattr(_spwm.g_windowsManager, 'battleWindow', None)
												_spvm = getattr(_spbw, 'vMarkersManager', None) if _spbw is not None else None
												_spmk = getattr(m_veh, 'marker', None)
												if _spvm is not None:
													if _svchk and _spmk in (None, -1) and getattr(m_veh, 'proxy', None) is not None:
														m_veh.marker = _spvm.createMarker(m_veh.proxy)
														_offh_sync_marker_health(m_veh, _p_vid)
													elif (not _svchk) and _spmk not in (None, -1):
														_spvm.destroyMarker(_spmk)
														m_veh.marker = None
											except Exception as _spe:
												LOG_DEBUG('spot marker sync err:', str(_spe))
											# The flags are provably right - 137 flips in one battle, not a single
											# want/got mismatch - and tanks still vanish. A model can carry
											# visible=True and draw nothing in two ways this probe never covered: it
											# is not in the world at all, or it sits somewhere other than the mock the
											# marker follows. Measure both; put it back when it fell out.
											if _svchk:
												try:
													if not getattr(_schk, 'inWorld', True):
														# READ-ONLY on purpose. Putting the model back crashed the client twice:
														# through _add_model (1.2.8) and through the entity (1.2.9), the latter
														# at the next onArenaCreated while the previous space was released. The
														# engine took this model out of the world and owns that decision; a
														# re-attach leaves a dangling reference that kills the space teardown.
														# Ask WHY it went instead, once per vehicle, and never write anything.
														if not getattr(m_veh, '_dbg_oow', False):
															m_veh._dbg_oow = True
															_ent_rw = getattr(m_veh, 'bw_entity', None)
															_emd_same = '?'
															try:
																if _ent_rw is not None:
																	_emd_same = getattr(_ent_rw, 'model', None) is _schk
															except Exception:
																pass
															LOG_DEBUG('VIS OUTOFWORLD id=%s ent=%s entHoldsIt=%s hp=%s wreck=%s dead=%s' % (
																eid, _ent_rw is not None, _emd_same, getattr(m_veh, 'health', '?'),
																getattr(m_veh, '_wreck_done', False), not getattr(m_veh, 'isAlive', True)))
													else:
														_mp = _schk.position
														_dxz = ((_mp.x - m_veh.position.x) ** 2 + (_mp.z - m_veh.position.z) ** 2) ** 0.5
														if _dxz > 25.0 and not getattr(m_veh, '_dbg_drift', False):
															m_veh._dbg_drift = True
															LOG_DEBUG('VIS DRIFT id=%s dist=%.1f model=(%.0f,%.0f) mock=(%.0f,%.0f)' % (
																eid, _dxz, _mp.x, _mp.z, m_veh.position.x, m_veh.position.z))
												except Exception:
													pass
											# The MARKER needs the same idempotent treatment as the model above. Its
											# state was only ever touched on a CHANGE of _spot_visible, so one failed
											# createMarker/destroyMarker left the two permanently out of step - and the
											# flag was already flipped, so nothing ever retried. An icon with no tank is
											# exactly that: the model went hidden while destroyMarker did not take. Same
											# bug the comment above describes for the model, one level up.
											try:
												from gui import WindowsManager as _WMk
												_bwk = getattr(_WMk.g_windowsManager, 'battleWindow', None)
												_vmk = getattr(_bwk, 'vMarkersManager', None) if _bwk is not None else None
												if _vmk is not None:
													_mk_now = getattr(m_veh, 'marker', None)
													_mk_has = _mk_now not in (None, -1)
													if _svchk and not _mk_has:
														try:
															m_veh.marker = _vmk.createMarker(m_veh.proxy)
															_offh_sync_marker_health(m_veh, _p_vid)
														except Exception: m_veh.marker = None
													elif (not _svchk) and _mk_has:
														try: _vmk.destroyMarker(_mk_now)
														except Exception: pass
														m_veh.marker = None
											except Exception:
												pass
											# Report every visibility FLIP with the full state, so an invisible tank
											# can be traced instead of guessed at: is it the spot timer, the model, or
											# the marker that disagrees? Also reads back what the engine actually
											# stored - a write that silently did not take shows up as a mismatch.
											try:
												_vprev = getattr(m_veh, '_dbg_vis', None)
												if _vprev != _svchk:
													m_veh._dbg_vis = _svchk
													LOG_DEBUG('VIS id=%s want=%s got=%s spotUntil=%.1f now=%.1f hp=%s marker=%s' % (
														eid, _svchk, getattr(_schk, "visible", "?"),
														(getattr(m_veh, '_spot_until', 0.0) or 0.0), BigWorld.time(),
														getattr(m_veh, 'health', '?'), getattr(m_veh, 'marker', None)))
											except Exception:
												pass
									_svis = BigWorld.time() < ((getattr(m_veh, '_spot_until', 0.0) or 0.0)) and (getattr(m_veh, 'health', 0) or 0) > 0
									if _svis != getattr(m_veh, '_spot_visible', True):
										m_veh._spot_visible = _svis
										_sch = getattr(m_veh, '_chassis_model', None)
										if _sch is not None:
											try:
												_sch.visible = _svis
												_sch.visibleAttachments = _svis
											except Exception:
												pass
										try:
											from gui import WindowsManager as _WMs
											_bws = getattr(_WMs.g_windowsManager, 'battleWindow', None)
											if _bws is not None:
												_vmm = getattr(_bws, 'vMarkersManager', None)
												if _vmm is not None:
													if _svis:
														if getattr(m_veh, 'marker', None) in (None, -1):
															m_veh.marker = _vmm.createMarker(m_veh.proxy)
															_offh_sync_marker_health(m_veh, _p_vid)
													else:
														if getattr(m_veh, 'marker', None) not in (None, -1):
															try:
																_vmm.destroyMarker(m_veh.marker)
															except Exception:
																pass
															m_veh.marker = None
												_smm = getattr(_bws, 'minimap', None)
												if _smm is not None:
													if _svis:
														_smm.notifyVehicleStart(eid)
													else:
														_smm.notifyVehicleStop(eid)
										except Exception:
											pass
							except Exception:
								pass
							# Track scroll (bot): y=left, z=right, traverse via turn rate
							try:
								_bfa = getattr(m_veh, '_fashion', None)
								if _bfa is not None:
									# physics.track_scroll: same law + clamp as the player feed
									_btls, _btrs = _PHY.track_scroll(_bphys, m_veh._veh_velocity, m_veh._veh_turn_velocity)
									_bfa.movementInfo = Math.Vector4(0.0, _btls, _btrs, 0.0)
							except Exception: pass
							# Allies never pass through the spotting block below - they count as always
							# visible, so their model is set once at spawn and never touched again. That
							# leaves them without the idempotent re-apply enemies get every tick: an ally
							# whose model ends up hidden for any reason stays hidden for the whole battle,
							# which matches the reports - single tanks, permanently, no trigger. Enemies
							# are left alone here; the spotting block owns them.
							try:
								if (getattr(m_veh, '_bot_team', 2) or 2) == (_p_team or 1):
									_avm = getattr(m_veh, '_chassis_model', None)
									_aal = (getattr(m_veh, 'health', 0) or 0) > 0
									if _avm is not None and _aal and not getattr(_avm, "visible", True):
										_avm.visible = True
										_avm.visibleAttachments = True
										LOG_DEBUG('VIS ALLY RESTORED id=%s' % eid)
							except Exception:
								pass
							# Drowning: same rules as the player - 1.6 m of water over the hull, 10 s,
							# probed ~3x/s per bot for perf. While submerged the bot is _offh_drowning,
							# which freezes its turret and stops it shooting further down, exactly as the
							# player's crew stops working the gun.
							try:
								# NOT getattr(..., 0.0) + dt. _MockVeh defines __getattr__ returning None for
								# every unknown attribute, so it never raises AttributeError and getattr NEVER
								# falls back to the default - it hands back None. None + dt raised TypeError on
								# the FIRST line of this block, every tick, for every bot, straight into the
								# bare except below. That is why no bot ever drowned and why not even the
								# diagnostics printed. The player path is unaffected: PlayerAccount raises
								# properly, so its identical-looking line works.
								m_veh._dwn_chk = (getattr(m_veh, '_dwn_chk', 0.0) or 0.0) + dt
								if m_veh._dwn_chk >= 0.3:
									_bdel = min(m_veh._dwn_chk, 0.5)
									m_veh._dwn_chk = 0.0
									_bdepth = _offh_water_depth(m_veh.position.x, m_veh.position.y, m_veh.position.z)
									_bwd = (20.0 - _bdepth) if _bdepth >= 0.0 else -1.0   # kept for the log below
									m_veh._offh_drowning = (_bdepth > 1.6)
									# One-shot state capture the FIRST time a bot is in real water. Three
									# bots drown per battle even though the nav paths avoid water entirely
									# and the escape water-guard never fires, so the entry mode is still
									# unknown - this records exactly what the bot was doing when it got wet
									# rather than leaving it to speculation.
									if _bdepth > 1.6 and not getattr(m_veh, '_wet_diag', False):
										m_veh._wet_diag = True
										try:
											_wp = getattr(m_veh, '_nav_wp', None)
											_wnd = getattr(m_veh, '_nav_dest', None)
											_wn = getattr(m_veh, '_route_node', None)
											_wd0 = -1.0
											if _wp:
												_wd0 = math.sqrt((_wp[0][0]-m_veh.position.x)**2 + (_wp[0][1]-m_veh.position.z)**2)
											LOG_DEBUG('WET ENTRY: bot=%s depth=%.2f pos=(%.0f,%.0f) escape=%s vel=%.1f '
												'node=%s waypoints=%s next_wp=%.0fm dest=%s'
												% (eid, _bdepth, m_veh.position.x, m_veh.position.z,
												   str(getattr(m_veh, '_wall_escape', None) or 0),
												   getattr(m_veh, '_veh_velocity', 0.0) or 0.0, str(_wn),
												   str(len(_wp) if _wp else 0), _wd0,
												   str(tuple(round(v,0) for v in _wnd) if _wnd else None)))
										except Exception as _we:
											LOG_DEBUG('WET ENTRY log err:', str(_we))
									# Diagnostic: report the FIRST time each bot touches water at all, plus how
									# deep. Drowning needs 10 s continuously past 1.6 m (same as the player and
									# as retail), so a bot merely fording a river never dies - this tells us
									# whether they reach water in the first place.
									# Across a full Slough round not one bot ever logged BOT IN WATER, so before
									# blaming the AI for staying dry, report what wg_collideWater returns for the
									# bot NEAREST the player, once a second. A steady None/-1 means the probe
									# itself is the problem, not where the bots drive.
									try:
										_wt = globals().get('g_offh_water_dbg_t', 0.0) or 0.0
										_wnow = BigWorld.time()
										if _wnow - _wt > 1.0:
											globals()['g_offh_water_dbg_t'] = _wnow
											LOG_DEBUG('WATER PROBE: bot=%s y=%.1f raw=%s depth=%s' % (eid, m_veh.position.y, _bwd, ('%.2f' % (20.0 - _bwd)) if (_bwd is not None and _bwd >= 0.0) else 'n/a'))
									except Exception:
										pass
									if _bwd is not None and _bwd >= 0.0 and (20.0 - _bwd) > 0.2:
										if not getattr(m_veh, '_wet_logged', False):
											m_veh._wet_logged = True
											LOG_DEBUG('BOT IN WATER: id=%s depth=%.2f m' % (eid, 20.0 - _bwd))
									if _bdepth > 1.6:
										m_veh._drown_t = (getattr(m_veh, '_drown_t', 0.0) or 0.0) + _bdel
										if int(m_veh._drown_t) != int(m_veh._drown_t - _bdel):
											LOG_DEBUG('BOT DROWNING: id=%s t=%.1f/10 s depth=%.2f' % (eid, m_veh._drown_t, _bdepth))
										if m_veh._drown_t > 10.0 and (getattr(m_veh, 'health', 1) or 0) > 0:
											# The hull is untouched, the crew drowns - so the bot keeps the HP it had
											# when it went under for display purposes, like the player does.
											m_veh._hp_display = getattr(m_veh, 'health', 0) or 0
											m_veh.health = 0
											m_veh._drowned = True
											_offh_set_alive(m_veh, False)
											m_veh._offh_drowning = False
											_offh_knock_out_everything(m_veh, False)
											LOG_DEBUG('BOT DROWNED: id=%s' % eid)
											try: player.arena.onVehicleKilled(eid, -1, 5)
											except Exception: pass
									else:
										m_veh._drown_t = 0.0
							except Exception: pass
							
							try:
								if getattr(m_veh, '_spot_visible', True) and getattr(m_veh, 'bw_entity', None) is not None and getattr(m_veh.bw_entity, 'filter', None) is not None:
									m_veh.bw_entity.filter.set(BigWorld.time(), _tick_space, m_veh.bw_entity.id, m_veh.position, (m_veh.matrix.roll, m_veh.matrix.pitch, m_veh.matrix.yaw), 0)
							except: pass
							
							if hasattr(m_veh, '_chassis_model'):
								if not getattr(m_veh, '_servo_added', False):
									try:
										m_veh._chassis_model.addMotor(BigWorld.Servo(m_veh.matrix))
										m_veh._servo_added = True
									except: pass
									
							# Otaceni veze nezavisle
							if hasattr(m_veh, '_t_mat'):
								# Věž by měla vždy mířit na hráče (cíl), nezávisle na tom, kam se vyhýbá trup
								t_yaw = _raw_target_yaw - m_veh.yaw
								while t_yaw > math.pi: t_yaw -= 2*math.pi
								while t_yaw < -math.pi: t_yaw += 2*math.pi
								
								# Načíst limity otáčení věže/děla z dat vozidla (pro TD a arty)
								bot_gun_min_yaw = -math.pi
								bot_gun_max_yaw =  math.pi
								try:
									if _td:
										yl = None
										if hasattr(_td, 'gun') and isinstance(_td.gun, dict):
											yl = _td.gun.get('turretYawLimits', None)
										if yl is None and hasattr(_td, 'turret') and isinstance(_td.turret, dict):
											yl = _td.turret.get('yawLimits', None)
										if yl is not None:
											bot_gun_min_yaw = float(yl[0])
											bot_gun_max_yaw = float(yl[1])
											# Konverze stupňů -> radiány (hodnoty > 10 jsou ve stupních)
											if abs(bot_gun_min_yaw) > 10.0 or abs(bot_gun_max_yaw) > 10.0:
												bot_gun_min_yaw = math.radians(bot_gun_min_yaw)
												bot_gun_max_yaw = math.radians(bot_gun_max_yaw)
								except: pass
								
								has_limited_traverse = not (bot_gun_min_yaw <= -math.pi + 0.1 and bot_gun_max_yaw >= math.pi - 0.1)
								
								# TD nesmí přebíjet řízení trupu kvůli míření, pokud se právě vyhýbá překážce!
								is_avoiding_obstacle = getattr(m_veh, '_feeler_timer', 0) > 0 or (_feeler_steer_yaw is not None if '_feeler_steer_yaw' in locals() else False)
								
								if has_limited_traverse and not is_avoiding_obstacle:
									# TD/Arty: pokud je cíl mimo limity, bot musí otočit celý trup
									if t_yaw < bot_gun_min_yaw - 0.05:
										# Cíl vlevo od limitu – otočit trup doleva
										m_veh._veh_turn_velocity = -bot_chassisRotSpd
									elif t_yaw > bot_gun_max_yaw + 0.05:
										# Cíl vpravo od limitu – otočit trup doprava
										m_veh._veh_turn_velocity = bot_chassisRotSpd
									
								# Omezit věž na limity vždy
								if has_limited_traverse:
									t_yaw = max(bot_gun_min_yaw, min(bot_gun_max_yaw, t_yaw))
								
								if getattr(m_veh, '_turret_yaw', None) is None: m_veh._turret_yaw = 0.0
								t_diff = t_yaw - m_veh._turret_yaw
								rot_speed = 0.5
								try:
									if _td: rot_speed = _td.turret['rotationSpeed']
								except: pass
								rot_step = rot_speed * dt
								try:
									_btsf = _module_factor(m_veh, 'turret_speed')
									if _btsf < 1.0:
										rot_step = rot_step * _btsf
								except Exception: pass
								# Frozen while submerged or with the turret rotator destroyed - the same two
								# conditions that freeze the player's turret. On a turretless tank this is
								# the gun lock: its aim is clamped to the hull-mounted yaw limits above, so
								# a frozen traverse leaves the gun pointing wherever the hull points.
								if getattr(m_veh, '_offh_drowning', False) or getattr(m_veh, 'is_turret_locked', False):
									rot_step = 0.0
									t_diff = 0.0
								
								if t_diff > rot_step: m_veh._turret_yaw += rot_step
								elif t_diff < -rot_step: m_veh._turret_yaw -= rot_step
								else: m_veh._turret_yaw = t_yaw
								
								m_veh._t_mat.setRotateYPR((m_veh._turret_yaw, 0, 0))
								# Barrel elevation toward the same target, slewed at the gun's own speed and
								# clamped to its real pitchLimits. Without this bots held the gun dead level
								# and every wreck ended up in the identical pose.
								if hasattr(m_veh, '_g_mat'):
									try:
										_bp_want = 0.0
										if target_pos is not None:
											_bp_dx = target_pos[0] - m_veh.position.x
											_bp_dz = target_pos[2] - m_veh.position.z
											_bp_flat = math.sqrt(_bp_dx * _bp_dx + _bp_dz * _bp_dz)
											if _bp_flat > 0.5:
												# BigWorld convention: nose-up is NEGATIVE pitch
												_bp_want = -math.atan2((target_pos[1] + 1.0) - (m_veh.position.y + 1.5), _bp_flat)
										_bp_min, _bp_max = -0.35, 0.15
										try:
											_bp_lim = _td.gun.get('pitchLimits', None) if (_td and isinstance(_td.gun, dict)) else None
											if _bp_lim is not None:
												_bp_l = _bp_lim.get('absolute', _bp_lim) if hasattr(_bp_lim, 'get') else _bp_lim
												_bp_min = float(_bp_l[0]); _bp_max = float(_bp_l[1])
										except Exception:
											pass
										if _bp_want < _bp_min: _bp_want = _bp_min
										elif _bp_want > _bp_max: _bp_want = _bp_max
										if getattr(m_veh, '_gun_pitch', None) is None: m_veh._gun_pitch = 0.0
										_bp_speed = 0.35
										try:
											if _td: _bp_speed = float(_td.gun.get('rotationSpeed', 0.35))
										except Exception:
											pass
										_bp_step = _bp_speed * dt
										_bp_diff = _bp_want - m_veh._gun_pitch
										if _bp_diff > _bp_step: m_veh._gun_pitch += _bp_step
										elif _bp_diff < -_bp_step: m_veh._gun_pitch -= _bp_step
										else: m_veh._gun_pitch = _bp_want
										m_veh._g_mat.setRotateYPR((0, m_veh._gun_pitch, 0))
									except Exception:
										pass
									
							# Strelba bota na hrace
							if getattr(getattr(player, 'arena', None), 'period', 3) != 3:
								continue # no shooting in prebattle countdown OR afterbattle (capture won)
							# Same gates the player's _mock_shoot applies to itself: a submerged crew is
							# fighting the water, and a destroyed gun does not fire at all.
							if getattr(m_veh, '_offh_drowning', False):
								continue
							if getattr(m_veh, 'is_gun_destroyed', False):
								continue
							# A bot must SEE what it shoots at. Without this it fires at
							# every target the AI has picked, spotted or not, which reads
							# as "bots are always firing even with nothing spotted".
							if not _offh_bot_can_see(m_veh, target_pos[0], target_pos[2],
							                         getattr(m_veh, 'typeDescriptor', None)):
								globals()['g_offh_los_blocked'] = (globals().get('g_offh_los_blocked', 0) or 0) + 1
								continue
							if getattr(m_veh, '_ai_shoot_timer', None) is None:
								m_veh._ai_shoot_timer = 0
								m_veh._ai_clip_size = 1
								m_veh._ai_clip = 1
								m_veh._ai_reload_intra = 0.0
								m_veh._ai_reload_full = 3.0
								try:
									_g = getattr(_td, 'gun', {}) if _td else {}
									if isinstance(_g, dict):
										if 'reloadTime' in _g: m_veh._ai_reload_full = float(_g['reloadTime'])
										if 'clip' in _g and len(_g['clip']) == 2:
											m_veh._ai_clip_size = int(_g['clip'][0])
											m_veh._ai_reload_intra = float(_g['clip'][1])
											m_veh._ai_clip = m_veh._ai_clip_size
								except: pass
								
							m_veh._ai_shoot_timer += dt
							
							# Zjistit absolutní úhel, kam míří dělo
							# `or 0.0`: the turret-slew block that initialises _turret_yaw sits one
							# level deeper than this, so a bot can reach the fire gate before it
							# has ever run - and `yaw + None` would abort the whole update.
							abs_gun_yaw = m_veh.yaw + (getattr(m_veh, '_turret_yaw', 0.0) or 0.0)
							# Gate on the bearing to the TARGET, not target_yaw: that one is
							# the steering direction (separation/feeler blended), so a
							# limited-traverse TD whose hull lined up with its own driving
							# direction fired at a player sitting 90 deg off to the side.
							gun_diff = _raw_target_yaw - abs_gun_yaw
							while gun_diff > math.pi: gun_diff -= 2*math.pi
							while gun_diff < -math.pi: gun_diff += 2*math.pi
							
							bot_reload = m_veh._ai_reload_intra if (m_veh._ai_clip_size > 1 and m_veh._ai_clip > 0 and m_veh._ai_clip < m_veh._ai_clip_size) else m_veh._ai_reload_full
							# A downed loader drags the reload out for a bot exactly as it does for the
							# player (a knocked-out commander adds his smaller malus on top), and a
							# damaged ammo bay on top of that. crew_stat_factor returns a TIME
							# multiplier - >1 is worse - so it multiplies. The old code divided by it
							# and only when it was below 1.0, a combination that can never be true:
							# the bot reload malus has never once fired.
							try:
								_brf = _crew_factor(m_veh, 'reload') * _module_factor(m_veh, 'reload')
								if _brf and _brf > 1.0:
									bot_reload = bot_reload * _brf
							except Exception:
								pass
							
							# Vystřelí jen když míří na hráče (tolerance +- 0.15 rad = ~8.5 stupně)
							if m_veh._ai_shoot_timer > bot_reload and dist < 150.0 and abs(gun_diff) < 0.15:
								m_veh._ai_shoot_timer = 0
								try:
									from gui.mods.offhangar import battle_ledger as _BLED
									_BLED.get().note_shot(getattr(m_veh, 'id', -1))
								except Exception:
									pass
								if m_veh._ai_clip_size > 1:
									m_veh._ai_clip -= 1
									if m_veh._ai_clip <= 0:
										m_veh._ai_clip = m_veh._ai_clip_size
								try:
									if g_projectile_mover and _td:
										from items import vehicles
										_shots = _td.gun['shots'] if hasattr(_td, 'gun') and 'shots' in _td.gun else []
										if not _shots and isinstance(_td.gun, dict): _shots = _td.gun.get('shots', [])
										if _shots:
											# One shell, one set of ballistics. Bots always load their gun's first
											# shot, and _speed/_gravity are taken from THAT entry - the aim solution,
											# the tracer, the trajectory walk and the time of flight below all read
											# these two, so a bot can never fire one shell and be scored with another.
											_shot = _shots[0]
											_effectsDescr = vehicles.g_cache.shotEffects[_shot['shell']['effectsIndex']]
											_gravity = _shot['gravity']
											_speed = _shot['speed']
											
											target_y = target_pos[1] if target_pos else veh_pos[1]
											start_p = Math.Vector3(m_veh.position.x, m_veh.position.y + 1.5, m_veh.position.z)
											# Shell leaves along the BARREL azimuth (hull yaw + slewed turret yaw =
											# exactly what _t_mat renders), not conjured straight at the target: the
											# fire gate allows up to ~8.5 deg of remaining slew, and shots taken
											# mid-slew used to home in anyway - now they genuinely go where the gun
											# points.
											#
											# The ELEVATION is now solved for the drop, the same closed form
											# wg_getShotAngles uses for the player. It used to be aimed dead at the
											# target while the tracer was launched WITH gravity, so every bot round
											# arced under its own aim point - and the damage never noticed, because
											# the hit test was a straight line. The shell you watched fall short was
											# not the shell that hit you.
											_b_dy = (target_y + 1.0) - start_p.y
											_b_elev = math.atan2(_b_dy, dist) if dist > 0.001 else 0.0
											try:
												_b_g = abs(float(_gravity))
												_b_root = _speed ** 4 - _b_g * (_b_g * dist * dist + 2.0 * _b_dy * _speed * _speed)
												if _b_root > 0.0 and dist > 0.001 and _b_g > 0.0:
													_b_elev = math.atan((_speed ** 2 - math.sqrt(_b_root)) / (_b_g * dist))
											except Exception:
												pass
											_b_ch = math.cos(_b_elev)
											dir_v = Math.Vector3(math.sin(abs_gun_yaw) * _b_ch, math.sin(_b_elev), math.cos(abs_gun_yaw) * _b_ch)
											dir_v.normalise()
											# Dispersion from the bot's OWN gun rather than a flat 0.03 rad for every
											# tank in the game, which shot a 0.30 gun and a 0.60 gun identically. The
											# 6x stands in for a bot that is moving and traversing rather than sitting
											# fully aimed, and is picked to leave the AVERAGE spread about where the
											# old constant put it - so this sharpens accurate guns and blunts derpy
											# ones without making bots as a whole easier or harder.
											_b_disp = 0.03
											try:
												_b_disp = float(_td.gun['shotDispersionAngle']) * 6.0
											except Exception:
												_b_disp = 0.03
											sigma = _b_disp / 3.0
											dir_v.x += random.gauss(0, sigma)
											dir_v.y += random.gauss(0, sigma)
											dir_v.z += random.gauss(0, sigma)
											dir_v.normalise()
											
											_vel = dir_v.scale(_speed)
											_cam_pos = BigWorld.camera().position if BigWorld.camera() else start_p
											# keep the shot id: explode() needs it to detonate this very tracer
											_b_sid = random.randint(10000, 99999)
											globals()['g_offh_adding_projectile'] = True
											try:
												g_projectile_mover.add(_b_sid, _effectsDescr, _gravity, start_p, _vel, start_p, True, _cam_pos)
											finally:
												globals()['g_offh_adding_projectile'] = False
											try:
												_pjb = getattr(g_projectile_mover, '_ProjectileMover__projectiles', {}).get(_b_sid)
												if _pjb is not None: _pjb['fireMissedTrigger'] = False
											except Exception: pass
											# Barrel recoil animation on the firing bot's gun
											_trigger_gun_recoil(getattr(m_veh, '_gun_recoil', None))
											try:
												_b_td2 = getattr(m_veh, 'typeDescriptor', None)
												_trigger_shot_impulse(getattr(m_veh, '_swinging', None), Math.Vector3(-dir_v.x, -dir_v.y, -dir_v.z), _b_td2.gun['impulse'] if _b_td2 else 0.0)
											except Exception:
												pass
											_mflash_played = _play_muzzle_flash(m_veh, getattr(m_veh, '_gun_model', None), getattr(m_veh, 'typeDescriptor', None), is_player=False)
											if not _mflash_played:
												# The old inline lookup checked gun['effects']['shotSound'], but
												# gun['effects'] is a (stages, effects, _) tuple in this build, so
												# it always fell through to the 20-45mm sound for every bot.
												_fallback_gun_sound(getattr(m_veh, 'typeDescriptor', None), getattr(m_veh, '_chassis_model', None))
											
											player_mock = mock_vehicles.get(_p_vid)
											if player_mock:
												# Vector3, NOT the raw veh_pos list: everything else reads .x/.z off a mock's
												# position, and a list there threw 157 'AttributeError: list object has no
												# attribute x' per session out of the bot separation scan, aborting that bot's
												# whole AI update for the frame. Pre-existing in 0.4.
												try: player_mock.position = Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2])
												except: pass
											
											end_p = start_p + dir_v.scale(500.0)
											
											world_hit_dist = 9999.0
											world_hit = None   # pre-bound: the terrain-impact test below reads it
											veh_hit_dist = 9999.0
											hit_veh = None
											hit_col = None
											_b_impact_pos = None
											_b_impact_dir = dir_v
											
											def _b_mock_test(p1, p2, _self=eid):
												"""Nearest mock on one chord. Wrecks included, exactly as the straight
												scan below did - a bot's round is stopped by a corpse in the way."""
												_best, _bv = None, None
												for _oe, _om in mock_vehicles.iteritems():
													if _oe == _self:
														continue
													_c = _om.collideSegment(p1, p2)
													if _c is not None and (_best is None or _c[0] < _best[0]):
														_best, _bv = _c, _om
												return (_bv, _best) if _best is not None else None
											
											# With the barrel now elevated for the drop, a straight line along it lands
											# HIGH - the same trap the player's shots were in. Walk the real arc whenever
											# the drop is worth having; keep the cheap single ray when it is not.
											if _offh_cfg_flag('ballistic_shells', True) and _offh_shell_drop(min(dist, 500.0), _speed, _gravity) > 0.25:
												_bwk = _offh_shell_path(_offh_bspace(), start_p, _vel, _gravity,
													min(float(_shot.get('maxDistance', 1000.0)), 1000.0), _b_mock_test, 0.05, 80)
												_b_impact_dir = _bwk['dir']
												_b_impact_pos = _bwk['pos']
												if _bwk['mock'] is not None:
													hit_veh = _bwk['mock'][0]
													# [0] carries the PATH length flown, which feeds penetration falloff.
													hit_col = (_bwk['mock'][3],) + tuple(_bwk['mock'][1][1:])
													veh_hit_dist = _bwk['mock'][3]
													_b_impact_pos = _bwk['mock'][2]
												elif _bwk['world'] is not None:
													world_hit = _bwk['world']
													world_hit_dist = _bwk['dist']
											else:
												try:
													world_hit = BigWorld.wg_collideSegment(_offh_bspace(), start_p, end_p, 128)
													if world_hit is not None:
														world_hit_dist = (world_hit[0] - start_p).length
												except Exception:
													pass
												for oeid, omeh in mock_vehicles.iteritems():
													if oeid != eid:   # Nezasahnout sam sebe
														try: omeh.position = omeh.model.position
														except: pass
														col = omeh.collideSegment(start_p, end_p)
														if col is not None and col[0] < veh_hit_dist:
															veh_hit_dist = col[0]
															hit_veh = omeh
															hit_col = col
												if hit_veh is not None:
													_b_impact_pos = start_p + dir_v.scale(veh_hit_dist)
												elif world_hit is not None:
													_b_impact_pos = world_hit[0]
											
											# Stop the tracer on the tank it struck - ProjectileMover only collides
											# static geometry, so a bot's round used to fly on past the tank it had
											# just damaged, exactly as the player's did.
											if hit_veh is not None and veh_hit_dist < world_hit_dist and g_projectile_mover and _b_impact_pos is not None:
												try: g_projectile_mover.hide(_b_sid, _b_impact_pos)
												except Exception: pass
											
											# A bot's damage lands when ITS shell does, same as the player's. Every name
											# below is bound as a DEFAULT ARG rather than closed over: this runs inside
											# the `for eid, m_veh in mock_vehicles` loop, so a closure would read whatever
											# bot the loop had reached by the time the callback fired - the same late-
											# binding trap that made the player's kills delete a bystander's model (fix A).
											# Same rule for bots: a round arriving at a tank that is already dead
											# deals nothing and steals no kill.
											def _offh_deliver_bot_shot(m_veh=m_veh, eid=eid, _shot=_shot, _effectsDescr=_effectsDescr, _b_sid=_b_sid, start_p=start_p, end_p=end_p, dir_v=dir_v, hit_veh=hit_veh, hit_col=hit_col, veh_hit_dist=veh_hit_dist, world_hit=world_hit, world_hit_dist=world_hit_dist, _b_impact_pos=_b_impact_pos, _b_impact_dir=_b_impact_dir, player_mock=player_mock, dist=dist, _gen=globals().get('G_MOCK_VEHICLES')):
												# BOUND FIRST. There is an `import BigWorld` further down this function, which
												# makes the name a LOCAL of it for the whole body - and _swap_destroyed_model_bot
												# is nested in here and reads it from this scope. On any path that reaches the
												# wreck swap before that import line runs, the name is unbound:
												# 'Swap bot destroyed model error: local variable BigWorld referenced before
												# assignment'. Introduced when this block was wrapped for arrival-time delivery.
												import BigWorld
												if globals().get('G_MOCK_VEHICLES') is not _gen:
													return
												if hit_veh is not None and veh_hit_dist < world_hit_dist:
													if (not getattr(hit_veh, 'isAlive', False)) or (getattr(hit_veh, 'health', 0) or 0) <= 0:
														return
												try:
													from gui import WindowsManager as _WMb
													if getattr(_WMb.g_windowsManager, 'battleWindow', None) is None:
														return
												except Exception:
													return
												# Missed every vehicle but hit the world: detonate the tracer there so a bot's
												# near miss throws the same dust/spall burst and crater the player's does.
												# Without this the bot shell simply flew on to the map edge, unseen.
												if not (hit_veh and veh_hit_dist < world_hit_dist) and world_hit is not None and world_hit_dist < 4900.0:
													try:
														_bgmat = _terrain_hit_material(_offh_bspace(), world_hit[0], _b_impact_dir)
														if (_bgmat + 'Hit') not in _effectsDescr:
															_bgmat = 'ground'
														if (_bgmat + 'Hit') in _effectsDescr:
															g_projectile_mover.explode(_b_sid, _effectsDescr, _bgmat, world_hit[0], _b_impact_dir)
													except Exception as _bge:
														LOG_DEBUG('Bot ground impact error:', str(_bge))
												# Pokud trefil nějaké vozidlo a bylo blíž než překážka
												if hit_veh and veh_hit_dist < world_hit_dist:
													# Trefil hráče?
													my_team = m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2
													player_team = getattr(player, '_offhangar_team', 1)
													if hit_veh == player_mock and getattr(player_mock, 'health', 0) > 0 and my_team != player_team:
														_dist, _hitAngleCos, _armor = hit_col[:3]
														# shared model - this path still carried the old piercingPower[0] +
														# "'HE' in shell name" test, so shots at the player never bounced either
														_pen_b, eff_armor, pierce_rng = _offh_penetration(_shot, float(_dist), _armor, _hitAngleCos)
														angle_cos = max(0.087, abs(_hitAngleCos))
													
														LOG_DEBUG('BOT HIT PLAYER! base=%.1f eff=%.1f pierce=%.1f' % (_armor, eff_armor, pierce_rng))
													
														auto_bounce = (_pen_b == 0)

														# Visible impact effect on the player's tank (sparks/bounce/ricochet)
														try:
															_hit_res = _pen_b
															_wpos = _b_impact_pos if _b_impact_pos is not None else (start_p + dir_v.scale(hit_col[0]))
															_play_vehicle_hit_effect(_shot['shell'], _wpos, _b_impact_dir, _hit_res, is_player_target=True)
															# Persistent shell-hole decal on the player's tank
															_p_td = loaded_models.get('td')
															_cn = _comp_name_from_hits(_p_td, hit_col[3] if len(hit_col) > 3 else [])
															_add_impact_decal(_target_sticker_map(player_mock), _cn, _wpos, _b_impact_dir, _hit_res)
														except Exception:
															pass

														dmg = 0
														# DIRECTION AND FLASH FOR ALL HITS
														try:
															px = player_mock.position
															import math
															import BigWorld
														
															# Left/Right is now CORRECT, but Front/Back is inverted.
															# Keep X inverted, and INVERT Z as well.
															dx = -(m_veh.position[0] - px[0])
															dz = -(m_veh.position[2] - px[2])
															hitDirYaw = math.atan2(dx, dz)
														
															if hasattr(player, 'inputHandler') and player.inputHandler:
																_aim = getattr(player.inputHandler, 'aim', None)
																if _aim and hasattr(_aim, 'showHit'):
																	# shell['kind'], never the NAME: every HEAT shell contains the letters 'HE'
																	# too, so the old substring test let a bot's failed HEAT round count as a hit.
																	isDamage = not auto_bounce and (pierce_rng >= eff_armor or _offh_is_he(_shot))
																	_aim.showHit(hitDirYaw, isDamage)
														
															if isDamage:
																fba = Math.Vector4Animation()
																fba.keyframes = [(0.0, Math.Vector4(1.0, 0.0, 0.0, 0.7)), (0.3, Math.Vector4(1.0, 0.0, 0.0, 0.7)), (1.5, Math.Vector4(1.0, 0.0, 0.0, 0.0))]
																fba.duration = 1.5
																BigWorld.flashBangAnimation(fba)
																def remove_fba(f=fba):
																	try: BigWorld.removeFlashBangAnimation(f)
																	except: pass
																BigWorld.callback(1.4, remove_fba)
														except Exception as e:
															LOG_DEBUG('HitDir calc err:', e)
														
														_he_bp = _offh_is_he(_shot)
														_pen_bp = (not auto_bounce) and pierce_rng >= eff_armor
														if auto_bounce or not (_pen_bp or _he_bp):
															LOG_DEBUG('BOT RICOCHET!')
															try:
																_offh_hit_sound('/hits/hits_n_impacts/tank_hit_armor_ricochet')
															except Exception as ex:
																LOG_DEBUG('Ricochet FM err:', ex)
															try:
																if hasattr(player.inputHandler, 'ctrl') and player.inputHandler.ctrl:
																	cam = getattr(player.inputHandler.ctrl, 'camera', None)
																	_dir = Math.Vector3(dx, 0, dz)
																	_dir.normalise()
																	if cam and hasattr(cam, 'applyImpulse'):
																		cam.applyImpulse(_dir, 0.5)
																	elif cam and hasattr(cam, 'impulseOscillator') and cam.impulseOscillator:
																		cam.impulseOscillator.applyImpulse(_dir * 0.5)
															except: pass
														else:
															_dmg_base = _shot['shell']['damage'][0]
															dmg = _dmg_base * random.uniform(0.75, 1.25)
															_he_thru_bp = _he_bp and not _pen_bp
															if _he_thru_bp:
																# Burst on the plate: half the nominal, minus 1.1x its nominal thickness.
																dmg = _offh_he_damage(dmg, _offh_he_nominal_armor(hit_col[3], getattr(player_mock, 'typeDescriptor', None)), 0.0)
																LOG_DEBUG('BOT HE NO PENETRATION -> %d damage' % dmg)
															try:
																# Blast also reaches whoever else is standing around the player.
																if _he_bp:
																	_offh_he_splash(_b_impact_pos if _b_impact_pos is not None else (start_p + dir_v.scale(hit_col[0])), _shot, m_veh.id, getattr(player, 'playerVehicleID', -1))
															except Exception as _hsp:
																LOG_DEBUG('HE splash err (bot->player):', str(_hsp))
															try:
																# start_p/end_p, not the two tank positions: hit_col's distances
																# are measured along THAT segment, and the interior zone needs
																# the real entry point.
																dmg = _apply_module_damage(player_mock, hit_col[3], start_p, end_p, dmg, _shot['shell'], m_veh.id, (not _he_thru_bp), _he_thru_bp)
															except Exception as ex:
																import traceback
																LOG_DEBUG("PLAYER MODULE DAMAGE ERROR:", traceback.format_exc() if 'traceback' in globals() else str(ex))
															# Module test bench: the crits above already happened, the
														# hull damage is what would end the run.
														# Ledger: the strike is recorded either way, but module test mode
														# suppresses the hull damage - the ledger must not book damage the
														# player never actually took.
														try:
															from gui.mods.offhangar import battle_ledger as _BLED
															_BLED.get().note_hit(
																getattr(m_veh, 'id', -1), getattr(player, 'playerVehicleID', -1),
																damage=(0 if _offh_module_test_mode() else
																	min(int(dmg), max(0, int(getattr(player_mock, 'health', 0))))),
																pierced=int(dmg) > 0, he=bool(_he_thru_bp))
														except Exception:
															pass
														if _offh_module_test_mode():
															if int(dmg) > 0:
																LOG_DEBUG('MODULE TEST: bot shell dealt %d hull damage, suppressed' % int(dmg))
														else:
															player_mock.health -= int(dmg)
															try:
																_offh_hit_sound('/hits/hits_n_impacts/tank_hit_armor_crit')
															except Exception as ex:
																LOG_DEBUG('Pierce FM err:', ex)
															try:
																if hasattr(player.inputHandler, 'ctrl') and player.inputHandler.ctrl:
																	cam = getattr(player.inputHandler.ctrl, 'camera', None)
																	_dir = Math.Vector3(dx, 0, dz)
																	_dir.normalise()
																	if cam and hasattr(cam, 'applyImpulse'):
																		cam.applyImpulse(_dir, 1.0)
																	elif cam and hasattr(cam, 'impulseOscillator') and cam.impulseOscillator:
																		cam.impulseOscillator.applyImpulse(_dir * 1.0)
															except: pass
															if player_mock.health <= 0:
																player_mock.health = 0
															# Update player vehicle HP physically
															if hasattr(player, 'vehicle') and player.vehicle:
																player.vehicle.health = player_mock.health
															# Update GUI
															try:
																import gui.WindowsManager
																bw = gui.WindowsManager.g_windowsManager.battleWindow
																if hasattr(bw, 'damagePanel'):
																	bw.damagePanel.updateHealth(player_mock.health)
																if hasattr(bw, 'vMarkersManager'):
																	pass # bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, player_mock.health, 1, 0)
															except: pass
															if player_mock.health <= 0:
																player_mock.health = 0
														
															# Update player vehicle HP physically
															if hasattr(player, 'vehicle') and player.vehicle:
																player.vehicle.health = player_mock.health
															
															# Update GUI
															try:
																import gui.WindowsManager
																bw = gui.WindowsManager.g_windowsManager.battleWindow
																if hasattr(bw, 'damagePanel'):
																	bw.damagePanel.updateHealth(player_mock.health)
																if hasattr(bw, 'vMarkersManager'):
																	pass # bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, player_mock.health, 1, 0)
															except: pass
													else:
														my_team = m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2
														target_team = hit_veh.publicInfo.get('team', 2) if getattr(hit_veh, 'publicInfo', None) is not None else (getattr(player, '_offhangar_team', 1) if getattr(player, 'playerVehicleID', -1) == hit_veh.id else 2)
														if getattr(hit_veh, 'health', 0) > 0 and my_team != target_team:
															# ARMOR PENETRATION LOGIC FOR BOT vs BOT
															_dmg_base = _shot['shell']['damage'][0]
															_dist, _hitAngleCos, _armor = hit_col[:3]
															_pen_res, eff_armor, pierce_rng = _offh_penetration(_shot, float(_dist), _armor, _hitAngleCos)
															auto_bounce = (_pen_res == 0)
														
															is_damage = (_pen_res == 2)
															# HE that failed to get through is not a miss - it bursts on the plate. Force
															# the damage branch and let the blast formula decide how much survives.
															_he_bb = _offh_is_he(_shot)
															_he_thru_bb = _he_bb and not is_damage
															if _he_thru_bb:
																is_damage = True

															# Visible impact effect + shell-hole decal on the hit bot
															try:
																_hit_res = 0 if auto_bounce else (2 if is_damage else 1)
																_wpos = _b_impact_pos if _b_impact_pos is not None else (start_p + dir_v.scale(hit_col[0]))
																_play_vehicle_hit_effect(_shot['shell'], _wpos, _b_impact_dir, _hit_res, target_mock=hit_veh)
																_cn = _comp_name_from_hits(getattr(hit_veh, 'typeDescriptor', None), hit_col[3] if len(hit_col) > 3 else [])
																_add_impact_decal(_target_sticker_map(hit_veh), _cn, _wpos, _b_impact_dir, _hit_res)
															except Exception:
																pass

															if is_damage:
																LOG_DEBUG('BOT HIT ENEMY BOT: %s' % ('HE BURST' if _he_thru_bb else 'PENETRATION!'))
																_dmg = int(_dmg_base * random.uniform(0.75, 1.25))
																if _he_thru_bb:
																	_dmg = _offh_he_damage(_dmg, _offh_he_nominal_armor(hit_col[3], getattr(hit_veh, 'typeDescriptor', None)), 0.0)
																try:
																	if _he_bb:
																		_offh_he_splash(_b_impact_pos if _b_impact_pos is not None else (start_p + dir_v.scale(hit_col[0])), _shot, m_veh.id, getattr(hit_veh, 'id', -1))
																except Exception as _hsb:
																	LOG_DEBUG('HE splash err (bot->bot):', str(_hsb))
																try:
																	_dmg = int(_apply_module_damage(hit_veh, hit_col[3], start_p, end_p, _dmg, _shot['shell'], m_veh.id, (not _he_thru_bb), _he_thru_bb))
																except Exception as ex:
																	import traceback
																	LOG_DEBUG("BOT MODULE DAMAGE ERROR:", traceback.format_exc() if 'traceback' in globals() else str(ex))
																# Ledger: shooter is the BOT, victim is whatever it struck - the
																# old damage_from_bots counter lived on the VICTIM and could never
																# say who fired, which is why every bot row showed the damage it
																# had received. Recorded before health drops, so the clamp below
																# sees the hit points that were really there.
																try:
																	from gui.mods.offhangar import battle_ledger as _BLED
																	_BLED.get().note_hit(
																		getattr(m_veh, 'id', -1), getattr(hit_veh, 'id', -1),
																		damage=min(int(_dmg), max(0, int(getattr(hit_veh, 'health', 0)))),
																		pierced=_dmg > 0, he=bool(_he_bb))
																except Exception:
																	pass
																hit_veh.health -= _dmg
																hit_veh.damage_from_bots = (getattr(hit_veh, 'damage_from_bots', 0) or 0) + _dmg
																hit_veh.last_killer_id = m_veh.id
																try:
																	player.arena.onVehicleStatisticsUpdate(hit_veh.id)
																	from gui import WindowsManager
																	bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
																	if bw and hasattr(bw, 'vMarkersManager'):
																		marker = getattr(hit_veh, 'marker', None)
																		if marker is not None:
																			bw.vMarkersManager.onVehicleHealthChanged(marker, max(0, hit_veh.health), m_veh.id, 0)
																			try:
																				bw.vMarkersManager.showVehicleDamageInfo(marker, _dmg, 0, 0, 0)
																			except:
																				pass
																		try: bw.minimap.notifyVehicleStop(hit_veh.id) if hit_veh.health <= 0 else None
																		except: pass
																except: pass
															else:
																LOG_DEBUG('BOT HIT ENEMY BOT: RICOCHET/NON-PEN!')
															if hit_veh.health <= 0:
																_offh_set_alive(hit_veh, False)
																try:
																	from gui import WindowsManager
																	bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
																	if bw and hasattr(bw, '_Battle__arena'):
																		bw._Battle__arena.vehicles[hit_veh.id]['isAlive'] = False
																		bw._Battle__updatePlayers()
																except: pass
																LOG_DEBUG('BOT KILLED ENEMY BOT!')
																try:
																	try: hit_veh.appearance.changeVisibility('', False, False)
																	except: pass
																	try:
																		if getattr(hit_veh, '_wreck_done', False):
																			raise StopIteration  # wreck already handled by another kill path
																		hit_veh._wreck_done = True
																		_dtd = hit_veh.typeDescriptor
																		_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
																		_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
																		_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
																		_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
																		_old_ch = hit_veh._chassis_model
																		_old_pos = _old_ch.position
																		_old_yaw = _old_ch.yaw
																		# pitch/roll as well: a wreck used to snap dead level on any slope
																		try: _old_pitch = _old_ch.pitch
																		except Exception: _old_pitch = 0.0
																		try: _old_roll = _old_ch.roll
																		except Exception: _old_roll = 0.0
																		_old_ch_ref = _old_ch
																		def _swap_destroyed_model_bot(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu, _old_ch_ref=_old_ch_ref, _old_pos=_old_pos, _old_yaw=_old_yaw, m_veh=hit_veh):
																			# A Model only streams in once it is IN THE WORLD. This waited for
																			# .loaded on four models that were never added, so whenever the assets
																			# were not already resident the wait never ended - and the caller had
																			# ALREADY hidden the tank, leaving a dead-tank marker floating over
																			# empty ground with no wreck under it. The player-kill path
																			# (_swap_destroyed_model) always did this right; these two did not.
																			if not getattr(m_veh, '_wreck_kicked', False):
																				m_veh._wreck_kicked = True
																				try: _d_ch.position = _old_pos
																				except Exception: pass
																				for _wm in (_d_ch, _d_hu, _d_tu, _d_gu):
																					try: _add_model(_wm)
																					except Exception: pass
																			if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
																				BigWorld.callback(0.1, _swap_destroyed_model_bot)
																				return
																			# Added above only to force the load; hull/turret/gun belong on nodes.
																			for _wm in (_d_hu, _d_tu, _d_gu):
																				try: BigWorld.delModel(_wm)
																				except Exception: pass
																			try: _old_ch_ref.visible = False
																			except: pass
																			try: _old_ch_ref.visibleAttachments = False
																			except: pass
																			try:
																				if getattr(m_veh, 'bw_entity', None) is not None:
																					m_veh.bw_entity.model = None  # chassis is entity-owned: delModel alone fails
																			except: pass
																			try: BigWorld.delModel(_old_ch_ref)
																			except: pass
																			# Wreck must rest on the ground (mid-air kill would leave a floating
																			# wreck). _wpos: NEVER rebind _old_pos - in the player-kill path this
																			# code sits in a nested function where _old_pos is only a closure var;
																			# assigning it made it local -> UnboundLocalError -> vanishing wrecks.
																			_wpos = _old_pos
																			try:
																				import BigWorld as _bwx, Math as _mx
																				_gw = _bwx.wg_collideSegment(_offh_bspace(), _mx.Vector3(_wpos.x, _wpos.y + 2.0, _wpos.z), _mx.Vector3(_wpos.x, _wpos.y - 500.0, _wpos.z), 128)
																				if _gw is not None and _wpos.y > _gw[0].y + 0.5:
																					_wpos = _mx.Vector3(_wpos.x, _gw[0].y, _wpos.z)
																			except Exception:
																				pass
																			_d_ch.position = _wpos
																			_d_ch.yaw = _old_yaw
																			# Whole orientation in one go. Model.pitch/.roll assigned separately after
																			# .yaw do NOT compose - each setter rebuilds the transform, which left the
																			# wreck mis-oriented (turretless hulls like the Foch 155 worst of all).
																			# A Servo on a prepared matrix is what the live chassis already uses.
																			try:
																				_wr_mat = Math.Matrix()
																				_wr_mat.setRotateYPR((_old_yaw, _old_pitch, _old_roll))
																				_wr_mat.translation = _wpos
																				_d_ch.addMotor(BigWorld.Servo(_wr_mat))
																				m_veh._wreck_mat = _wr_mat   # hold a ref: a GC'd matrix drops the wreck
																			except Exception as _wme:
																				LOG_DEBUG('Wreck orientation failed:', str(_wme))
																			_h_mat = Math.Matrix(); _h_mat.setIdentity()
																			# freeze the turret where the bot last aimed (identity snapped it forward)
																			# snapshot of the last aim: turret where it pointed, barrel where it sat
																			# The chassis matrix is kept alive on the mock for exactly this reason; the
																			# turret and gun matrices were not. Model.node(name, matrix) does not own the
																			# matrix, so once these locals went out of scope the collector could take them
																			# and the joint fell back to identity - the wreck's turret snapping to 0/0
																			# some deaths but not others, depending on GC timing.
																			_t_mat = Math.Matrix(); _t_mat.setRotateYPR((float(getattr(m_veh, '_turret_yaw', 0.0) or 0.0), 0, 0))
																			m_veh._wreck_t_mat = _t_mat   # hold a ref: a GC'd matrix drops the node back to identity
																			_g_mat = Math.Matrix(); _g_mat.setRotateYPR((0, float(getattr(m_veh, '_gun_pitch', 0.0) or 0.0), 0))
																			m_veh._wreck_g_mat = _g_mat   # hold a ref: a GC'd matrix drops the node back to identity
																			try: _d_ch.node('V').attach(_d_hu)
																			except: pass
																			try: 
																				m_veh._d_t_node = _d_hu.node('HP_turretJoint', _t_mat)
																				m_veh._d_t_node.attach(_d_tu)
																			except: pass
																			try: 
																				m_veh._d_g_node = _d_tu.node('HP_gunJoint', _g_mat)
																				m_veh._d_g_node.attach(_d_gu)
																			except: pass
																		BigWorld.callback(0.1, _swap_destroyed_model_bot)
																	except Exception as e:
																		LOG_DEBUG('Swap bot destroyed model error:', e)
																
																	if hasattr(player.arena, 'statistics'):
																		if eid not in player.arena.statistics: player.arena.statistics[eid] = {'frags': 0}
																		_atk_team = getattr(m_veh, '_bot_team', m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2)
																		_vic_team = getattr(hit_veh, '_bot_team', hit_veh.publicInfo.get('team', 2) if getattr(hit_veh, 'publicInfo', None) is not None else 2)
																		_frag_diff_bot = -1 if _atk_team == _vic_team else 1
																		player.arena.vehicles[eid]['frags'] = player.arena.vehicles[eid].get('frags', 0) + _frag_diff_bot
																		player.arena.statistics[eid]['frags'] = player.arena.statistics[eid].get('frags', 0) + _frag_diff_bot
																	player.arena.onVehicleKilled(hit_veh.id, eid, 0)
																	try:
																		if hasattr(player, 'onVehicleKilled'): player.onVehicleKilled(hit_veh.id, eid, 0)
																	except: pass
																	for v_id in player.arena.vehicles:
																		if v_id not in player.arena.statistics: player.arena.statistics[v_id] = {'frags': 0}
																	player.arena.onVehicleStatisticsUpdate(eid)
																	if hasattr(bw, '_Battle__updatePlayers'):
																		try: bw._Battle__updatePlayers()
																		except: pass
																	if hasattr(bw, '_Battle__fragCorrelation'):
																		p_team = getattr(player, '_offhangar_team', 1)
																		allied = sum(v.get('frags', 0) for i,v in player.arena.vehicles.items() if i in player.arena.statistics and v.get('team') == p_team)
																		enemy = sum(v.get('frags', 0) for i,v in player.arena.vehicles.items() if i in player.arena.statistics and v.get('team') != p_team)
																		try: bw._Battle__fragCorrelation.updateFrags(allied, enemy)
																		except: pass
																	pass  # kill feed is posted centrally in _KillEventWrapper
																except: pass
														else:
															LOG_DEBUG('BOT MISSED PLAYER - Hit another vehicle (corpse/ally) first at dist %.1f' % veh_hit_dist)
												elif world_hit_dist < 9999.0:
													LOG_DEBUG('BOT MISSED PLAYER - Hit obstacle (terrain/building) first at dist %.1f' % world_hit_dist)
											
											_b_tof = 0.0
											try:
												_b_d = veh_hit_dist if (hit_veh is not None and veh_hit_dist < world_hit_dist) else (world_hit_dist if world_hit_dist < 4900.0 else 0.0)
												if _speed > 0.0 and 0.0 < _b_d < 5000.0:
													_b_tof = min(_b_d / float(_speed), 5.0)
											except Exception:
												_b_tof = 0.0
											if _b_tof > 0.03:
												BigWorld.callback(_b_tof, _offh_deliver_bot_shot)
											else:
												_offh_deliver_bot_shot()
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
						except Exception as e:
							import traceback
							LOG_DEBUG('Bot AI Exception:', traceback.format_exc())
							
				# PLAYER DEATH CHECK
				try:
					player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					if player_mock and player_mock.health <= 0 and getattr(player, '_is_dead', False) is not True:
						player._is_dead = True
						player._offh_spectating = True
						# A destroyed tank has every module and every crewman out - not just when it
						# drowned, which was the only path that used to do this.
						try: _offh_knock_out_everything(player_mock, True)
						except Exception as _koe: LOG_DEBUG('death knockout err:', str(_koe))
						# leave sniper/strategic so the postmortem cam is a stable follow, not stuck zoomed
						try: g_offline_aih.onControlModeChanged('arcade')
						except Exception: pass
						# Retail's desaturated postmortem look: PostMortemControlMode.enable() runs
						# g_postProcessing.enable('postmortem'), a chain of HSV saturation cut plus
						# the postmortem_correction LUT (system/post_processing/chains/wg_postmortem).
						# Must come AFTER the arcade switch, whose own enable() sets the arcade preset.
						_offh_postmortem_grading()
						# The arcade switch above is not the only thing that can clobber the chain
						# (control-mode enable() calls g_postProcessing.enable for its own preset),
						# so re-assert once the postmortem camera has settled.
						try: BigWorld.callback(0.5, _offh_postmortem_grading)
						except Exception: pass
						player._offh_spec_idx = 0
						# dead -> hide the aim crosshair / gun marker
						try:
							_dh_ctrl = getattr(getattr(player, 'inputHandler', None), 'ctrl', None)
							if _dh_ctrl is not None:
								try: _dh_ctrl.showGunMarker(False)
								except Exception: pass
								try: _dh_ctrl.showGunMarker2(False)
								except Exception: pass
						except Exception: pass
						# crew death voice like live (Avatar plays soundNotifications 'vehicle_destroyed')
						# vehicle_destroyed carries no shouldBindToPlayer, and the mod's
						# instances no longer bind anything implicitly, so leave it unbound:
						# a line about the player's OWN death must not be filtered by whether
						# some other tank is still alive.
						_offh_notify('vehicle_destroyed')
						LOG_DEBUG('Player is dead. Spawning destroyed model and ending battle.')
						try:
							killer_id = getattr(player_mock, 'last_killer_id', -1)
							p_id = player.playerVehicleID
							if killer_id != -1 and killer_id in player.arena.vehicles and hasattr(player.arena, 'onVehicleKilled'):
								player.arena.vehicles[killer_id]['frags'] = player.arena.vehicles[killer_id].get('frags', 0) + 1
								if hasattr(player.arena, 'statistics'):
									if killer_id not in player.arena.statistics: player.arena.statistics[killer_id] = {'frags': 0}
									player.arena.statistics[killer_id]['frags'] = player.arena.statistics[killer_id].get('frags', 0) + 1
								player.arena.onVehicleKilled(p_id, killer_id, 0)
								player.arena.onVehicleStatisticsUpdate(killer_id)
								
								from gui import WindowsManager
								bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, '_Battle__fragCorrelation'):
									p_team = getattr(player, 'team', 1)
									allied = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') == p_team)
									enemy = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') != p_team)
									bw._Battle__fragCorrelation.updateFrags(allied, enemy)
									# kill feed is posted centrally in _KillEventWrapper
						except Exception as _e:
							LOG_DEBUG('Frag update error:', _e)
						
						# Swap model - hide live models, show the destroyed ones. The block below
						# already carries the live turret bearing and gun pitch across (_dead_tyaw /
						# _dead_gpitch), so the pose survives either way. What must NOT survive a
						# drowning is the burnt-out look: the tank sank where it stood, it did not
						# blow up, so it keeps its intact models - same swap, undamaged variants.
						try:
							_dtd = getattr(player_mock, 'typeDescriptor', None) or loaded_models.get('td')
							_dkey = 'undamaged' if getattr(player_mock, '_drowned', False) else 'destroyed'
							_d_ch = BigWorld.Model(_dtd.chassis['models'][_dkey])
							_d_hu = BigWorld.Model(_dtd.hull['models'][_dkey])
							_d_tu = BigWorld.Model(_dtd.turret['models'][_dkey])
							_d_gu = BigWorld.Model(_dtd.gun['models'][_dkey])
							
							try: _dead_tyaw = turret_yaw[0]
							except Exception: _dead_tyaw = 0.0
							try: _dead_gpitch = gun_pitch[0]
							except Exception: _dead_gpitch = 0.0
							def _swap_player_destroyed(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu, _tyaw=_dead_tyaw, _gpitch=_dead_gpitch):
								try:
									# Force load
									_add_model(_d_ch)
									_add_model(_d_hu)
									_add_model(_d_tu)
									_add_model(_d_gu)
									
									def _attach_when_ready():
										if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
											BigWorld.callback(0.1, _attach_when_ready)
											return
										try: BigWorld.delModel(_d_hu)
										except: pass
										try: BigWorld.delModel(_d_tu)
										except: pass
										try: BigWorld.delModel(_d_gu)
										except: pass
										
										_live_chassis = loaded_models.get('chassis') or loaded_models.get('hull')
										if _live_chassis is not None:
											try:
												for _mot in list(_live_chassis.motors):
													_live_chassis.delMotor(_mot)
											except: pass
											try: _live_chassis.visible = False
											except: pass
											try: BigWorld.delModel(_live_chassis)
											except: pass
										
										try: _d_ch.node('V').attach(_d_hu)
										except: pass
										# freeze turret/gun at the last aimed direction (not snapped forward)
										try:
											_t_mat = Math.Matrix(); _t_mat.setRotateYPR((_tyaw, 0, 0))
											mock_veh._wreck_t_mat = _t_mat   # hold a ref: a GC'd matrix drops the node back to identity
											_d_hu.node('HP_turretJoint', _t_mat).attach(_d_tu)
										except: pass
										try:
											_g_mat = Math.Matrix(); _g_mat.setRotateYPR((0, _gpitch, 0))
											mock_veh._wreck_g_mat = _g_mat   # hold a ref: a GC'd matrix drops the node back to identity
											_d_tu.node('HP_gunJoint', _g_mat).attach(_d_gu)
										except: pass
										
										_d_ch.position = Math.Vector3(mock_veh.position)
										try: _d_ch.addMotor(BigWorld.Servo(chassis_mp))
										except: pass
										try:
											mock_veh._collision_obstacle = BigWorld.PyModelObstacle(
												_ptd.hull['models']['destroyed'],
												_ptd.turret['models']['destroyed'],
												chassis_mp,
												False
											)
										except: pass
										LOG_DEBUG('Player destroyed model placed OK')
									_attach_when_ready()
								except Exception as _e:
									import traceback
									LOG_DEBUG('Player model swap failed:', traceback.format_exc())
							
							BigWorld.callback(0.1, _swap_player_destroyed)
						except Exception as _e: LOG_DEBUG('Player death model err:', str(_e))
						
						# Exit battle in 5 seconds - use game.fini() which is the proper hook
						def _exit_battle():
							try:
								if _exit_done[0]:
									LOG_DEBUG('Player death exit skipped: another exit path already ran')
									return
								_exit_done[0] = True
								LOG_DEBUG('Player death: triggering exit to hangar')
								_battle_finished[0] = True
								try:
									import SoundGroups as _SG
									if getattr(_SG, 'g_instance', None) is not None:
										_SG.g_instance.enableArenaSounds(False)
										_SG.g_instance.enableLobbySounds(True)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								# Kill the crew-voice engine BEFORE the hangar loads. This used
								# to CREATE+start a fresh IngameSoundNotifications here (merge
								# artifact): the instance lived on the persistent account, its
								# active voice event never got an end-callback once arena sounds
								# died, and the jammed 'voice' queue silenced ALL crew voices in
								# every later battle. destroy() stops active lines and lets the
								# next battle lazily build a clean instance.
								try:
									_sn = getattr(player, 'soundNotifications', None)
									if _sn is not None:
										try: _sn.destroy()
										except Exception: pass
									try: del player.soundNotifications
									except Exception: pass
								except: pass
								
								try:
									_aih = getattr(player, 'inputHandler', None)
									if _aih is not None:
										try: _aih._AvatarInputHandler__isStarted = False
										except: pass
										for _cm in getattr(_aih, '_AvatarInputHandler__ctrls', {}).values():
											try: _cm.destroy()
											except: pass
										try:
											import game
											if hasattr(_aih, '_AvatarInputHandler__onRecreateDevice'):
												game.g_guiResetters.remove(_aih._AvatarInputHandler__onRecreateDevice)
										except: pass
										try: player.inputHandler = None
										except: pass
								except Exception as e:
									import traceback
									LOG_DEBUG('Failed to stop AIH:', traceback.format_exc())
								
								import gui.mods.offhangar._constants as _c
								from gui import WindowsManager
								
								try:
									if hasattr(WindowsManager.g_windowsManager, 'destroyBattle'):
										WindowsManager.g_windowsManager.destroyBattle()
									else:
										WindowsManager.g_windowsManager.hideAll()
								except Exception:
									pass
									
								try:
									global g_offline_models
									# Clear FIRST: delModel's pending error raises at loop
									# exhaustion; a post-loop clear was being skipped, so the
									# player's WRECK models leaked into the next battle (the
									# 'thrown back into the same round with my dead tank' bug).
									_gm_list = list(g_offline_models)
									g_offline_models = []
									for m in _gm_list:
										_offh_del_model(m)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								# Free everything the battle allocated BEFORE camera/draw go down:
								# chunk unloading after delSpaceGeometryMapping only completes
								# while the engine still draws (memlog: the sweep-first 'quit'
								# path returned ~700 MB, this post-draw-off path returned none).
								try:
									_offh_battle_sweep('exit')
								except Exception as e:
									LOG_DEBUG('Sweep err:', str(e))
								
								try:
									global g_projectile_mover
									if g_projectile_mover is not None:
										g_projectile_mover.destroy()
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())

								try:
									BigWorld.camera(None)
									BigWorld.worldDrawEnabled(False)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									import gui.ClientHangarSpace
									LOG_DEBUG('ClientHangarSpace module dir:', dir(gui.ClientHangarSpace))
									LOG_DEBUG('ClientHangarSpace class dir:', dir(gui.ClientHangarSpace.ClientHangarSpace))
								except Exception as e:
									LOG_DEBUG('ClientHangarSpace error:', e)
								
								try:
									BigWorld.worldDrawEnabled(True)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
									
								try:
									from gui import WindowsManager
									
									if hasattr(WindowsManager.g_windowsManager, 'showLobby'):
										WindowsManager.g_windowsManager.showLobby()
										LOG_DEBUG('Triggered showLobby() for full UI and camera reload!')
										
									from gui.Scaleform.utils.HangarSpace import g_hangarSpace
									if g_hangarSpace is not None:
										try:
											g_hangarSpace.destroy()
										except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
										# WG 'no man's land' purge: hangar destroyed, nothing active, before re-init.
										_offh_safe_purge()
										g_hangarSpace.init(True)
										g_hangarSpace.refreshVehicle()
										LOG_DEBUG('Restored HangarSpace via global instance!')
									else:
										LOG_DEBUG('Global g_hangarSpace is None!')
										
								except Exception as e:
									import traceback
									LOG_DEBUG('HangarSpace restore error:', traceback.format_exc())

								# The battle-exit resync can leave a stale 'download/...'
								# entry in the global Waiting overlay (its completion
								# callback is lost in the lobby transition). The overlay
								# then resurfaces over the next opened view - e.g. the
								# Research screen - as an infinite spinner, although the
								# view underneath loaded fine. Flush once things settle.
								def _flush_stale_waiting():
									try:
										from gui.Scaleform.Waiting import Waiting
										Waiting.close()
										LOG_DEBUG('OfflineBattle: flushed stale Waiting overlay')
									except Exception:
										pass
								try:
									BigWorld.callback(3.0, _flush_stale_waiting)
								except Exception:
									pass
								
								try:
									BigWorld.worldDrawEnabled(True)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
									
								# Set the allow flag and trigger native exit
								for _e in BigWorld.entities.values():
									if _e.__class__.__name__ in ('PlayerAccount', 'Account'):
										_e._offline_allow_become_non_player = True
										if hasattr(_e, '_offhangar_orig_stats') and _e._offhangar_orig_stats is not None:
											_e.stats = _e._offhangar_orig_stats
										try: _e.showGUI(_c.OFFLINE_GUI_CTX)
										except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
							except Exception as e:
								import traceback
								LOG_DEBUG('Player exit battle err:', traceback.format_exc())
						
						# Post-death: enter ally spectator instead of auto-exiting. The tick follows
						# a living ally; K/ESC still leaves via the normal exit path.
						LOG_DEBUG('OfflineBattle: player dead -> ally spectator (K to exit)')
						# native post-death GUI overlay (post-mortem tips panel)
						try:
							from gui import WindowsManager as _pmwm
							_pmwm.g_windowsManager.showPostMortem()
						except Exception: pass
				except Exception as e: LOG_DEBUG('Player death check err:', str(e))
				# Post-death spectator. Target 0 = the dead tank itself (stay on it first);
				# left-click then cycles to living team-mates. Drive WGs native post-death
				# panels via onPostmortemVehicleChanged (offline mocks are not real entities so
				# PostMortemControlMode cannot; we point the camera + panels directly).
				try:
					_pl_s = BigWorld.player()
					if getattr(_pl_s, '_offh_spectating', False):
						# dead -> hide the aim reticle (incl. reload/ammo timer) + gun marker
						try:
							_saim = getattr(getattr(_pl_s, 'inputHandler', None), 'aim', None)
							if _saim is not None:
								try: _saim.setVisible(False)
								except Exception: pass
								try: _saim.component.visible = False
								except Exception: pass
							_sctrl = getattr(getattr(_pl_s, 'inputHandler', None), 'ctrl', None)
							if _sctrl is not None:
								try: _sctrl.showGunMarker(False)
								except Exception: pass
								try: _sctrl.showGunMarker2(False)
								except Exception: pass
								# showGunMarker asserts __isEnabled; if the ctrl is disabled the assert is
								# swallowed and the Zielkreis stays. Hide the _SuperGunMarker directly.
								for _gmn in ('_ArcadeControlMode__gunMarker', '_SniperControlMode__gunMarker', '_ArtyControlMode__gunMarker', '_StrategicControlMode__gunMarker'):
									_gm = getattr(_sctrl, _gmn, None)
									if _gm is not None:
										# hide the _SuperGunMarker's two flash markers DIRECTLY. Never call show2(False):
										# in retail it runs show(not flag)=show(True), re-showing the marker we just hid.
										for _gmi in ('_SuperGunMarker__gm1', '_SuperGunMarker__gm2'):
											_gmf = getattr(_gm, _gmi, None)
											if _gmf is not None:
												try: _gmf.show(False)
												except Exception: pass
							# hide the bottom-left tank indicator (rotating silhouette): offline mocks are
							# never _setup so it keeps the player's live turret matrix and keeps spinning.
							try:
								from gui import WindowsManager as _wmti
								_bwti = getattr(_wmti.g_windowsManager, 'battleWindow', None)
								_dpti = getattr(_bwti, 'damagePanel', None) if _bwti is not None else None
								_uiti = getattr(_dpti, '_DamagePanel__ui', None) if _dpti is not None else None
								_compti = getattr(_uiti, 'component', None) if _uiti is not None else None
								_mcti = getattr(_compti, 'tankIndicator', None) if _compti is not None else None
								if _mcti is not None: _mcti.visible = False
							except Exception: pass
						except Exception: pass
						_mvs = globals().get('G_MOCK_VEHICLES', {}) or {}
						_pteam = getattr(_pl_s, '_offhangar_team', 1)
						_pvid = getattr(_pl_s, 'playerVehicleID', -1)
						_targets = []
						_pmk = _mvs.get(_pvid)
						if _pmk is not None: _targets.append((_pvid, _pmk))
						for _mk, _mv2 in _mvs.items():
							if _mk == _pvid: continue
							if (getattr(_mv2, 'health', 0) or 0) <= 0 or not getattr(_mv2, 'isAlive', False): continue
							_vt = getattr(_mv2, '_bot_team', None)
							if _vt is None:
								_pi = getattr(_mv2, 'publicInfo', None)
								_vt = _pi.get('team', 2) if _pi is not None else 2
							if _vt == _pteam: _targets.append((_mk, _mv2))
						if _targets:
							# A click on a name in the players panel arrives as _offh_spec_want
							# (Account.selectPlayer). Consume it here: the list is rebuilt every
							# frame, so a vehicle id is stable where an index is not.
							_want = getattr(_pl_s, '_offh_spec_want', None)
							if _want is not None:
								_pl_s._offh_spec_want = None
								for _wi in range(len(_targets)):
									if _targets[_wi][0] == _want:
										_pl_s._offh_spec_idx = _wi
										break
							_si = getattr(_pl_s, '_offh_spec_idx', 0) % len(_targets)
							_pl_s._offh_spec_idx = _si
							_aid, _amock = _targets[_si]
							_scam = BigWorld.camera()
							if getattr(_pl_s, '_offh_spec_cur', None) != _aid:
								_pl_s._offh_spec_cur = _aid
								# Bind the camera to a LIVE translation provider ONCE per target change.
								# The old code built a fresh Math.Matrix and re-assigned cam.target EVERY
								# frame: ~60 new matrix objects/s (the churn that crashed this client before)
								# carrying a one-frame-stale position, and the constant re-assign fought WG's
								# own camera update - that is the postmortem judder. The mock's .matrix is a
								# persistent Math.Matrix mutated in place each tick, so the provider tracks it
								# for free (same pattern as _force_camera_to_model).
								try:
									_amx = getattr(_amock, 'matrix', None)
									if _scam is not None and hasattr(_scam, 'target') and _amx is not None:
										_smp = Math.WGTranslationOnlyMP()
										_smp.source = _amx
										_scam.target = _smp
										_pl_s._offh_spec_mp = _smp   # keep a ref alive; a GC'd provider drops the camera
								except Exception as _sce:
									LOG_DEBUG('Spectator camera bind error:', str(_sce))
								try:
									from gui import WindowsManager as _pmwm2
									_bw2 = getattr(_pmwm2.g_windowsManager, 'battleWindow', None)
									if _bw2 is not None:
										if hasattr(_bw2, 'onPostmortemVehicleChanged'):
											_bw2.onPostmortemVehicleChanged(_aid)
										# switchToVehicle() waits for a real BigWorld.entity (offline mocks never
										# are) so it resets HP to 0 forever - feed the mock's max HP straight in.
										_dp = getattr(_bw2, 'damagePanel', None)
										if _dp is not None:
											_amh = getattr(getattr(_amock, 'typeDescriptor', None), 'maxHealth', None) or getattr(_amock, 'maxHealth', None) or int(getattr(_amock, 'health', 1000) or 1000)
											try: _dp._DamagePanel__callFlash('setMaxHealth', [int(_amh)])
											except Exception: pass
											# switchToVehicle wipes every module icon back to normal for the new
											# tank, and offline nothing ever put them back - so cycling to an ally
											# and back showed your own broken modules as intact. Repaint the panel
											# from the mock now being displayed.
											_offh_repaint_damage_panel(_amock)
								except Exception: pass
							# keep the spectated tank's HP bar live each frame (it takes damage as it fights)
							try:
								from gui import WindowsManager as _pmwm3
								_bw3 = getattr(_pmwm3.g_windowsManager, 'battleWindow', None)
								_dpf = getattr(_bw3, 'damagePanel', None) if _bw3 is not None else None
								if _dpf is not None:
									# Target 0 of the spectator list is the player's OWN wreck, so this push
									# owns the bar right after death - reading .health straight put a drowned
									# tank back to 0 every frame, undoing the drown block's display value.
									_dpf.updateHealth(_offh_hp_display(_amock))
							except Exception: pass
				except Exception:
					pass
				_PROBE.end()
				BigWorld.callback(0.0, _aih_tick)
			except Exception as e:
				import traceback
				LOG_DEBUG('AIH_TICK CRASH:', traceback.format_exc())
				_PROBE.end()
				BigWorld.callback(0.0, _aih_tick)
			return
		# Battle starts here: open the CSV and log what this client renders at.
		_PROBE.start()
		_dump_graphics()
		BigWorld.callback(0.0, _aih_tick)

		# Patch SniperCamera.__cameraUpdate to sync camera source position every frame
		try:
			import AvatarInputHandler.cameras as _cams
			_orig_cam_update = getattr(_cams.SniperCamera, '_orig_cam_update', None)
			if not _orig_cam_update:
				_orig_cam_update = _cams.SniperCamera._SniperCamera__cameraUpdate
				_cams.SniperCamera._orig_cam_update = _orig_cam_update
			_mv_ref = mock_veh
			_vm_ref = veh_matrix
			def _patched_cam_update(cam_self, *a, **kw):
				_orig_cam_update(cam_self, *a, **kw)
				try:
					cam = getattr(cam_self, '_SniperCamera__cam', None)
					if cam is not None and hasattr(cam, 'source'):
						if 'gun_node_matrix' in loaded_models:
							cam.source = loaded_models['gun_node_matrix']
						else:
							mp = Math.WGTranslationOnlyMP()
							mp.source = _vm_ref
							cam.source = mp
				except Exception:
					pass
			_cams.SniperCamera._SniperCamera__cameraUpdate = _patched_cam_update
			_cams.SniperCamera._offhangar_patched = True
			LOG_DEBUG('OfflineBattle.SniperCamera.__cameraUpdate patched')
		except Exception:
			LOG_CURRENT_EXCEPTION()

		# Patch control_modes and cameras ticks to stop gracefully after player is gone
		try:
			import AvatarInputHandler.control_modes as _ctrl
			import AvatarInputHandler.cameras as _cams2
			
			if hasattr(_ctrl.ArcadeControlMode, '_ArcadeControlMode__tick') and not hasattr(_ctrl.ArcadeControlMode, '_offhangar_patched'):
				# Patch ArcadeControlMode.__tick
				_orig_ctrl_tick = getattr(_ctrl.ArcadeControlMode, '_ArcadeControlMode__tick')
				def _safe_ctrl_tick(self_cm, *a, **kw):
					if BigWorld.player() is None:
						return  # Stop ticking after battle ends
					return _orig_ctrl_tick(self_cm, *a, **kw)
				_ctrl.ArcadeControlMode._ArcadeControlMode__tick = _safe_ctrl_tick
				_ctrl.ArcadeControlMode._offhangar_patched = True
				
			if hasattr(_cams2, 'ArcadeCamera') and hasattr(_cams2.ArcadeCamera, '_ArcadeCamera__cameraUpdate') and not hasattr(_cams2.ArcadeCamera, '_offhangar_patched'):
				# Patch ArcadeCamera.__cameraUpdate
				_orig_arc_cam = getattr(_cams2.ArcadeCamera, '_ArcadeCamera__cameraUpdate')
				def _safe_arc_cam(self_ac, *a, **kw):
					if BigWorld.player() is None:
						# The original reschedules itself as its FIRST statement, so a
						# plain early return here kills camera pivot updates permanently
						# after one transient None-player tick. Keep the chain alive.
						try:
							self_ac._ArcadeCamera__cameraUpdateCallbackId = BigWorld.callback(0.5, lambda: _safe_arc_cam(self_ac))
						except Exception:
							pass
						return
					return _orig_arc_cam(self_ac, *a, **kw)
				_cams2.ArcadeCamera._ArcadeCamera__cameraUpdate = _safe_arc_cam
				_cams2.ArcadeCamera._offhangar_patched = True
			
			LOG_DEBUG('OfflineBattle.control_modes/cameras ticks patched for safe exit')
		except Exception:
			LOG_CURRENT_EXCEPTION()

		_install_input_chain_debug()
		g_offline_aih = AvatarInputHandler.AvatarInputHandler()
		player.inputHandler = g_offline_aih
		try:
			g_offline_aih.start()
		except Exception as e:
			import traceback
			LOG_DEBUG('AvatarInputHandler.start ERROR:', traceback.format_exc())
		
		# After AIH.start(), forcibly redirect camera to our spawn position.
		# AIH may set cam.target to (0,0,0) from a defaulted entity matrix.
		# We override it directly using CursorCamera.
		def _force_camera_to_model():
			try:
				import BigWorld, Math
				cam = BigWorld.camera()
				if cam is not None and hasattr(cam, 'target'):
					# Set cam.target to a translation-only provider tracking veh_matrix.
					# This prevents the camera from turning when the tank hull turns.
					mp = Math.WGTranslationOnlyMP()
					mp.source = veh_matrix
					cam.target = mp
					LOG_DEBUG('OfflineBattle.force_camera: set target to', veh_pos[0], veh_pos[1], veh_pos[2])
				else:
					LOG_DEBUG('OfflineBattle.force_camera: cam=', cam, 'has target=', hasattr(cam, 'target') if cam else False)
			except Exception as e:
				import traceback
				LOG_DEBUG('OfflineBattle.force_camera ERROR:', traceback.format_exc())
		BigWorld.callback(0.1, _force_camera_to_model)
		BigWorld.callback(0.5, _force_camera_to_model)
		BigWorld.callback(1.0, _force_camera_to_model)


		from gui import WindowsManager
		from gui.Scaleform.Waiting import Waiting
		try:
			player = BigWorld.player()
			
			import gui.Scaleform.Battle
			import Avatar
			class _FakeAvatarMod(object):
				PlayerAvatar = type(player)
			
			if hasattr(gui.Scaleform.Battle, 'Avatar'):
				gui.Scaleform.Battle.orig_Avatar = gui.Scaleform.Battle.Avatar
			gui.Scaleform.Battle.Avatar = _FakeAvatarMod
			
			if hasattr(Avatar, 'PlayerAvatar'):
				Avatar.orig_PlayerAvatar = Avatar.PlayerAvatar
			Avatar.PlayerAvatar = type(player)
			
			if not hasattr(player, 'denunciationsLeft'):
				player.denunciationsLeft = 0
				
			if not hasattr(player, 'onSpaceLoaded'):
				class _DummyEvent(object):
					def __iadd__(self, *a, **k): return self
					def __isub__(self, *a, **k): return self
					def __call__(self, *a, **k): return True
					def isActive(self): return True
				player.onSpaceLoaded = _DummyEvent()
			
			if not hasattr(player, 'playerVehicleID'):
				player.playerVehicleID = 0
				
			import types
			if hasattr(player, 'getOwnVehicleShotDispersionAngle'):
				# Rebind EVERY battle: the old name-based guard kept the FIRST
				# battle's _gun_state closure forever, so later tanks fired with
				# battle 1's dispersion. Keep the true original on the player.
				_orig_get_disp = getattr(player, '_offh_orig_get_disp', None)
				if _orig_get_disp is None:
					_orig_get_disp = player.getOwnVehicleShotDispersionAngle
					player._offh_orig_get_disp = _orig_get_disp
				def _mock_getOwnVehicleShotDispersionAngle(self, turretRotationSpeed, withShot=0):
					orig = _orig_get_disp(turretRotationSpeed, withShot)
					return (_gun_state.get('dispersion', orig[0]), orig[1])
				player.getOwnVehicleShotDispersionAngle = types.MethodType(_mock_getOwnVehicleShotDispersionAngle, player)
			
			# VŽDY resetuj životní funkce při nové bitvě
			player.isVehicleAlive = True
			# Crew voices need this object to exist. Retail builds it in
			# Avatar.__startGUI; offline it was only ever created lazily, on the first
			# reload or on death. Until then every voice line hit
			#     getattr(player, 'soundNotifications', None)
			# on a PlayerAccount, which RAISES AttributeError, so getattr answered None
			# and the line was dropped without a word. python.log shows the same gap from
			# the other side: "PlayerAccount object has no attribute soundNotifications".
			# Build it once, here, before anything can want it.
			try:
				if _offh_player_notifications() is not None:
					LOG_DEBUG('OfflineBattle: soundNotifications ready at battle start')
			except Exception as _isne:
				LOG_DEBUG('soundNotifications init err:', str(_isne))
			player._is_dead = False
			player._offh_spectating = False
			player._offh_spec_cur = None
			player._offh_spec_idx = 0
			# Belt and braces: if a previous battle exited on a path that skipped the
			# sweep, the postmortem grading would still be on. The control mode sets its
			# own preset a moment later anyway, but start from a clean slate.
			try:
				from post_processing import g_postProcessing as _offh_pp3
				_offh_pp3.disable()
			except Exception:
				pass
			player._crosshair_init_done = False
			if hasattr(player, 'vehicle') and player.vehicle is not None:
				try: player.vehicle.typeDescriptor = td
				except Exception: pass
				player.vehicle.health = getattr(td, 'maxHealth', 400)
				# This one is the REAL BigWorld vehicle, and Battle.py binds the damage
				# panel to it first: DamagePanel._setup calls vehicle.isAlive(). A plain
				# True shadows the entity's own method and the call raises
				# TypeError: 'bool' object is not callable, killing the panel setup on
				# every retry of its 0.05 s waiting loop.
				_offh_set_alive(player.vehicle, True)
				
			if not hasattr(player, 'name'):
				player.name = 'Player'
			if not hasattr(player, 'team'):
				player.team = 1
			
			

			


			# ---- consumables / equipment (ported) ----
			def _offh_player_mock():
				import BigWorld
				_mv = globals().get('G_MOCK_VEHICLES', {}) or {}
				return _mv.get(getattr(BigWorld.player(), 'playerVehicleID', -1))
			
			def _offh_device_ui_state(mock, td, ui_name):
				from gui.mods.offhangar import device_damage as _DDs
				healths = _REPAIR_UI_TO_HEALTH.get(ui_name, (ui_name + 'Health',))
				destroyed = getattr(mock, '_destroyed_devices', None) or set()
				dh = getattr(mock, 'devices_hp', None) or {}
				st = None
				for h in healths:
					if h in destroyed:
						return 'destroyed'
					if h in dh:
						mx = _DDs.device_max_hp(td, h)
						if mx is not None and dh[h] < mx:
							st = 'critical'
				return st
			
			def _offh_repair_device(mock, td, ui_name):
				from gui.mods.offhangar import device_damage as _DDs
				healths = _REPAIR_UI_TO_HEALTH.get(ui_name, (ui_name + 'Health',))
				dh = getattr(mock, 'devices_hp', None)
				if dh is None:
					dh = {}
					mock.devices_hp = dh
				destroyed = _dev_destroyed_set(mock)
				repaired = False
				for h in healths:
					mx = _DDs.device_max_hp(td, h)
					if (h in destroyed) or (h in dh and (mx is None or dh[h] < mx)):
						repaired = True
					if mx is not None:
						dh[h] = mx
					destroyed.discard(h)
					if getattr(mock, '_module_states', None):
						mock._module_states.pop(h, None)
				_refresh_mobility_flags(mock)
				try:
					import gui.WindowsManager
					bw = gui.WindowsManager.g_windowsManager.battleWindow
					if bw is not None and hasattr(bw, 'damagePanel'):
						for h in healths:
							try: bw.damagePanel.updateState(_module_ui_name(h), 'normal')
							except Exception: pass
				except Exception:
					pass
				return repaired
			
			def _offh_activate_equipment(idx, deviceName=None):
				import BigWorld, random
				from gui.mods.offhangar import device_damage as _DDs
				mock = _offh_player_mock()
				if mock is None:
					return
				cons = None
				for c in _gun_state.get('consumables', []):
					if c.get('slot') == idx:
						cons = c
						break
				if cons is None or cons.get('used'):
					return
				tag = cons.get('tag')
				name = str(cons.get('name', '')).lower()
				td = _device_td(mock)
				try:
					import gui.WindowsManager
					bw = gui.WindowsManager.g_windowsManager.battleWindow
				except Exception:
					bw = None
				panel = getattr(bw, 'consumablesPanel', None) if bw is not None else None
				def _consume():
					cons['used'] = True
					if panel is not None:
						try: panel.setItemQuantityInSlot(idx, 0)
						except Exception: pass
						# 0, NOT -1. In ConsumablesPanel, -1 is the "optional device is ACTIVE"
						# signal - setOptionalDeviceState sends `-1 if isOn else 0` - so passing
						# it here lit the slot up GREEN the instant the kit was emptied, and the
						# spent consumable read as ready again. The real equipment path
						# (Avatar.__processVehicleEquipments) only ever zeroes the quantity.
						try: panel.setCoolDownTime(idx, 0)
						except Exception: pass
					LOG_DEBUG('CONSUMABLE spent: slot=%s tag=%s name=%s' % (idx, tag, name))
				def _err(msg):
					try:
						if bw is not None and hasattr(bw, 'vErrorsPanel'):
							bw.vErrorsPanel.showMessage(msg)
					except Exception:
						pass
				if tag == 'extinguisher':
					if getattr(mock, 'is_on_fire', False):
						_offh_extinguish(mock, mock is _offh_player_mock(), 'extinguisher')
						_consume()
					else:
						_err('extinguisherDoesNotActivated')
					return
				is_big = ('large' in name)
				if tag == 'repairkit':
					if deviceName is not None:
						if _offh_repair_device(mock, td, str(deviceName)):
							_consume()
						if panel is not None:
							try: panel.collapseEquipmentSlot(idx)
							except Exception: pass
							# collapseEquipmentSlot only animates Flash; the private expand index is
							# otherwise cleared by a callback we cannot rely on offline.
							try: panel._ConsumablesPanel__removeExpandEquipment(idx)
							except Exception: pass
						return
					_damaged = bool(getattr(mock, '_destroyed_devices', None))
					if not _damaged:
						dh = getattr(mock, 'devices_hp', None) or {}
						for _h, _hp in dh.items():
							_mx = _DDs.device_max_hp(td, _h)
							if _mx is not None and _hp < _mx:
								_damaged = True
								break
					if not _damaged:
						_err('repairkitAllDevicesAreNotDamaged')
						return
					if is_big:
						dh = getattr(mock, 'devices_hp', None) or {}
						for _h in list(dh.keys()):
							_mx = _DDs.device_max_hp(td, _h)
							if _mx is not None:
								dh[_h] = _mx
						if getattr(mock, '_destroyed_devices', None):
							mock._destroyed_devices.clear()
						_refresh_mobility_flags(mock)
						if getattr(mock, '_module_states', None):
							mock._module_states.clear()
						if bw is not None and hasattr(bw, 'damagePanel'):
							for _dn in ('engineHealth', 'ammoBayHealth', 'fuelTankHealth', 'radioHealth', 'leftTrackHealth', 'rightTrackHealth', 'gunHealth', 'turretRotatorHealth', 'surveyingDeviceHealth'):
								try: bw.damagePanel.updateState(_module_ui_name(_dn), 'normal')
								except Exception: pass
						_consume()
					else:
						entityStates = {}
						devs = getattr(getattr(td, 'type', None), 'devices', None)
						if devs:
							for d in devs:
								dn = getattr(d, 'name', '')
								if dn.endswith('Health'):
									ui = dn[:-6]
									entityStates[ui] = _offh_device_ui_state(mock, td, ui)
						else:
							for ui in ('engine', 'ammoBay', 'gun', 'turretRotator', 'leftTrack', 'rightTrack', 'surveyingDevice', 'radio', 'fuelTank'):
								entityStates[ui] = _offh_device_ui_state(mock, td, ui)
						if panel is not None:
							try: panel.expandEquipmentSlot(idx, 'repairkit', entityStates)
							except Exception as _ee: LOG_DEBUG('expandEquipmentSlot(repairkit) err:', str(_ee))
						else:
							# No panel to pick from (offline the Flash slot does not always expand):
							# a SMALL kit still repairs exactly ONE module, never the whole tank.
							# Destroyed first, otherwise the worst damaged one.
							_one = None
							_dhk = getattr(mock, 'devices_hp', None) or {}
							_dead = sorted(getattr(mock, '_destroyed_devices', None) or ())
							if _dead:
								_one = _dead[0]
							else:
								_worst = None
								for _h2, _hp2 in sorted(_dhk.items()):
									_mx2 = _DDs.device_max_hp(td, _h2)
									if _mx2 and _hp2 < _mx2:
										_frac = float(_hp2) / float(_mx2)
										if _worst is None or _frac < _worst[0]:
											_worst = (_frac, _h2)
								if _worst is not None:
									_one = _worst[1]
							if _one is not None and _offh_repair_device(mock, td, _module_ui_name(_one)):
								LOG_DEBUG('SMALL REPAIR KIT: repaired %s only (no selection panel)' % _one)
								_consume()
					return
				if tag == 'medkit':
					ko = getattr(mock, '_crew_ko', None) or set()
					if deviceName is not None:
						if deviceName in ko:
							ko.discard(deviceName)
							_recompute_crew_impaired(mock)
							if bw is not None and hasattr(bw, 'damagePanel'):
								try: bw.damagePanel.updateState(str(deviceName), 'normal')
								except Exception: pass
							_consume()
						if panel is not None:
							try: panel.collapseEquipmentSlot(idx)
							except Exception: pass
							try: panel._ConsumablesPanel__removeExpandEquipment(idx)
							except Exception: pass
						return
					if not ko:
						_err('medkitAllTankmenAreSafe')
						return
					if is_big:
						for _cn in list(ko):
							if bw is not None and hasattr(bw, 'damagePanel'):
								try: bw.damagePanel.updateState(_cn, 'normal')
								except Exception: pass
						ko.clear()
						_recompute_crew_impaired(mock)
						_consume()
					else:
						entityStates = {}
						for _cn in _crew_roster(td):
							entityStates[_cn] = 'destroyed' if _cn in ko else None
						if panel is not None:
							try: panel.expandEquipmentSlot(idx, 'medkit', entityStates)
							except Exception as _me: LOG_DEBUG('expandEquipmentSlot(medkit) err:', str(_me))
					return
			
			def _offh_damage_icon(tag, deviceName=None):
				for c in _gun_state.get('consumables', []):
					if c.get('tag') == tag and not c.get('used'):
						_offh_activate_equipment(c.get('slot'), deviceName)
						return
			
			# ---- broken-track visual (ported) ----
			def _clear_crashed_track(mock):
				'''Detach and drop a mock's crashed-track overlay, reset the cached state.'''
				cm = getattr(mock, '_crashed_track_model', None)
				parent = getattr(mock, '_crashed_track_parent', None)
				if cm is not None and parent is not None:
					try:
						parent.root.detach(cm)
					except Exception as _de:
						LOG_DEBUG('crashed model detach err:', str(_de))
				mock._crashed_track_model = None
				mock._crashed_track_fashion = None
				mock._crashed_track_parent = None
			
			def _sync_crashed_track(mock, chassis_model, fashion, td):
				'''Broken-track visual, like the game's _CrashedTrackController. Idempotent
				through a cached (left, right) state; every native call is guarded because a
				fashion on an offline mock is delicate.'''
				if mock is None:
					return
				destroyed = getattr(mock, '_destroyed_devices', None) or set()
				left = 'leftTrackHealth' in destroyed
				right = 'rightTrackHealth' in destroyed
				state = (left, right)
				_dead = (getattr(mock, 'health', 1) <= 0) or getattr(mock, '_is_killed', False)
				# Hot path (every frame, every bot): alive and unchanged -> out before the
				# config read, so this stays cheap.
				if not _dead and getattr(mock, '_crashed_tracks_state', None) == state:
					return
				try:
					from _constants import CONFIG_OPTIONS as _CTV
					if not bool(_CTV.get('crashed_track_visual', True)):
						return
				except Exception:
					pass
				# A dead tank already shows its full destroyed model with broken tracks baked
				# in, so drop the overlay instead of driving it.
				if _dead:
					if getattr(mock, '_crashed_track_model', None) is not None:
						_clear_crashed_track(mock)
					mock._crashed_tracks_state = None
					return
				any_broken = left or right
				crashed_model = getattr(mock, '_crashed_track_model', None)
				crashed_fashion = getattr(mock, '_crashed_track_fashion', None)
				# The live fashion attaches a moment after spawn; until it exists we cannot
				# hide the intact track, so retry next frame rather than cache a half state.
				if fashion is None and (any_broken or crashed_model is not None):
					return
				mock._crashed_tracks_state = state
				# 1) live chassis: hide the intact scrolling track on the broken side(s)
				if fashion is not None:
					try:
						fashion.hideTracks(bool(left), bool(right))
					except Exception as _he:
						LOG_DEBUG('hideTracks(main) err:', str(_he))
				if any_broken:
					# 2) attach the destroyed chassis model + its own fashion, once
					if crashed_model is None and chassis_model is not None and td is not None:
						try:
							crashed_model = BigWorld.Model(td.chassis['models']['destroyed'])
							try:
								crashed_fashion = BigWorld.WGVehicleFashion(True)
							except Exception:
								crashed_fashion = BigWorld.WGVehicleFashion()
							try:
								crashed_fashion.maxMovement = td.physics['speedLimits'][0]
								_sw = td.hull['swinging']
								crashed_fashion.setPitchSwinging('V', *_sw['pitchParams'])
								crashed_fashion.setRollSwinging('V', *_sw['rollParams'])
								crashed_fashion.setShotSwinging('V', _sw['sensitivityToImpulse'])
								_tr = td.chassis['tracks']
								crashed_fashion.setLods(td.chassis['traces']['lodDist'], td.chassis['wheels']['lodDist'], _tr['lodDist'], _sw['lodDist'])
								crashed_fashion.setTracks(_tr['leftMaterial'], _tr['rightMaterial'], _tr['textureScale'])
								crashed_fashion.movementInfo = Math.Vector4(0.0, 0.0, 0.0, 0.0)
							except Exception as _cfe:
								LOG_DEBUG('crashed fashion setup err:', str(_cfe))
							try:
								crashed_model.wg_fashion = crashed_fashion
							except Exception:
								pass
							try:
								chassis_model.root.attach(crashed_model)
								mock._crashed_track_model = crashed_model
								mock._crashed_track_fashion = crashed_fashion
								mock._crashed_track_parent = chassis_model
							except Exception as _ae:
								LOG_DEBUG('crashed model attach err:', str(_ae))
								crashed_model = None
								crashed_fashion = None
						except Exception as _cme:
							LOG_DEBUG('crashed model build err:', str(_cme))
					# show ONLY the broken side(s) on the overlay
					if crashed_fashion is not None:
						try:
							crashed_fashion.hideTracks(not left, not right)
						except Exception as _che:
							LOG_DEBUG('hideTracks(crashed) err:', str(_che))
				else:
					# both tracks functional again: drop the overlay, restore the live tracks
					if crashed_model is not None:
						_clear_crashed_track(mock)
			
			# ---- crew injuries (ported) ----
			def _device_td(mock):
				# PERF: this used to read
				#   getattr(mock, 'typeDescriptor', getattr(BigWorld.player(), 'vehicleTypeDescriptor', None))
				# and Python evaluates arguments EAGERLY, so the fallback ran on every
				# call even when the mock had its own descriptor - 38128 forced
				# BigWorld.player() calls plus 38128 trips through the offline account's
				# __getattribute__ per 300 ticks. Only take that path when it is needed.
				td = getattr(mock, 'typeDescriptor', None)
				if td is not None:
					return td
				import BigWorld
				return getattr(BigWorld.player(), 'vehicleTypeDescriptor', None)
			
			def _crew_roster(td):
				# Crew instance names ('commander','driver','gunner1',...) - the crew health
				# extra names minus 'Health'.
				names = []
				try:
					enumRoles = {'gunner': 1, 'loader': 1, 'radioman': 1}
					for roles in getattr(getattr(td, 'type', None), 'crewRoles', []):
						mainRole = roles[0]
						if mainRole in enumRoles:
							names.append(mainRole + str(enumRoles[mainRole]))
							enumRoles[mainRole] += 1
						else:
							names.append(mainRole)
				except Exception:
					pass
				if not names:
					names = ['commander', 'driver', 'gunner1', 'loader1', 'radioman1']
				return names
			
			def _recompute_crew_impaired(mock):
				# Cache the impaired BASE roles. A small crew has men covering SEVERAL roles,
				# so one casualty can impair more than one - read that from td.type.crewRoles
				# rather than assuming one role per man.
				from gui.mods.offhangar import device_damage as _DDc
				ko = getattr(mock, '_crew_ko', None)
				if not ko:
					mock._crew_impaired = frozenset()
					return
				td = _device_td(mock)
				roster = _crew_roster(td)
				try:
					crewRoles = list(getattr(getattr(td, 'type', None), 'crewRoles', []))
				except Exception:
					crewRoles = []
				roles = set()
				for i, inst in enumerate(roster):
					if inst in ko:
						if i < len(crewRoles):
							for r in crewRoles[i]:
								roles.add(r)
						else:
							roles.add(_DDc.crew_role_base(inst))
				mock._crew_impaired = frozenset(roles)
			
			def _crew_factor(mock, stat):
				# Stat multiplier from this mock's knocked-out crew (1.0 when all fit).
				imp = getattr(mock, '_crew_impaired', None)
				if not imp:
					return 1.0
				try:
					from gui.mods.offhangar import device_damage as _DDc
					return _DDc.crew_stat_factor(imp, stat)
				except Exception:
					return 1.0
			
			def _module_factor(mock, stat):
				# Stat multiplier from this mock's MODULE state (1.0 when everything is
				# whole), the counterpart of _crew_factor. The client never had these -
				# avatar.py only gates input on a destroyed engine/track/gun - so the
				# numbers are reconstructed in device_damage.DAMAGED_MODULE_EFFICIENCY.
				if mock is None:
					return 1.0
				try:
					return _dd().module_stat_factor(getattr(mock, 'devices_hp', None),
					                              getattr(mock, '_destroyed_devices', None),
					                              _device_td(mock), stat)
				except Exception:
					return 1.0

			def _knock_out_crew(mock, crew_name, is_player_target):
				# Binary knock-out (a med kit revives). True when newly downed.
				ko = getattr(mock, '_crew_ko', None)
				if ko is None:
					ko = set()
					mock._crew_ko = ko
				if crew_name in ko:
					return False
				ko.add(crew_name)
				_recompute_crew_impaired(mock)
				LOG_DEBUG('CREW KO:', getattr(mock, 'id', 'PLAYER'), crew_name)
				if is_player_target:
					try:
						import gui.WindowsManager
						bw = gui.WindowsManager.g_windowsManager.battleWindow
						if bw is not None and hasattr(bw, 'damagePanel'):
							try: bw.damagePanel.updateState(crew_name, 'destroyed')
							except Exception as _cse: LOG_DEBUG('crew updateState err:', crew_name, str(_cse))
						_tdk = getattr(BigWorld.player(), 'vehicleTypeDescriptor', None)
						_exk = _tdk.extrasDict.get(crew_name + 'Health') if (_tdk is not None and hasattr(_tdk, 'extrasDict')) else None
						_sndk = getattr(_exk, 'sounds', {}).get('destroyed') if _exk is not None else None
						_offh_play_crit_voice(_sndk)
					except Exception as _ke:
						LOG_DEBUG('crew KO ui err:', str(_ke))
				return True
			
			# ---- module damage / repair support (ported from the shared build) ----
			_REPAIR_UI_TO_HEALTH = {
				'engine': ('engineHealth',), 'ammoBay': ('ammoBayHealth',),
				'gun': ('gunHealth',), 'turretRotator': ('turretRotatorHealth',),
				'surveyingDevice': ('surveyingDeviceHealth',), 'radio': ('radioHealth',),
				'fuelTank': ('fuelTankHealth',),
				'chassis': ('leftTrackHealth', 'rightTrackHealth'),
				'track': ('leftTrackHealth', 'rightTrackHealth'),
				'leftTrack': ('leftTrackHealth',), 'rightTrack': ('rightTrackHealth',),
			}
			
			def _dev_destroyed_set(mock):
				s = getattr(mock, '_destroyed_devices', None)
				if s is None:
					s = set()
					mock._destroyed_devices = s
				return s
			
			def _module_ui_name(name):
				# Damage-panel device name = extra name minus 'Health'; tracks keep their
				# side, which is what the real 0.8.2 Avatar sends.
				return name[:-6] if name.endswith('Health') else name
			
			def _refresh_mobility_flags(mock):
				# A destroyed track/engine is functional again only once auto-repair reaches
				# ~50% (the repair tick drops it from the set), so gameplay keys off the
				# destroyed-set rather than raw HP.
				s = _dev_destroyed_set(mock)
				mock.is_tracked = ('leftTrackHealth' in s) or ('rightTrackHealth' in s)
				mock.is_engine_dead = ('engineHealth' in s)
				mock.is_gun_destroyed = ('gunHealth' in s)
				mock.is_turret_locked = ('turretRotatorHealth' in s)
			
			# These live in the battle scope, but _offh_knock_out_everything is module-level
			# and is what every death path calls. Reaching them from there raised NameError
			# into a bare except, so a destroyed tank pushed NOTHING to the damage panel -
			# 'KNOCKOUT called: ... crew=0' in the log with no follow-up line was exactly
			# that (_crew_roster has a 5-man fallback and cannot return an empty list).
			for _hn, _hf in (('_device_td', _device_td),
				('_crew_roster', _crew_roster),
				('_recompute_crew_impaired', _recompute_crew_impaired),
				('_dev_destroyed_set', _dev_destroyed_set),
				('_module_ui_name', _module_ui_name),
				('_refresh_mobility_flags', _refresh_mobility_flags)):
				globals()[_hn] = _hf
			
			def _offh_play_crit_voice(snd):
				'''Queue one module or crew voice line for the player's own tank.
				
				No throttling here, deliberately. gui/sound_notifications.xml gives every
				module and crew line playRules 3 - append to the voice queue and wait its
				turn - and the engine already rate-limits repeats of the SAME line through
				minTimeBetweenEvents. Only playRules 1 wipes the queue, and that is reserved
				for the kill lines. An earlier version of this function held a single global
				0.7 s gate that dropped any line following any other one whatever they were,
				so a shell that broke a track AND downed the driver reported whichever won
				the race and silently swallowed the other.
				
				The binding is explicit because every one of these events sets
				shouldBindToPlayer, which makes play() resolve it through
				BigWorld.player().vehicle.id - a stub offline. __playFirstFromQueue drops any
				queued line whose bound vehicle is not in arena.vehicles or not alive, with no
				error, which is the other half of why these lines came and went.'''
				import BigWorld
				# One strike, one report. A single shell now routinely crits two or three
				# things at once (34 such strikes in one battle log), and every line is
				# ~2.5 s while WG drops anything that has waited longer than its 3 s
				# timeout - so in a busy fight the extra lines were not being heard, they
				# were being binned. While a strike is scoring, calls land in this list and
				# the worst one is spoken at the end of it.
				if _OFFH_VOICE_BURST[0] is not None:
					if snd:
						_OFFH_VOICE_BURST[0].append(snd)
					return
				if not snd:
					return
				try:
					_p = BigWorld.player()
					# OWN queue for crew and module lines.
					#
					# IngameSoundNotifications keeps ONE active event per category, and in
					# sound_notifications.xml practically everything is category 'voice':
					#   module/crew lines   playRules 3  - queue up and wait
					#   armor_pierced_*     playRules 2  - jump the queue
					#   *_killed_*          playRules 1  - WIPE the queue, stop what is talking
					#
					# Sharing one queue means the polite lines never get a turn in a close
					# fight, and get cut mid-word when they do - which reads as "crew sounds
					# break when enemies are near". Measured: it is NOT channel starvation,
					# the log shows 12 live events, 4 shot effects a second and zero failures.
					# A second instance gives them a queue that hit and kill reports cannot
					# reach. Same class, same rules - only the contention is gone.
					_sn = globals().get('g_offh_crew_notif')
					if _sn is None:
						try:
							_sn = _offh_make_notifications()
							if _sn is None:
								raise RuntimeError('IngameSoundNotifications unavailable')
							globals()['g_offh_crew_notif'] = _sn
							LOG_DEBUG('OfflineBattle: crew voice queue created (separate from hit/kill reports)')
							# DIAGNOSTIC SHIM. Only three things can stop a line that is already
							# speaking: a playRules 1 event, enable(False) and enableCategory - and none
							# of them should ever reach THIS instance. Wrap the two methods that do the
							# stopping so the caller has to identify itself. The wrapper only logs and
							# forwards; behaviour is unchanged. Remove once the cutter is found.
							try:
								import traceback as _tbv
								def _crew_shim(_inst, _attr, _label):
									_orig = getattr(_inst, _attr, None)
									if _orig is None:
										return
									def _wrapped(*_a, **_kw):
										try:
											LOG_DEBUG('CREWVOICE CUT: %s%s' % (_label, _a and (' ' + repr(_a)) or ''))
											for _ln in _tbv.format_stack()[-7:-1]:
												LOG_DEBUG('   ' + _ln.strip().replace(chr(10), ' | '))
										except Exception:
											pass
										return _orig(*_a, **_kw)
									setattr(_inst, _attr, _wrapped)
								_crew_shim(_sn, '_IngameSoundNotifications__clearQueue', 'clearQueue')
								_crew_shim(_sn, 'cancel', 'cancel')
								_crew_shim(_sn, 'enable', 'enable')
								_crew_shim(_sn, 'enableCategory', 'enableCategory')
							except Exception as _she:
								LOG_DEBUG('crew voice shim err:', str(_she))
						except Exception as _cne:
							LOG_DEBUG('crew voice queue err:', str(_cne))
							_sn = _offh_player_notifications()
					if _sn is None:
						return
					# NEVER bind these to a vehicle. __playFirstFromQueue re-checks the
					# binding at PLAY time against arena.vehicles - a fake arena here - and a
					# miss makes it `continue` with no error at all. Binding buys nothing
					# either: these are the player's own crew lines, and an unbound line
					# always plays.
					#
					# Passing None is only HALF of not binding, which is what kept these
					# lines silent for so long: every module and crew event sets
					# shouldBindToPlayer, so play() would re-bind a None through
					# BigWorld.player().vehicle - an attribute a PlayerAccount does not
					# have - and raise before queueing anything. The flag is cleared on
					# the instance in _offh_prepare_notifications; this stays None.
					_vid = None
					# Verify the instance instead of trusting it. destroy() leaves an object
					# that looks fine but has __soundQueues = None and __isEnabled False, and
					# it then swallows every line for the rest of the battle without a word.
					try:
						_qs = getattr(_sn, '_IngameSoundNotifications__soundQueues', None)
						_en = getattr(_sn, '_IngameSoundNotifications__isEnabled', False)
						if _qs is None or not _en:
							_sn2 = _offh_make_notifications()
							if _sn2 is not None:
								_sn = _sn2
								globals()['g_offh_crew_notif'] = _sn
								LOG_DEBUG('CREWVOICE instance REBUILT (was dead: queues=%s enabled=%s)' % (_qs is not None, _en))
					except Exception, _e_rb:
						LOG_DEBUG('CREWVOICE rebuild failed: %s' % _e_rb)
					# Which instance actually speaks? The last log had no 'queue created' line
					# and still played, which would mean these lines go into WG's instance -
					# where every playRules 1 kill report stops whatever is speaking.
					try:
						if not globals().get('_offh_crew_inst_logged'):
							globals()['_offh_crew_inst_logged'] = True
							LOG_DEBUG('CREWVOICE instance: ours=%s wg=%s' % (
								_sn is globals().get('g_offh_crew_notif'),
								_sn is getattr(_p, 'soundNotifications', None)))
					except Exception:
						pass
					# Bound the backlog - but do NOT wipe it.
					#
					# This used to `del _vq[:]` on every call, i.e. at most ONE line could ever be
					# waiting and any line already waiting was thrown away by the next report.
					# Together with the burst picker only ever offering one line per strike, that
					# is why crits went unannounced: a shell that broke a track AND downed the
					# driver could physically only produce one line, and a second shell landing
					# before the first finished speaking deleted whatever was still queued.
					#
					# The backlog the wipe was aimed at is already handled by WG, and handled
					# better: play() stamps every item with time + timeout (3 s) and
					# __playFirstFromQueue drops any item whose stamp has passed. Prune by that
					# same rule here so the cap counts only lines that can still be spoken in
					# time, then allow a couple to wait. A line arriving at a full queue is
					# dropped rather than displacing one, because the burst enqueues worst-first:
					# whatever is already in there outranks the newcomer.
					try:
						_q = getattr(_sn, '_IngameSoundNotifications__soundQueues', None)
						_vq = _q.get('voice') if _q else None
						if _vq is not None:
							_vq_now = BigWorld.time()
							_vq[:] = [_vqi for _vqi in _vq if _vqi[1] > _vq_now]
							if len(_vq) >= _OFFH_VOICE_QUEUE_MAX:
								LOG_DEBUG('CREWVOICE queue full (%d waiting), dropped: %s' % (len(_vq), snd))
								return
					except Exception:
						pass
					# NO burst rule here any more, and it must not come back.
					#
					# It used to cancel the previous line whenever a new one arrived within
					# 2 s, on the theory that the newest report is the truest. What that
					# actually did was cut the line that was still speaking - which IS the
					# "crew sounds get cut off" report. The log caught it red-handed:
					#   CREWVOICE play: driver_killed
					#   CREWVOICE play: radio_damaged
					#   CREWVOICE CUT: cancel ('driver_killed', False)
					# One shell that downs the driver and knocks the radio about produces two
					# crits milliseconds apart, and the second one silenced the first mid-word.
					# Interior crits make that combination the normal case rather than a rare
					# one, so the rule went from occasionally rude to constantly wrong.
					#
					# The problem it was aimed at - a queued line landing long after the moment
					# it describes - is already solved by WG: play() stamps every queue item
					# with `time + soundDesc['timeout']`, timeout defaults to 3.0 s, and
					# __playFirstFromQueue silently drops any item whose stamp has passed. A
					# report that cannot be spoken within 3 s is discarded on its own, without
					# anyone having to interrupt the line in progress.
					# A destroyed line makes the damaged one obsolete. Appending it (playRules
					# 3) means the crew keeps reporting the track as merely damaged for another
					# ~2.5 s while it is already gone and repairs may be running - the 'lagging
					# behind' the tester described. So the weaker line for the SAME device is
					# dropped from the WAITING queue.
					#
					# Queue only. cancel() would also stop the weaker line if it happened to be
					# the one speaking, and cutting a line in progress is the very complaint
					# this build set out to fix. Deleting queue entries is safe: it is exactly
					# what cancel() does to the queue, and it never touches __activeEvents.
					#
					# Do NOT generalise this by stopping the active sound directly and clearing
					# activeEvents by hand - 1.3.6 did that and the crew went silent for the
					# whole battle: __onSoundEnd still fires for the stopped sound, finds the
					# slot already empty, and the queue pump WG drives from there never runs
					# again.
					try:
						if snd.endswith('_destroyed'):
							_weak = snd[:-len('_destroyed')] + '_damaged'
							_evs = getattr(_sn, '_IngameSoundNotifications__events', None) or {}
							_wpath = ((_evs.get(_weak) or {}).get('voice') or {}).get('sound')
							_qv = (getattr(_sn, '_IngameSoundNotifications__soundQueues', None) or {}).get('voice')
							if _wpath and _qv:
								for _qi in range(len(_qv) - 1, -1, -1):
									if _qv[_qi][0] == _wpath:
										del _qv[_qi]
										LOG_DEBUG('CREWVOICE dropped queued %s, superseded by %s' % (_weak, snd))
					except Exception:
						pass
					LOG_DEBUG('CREWVOICE play: %s bind=%s' % (snd, _vid))
					# Own try/except around the call. The outer one is bare, so every
					# exception play() raised so far was discarded - and play() CAN raise
					# here: it re-binds unbound lines via BigWorld.player().vehicle.id, and
					# our player is a mock. Report it instead of guessing at it.
					try:
						_sn.play(snd, _vid)
					except Exception, _e_pl:
						LOG_DEBUG('CREWVOICE play RAISED: %s: %s' % (type(_e_pl).__name__, _e_pl))
					# Re-order what is WAITING so the worst news is spoken first.
					#
					# playRules 3 is a plain append, which is right for a server that sends one
					# crit at a time but wrong here: a queue holding 'radio damaged' would make a
					# commander killed by the NEXT shell wait behind it, and at ~2.5 s a line the
					# 3 s stamp then discards it unheard. Only about one line can be spoken every
					# 2.5 s no matter what, so which line wins the slot is the whole game.
					#
					# Sorting the WAITING list only - never touching what is speaking - is safe:
					# it is the same list WG pops from, and it holds plain tuples.
					try:
						_vq2 = (getattr(_sn, '_IngameSoundNotifications__soundQueues', None) or {}).get('voice')
						if _vq2 and len(_vq2) > 1:
							_vq2.sort(key=lambda _it: 0 if (_it[0].endswith('_destroyed') or _it[0].endswith('_killed')) else 1)
					except Exception:
						pass
					# What did the call actually do? An empty queue with nothing active means
					# it never enqueued (unknown event name, or minTimeBetweenEvents). A
					# non-empty queue with something already active means the pump is stuck:
					# __playFirstFromQueue only runs `if activeEvents[category] is None`, so a
					# sound that never reports finishing mutes the whole rest of the battle.
					try:
						_qd = getattr(_sn, '_IngameSoundNotifications__soundQueues', None)
						_ad = getattr(_sn, '_IngameSoundNotifications__activeEvents', None)
						_av = (_ad or {}).get('voice')
						LOG_DEBUG('CREWVOICE after play: queued=%s active=%s' % (
							len((_qd or {}).get('voice') or []), _av and _av.get('soundPath')))
					except Exception:
						pass
					# An EARLY-END probe used to sit here. It is gone because it measured the
					# wrong thing: `duration` reports the LONGEST variant of a random FMOD
					# container, so a shorter variant ending normally looked like a cut. Its
					# verdict, before it was believed too far: two lines ended with WG's voice
					# slot empty the whole time and one ended while WG's line had already run
					# 1.44 s ALONGSIDE ours - no exclusive channel, nothing to steal.
				except Exception:
					pass
			
			def _push_device_ui(target_mock, is_player_target, name, current_hp, max_hp, state=None):
				# Entry probe. CREWVOICE play sits at the END of this chain, so a zero there
				# cannot tell "no module was ever damaged" apart from "the chain broke on the
				# way". This fires for EVERY module state push, bots included, before any of
				# the guards below.
				try:
					LOG_DEBUG('DEVUI %s hp=%s/%s state=%s player=%s' % (name, current_hp, max_hp, state, is_player_target))
				except Exception:
					pass
				# The damage panel only ever shows the PLAYER's own modules.
				if not is_player_target:
					return
				try:
					from gui.mods.offhangar import device_damage as _DDui
					import gui.WindowsManager
					bw = gui.WindowsManager.g_windowsManager.battleWindow
					if bw is None or not hasattr(bw, 'damagePanel'):
						return
					dev_state = state if state is not None else _DDui.device_state(current_hp, max_hp)
					ui_name = _module_ui_name(name)
					# updateState is the real 0.8.2 method (Battle.py:1491). The old code called
					# updateDeviceState, which does not exist - it raised into a bare except and
					# the panel never showed a single module hit.
					try: bw.damagePanel.updateState(ui_name, dev_state)
					except Exception as _e2: LOG_DEBUG('updateState error:', ui_name, dev_state, str(_e2))
					# Speak only when a module gets WORSE.
					#
					# This fired on every state change, and crew auto-repair produces a stream of
					# them: a destroyed module climbing back to its regen cap crosses into
					# 'critical', which announced "engine damaged" for a module that had just
					# gotten BETTER. Several modules repairing at once meant a constant feed into
					# a queue whose lines are ~2 s each - lines arriving late, on top of each
					# other, cut short. Retail has no such stream: the server reports a crit when
					# one happens and says nothing while the crew patches things up.
					try:
						# 'repaired' ranks with 'critical': the module is back in service but
						# still damaged, so it must not re-arm the announcement latch either.
						_rank = {'normal': 0, 'repaired': 1, 'critical': 1, 'destroyed': 2}
						_vs = getattr(target_mock, '_voice_states', None)
						if _vs is None:
							_vs = {}
							target_mock._voice_states = _vs
						# _vs holds what was ANNOUNCED, never merely what is current. Writing the
						# current state here was the overload: auto-repair lifts a destroyed
						# module into 'critical', which lowered the latch, and the next hit back
						# to 'destroyed' counted as a fresh worsening and spoke again. Repairs
						# make that boundary oscillate, so the same track got reported over and
						# over.
						#
						# A partial recovery must NOT re-arm anything. Only a finished repair -
						# back to 'normal' - does. One announcement per destruction, silence
						# however often it is hit while it lies there, and it may speak again
						# only after it has been repaired and destroyed anew. Same for every
						# module, which is what keeps a busy fight from turning into a stream.
						_was = _rank.get(_vs.get(name, 'normal'), 0)
						_now_r = _rank.get(dev_state, 0)
						if dev_state == 'normal':
							_vs[name] = 'normal'
						# Recovery lines are not announcements of damage, so they bypass the
						# worsening latch - retail plays one every time a module comes back. The
						# extra's sound keys are NOT the panel's state names (avatar.py maps
						# them): 'fixed' on a full repair, 'functional' when a destroyed module
						# is back in service, and 'functionalCanMove' for a track when the other
						# one is still under the tank.
						_snd_key = None
						if dev_state == 'normal':
							_snd_key = 'fixed'
						elif dev_state == 'repaired':
							_snd_key = 'functional'
							if name in ('leftTrackHealth', 'rightTrackHealth'):
								_other = 'rightTrackHealth' if name == 'leftTrackHealth' else 'leftTrackHealth'
								if _other not in (getattr(target_mock, '_destroyed_devices', None) or ()):
									_snd_key = 'functionalCanMove'
						elif _now_r > _was:
							_vs[name] = dev_state
							_snd_key = dev_state
						if _snd_key is not None:
							_tdu = getattr(BigWorld.player(), 'vehicleTypeDescriptor', None)
							_ex = _tdu.extrasDict.get(name) if (_tdu is not None and hasattr(_tdu, 'extrasDict')) else None
							_snd = getattr(_ex, 'sounds', {}).get(_snd_key) if _ex is not None else None
							_offh_play_crit_voice(_snd)
					except Exception:
						pass
				except Exception as _e:
					LOG_DEBUG('DAMAGE_PANEL_UI_ERR:', str(_e))
			
			# _offh_extinguish is module level (every fire path calls it) and needs this
			# one; publish it the same way the other battle-scope helpers are published.
			globals()['_push_device_ui'] = _push_device_ui

			def _tick_module_repair(mock, td, dt, is_player_target, repair_skill=100.0, has_big_kit=False):
				'''Crew auto-repair: destroyed modules climb back to functional (~50%) over
				repair_seconds (scaled by crew skill, toolbox, large kit); damaged modules
				regen toward the same cap. Drives the panel repair bar and state icons, and
				clears the mobility flags when tracks/engine come back.'''
				if mock is None or dt is None or dt <= 0:
					return
				# A destroyed vehicle repairs nothing - its crew is gone. Only the PLAYER call
				# site checked this, so dead bots kept repairing, and on the frame the player
				# died the panel still showed a repair running on a wreck.
				if (getattr(mock, 'health', 0) or 0) <= 0 or getattr(mock, '_is_killed', False):
					return
				dh = getattr(mock, 'devices_hp', None)
				if not dh:
					return
				from gui.mods.offhangar import device_damage as _DDr
				destroyed = _dev_destroyed_set(mock)
				states = getattr(mock, '_module_states', None)
				if states is None:
					states = {}
					mock._module_states = states
				bw = None
				if is_player_target:
					try:
						import gui.WindowsManager
						bw = gui.WindowsManager.g_windowsManager.battleWindow
					except Exception:
						bw = None
				for _name in list(dh.keys()):
					max_hp = _DDr.device_max_hp(td, _name)
					if max_hp is None:
						continue
					cap = _DDr.device_regen_hp(td, _name)
					if not cap:
						cap = int(max_hp * _DDr.CRITICAL_HP_FRACTION)
					hp = dh[_name]
					# The fuel tank is not patched up while it burns - the fire ending is what
					# restores it (_offh_extinguish), so it stays red until then.
					_burning_tank = (_name in _DDr.NO_REPAIR_PROGRESS_DEVICES
						and bool(getattr(mock, 'is_on_fire', False)))
					if hp < cap and not _burning_tank:
						hp = _DDr.repair_step_hp(hp, _name, td, dt, repair_skill, has_big_kit)
						dh[_name] = hp
					was_destroyed = _name in destroyed
					functional = hp >= cap
					_repair_done = False
					if was_destroyed and functional:
						destroyed.discard(_name)
						# Repair finished. Do NOT close the bar with (100, 0) - retail never sends
						# 100 at all. The server streams DESTROYED_DEVICE_IS_REPAIRING while the
						# repair runs and then simply stops; what clears the bar is the DEVICE STATE
						# leaving destroyed. Pushing 100 left the bar drawn full at the end of the
						# sequence even though the track was long since fixed. The clear happens
						# below, AFTER the state change, so the panel sees them in retail order.
						_repair_done = True
						_rui = getattr(mock, '_repair_ui_pct', None)
						if _rui is not None:
							_rui.pop(_name, None)
					if _name in destroyed:
						new_state = 'destroyed'
					else:
						new_state = _DDr.device_state(hp, max_hp)
					if is_player_target and bw is not None and hasattr(bw, 'damagePanel') and _name in destroyed and cap > 0 and _name not in _DDr.NO_REPAIR_PROGRESS_DEVICES:
						frac = hp / float(cap)
						if frac < 0.0: frac = 0.0
						elif frac > 1.0: frac = 1.0
						pct = int(round(100.0 * frac))
						secs = _DDr.repair_seconds(_name, td, repair_skill, has_big_kit)
						secs_left = max(0.0, secs * (1.0 - frac))
						# push only when the integer percent changes, so the Flash bar animates
						# smoothly instead of being re-sent every frame
						_rl = getattr(mock, '_repair_ui_pct', None)
						if _rl is None:
							_rl = {}
							mock._repair_ui_pct = _rl
						if _rl.get(_name) != pct:
							_rl[_name] = pct
							try: bw.damagePanel.updateModuleRepair(_module_ui_name(_name), pct, secs_left)
							except Exception as _mre: LOG_DEBUG('updateModuleRepair err:', _module_ui_name(_name), str(_mre))
					if _repair_done and is_player_target and bw is not None and hasattr(bw, 'damagePanel') and _name not in _DDr.NO_REPAIR_PROGRESS_DEVICES:
						# 0 percent / 0 s = nothing in progress. The opening frame starts a bar with
						# (0, seconds), so the zero SECONDS is what marks it finished rather than running.
						try: bw.damagePanel.updateModuleRepair(_module_ui_name(_name), 0, 0.0)
						except Exception: pass
					if _repair_done:
						# Retail order (avatar.py, DEVICE_REPAIRED_TO_CRITICAL): the bar stops,
						# then the device goes to 'repaired' - not straight to 'critical'. That
						# is also what selects the 'functional' / 'functionalCanMove' voice line.
						# Bookkeeping keeps the real state so the next tick pushes nothing.
						_push_device_ui(mock, is_player_target, _name, hp, max_hp, state='repaired')
						states[_name] = new_state
					if states.get(_name) != new_state:
						_push_device_ui(mock, is_player_target, _name, hp, max_hp, state=new_state)
						states[_name] = new_state
				# Unconditional. mobility_dirty only fires on the single frame a module crosses
				# back to functional, so any path that touched the destroyed-set without going
				# through this loop left is_tracked/is_engine_dead latched True - an
				# immobilised tank that never moved again. It is four set lookups.
				_refresh_mobility_flags(mock)
				# Broken-track visual follows the same destroyed-set this tick maintains.
				try:
					if is_player_target:
						_ch_m = loaded_models.get('chassis')
						_fa_m = loaded_models.get('_fashion')
					else:
						_ch_m = getattr(mock, '_chassis_model', None)
						_fa_m = getattr(mock, '_fashion', None)
					_sync_crashed_track(mock, _ch_m, _fa_m, td)
				except Exception as _cte:
					LOG_DEBUG('crashed track sync err:', str(_cte))
			
			def _offh_he_splash(burst_pos, _shot, attacker_id, direct_id):
				'''HE blast on every OTHER vehicle within explosionRadius.
				
				The vehicle actually struck is skipped: it is the dist_frac 0 case and its
				damage is applied by the shot path that called us, so counting it here too
				would double it. For each victim a ray is run from the burst point into the
				hull, which yields the real plate facing the blast and the device hitboxes
				behind it out of the vehicle's OWN collision model - the same source a direct
				hit uses, so a tank turned side-on takes the blast on its side armour.'''
				import BigWorld, Math, random
				_R = _offh_he_radius(_shot)
				if _R <= 0.0 or burst_pos is None:
					return
				_shell_s = (_shot.get('shell') or {}) if hasattr(_shot, 'get') else {}
				try:
					_base = float(_shell_s['damage'][0])
				except Exception:
					return
				_pl = BigWorld.player()
				if _pl is None:
					return
				_pvid_s = getattr(_pl, 'playerVehicleID', -1)
				_hit_any = 0
				for _sid2, _sm in ((globals().get('G_MOCK_VEHICLES', {}) or {}).items()):
					if _sid2 == direct_id:
						continue
					if (getattr(_sm, 'health', 0) or 0) <= 0 or not getattr(_sm, 'isAlive', False):
						continue
					_sp = getattr(_sm, 'position', None)
					if _sp is None:
						continue
					_dx = _sp.x - burst_pos.x
					_dy = _sp.y - burst_pos.y
					_dz = _sp.z - burst_pos.z
					_dd = (_dx * _dx + _dy * _dy + _dz * _dz) ** 0.5
					if _dd > _R:
						continue
					_hits_s = []
					_nom_s = 0.0
					try:
						# Aim a metre above the mock position: that is the CHASSIS origin, at track
						# height, so a low burst beside the hull crossed only track material (spaced,
						# skipped) and reported no armour at all.
						_aim_s = Math.Vector3(_sp.x, _sp.y + 1.0, _sp.z)
						_col_s = _sm.collideSegment(burst_pos, _aim_s)
						if _col_s is not None:
							_hits_s = _col_s[3] if len(_col_s) > 3 else []
						_nom_s = _offh_he_nominal_armor(_hits_s, getattr(_sm, 'typeDescriptor', None))
					except Exception:
						_nom_s = _offh_he_hull_armor(getattr(_sm, 'typeDescriptor', None))
					# Same +/-25% spread the direct hit gets (shell damageRandomization).
					_sd = _offh_he_damage(_base * random.uniform(0.75, 1.25), _nom_s, _dd / _R)
					if _sd <= 0:
						continue
					_hit_any += 1
					# Module and crew crits from the blast. penetrated=False keeps the roll to
					# what sits in front of the plate - splash reaches tracks and external gear,
					# not the ammo bay through 100 mm of hull.
					try:
						_apply_module_damage(_sm, _hits_s, burst_pos, _sp, _sd, _shell_s, attacker_id, False, True)
					except Exception as _hme:
						LOG_DEBUG('HE splash module damage err:', str(_hme))
					_was = getattr(_sm, 'health', 0) or 0
					_act = _sd if _sd < _was else _was
					_sm.health = _was - _sd
					# Ledger: the blast carries a real attacker_id, so ONE call covers
					# the player and every bot. Splash counts as an he_hit on each tank
					# it reaches, which is what the results screen means by he_hits.
					try:
						from gui.mods.offhangar import battle_ledger as _BLED
						_BLED.get().note_hit(attacker_id, _sid2, damage=int(_act),
							pierced=False, he=True)
					except Exception:
						pass
					if attacker_id == _pvid_s:
						_sm.damage_from_player = (getattr(_sm, 'damage_from_player', 0) or 0) + _act
						_sm.hits_from_player = (getattr(_sm, 'hits_from_player', 0) or 0) + 1
					else:
						_sm.damage_from_bots = (getattr(_sm, 'damage_from_bots', 0) or 0) + _act
					_sm.last_killer_id = attacker_id
					LOG_DEBUG('HE SPLASH: target=%s dist=%.1fm/%.1fm armor=%.0f dmg=%d hp=%d' % (
						_sid2, _dd, _R, _nom_s, _sd, max(0, _sm.health)))
					try:
						from gui import WindowsManager as _hewm
						_hebw = getattr(_hewm.g_windowsManager, 'battleWindow', None)
					except Exception:
						_hebw = None
					if _sid2 == _pvid_s:
						# The player caught in his own or a bot's blast: same HP plumbing the
						# direct-hit path uses, or the bar simply would not move.
						try:
							if getattr(_pl, 'vehicle', None):
								_pl.vehicle.health = max(0, _sm.health)
								_pl.guiSessionProvider.invalidateVehicleState(1, _pvid_s, max(0, _sm.health), max(0, _sm.health))
							if _hebw is not None and hasattr(_hebw, 'damagePanel'):
								_hebw.damagePanel.updateHealth(max(0, _sm.health))
						except Exception:
							pass
					else:
						try:
							_mk = getattr(_sm, 'marker', None)
							if _hebw is not None and getattr(_hebw, 'vMarkersManager', None) and _mk not in (None, -1):
								_hebw.vMarkersManager.onVehicleHealthChanged(_mk, max(0, _sm.health), attacker_id, 0)
								_hebw.vMarkersManager.showVehicleDamageInfo(_mk, _sd, 0, 0, 1)
						except Exception:
							pass
					if _sm.health <= 0:
						_sm.health = 0
						try:
							_pl.arena.onVehicleKilled(_sm.id, attacker_id, 0)
						except Exception:
							pass
				if _hit_any:
					LOG_DEBUG('HE BURST: %d vehicle(s) caught in a %.1f m blast' % (_hit_any, _R))
			
			def _apply_module_damage(target_mock, all_hits, start_pos, end_pos, dmg, _shell, attacker_id, penetrated=None, by_explosion=False):
				'''Roll module and crew crits for one strike.
				
				penetrated: True the shell got through, False it did not, None unknown (the
				bot call sites, which already sit behind their own penetration branch).
				False restricts the roll to devices IN FRONT of the plate that stopped the
				round - see the _stop_d block below.
				
				by_explosion: this is HE splash rather than a solid hit, so every saving throw
				reads the material's chanceToHitByExplosion. Blast reaches externally mounted
				gear far more readily than it reaches anything behind a plate, which is what
				the two separate XML values encode.'''
				import BigWorld, Math, random
				from gui.mods.offhangar import device_damage as _device_damage
				try:
					from _constants import CONFIG_OPTIONS as _MDCFG
				except Exception:
					_MDCFG = {}
				if not bool(_MDCFG.get('module_damage', True)):
					return dmg
				_crew_on = bool(_MDCFG.get('crew_damage', True))
				_crew_hit = False
				_pvid = getattr(BigWorld.player(), 'playerVehicleID', -1)
				is_player_target = (getattr(target_mock, 'id', -1) == _pvid)
				if is_player_target and not bool(_MDCFG.get('player_module_damage', True)):
					return dmg
				if getattr(target_mock, 'devices_hp', None) is None:
					target_mock.devices_hp = {}
				# 0.8.2 shells carry damage as (armor, devices); there is no 'deviceDamage' key.
				_shell_dmg = _device_damage.module_damage_roll(_shell)
				if _shell_dmg is None:
					_shell_dmg = dmg
				is_player_attacker = (attacker_id == _pvid)
				target_mock.last_sound = 'armor_pierced_by_player' if is_player_attacker else 'armor_pierced'
				td = _device_td(target_mock)
				# A shell that did NOT get through can only crit what sits IN FRONT of the plate
				# that stopped it. The player's shot path calls this on every strike (so a pure
				# track hit can still break a track, since tracks deal 0 structure damage), and
				# without this window it also rolled the engine, fuel tank, crew and ammo bay
				# deep inside the hull on a shot that visibly bounced. A destroyed ammo bay is
				# an instant kill, so that read as a random ammo rack on a ricochet.
				# Where the shell LEAVES the hull. The ray runs far past the tank, so the
				# hit list also contains the far-side track on the way out - and a track
				# material has chanceToHitByProjectile 1.0, so every penetrating hull hit
				# broke a track that the shell never really reached. That is the "tracked
				# out of nowhere when shooting the hull" report. A round is spent once it
				# has crossed the far wall, so stop scoring after the SECOND structural
				# plate: entry, interior, exit.
				_exit_d = None
				try:
					_walls = 0
					for _h1 in sorted(all_hits, key=lambda _x: _x[0]):
						_m1 = _h1[2]
						if _m1 is None:
							continue
						if getattr(_m1, 'vehicleDamageFactor', 1.0) != 0.0 and float(getattr(_m1, 'armor', 0.0) or 0.0) > 0.0:
							_walls += 1
							if _walls >= 2:
								_exit_d = _h1[0]
								break
				except Exception:
					_exit_d = None
				_stop_d = None
				if penetrated is False:
					_stop_d = 1e9
					for _h0 in all_hits:
						try:
							_hd0, _hm0 = _h0[0], _h0[2]
						except Exception:
							continue
						if _hm0 is None:
							continue
						# structural = deals hull damage AND has thickness; that is the plate the
						# penetration test was run against.
						if getattr(_hm0, 'vehicleDamageFactor', 1.0) != 0.0 and float(getattr(_hm0, 'armor', 0.0) or 0.0) > 0.0:
							if _hd0 < _stop_d:
								_stop_d = _hd0
				# Interior devices have no collision geometry in this client: all 1975
				# collision meshes carry armor_N, gun, both tracks, surveyingDevice and
				# gunBreech and nothing else. WG resolved engine / ammo bay / fuel tank /
				# radio / turret ring / crew hits server-side against a model that was never
				# shipped, so no ray can ever reach them. A penetrating strike therefore gets
				# ONE reconstructed interior roll, aimed at the compartment the shell entered.
				# It is appended as a synthetic hit at distance 0 and runs through the SAME
				# scoring loop below, so HP, panel, voice, fire and ammo-rack detonation all
				# behave exactly as they do for a hit that came out of the collision model.
				_scored = all_hits
				if penetrated is not False and bool(_MDCFG.get('internal_module_damage', True)):
					try:
						# Preferred path: the adopted per-tank profiles give every interior
						# module and crewman a real box, so the shell either crosses one or
						# it does not - no zone guess involved. Each crossed box gets its own
						# saving throw, which is how a round through the engine bay can take
						# the engine AND a fuel tank.
						_covered = set()
						for _h2 in all_hits:
							_m2 = _h2[2]
							_x2 = getattr(_m2, 'extra', None) if _m2 is not None else None
							if _x2 is not None:
								_covered.add(str(getattr(_x2, 'name', '')))
						_rost = _crew_roster(td)
						_real = _offh_internal_ray_hits(target_mock, td, start_pos, end_pos, _covered)
						if _real is not None:
							if not _crew_on:
								_real = [_r for _r in _real if _r[1][:-6] not in _rost]
							if _real:
								LOG_DEBUG('INTERIOR GEOMETRY: %s' % ', '.join(
									['%s@%.2f' % (_n2, _d2) for _d2, _n2 in _real]))
								_scored = list(all_hits)
								for _d2, _n2 in _real:
									_scored.append((0.0, 1.0, _SynthMaterial(_n2), None))
							else:
								LOG_DEBUG('INTERIOR GEOMETRY: shell path crossed no interior box')
						else:
							# Fallback: no profile for this tank (or the feature is off).
							# One reconstructed roll against the compartment the shell entered.
							_zone = _offh_interior_zone(target_mock, all_hits, start_pos, end_pos, td)
							_cands = _device_damage.interior_candidates(_zone, _rost, td)
							if not _crew_on:
								# Crew candidates are exactly the roster instances plus 'Health'.
								_cands = [_c for _c in _cands if _c[0][:-6] not in _rost]
							_pick = _device_damage.pick_interior(_cands)
							if _pick is not None:
								LOG_DEBUG('INTERIOR ROLL: zone=%s pick=%s (%d candidates)' % (_zone, _pick, len(_cands)))
								_scored = list(all_hits)
								_scored.append((0.0, 1.0, _SynthMaterial(_pick), None))
					except Exception as _ie:
						LOG_DEBUG('interior roll err:', str(_ie))
				# Re-entrant: HE splash scores other vehicles through this same function, so
				# only the outermost strike owns the collector.
				_own_burst = _OFFH_VOICE_BURST[0] is None
				if _own_burst:
					_OFFH_VOICE_BURST[0] = []
				try:
					_blocked = 0
					for h in _scored:
						h_dist, h_angle, h_mat, h_comp = h
						if h_mat is None:
							continue
						_extra = getattr(h_mat, 'extra', None)
						if _extra is None:
							continue          # plain armour plate, nothing to crit
						# NO vehicleDamageFactor filter here. It used to drop every device material
						# whose plate ALSO damages the hull, on the theory that those were armour
						# rather than gear. The shipped data says otherwise: engine, ammo bay, fuel
						# tank, radio, turret ring and gunBreech all carry vehicleDamageFactor 1.0
						# and WG crits every one of them. Of those, only gunBreech has geometry in
						# this client (37 vehicles), so the filter's real effect was that a shell
						# through the breech could not damage the gun. vehicleDamageFactor governs
						# how much of the round goes into the HULL, which the penetration path
						# already owns; it says nothing about whether a crit may happen.
						_name = getattr(_extra, 'name', 'Unknown')
						if _stop_d is not None and h_dist > _stop_d:
							_blocked += 1
							continue          # behind the plate that stopped the shell
						if _exit_d is not None and h_dist > _exit_d:
							_blocked += 1
							continue          # the shell has left the tank - exit-side track, not a hit
						# Chance source, so the log answers whether the era fallback table is ever
						# reached. MaterialInfo always carries chanceToHitByProjectile (vehicles.py
						# _readArmor copies it from g_cache.commonConfig), so 'FALLBACK' here means
						# the material object itself is not what we think it is.
						_live_c = getattr(h_mat, 'chanceToHitByExplosion' if by_explosion else 'chanceToHitByProjectile', None)
						LOG_DEBUG('CRIT ROLL: %s chance=%s src=%s%s' % (_name, _live_c, 'mat' if _live_c is not None else 'FALLBACK', ' splash' if by_explosion else ''))
						if _name in _device_damage.CREW_HEALTH_NAMES:
							if _crew_on and random.random() < _device_damage.saving_throw(h_mat, _name, by_explosion):
								if _knock_out_crew(target_mock, _name[:-6], is_player_target):
									_crew_hit = True
									target_mock.last_sound = 'armor_pierced_crit_by_player' if is_player_attacker else 'armor_pierced_crit'
							continue
						# INCLUSION list: only real, modelled devices are scored. The old exclusion
						# list ('everything except tracks and gun') both credited unmodelled extras
						# AND made track/gun crits impossible.
						if _name not in _device_damage._DEVICE_HP_SPEC:
							continue
						if random.random() >= _device_damage.saving_throw(h_mat, _name, by_explosion):
							continue   # saving throw failed: no crit on this device
						max_hp = _device_damage.device_max_hp(td, _name)
						if max_hp is None:
							max_hp = 100
						current_hp = target_mock.devices_hp.get(_name, max_hp)
						current_hp -= _shell_dmg
						# Clamp at 0 so auto-repair does not have to climb out of a deficit.
						if current_hp < 0:
							current_hp = 0
						target_mock.devices_hp[_name] = current_hp
						target_mock.last_sound = 'armor_pierced_crit_by_player' if is_player_attacker else 'armor_pierced_crit'
						_push_device_ui(target_mock, is_player_target, _name, current_hp, max_hp)
						if 'ammo' in _name.lower() and current_hp <= 0 and is_player_target and _offh_module_test_mode():
							# Test bench: the rack still reads destroyed on the panel and can be
							# repaired, it just does not end the run.
							LOG_DEBUG('MODULE TEST: ammo rack detonation on the player suppressed')
						elif 'ammo' in _name.lower() and current_hp <= 0:
							# A detonated ammo rack destroys the tank outright - the era rule, not a roll.
							LOG_DEBUG('AMMO RACK DETONATION: target=%s penetrated=%s hp_was=%s shell_dev_dmg=%.0f' % (
								getattr(target_mock, 'id', '?'), penetrated, max_hp, _shell_dmg))
							dmg = target_mock.health + 10
							target_mock._is_killed = True
							target_mock._ammo_rack_death = True   # picks the 'explosion' death effect
							target_mock.last_sound = 'enemy_killed_by_player' if is_player_attacker else 'enemy_killed'
							try:
								BigWorld.player().arena.onVehicleKilled(target_mock.id, attacker_id, 1)
							except Exception:
								pass
							break
						if current_hp <= 0:
							_dev_destroyed_set(target_mock).add(_name)
							_refresh_mobility_flags(target_mock)
							# Opening frame at 0%, so the bar appears the instant the module breaks
							# instead of only on the next repair tick - but not when this very shot is
							# killing the tank, or the panel starts a repair on a wreck.
							if is_player_target and not getattr(target_mock, '_is_killed', False) and (getattr(target_mock, 'health', 0) or 0) > 0:
								try:
									import gui.WindowsManager as _WMrb
									_bwrb = getattr(_WMrb.g_windowsManager, 'battleWindow', None)
									if _bwrb is not None and hasattr(_bwrb, 'damagePanel'):
										from gui.mods.offhangar import device_damage as _DDrb
										_secs0 = _DDrb.repair_seconds(_name, td)
										_bwrb.damagePanel.updateModuleRepair(_module_ui_name(_name), 0, _secs0)
								except Exception: pass
							if ('engine' in _name.lower() or 'fuel' in _name.lower()) and not getattr(target_mock, 'is_on_fire', False):
								# Fuel tank always ignites; an engine only rolls for it, and the hit must
								# first clear miscParams/minFireStartingDamage (21).
								_ignite = ('fuel' in _name.lower())
								if not _ignite and 'engine' in _name.lower():
									_fsc = 0.15
									try:
										_eng = getattr(td, 'engine', None)
										if _eng is not None and hasattr(_eng, 'get'):
											_fsc = float(_eng.get('fireStartingChance', 0.15))
									except Exception:
										pass
									_ignite = (_shell_dmg >= _device_damage.MIN_FIRE_STARTING_DAMAGE) and (random.random() < _fsc)
								if _ignite:
									_offh_ignite(target_mock, is_player_target, _name + ' destroyed', is_player_attacker)
						elif ('fuel' in _name.lower() and current_hp > 0
								and not getattr(target_mock, 'is_on_fire', False)
								and _shell_dmg >= _device_damage.MIN_FIRE_STARTING_DAMAGE):
							# A fuel tank that is merely HOLED can already set the tank alight - that is
							# the whole reason a hit in the tank is feared. The shipped data gives the
							# fuel tank no fire parameter of its own; only the engine carries
							# fireStartingChance (0.12 on the diesel V-2-54), so the roll borrows that
							# behind the same minFireStartingDamage gate. RECONSTRUCTED - destruction
							# still ignites unconditionally above.
							_fsc2 = 0.15
							try:
								_eng2 = getattr(td, 'engine', None)
								if _eng2 is not None and hasattr(_eng2, 'get'):
									_fsc2 = float(_eng2.get('fireStartingChance', 0.15))
							except Exception:
								pass
							if random.random() < _fsc2:
								_offh_ignite(target_mock, is_player_target, _name + ' holed', is_player_attacker)
					if _blocked:
						LOG_DEBUG('CRIT GATE: %d device hit(s) behind the stopping plate ignored (no penetration)' % _blocked)
				finally:
					if _own_burst:
						_pending_voice = _OFFH_VOICE_BURST[0] or []
						_OFFH_VOICE_BURST[0] = None
						_ordered_voice = _offh_voice_burst_order(_pending_voice)
						if len(_ordered_voice) > 1:
							LOG_DEBUG('CREWVOICE burst: %d reports from one strike, queueing worst first: %s'
								% (len(_ordered_voice), _ordered_voice))
						for _snd_q in _ordered_voice:
							_offh_play_crit_voice(_snd_q)
				return dmg
			def _mock_shoot():
				import BigWorld, Math, math, random
				if getattr(BigWorld.player(), '_is_dead', False) is True: return
				# Submerged crew cannot work the gun.
				if getattr(BigWorld.player(), '_offh_drowning', False): return
				# A destroyed gun cannot fire at all. The flag was computed but never read.
				try:
					_pm_gun = mock_vehicles.get(getattr(BigWorld.player(), 'playerVehicleID', -1))
					if _pm_gun is not None and getattr(_pm_gun, 'is_gun_destroyed', False): return
				except Exception: pass
				# No shooting during the pre-battle countdown (like the original)
				try:
					if getattr(BigWorld.player().arena, 'period', 3) != 3: return  # prebattle AND afterbattle (capture won)
				except Exception:
					pass
				try:
					# --- RELOAD LOGIC ---
					if not _gun_state['initialized']: return
					if _gun_state['reloadTime'] > 0: return
					idx = _gun_state.get('shot_index', 0)
					ammo_key = 'ammo_%d' % idx
					if _gun_state.get(ammo_key, 1) <= 0: return
					
					_gun_state[ammo_key] -= 1
					_gun_state['clip'] -= 1
					try:
						from gui.mods.offhangar import battle_ledger as _BLED
						_BLED.get().note_shot(getattr(BigWorld.player(), 'playerVehicleID', -1), idx)
					except Exception:
						pass
					import math
					# THE CIRCLE THIS SHELL IS SCATTERED IN, captured before the shot's own
					# bloom widens it. The bloom belongs to the NEXT round: retail fires with
					# the circle you were looking at when you pulled the trigger, and only
					# then calls getOwnVehicleShotDispersionAngle(..., withShot=1) from
					# updateVehicleAmmo. Offline the scatter was read further down from
					# gunRotator.dispersionAngle - which the block below had already
					# overwritten with the post-shot value - so every shell, however
					# patiently aimed, was thrown with the full after-shot cone. On this
					# KV-2 that is 0.0291 rad against a base of 0.006: 4.85x the scatter the
					# player was promised, on every single shot.
					_fire_disp = _gun_state.get('dispersion', 0.0)
					# After-shot bloom, exactly as Avatar.updateVehicleAmmo triggers it:
					# getOwnVehicleShotDispersionAngle(turretSpeed, withShot=1) rebuilds the
					# ideal factor with the afterShot term inside the SAME sqrt as the
					# movement terms and scaled by the same additive factor, then restarts
					# the aiming decay from it. It is a floor, not an addition - a circle
					# already wider than that keeps its own value - and it lands in this
					# frame, not over the next dozen.
					_as = _gun_state.get('after_shot', 1.5)
					_shot_ideal = math.sqrt(1.0 + _gun_state.get('_disp_terms', 0.0)
						+ _as * _as * _gun_state.get('_disp_add2', 1.0))
					_base_eff_s = _gun_state.get('_base_eff', 0.0) or _gun_state['base_dispersion']
					_cur_f = (_gun_state['dispersion'] / _base_eff_s) if _base_eff_s > 0.0 else _shot_ideal
					_new_f = max(_cur_f, _shot_ideal)
					_gun_state['aim_start_f'] = _new_f
					_gun_state['aim_start_t'] = BigWorld.time()
					_gun_state['dispersion'] = _base_eff_s * _new_f
					
					if _gun_state['clip'] > 0:
						_gun_state['reloadTime'] = _gun_state['clip_reload']
					else:
						# A knocked-out loader drags the reload out; a knocked-out commander adds a
						# smaller malus on top (device_damage.crew_stat_factor).
						try:
							_pm_cr = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
							# A damaged ammo bay drags it out on top of that (destroyed detonates).
							_gun_state['reloadTime'] = _gun_state['reload'] * (_crew_factor(_pm_cr, 'reload') * _module_factor(_pm_cr, 'reload') if _pm_cr is not None else 1.0)
						except Exception:
							_gun_state['reloadTime'] = _gun_state['reload']
						
					if hasattr(BigWorld.player(), 'gunRotator'):
						BigWorld.player().gunRotator.dispersionAngle = _gun_state['dispersion']
						
					player = BigWorld.player()
					player._offhangar_shots_fired = getattr(player, '_offhangar_shots_fired', 0) + 1
						
					# UPDATE RELOAD UI
					try:
						from gui import WindowsManager
						panel = WindowsManager.g_windowsManager.battleWindow.consumablesPanel
						if panel:
							shot_idx = _gun_state.get('shot_index', 0)
							panel.setShellQuantityInSlot(shot_idx, _gun_state['ammo_%d' % shot_idx], _gun_state['clip'])
							try: panel.setCoolDownTime(shot_idx, 0.0)
							except Exception as e: LOG_DEBUG('setCoolDownTime reset error:', str(e))
							try: panel.setCoolDownTime(shot_idx, _gun_state['reloadTime'])
							except Exception as e: LOG_DEBUG('setCoolDownTime error:', str(e))
						aim = getattr(g_offline_aih, 'aim', None)
						if aim:
							try: aim.setReloading(0.0, None)
							except: pass
							try: aim.setReloading(_gun_state['reloadTime'], None)
							except Exception as e: LOG_DEBUG('setReloading error:', str(e))
							shot_idx = _gun_state.get('shot_index', 0)
							aim.setAmmoStock(_gun_state['ammo_%d' % shot_idx], _gun_state['clip'], False)
					except Exception as e:
						LOG_DEBUG('Normal shoot UI error:', str(e))

					# Auto-load the next stocked shell type when this one just ran out
					# (was: the gun 'reloaded' an empty shell and refused to fire while
					# other types were still in the rack). Deferred one frame because the
					# in-flight shot below re-reads shot_index for its ballistics.
					if _gun_state.get(ammo_key, 0) <= 0:
						def _offh_auto_next_shell():
							try:
								if _gun_state.get('shot_index', 0) != idx: return  # user switched already
								if _gun_state.get(ammo_key, 0) > 0: return
								_next = None
								for _off in range(1, 10):
									_i = (idx + _off) % 10
									if _i != idx and _gun_state.get('ammo_%d' % _i, 0) > 0:
										_next = _i
										break
								if _next is None: return  # completely dry
								_gun_state['shot_index'] = _next
								_gun_state['next_shot_index'] = _next
								# The magazine is EMPTY while the type is being changed - the gun is
								# being cleared and refilled with the other shell, and the reload
								# forced below IS that. Filling the clip here instead drew a FULL
								# magazine on a gun that was still reloading and could not fire:
								# "the UI showed a fully loaded magazine even though the tank was
								# still reloading". The manual 1/2/3 switch has always zeroed it for
								# this reason; only this automatic path, taken when a shell type runs
								# dry, did not. The reload-complete handler puts the rounds back.
								_gun_state['clip'] = 0
								if _gun_state['reloadTime'] < _gun_state['reload']:
									_gun_state['reloadTime'] = _gun_state['reload']  # type change = full reload
								try:
									from gui import WindowsManager
									_bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
									_pnl = getattr(_bw, 'consumablesPanel', None) if _bw else None
									if _pnl:
										_pnl.setCurrentShell(_next)
										_pnl.setShellQuantityInSlot(_next, _gun_state['ammo_%d' % _next], _gun_state['clip'])
										try: _pnl.setCoolDownTime(_next, 0.0)
										except Exception: pass
										try: _pnl.setCoolDownTime(_next, _gun_state['reloadTime'])
										except Exception: pass
									_aim = getattr(g_offline_aih, 'aim', None)
									if _aim:
										try: _aim.setReloading(0.0, None)
										except: pass
										try: _aim.setReloading(_gun_state['reloadTime'], None)
										except Exception: pass
										_aim.setAmmoStock(_gun_state['ammo_%d' % _next], _gun_state['clip'], False)
								except Exception as _ase:
									LOG_DEBUG('Auto shell switch UI error:', str(_ase))
							except Exception as _ase:
								LOG_DEBUG('Auto shell switch error:', str(_ase))
						BigWorld.callback(0.0, _offh_auto_next_shell)

					try:
						player._Avatar__shotWaitingTimerID = None
					except: pass
					
					# --- RAYCAST HIT DETECTION ---
					start_pos, dir_vec = player.gunRotator._VehicleGunRotator__getCurShotPosition()
					dir_vec.normalise()
					
					# Apply Player Dispersion based on actual aiming circle
					# (config.json "perfect_accuracy": true disables all scatter - testing aid)
					# _fire_disp is the circle as it stood when the trigger was pulled. Reading
					# gunRotator.dispersionAngle here instead took the value the after-shot
					# bloom had already written a few lines above - see the note there.
					disp_angle = _fire_disp or getattr(player.gunRotator, 'dispersionAngle', _gun_state.get('dispersion', 0.02))
					from _constants import CONFIG_OPTIONS as _CFG_ACC
					sigma = 0.0 if _CFG_ACC.get('perfect_accuracy', False) else disp_angle / 3.0
					dir_vec.x += random.gauss(0, sigma)
					dir_vec.y += random.gauss(0, sigma)
					dir_vec.z += random.gauss(0, sigma)
					dir_vec.normalise()
					
					# Pre-bind so the ground-impact detonation below can reference these
					# even when no tracer/shot resolved this frame (else NameError).
					_sid = None
					_effectsDescr = None
					_w_col = None
					# --- TRACER ---
					try:
						if g_projectile_mover:
							from items import vehicles
							_our_td = loaded_models.get('td')
							_our_shots = _our_td.gun.get('shots', []) if _our_td else []
							_si = _gun_state.get('shot_index', 0)
							_si = min(_si, len(_our_shots) - 1) if _our_shots else 0
							_shot = _our_shots[_si] if _our_shots else None
							
							if _shot:
								_effectsDescr = vehicles.g_cache.shotEffects[_shot['shell']['effectsIndex']]
								_gravity = _shot['gravity']
								_speed = _shot['speed']
								_vel = dir_vec.scale(_speed)
								import random
								_sid = random.randint(10000, 99999)
								_cam_pos = BigWorld.camera().position if BigWorld.camera() else start_pos
								# isOwnShoot=True picks projModelOwnShotName - the BRIGHT own-shot tracer model -
								# and enables autoscale. It also sets fireMissedTrigger, whose only consumer is
								# TriggersManager.g_manager.fireTrigger; that singleton is never set up offline,
								# so clear the flag right after. The visual is already decided at construction.
								globals()['g_offh_adding_projectile'] = True
								try:
									g_projectile_mover.add(_sid, _effectsDescr, _gravity, start_pos, _vel, start_pos, True, _cam_pos)
								finally:
									globals()['g_offh_adding_projectile'] = False
								try:
									_pj = getattr(g_projectile_mover, '_ProjectileMover__projectiles', {}).get(_sid)
									if _pj is not None: _pj['fireMissedTrigger'] = False
								except Exception: pass
					except Exception as e:
						import traceback
						LOG_DEBUG('Tracer spawn error:', traceback.format_exc())
					
					# Which shell is in the breech. Resolved HERE, above every impact branch.
					#
					# The HE ground-splash test below reads _shots/_sidx, but they were only ever
					# assigned inside the ENEMY-hit branch further down - and the two branches are
					# mutually exclusive. So every shot that struck terrain instead of a tank raised
					# UnboundLocalError before the splash could run, straight into a swallowed
					# LOG_DEBUG ('HE ground splash err: local variable _shots referenced before
					# assignment' - four times in one session's log). A high-explosive round landing
					# in the dirt beside a tank therefore did nothing at all, which is the whole
					# point of a derp gun and exactly the case the splash was written for.
					#
					# Pre-bound to safe defaults so a failure here reads as 'no HE' rather than
					# raising again. The enemy-hit branch still derives its own copy: that is five
					# cheap lookups, and leaving a working path untouched is worth more than the
					# duplication. (Moved above the hit test, which now needs the shell's own
					# speed and gravity to walk its trajectory.)
					_shots, _sidx = [], 0
					try:
						_td_g = loaded_models.get('td')
						_shots = (_td_g.gun.get('shots', []) or []) if _td_g is not None else []
						if _shots:
							_sidx = min(_gun_state.get('shot_index', 0), len(_shots) - 1)
					except Exception as _sre:
						LOG_DEBUG('shot descriptor lookup failed:', str(_sre))

					hit_dist = 99999.0
					enemy_mock = None
					enemy_hit_info = None
					end_pos = start_pos + dir_vec.scale(5000.0)
					_impact_pos = None      # where the shell actually met the tank
					_impact_dir = dir_vec   # travel direction AT the impact - an arc bends

					def _offh_shot_breaks(hit_pos, dirv):
						"""Did the shell smash the destructible it just hit (fence, tree, pole)?"""
						_destr_fn = loaded_models.get('_destr_fn')
						if _destr_fn is None:
							return False
						try:
							_mi = BigWorld.wg_getMatInfoNearPoint(_offh_bspace(), start_pos,
								hit_pos + dirv.scale(0.3), hit_pos, lambda *a: False)
						except Exception:
							return False
						if _mi is None:
							return False
						try:
							return bool(_destr_fn(_offh_bspace(), _mi, math.atan2(dirv.x, dirv.z), 12.0))
						except Exception:
							return False

					def _offh_mock_test(p1, p2, _pid=getattr(player, 'playerVehicleID', -1)):
						"""Nearest mock struck on one chord, in collideSegment's own shape.

						Called once per chord rather than once per shot, so it opens with a
						bounding-sphere reject: no tank is 12 m wide, and a full localHitTest
						against every component of every bot for every chord of every shot is
						not worth paying for the ones nowhere near the line."""
						_best, _bveh = None, None
						_seg = p2 - p1
						_seg_len = _seg.length
						if _seg_len <= 1e-06:
							return None
						for _e, _m in mock_vehicles.iteritems():
							# Health as well as the flag: isAlive is not cleared on every death
							# path (fire, ramming, falling), so wrecks stayed shootable and
							# answered with a 'no penetration' call-out.
							if _e == _pid or not getattr(_m, 'isAlive', False):
								continue
							if (getattr(_m, 'health', 0) or 0) <= 0:
								continue
							try:
								_to = _m.position - p1
								_t = (_to.x * _seg.x + _to.y * _seg.y + _to.z * _seg.z) / (_seg_len * _seg_len)
								_t = max(0.0, min(1.0, _t))
								if (_to - _seg.scale(_t)).length > 12.0:
									continue
							except Exception:
								pass
							_c = _m.collideSegment(p1, p2)
							if _c is not None and (_best is None or _c[0] < _best[0]):
								_best, _bveh = _c, _m
						return (_bveh, _best) if _best is not None else None

					# Does this shell drop enough for a straight hitscan to be wrong? The barrel
					# is ELEVATED to compensate for exactly that drop, so a straight ray along it
					# misses HIGH by roughly twice it. Under a quarter of a metre nobody can tell
					# and the cheap single-ray path is kept; above it - every howitzer, and any
					# gun at long range - the shell is walked down its real parabola chord by
					# chord, the way the server resolves it. The TRACER was always drawn on that
					# parabola, so until now the shell you watched and the shell that dealt the
					# damage were two different objects tens of metres apart.
					_shot_h = _shots[_sidx] if _shots else None
					_ballistic = False
					if _shot_h is not None and _offh_cfg_flag('ballistic_shells', True):
						try:
							_probe = BigWorld.wg_collideSegment(_offh_bspace(), start_pos, end_pos, 128)
							_probe_d = (_probe[0] - start_pos).length if _probe is not None else 1000.0
							_ballistic = _offh_shell_drop(_probe_d, _shot_h['speed'], _shot_h['gravity']) > 0.25
						except Exception:
							_ballistic = False

					world_dist = 99999.0
					if _ballistic:
						try:
							_walk_max = min(float(_shot_h.get('maxDistance', 1000.0)), 5000.0)
							# Chord-by-chord trace for the first ballistic shot of the battle:
							# height at each end, ground height under it, and what (if anything)
							# the chord collided with. A walk that stops while the shell still
							# has metres of air under it is a spurious collision; one that stops
							# with zero clearance is real terrain and the aim is the problem.
							_wtrace = [] if not _gun_state.get('_walk_traced') else None
							_wk = _offh_shell_path(_offh_bspace(), start_pos,
								dir_vec.scale(_shot_h['speed']), _shot_h['gravity'],
								_walk_max, _offh_mock_test, 0.1, 100, _wtrace)
							if _wtrace:
								_gun_state['_walk_traced'] = True
								LOG_DEBUG('WALK TRACE from %s (aim %s):' % (start_pos, _gun_state.get('_aim_pt')))
								for _ti, _y1, _y2, _d2, _gy, _wd, _vd in _wtrace:
									LOG_DEBUG('  chord %2d: y %.1f->%.1f at %.0fm | ground %s | clearance %s | worldHit %s vehHit %s' % (
										_ti, _y1, _y2, _d2,
										('%.1f' % _gy) if _gy is not None else 'n/a',
										('%.1f' % (_y2 - _gy)) if _gy is not None else 'n/a',
										('%.1f' % _wd) if _wd is not None else '-',
										('%.1f' % _vd) if _vd is not None else '-'))
							# A fence or a tree in the way breaks and the shell carries on, exactly
							# as on the straight path - one re-walk from just past the debris.
							if _wk['mock'] is None and _wk['world'] is not None and _offh_shot_breaks(_wk['pos'], _wk['dir']):
								_resume = _wk['pos'] + _wk['dir'].scale(0.6)
								_flown = _wk['dist'] + 0.6
								_wk2 = _offh_shell_path(_offh_bspace(), _resume,
									_wk['dir'].scale(_shot_h['speed']), _shot_h['gravity'],
									max(0.0, _walk_max - _flown), _offh_mock_test, 0.1, 100)
								_wk2['dist'] += _flown
								if _wk2['mock'] is not None:
									_m2 = _wk2['mock']
									_wk2['mock'] = (_m2[0], _m2[1], _m2[2], _m2[3] + _flown)
								_wk = _wk2
							_impact_dir = _wk['dir']
							if _wk['mock'] is not None:
								_mk_veh, _mk_col, _mk_pos, _mk_d = _wk['mock']
								enemy_mock = _mk_veh
								# [0] doubles as 'distance flown' and feeds penetration falloff,
								# so it has to be the PATH length - not the offset inside the one
								# chord that happened to contain the tank.
								enemy_hit_info = (_mk_d,) + tuple(_mk_col[1:])
								hit_dist = _mk_d
								_impact_pos = _mk_pos
								_w_col = None
							elif _wk['world'] is not None:
								_w_col = _wk['world']
								world_dist = _wk['dist']
							LOG_DEBUG('BALLISTIC shot: speed=%.0f g=%.2f drop@%.0fm=%.2fm -> %s at %.1f m' % (
								_shot_h['speed'], _shot_h['gravity'], _probe_d,
								_offh_shell_drop(_probe_d, _shot_h['speed'], _shot_h['gravity']),
								'TANK' if enemy_mock is not None else ('world' if _w_col is not None else 'nothing'),
								hit_dist if enemy_mock is not None else world_dist))
							# Everything needed to tell an aiming fault from a trajectory fault:
							# if the barrel left at the elevation getShotAngles asked for, the arc
							# HAS to come down on the aim point. If impact and aim point disagree
							# while the pitches match, the ballistics are wrong; if the pitches
							# disagree, the gun never got where it was told to go (still slewing,
							# clamped by an elevation limit, or the aim fell back to no-gravity
							# trigonometry) and the trajectory is doing its job faithfully.
							try:
								_aimpt = _gun_state.get('_aim_pt')
								_apd = (_aimpt - start_pos).length if _aimpt is not None else -1.0
								_impd = ((_wk['pos'] - start_pos).length) if _wk is not None else -1.0
								_dyv = Math.Vector3(dir_vec)
								# Elevation of the shell's actual launch vector, sign-matched to the
								# 'asked' figure below (both positive = muzzle up). Carries this
								# shot's dispersion scatter, which is a fraction of a degree.
								_cur_p = math.degrees(math.asin(max(-1.0, min(1.0, _dyv.y))))
								# The elevation that WOULD put this shell on the aim point, solved
								# in world space from the same muzzle, speed and gravity. Compared
								# against the elevation the shell actually left at, this needs no
								# assumption about anyone's pitch sign convention.
								_need_p = None
								try:
									_gv = abs(float(_shot_h['gravity'])); _sv = float(_shot_h['speed'])
									_fd = math.sqrt((_aimpt.x - start_pos.x) ** 2 + (_aimpt.z - start_pos.z) ** 2)
									_fy = _aimpt.y - start_pos.y
									_rt = _sv ** 4 - _gv * (_gv * _fd * _fd + 2.0 * _fy * _sv * _sv)
									if _rt > 0.0:
										_need_p = math.degrees(math.atan((_sv ** 2 - math.sqrt(_rt)) / (_gv * _fd)))
								except Exception:
									_need_p = None
								LOG_DEBUG('SHOT DIAG: aim=%s dist=%.1fm | impact=%s dist=%.1fm | overshoot %.1fm, %.2fm high'
									' | launch %+.3fdeg, needed %s | gunPitch %+.3fdeg hull %+.3fdeg asked %+.3fdeg'
									' | src=%s fireDisp=%.4f (%.2fx base) postShot=%.4f' % (
									_aimpt, _apd, _wk['pos'], _impd, _impd - _apd,
									(_wk['pos'].y - _aimpt.y) if _aimpt is not None else 0.0,
									_cur_p, ('%+.3fdeg' % _need_p) if _need_p is not None else 'n/a',
									math.degrees(-_gun_state.get('_aim_cur_pitch', 0.0)),
									math.degrees(_gun_state.get('_aim_hull_pitch', 0.0)),
									math.degrees(-_gun_state.get('_aim_req_pitch', 0.0)),
									_gun_state.get('_aim_src', '?'), _fire_disp,
									_fire_disp / max(_gun_state.get('base_dispersion', 1e-06), 1e-06),
									_gun_state.get('dispersion', 0.0)))
							except Exception as _sde:
								LOG_DEBUG('SHOT DIAG err:', str(_sde))
						except Exception as _bwe:
							import traceback
							LOG_DEBUG('Ballistic shot walk error:', traceback.format_exc())
							_ballistic = False
					if not _ballistic:
						# World collision: shells stop at solid walls/terrain but break fences/trees
						try:
							_w_col = BigWorld.wg_collideSegment(_offh_bspace(), start_pos, end_pos, 128)
							if _w_col is not None:
								world_dist = (_w_col[0] - start_pos).length
								if _offh_shot_breaks(_w_col[0], dir_vec):
									# Destructible broken by the shell: re-cast past the debris
									_w_col2 = BigWorld.wg_collideSegment(_offh_bspace(), _w_col[0] + dir_vec.scale(0.6), end_pos, 128)
									world_dist = ((_w_col2[0] - start_pos).length + 0.6) if _w_col2 is not None else 99999.0
									_w_col = _w_col2
						except Exception as _we:
							LOG_DEBUG('Shot world-collision error:', str(_we))

						for eid, m_veh in mock_vehicles.iteritems():
							if eid != player.playerVehicleID and getattr(m_veh, 'isAlive', False) and (getattr(m_veh, 'health', 0) or 0) > 0:
								# Sync stored position with model for future checks
								try: m_veh.position = m_veh.model.position
								except: pass
								col = m_veh.collideSegment(start_pos, end_pos)
								if col is not None and col[0] < hit_dist:
									hit_dist = col[0]
									enemy_mock = m_veh
									enemy_hit_info = col

						# A solid wall in front of the target blocks the shell
						if enemy_mock is not None and hit_dist > world_dist + 0.5:
							LOG_DEBUG('Shot blocked by world at %.1f m (tank was at %.1f m)' % (world_dist, hit_dist))
							enemy_mock = None
							enemy_hit_info = None
						if enemy_mock is not None:
							_impact_pos = start_pos + dir_vec.scale(hit_dist)

					# Stop the tracer ON the tank it struck. ProjectileMover collides against
					# STATIC geometry only (mock tanks are not entities the engine knows about),
					# so a shell that hit a tank kept flying to the terrain behind it and the
					# round visibly sailed past a target that had just taken damage. hide() sets
					# the projectile's stop plane at the impact, which is what the online client
					# does when the server reports the shell stopping on a vehicle.
					if enemy_mock is not None and g_projectile_mover and _sid is not None and _impact_pos is not None:
						try:
							g_projectile_mover.hide(_sid, _impact_pos)
						except Exception as _hde:
							LOG_DEBUG('tracer hide err:', str(_hde))

					# Damage lands when the SHELL DOES. The whole resolution used to run the instant
					# the trigger was pulled, so hit points, crits and the kill were applied about a
					# second before a slow round actually arrived - "damage is instant regardless of
					# shell velocity", and a tracer could still be in the air over a tank that had
					# already lost the health. Retail resolves on the server at impact and the client
					# only learns of it then. The GEOMETRY is still solved at fire time - that is what
					# stops the tracer in the right place, and it is what this mod has always done -
					# only the consequences now wait out the flight.
					#
					# _shots/_sidx arrive as default args because the body rebinds them, which would
					# otherwise make the ground-splash read above them an UnboundLocalError. And
					# G_MOCK_VEHICLES is the battle's identity: a round still in the air when the
					# battle ends must not deliver into the next one.
					def _offh_deliver_shot(_shots=_shots, _sidx=_sidx, _gen=globals().get('G_MOCK_VEHICLES')):
						if globals().get('G_MOCK_VEHICLES') is not _gen:
							return
						# The target died while this round was in the air - someone else got
						# there first. A shell that arrives at a wreck deals nothing and
						# certainly does not earn the frag: without this the delivery still
						# subtracted health, saw it at or below zero and awarded the kill,
						# stealing credit for a tank that was already dead.
						if enemy_mock is not None:
							if (not getattr(enemy_mock, 'isAlive', False)) or (getattr(enemy_mock, 'health', 0) or 0) <= 0:
								LOG_DEBUG('SHELL WASTED: %s was already dead when the round arrived' % getattr(enemy_mock, 'id', '?'))
								return
						# Back in the garage before the round landed: the battle window is what
						# every other exit-sensitive path here tests (see the F12 re-entry guard),
						# and delivering into a torn-down battle UI is how you get a crash on a
						# results screen rather than a hit marker.
						try:
							from gui import WindowsManager as _WMd
							if getattr(_WMd.g_windowsManager, 'battleWindow', None) is None:
								return
						except Exception:
							return
						# Ground/wall impact: offline nothing calls ProjectileMover.explode(),
						# so the tracer would fly to the map edge with no effect. Detonate it
						# at the terrain hit so shooting the ground/stone/water shows the stock
						# dust/splash burst + crater decal (deferred to shell arrival). terrain-
						# Effects are fire-and-forget (auto-expire) + the channel is destroyed
						# in the sweep -> no leak.
						if enemy_mock is None and g_projectile_mover and _sid is not None and _effectsDescr is not None and _w_col is not None and world_dist < 4900.0:
							try:
								# _impact_dir, not dir_vec: on an arcing shell the muzzle line and
								# the direction it comes down at are different vectors, and the
								# impact effect is oriented by the latter.
								_gmat = _terrain_hit_material(_offh_bspace(), _w_col[0], _impact_dir)
								# Fall back to ground if this shell has no effect for the
								# detected surface (shotEffects always defines groundHit).
								if (_gmat + 'Hit') not in _effectsDescr:
									_gmat = 'ground'
								if (_gmat + 'Hit') in _effectsDescr:
									g_projectile_mover.explode(_sid, _effectsDescr, _gmat, _w_col[0], _impact_dir)
							except Exception as _gee:
								LOG_DEBUG('Ground impact effect error:', str(_gee))
							# HE does not have to touch the tank. A round into the dirt next to one still
							# hurts it, and that is the entire point of a derp gun or artillery - without
							# this a near miss was simply a miss.
							try:
								if _shots and _offh_is_he(_shots[_sidx]):
									_offh_he_splash(_w_col[0], _shots[_sidx], getattr(player, 'playerVehicleID', -1), None)
							except Exception as _gse:
								LOG_DEBUG('HE ground splash err:', str(_gse))
						if enemy_mock and enemy_hit_info:
							# Calculate real damage from gun.shots[i].shell descriptor
							# Pre-bind: _apply_module_damage below runs OUTSIDE this try and reads both. On
							# the fallback path (shell has no 'damage' key, or the try dies early) they stayed
							# unbound -> UnboundLocalError, silently swallowed, and module crits (tracks/engine/
							# crew) never applied - it only logged 'MODULE DAMAGE ERROR'.
							all_hits = []
							_shell = None
							_hit_res = 2   # pre-bound: the miss/bounce sound branch reads it
							_he_snd_override = None
							try:
								_td = loaded_models.get('td')
								_gun = _td.gun
								_shots = _gun.get('shots', [])
								_sidx = _gun_state.get('shot_index', 0)
								_sidx = min(_sidx, len(_shots) - 1) if _shots else 0
								_shell = _shots[_sidx].get('shell') if _shots else None
							
								dmg = 0
								# Bound before the branches below so the module call can never hit an
								# UnboundLocalError and skip every crit. None = verdict unknown.
								_offh_penetrated = None
								if _shell and 'damage' in _shell:
									_dmg_data = _shell['damage']
									if hasattr(_dmg_data, '__len__') and len(_dmg_data) >= 1: avg = float(_dmg_data[0])
									else: avg = float(_dmg_data)
									dmg = int(random.uniform(avg * 0.75, avg * 1.25))
								
									# ARMOR PENETRATION LOGIC (Real HitBox) - shared model, see _offh_penetration
									_dist, _hitAngleCos, _armor = enemy_hit_info[:3]
									all_hits = enemy_hit_info[3] if len(enemy_hit_info) > 3 else []
									# Resolve against the first STRUCTURAL plate, not the nearest hit. The nearest
									# hit is often a track (vehicleDamageFactor 0), and testing the round against
									# the track and then subtracting full hull damage is what made tracks deal
									# structure damage. Spaced plates only cost penetration; HEAT dies on them.
									_spaced_mm = 0.0
									_res_hull = _offh_resolve_hull_hit(_shots[_sidx], float(_dist), all_hits)
									if _res_hull is None:
										# never reached structure - the track swallowed it
										_pen_res, eff_armor, pierce_rng = 1, 0.0, 0.0
										_hitAngleCos_s = _hitAngleCos
										LOG_DEBUG('TRACK ABSORBED the shell - no hull damage')
									else:
										_pen_res, eff_armor, pierce_rng, _spaced_mm, _hitAngleCos_s = _res_hull
										if _spaced_mm > 0.0:
											LOG_DEBUG('SPACED ARMOUR: %.0f mm eaten before the hull plate' % _spaced_mm)
									angle_cos = max(0.087, abs(_hitAngleCos_s))
								
									LOG_DEBUG('REAL ARMOR: base=%.1f eff=%.1f pierce=%.1f angle_cos=%.2f' % (_armor, eff_armor, pierce_rng, angle_cos))
								
									auto_bounce = (_pen_res == 0)
									# Gate for the module roll below. This is the ONLY call site that runs on a
									# bounce as well (so track hits still register), so it is the only one that
									# has to tell _apply_module_damage what actually happened.
									_offh_penetrated = (_pen_res == 2)

									_hit_res = 2  # penetration by default (pre-bound: the sound below reads it)
									_he_shot = _offh_is_he(_shots[_sidx])
									if auto_bounce:
										dmg = 0
										_hit_res = 0  # ricochet
										LOG_DEBUG('REAL RICOCHET (Auto-Bounce >70 deg)!')
									elif _pen_res == 1:
										dmg = 0
										_hit_res = 1  # non-penetration
										LOG_DEBUG('REAL RICOCHET / NON-PENETRATION!')
									if _he_shot and _pen_res != 2:
										# A high-explosive round that does not get through is not a zero. It
										# detonates on the plate and pushes what is left through it: half the
										# nominal, minus 1.1x the plate's NOMINAL thickness. Against heavy armour
										# that lands on 0 by itself, which is why a derp gun wants thin plate.
										_he_nom = _offh_he_nominal_armor(all_hits, getattr(enemy_mock, 'typeDescriptor', None))
										dmg = _offh_he_damage(dmg if dmg > 0 else int(avg), _he_nom, 0.0)
										_hit_res = 2 if dmg > 0 else _hit_res
										# Blast through armour it did not pierce is not a penetration, and 0.8.2
										# has its own crew line for it.
										if dmg > 0:
											_he_snd_override = 'damage_by_near_explosion_by_player'
										LOG_DEBUG('HE NO PENETRATION: armor=%.0f -> %d damage' % (_he_nom, dmg))
									# Visible impact effect + shell-hole decal on the target -
									# the ProjectileMover only shows impacts on static geometry,
									# never on the mock tanks.
									try:
										# _impact_pos/_impact_dir are the real point and heading of the
										# strike. enemy_hit_info[0] is the PATH length flown, which on
										# an arc is longer than the straight muzzle line - projecting it
										# along dir_vec would put the impact burst and the shell hole
										# past the tank and in the air above it.
										_wpos = _impact_pos if _impact_pos is not None else (start_pos + dir_vec.scale(enemy_hit_info[0]))
										_play_vehicle_hit_effect(_shell, _wpos, _impact_dir, _hit_res, target_mock=enemy_mock)
										_cn = _comp_name_from_hits(getattr(enemy_mock, 'typeDescriptor', None), enemy_hit_info[3] if len(enemy_hit_info) > 3 else [])
										_add_impact_decal(_target_sticker_map(enemy_mock), _cn, _wpos, _impact_dir, _hit_res)
									except Exception:
										pass
								else:
									# No invented damage. Every 0.8.2 shell has damage=(armor, devices); if we
									# land here the descriptor lookup itself is broken, and rolling 250-450
									# would just paper over it with a number the module system then trusts.
									dmg = 0
									_offh_penetrated = None
									LOG_DEBUG('SHELL DESCRIPTOR HAS NO damage FIELD - no damage dealt')
							except Exception as e:
								import traceback
								LOG_DEBUG('Damage calc error:', traceback.format_exc())
								# Deal nothing rather than a random number: a silent 250-450 hid the real
								# fault and fed the module/crew system fabricated input.
								dmg = 0
								_offh_penetrated = None
						
							# Modules and crew take their hits on ANY strike that reached the tank, not
							# only when hull damage resulted. Since tracks became spaced armour a pure
							# track hit deals 0 structure damage, so gating this on dmg > 0 meant a track
							# could never be broken at all.
							# HE also reaches whatever else is standing near the impact. The tank
							# actually struck is excluded - it is handled right above as the dist 0 case.
							try:
								if _offh_is_he(_shots[_sidx]):
									_offh_he_splash(_impact_pos if _impact_pos is not None else (start_pos + dir_vec.scale(enemy_hit_info[0])),
										_shots[_sidx],
										getattr(player, 'playerVehicleID', -1), getattr(enemy_mock, 'id', -1))
							except Exception as _hse:
								LOG_DEBUG('HE splash err:', str(_hse))
							try:
								# Clear first. _apply_module_damage stamps last_sound on entry, but it
								# returns before that when module damage is switched off - and the report
								# below reads last_sound to decide whether this strike critted anything.
								enemy_mock.last_sound = None
								dmg = _apply_module_damage(enemy_mock, all_hits, start_pos, end_pos, dmg, _shell, getattr(player, 'playerVehicleID', -1), _offh_penetrated)
								# (the hit line is chosen below, from last_sound or the HE override)
							except Exception as ex:
								import traceback
								LOG_DEBUG("MODULE DAMAGE ERROR:", traceback.format_exc())
							# Ledger: record the STRIKE, before the dmg > 0 gate below. A
							# ricochet or a bare track hit deals no structure damage but is
							# still a hit, and leaving it out understates the accuracy the
							# sniper medal is judged on. damageDealt takes what actually came
							# off the tank, not the nominal roll.
							try:
								from gui.mods.offhangar import battle_ledger as _BLED
								_BLED.get().note_hit(
									getattr(player, 'playerVehicleID', -1),
									getattr(enemy_mock, 'id', -1),
									damage=min(int(dmg), max(0, int(getattr(enemy_mock, 'health', 0)))) if dmg > 0 else 0,
									pierced=bool(_offh_penetrated) if _offh_penetrated is not None else (_hit_res == 2),
									he=bool(_offh_is_he(_shots[_sidx])),
									crits=1 if getattr(enemy_mock, 'last_sound', None) else 0)
							except Exception:
								pass
							if dmg > 0:

								actual_dmg = min(dmg, max(0, enemy_mock.health))
								enemy_mock.health -= dmg
								enemy_mock.damage_from_player = (getattr(enemy_mock, 'damage_from_player', 0) or 0) + actual_dmg
								enemy_mock.hits_from_player = (getattr(enemy_mock, 'hits_from_player', 0) or 0) + 1
								LOG_DEBUG('HIT! Damage:', dmg, 'Enemy HP:', enemy_mock.health)
							
								try:
									_is_ally = _offh_is_ally(enemy_mock)
									if enemy_mock.health <= 0:
										sound_str = 'ally_killed_by_player' if _is_ally else 'enemy_killed_by_player'
									elif _is_ally:
										# 0.8.2 ships no ally HIT line - only ally_killed. Announcing a penetration on
										# a team-mate with the ENEMY line was simply wrong, so say nothing at all.
										sound_str = None
									else:
										sound_str = _he_snd_override or getattr(enemy_mock, 'last_sound', None) or 'armor_pierced_by_player'
									# sound_str is deliberately None for a hit on a team-mate (0.8.2 ships no
									# ally-hit line); _offh_notify ignores a falsy event instead of asking the
									# engine for it, which used to log "Couldn't find None event" on every one.
									_offh_notify(sound_str)
								except Exception as e:
									LOG_DEBUG('Hit sound error:', str(e))
							else:
								# dmg == 0. Retail grades this from SHOT_RESULT, not from the damage number
								# (Avatar.playShotResultNotification / __shotResultSeveritiesForEnemy):
								#   RICOCHET                -> armor_ricochet_by_player
								#   ARMOR_NOT_PIERCED       -> armor_not_pierced_by_player
								#   ARMOR_PIERCED_NO_DAMAGE -> armor_pierced_crit_by_player
								# Only the first two were here, so the commonest zero-damage hit in this
								# build - a round that got through, or through a track, and broke something
								# without costing hit points - was announced as a FAILURE to penetrate.
								# _apply_module_damage upgrades last_sound to the crit line whenever a device
								# or a crewman was actually scored, and that is the crit flag read here.
								try:
									_crit_scored = str(getattr(enemy_mock, 'last_sound', '') or '').endswith('_crit_by_player')
									if _offh_penetrated is True or _crit_scored:
										_nz = 'armor_pierced_crit_by_player'
									elif _hit_res == 0:
										_nz = 'armor_ricochet_by_player'
									else:
										_nz = 'armor_not_pierced_by_player'
									if _offh_is_ally(enemy_mock):
										_nz = None   # no ally bounce / no-pen line exists either
									_offh_notify(_nz, getattr(enemy_mock, 'id', None))
								except Exception as _nze:
									LOG_DEBUG('No-pen sound error:', str(_nze))
						
							# Update vehicle marker health
							try:
								hp_percent = max(0, int((float(enemy_mock.health) / float(enemy_mock.maxHealth)) * 100.0))
								player.arena.onVehicleStatisticsUpdate(enemy_mock.id)
								from gui import WindowsManager
								bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, 'vMarkersManager'):
									marker = getattr(enemy_mock, 'marker', None)
									if marker is not None:
										bw.vMarkersManager.onVehicleHealthChanged(marker, max(0, enemy_mock.health), getattr(player, 'playerVehicleID', -1), 0)
										try:
											bw.vMarkersManager.showVehicleDamageInfo(marker, dmg, 0, 0, 1)
										except:
											pass
										LOG_DEBUG('HP updated via marker, HP=%d' % enemy_mock.health)
									else:
										LOG_DEBUG('No marker on enemy_mock!')
								if bw and hasattr(bw, 'minimap'):
									try: bw.minimap.notifyVehicleStop(enemy_mock.id) if enemy_mock.health <= 0 else None
									except: pass
								try:
									player.showVehicleDamageInfo(enemy_mock.id, 0, 0, dmg)
								except:
									pass
							except Exception as e:
								LOG_DEBUG('Hit GUI error:', str(e))
						
							if enemy_mock.health <= 0:
								_offh_set_alive(enemy_mock, False)
								try:
									from gui import WindowsManager
									bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
									if bw and hasattr(bw, '_Battle__arena'):
										bw._Battle__arena.vehicles[enemy_mock.id]['isAlive'] = False
										bw._Battle__updatePlayers()
								except: pass
								LOG_DEBUG('ENEMY DESTROYED!')
								try:
									p_id = getattr(player, 'playerVehicleID', -1)
									if p_id != -1 and p_id in player.arena.vehicles and hasattr(player.arena, 'onVehicleKilled'):
										_pteam = getattr(player, '_offhangar_team', 1)
										_vteam = getattr(enemy_mock, '_bot_team', enemy_mock.publicInfo.get('team', 2) if getattr(enemy_mock, 'publicInfo', None) is not None else 2)
										_frag_diff = -1 if _pteam == _vteam else 1
										player.arena.vehicles[p_id]['frags'] = player.arena.vehicles[p_id].get('frags', 0) + _frag_diff
										if _frag_diff == -1:
											player.arena.vehicles[p_id]['isTeamKiller'] = True
											player.isTeamKiller = True
											LOG_DEBUG('ARENA DIR: %s' % dir(player.arena))
											try: player.arena.onTeamKiller(p_id)
											except Exception as e: LOG_DEBUG('onTeamKiller error:', str(e))
											try:
												from gui import WindowsManager
												bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
												if bw and hasattr(bw, '_Battle__vehicles'):
													try:
														if hasattr(bw, '_Battle__arena'):
															bw._Battle__arena.vehicles[p_id]['isTeamKiller'] = True
															LOG_DEBUG('Updated bw._Battle__arena for p_id')
														bw._Battle__updatePlayers()
														if hasattr(bw, '_Battle__onTeamKiller'):
															bw._Battle__onTeamKiller(p_id)
													except Exception as e: LOG_DEBUG('Update __arena error:', str(e))
											except Exception as e: LOG_DEBUG('BW VEHS ERROR:', str(e))
											try: player.arena.onVehicleUpdated(p_id)
											except: pass
											try: player.arena.onVehicleAdded(p_id)
											except: pass
										if hasattr(player.arena, 'statistics'):
											if p_id not in player.arena.statistics: player.arena.statistics[p_id] = {'frags': 0}
											player.arena.statistics[p_id]['frags'] = player.arena.statistics[p_id].get('frags', 0) + _frag_diff
										player.arena.onVehicleKilled(enemy_mock.id, p_id, 0)
										# kill feed is posted centrally in _KillEventWrapper
										for v_id in player.arena.vehicles:
											if v_id not in player.arena.statistics: player.arena.statistics[v_id] = {'frags': 0}
										player.arena.onVehicleStatisticsUpdate(p_id)
										if hasattr(bw, '_Battle__updatePlayers'):
											try: bw._Battle__updatePlayers()
											except Exception as e: LOG_DEBUG('updatePlayers error:', e)
										LOG_DEBUG('FRAGS AFTER:', player.arena.vehicles[p_id].get('frags'))
										LOG_DEBUG('ARENA HAS STATS:', hasattr(player.arena, 'statistics'))
										from gui import WindowsManager
										bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
										if bw and hasattr(bw, '_Battle__fragCorrelation'):
											p_team = getattr(player, 'team', 1)
											allied = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') == p_team)
											enemy = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') != p_team)
											bw._Battle__fragCorrelation.updateFrags(allied, enemy)
											# kill feed is posted centrally in _KillEventWrapper
								except Exception as _e:
									LOG_DEBUG('Frag update error:', _e)
							
								# --- SWAP TO DESTROYED MODEL ---
								try:
									if getattr(enemy_mock, '_wreck_done', False):
										raise StopIteration  # wreck already handled by another kill path
									enemy_mock._wreck_done = True
									_dtd = enemy_mock.typeDescriptor
									_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
									_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
									_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
									_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
									_old_ch = enemy_mock._chassis_model
									_old_pos = _old_ch.position
									_old_yaw = _old_ch.yaw
									# pitch/roll as well: a wreck used to snap dead level on any slope
									try: _old_pitch = _old_ch.pitch
									except Exception: _old_pitch = 0.0
									try: _old_roll = _old_ch.roll
									except Exception: _old_roll = 0.0
									_old_ch_ref = _old_ch
									# m_veh=enemy_mock is NOT decoration - it is THE invisible-tank bug.
									# The body below uses m_veh for the killed tank, but m_veh is the loop
									# variable of the hit search above (for eid, m_veh in
									# mock_vehicles.iteritems()), which the loop leaves bound to the LAST
									# mock it walked - a live, untouched bot, since the hit itself is
									# remembered in enemy_mock. So every kill the PLAYER scored ran the
									# wreck code against a bystander: it detached that bot's chassis from
									# its entity (bw_entity.model = None, a few lines down), which takes
									# the model out of the world for good. The bot kept driving, aiming
									# and shooting, it stayed shootable and its marker stayed up - only
									# the geometry was gone, and nothing ever puts it back. Same victim
									# every battle, because the mock dict is keyed by spawn id and its
									# iteration order is fixed, so it always looked like one particular
									# tank was cursed.
									# It also aimed the wreck's node/matrix refs and its collision
									# obstacle at that bystander instead of at the wreck.
									# The bot-vs-bot and burn-out swaps already bind their victim this
									# way (m_veh=hit_veh / _mv=_mv); this path was the one that did not.
									def _swap_destroyed_model(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu, _old_ch_ref=_old_ch_ref, _old_pos=_old_pos, _old_yaw=_old_yaw, m_veh=enemy_mock):
										_add_model(_d_ch)
										_add_model(_d_hu)
										_add_model(_d_tu)
										_add_model(_d_gu)
										def _attach_when_ready():
											if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
												BigWorld.callback(0.1, _attach_when_ready)
												return
											try: BigWorld.delModel(_d_hu)
											except: pass
											try: BigWorld.delModel(_d_tu)
											except: pass
											try: BigWorld.delModel(_d_gu)
											except: pass
											try: _old_ch_ref.visible = False
											except: pass
											try: _old_ch_ref.visibleAttachments = False
											except: pass
											try: BigWorld.delModel(_old_ch_ref)
											except: pass
										
											if getattr(m_veh, 'bw_entity', None) is not None:
												try: m_veh.bw_entity.model = None
												except:
													try: m_veh.bw_entity.model = BigWorld.Model('')
													except: pass
										
											# Wreck must rest on the ground (mid-air kill would leave a floating
											# wreck). _wpos: NEVER rebind _old_pos - in the player-kill path this
											# code sits in a nested function where _old_pos is only a closure var;
											# assigning it made it local -> UnboundLocalError -> vanishing wrecks.
											_wpos = _old_pos
											try:
												import BigWorld as _bwx, Math as _mx
												_gw = _bwx.wg_collideSegment(_offh_bspace(), _mx.Vector3(_wpos.x, _wpos.y + 2.0, _wpos.z), _mx.Vector3(_wpos.x, _wpos.y - 500.0, _wpos.z), 128)
												if _gw is not None and _wpos.y > _gw[0].y + 0.5:
													_wpos = _mx.Vector3(_wpos.x, _gw[0].y, _wpos.z)
											except Exception:
												pass
											_d_ch.position = _wpos
											_d_ch.yaw = _old_yaw
											# Whole orientation in one go. Model.pitch/.roll assigned separately after
											# .yaw do NOT compose - each setter rebuilds the transform, which left the
											# wreck mis-oriented (turretless hulls like the Foch 155 worst of all).
											# A Servo on a prepared matrix is what the live chassis already uses.
											try:
												_wr_mat = Math.Matrix()
												_wr_mat.setRotateYPR((_old_yaw, _old_pitch, _old_roll))
												_wr_mat.translation = _wpos
												_d_ch.addMotor(BigWorld.Servo(_wr_mat))
												m_veh._wreck_mat = _wr_mat   # hold a ref: a GC'd matrix drops the wreck
											except Exception as _wme:
												LOG_DEBUG('Wreck orientation failed:', str(_wme))
											# freeze the turret where the bot last aimed (identity snapped it forward)
											# snapshot of the last aim: turret where it pointed, barrel where it sat
											_t_mat = Math.Matrix(); _t_mat.setRotateYPR((float(getattr(m_veh, '_turret_yaw', 0.0) or 0.0), 0, 0))
											m_veh._wreck_t_mat = _t_mat   # hold a ref: a GC'd matrix drops the node back to identity
											_g_mat = Math.Matrix(); _g_mat.setRotateYPR((0, float(getattr(m_veh, '_gun_pitch', 0.0) or 0.0), 0))
											m_veh._wreck_g_mat = _g_mat   # hold a ref: a GC'd matrix drops the node back to identity
											try: _d_ch.node('V').attach(_d_hu)
											except: pass
											try: 
												m_veh._d_t_node = _d_hu.node('HP_turretJoint', _t_mat)
												m_veh._d_t_node.attach(_d_tu)
											except: pass
											try: 
												m_veh._d_g_node = _d_tu.node('HP_gunJoint', _g_mat)
												m_veh._d_g_node.attach(_d_gu)
											except: pass
											try:
												m_veh._collision_obstacle = BigWorld.PyModelObstacle(
													_dtd.hull['models']['destroyed'],
													_dtd.turret['models']['destroyed'],
													m_veh.matrix,
													False
												)
											except: pass
											LOG_DEBUG('Destroyed model swapped OK')
										_attach_when_ready()
									BigWorld.callback(0.0, _swap_destroyed_model)
								except Exception as _de:
									LOG_DEBUG('Destroyed model swap error:', str(_de))

					_tof = 0.0
					try:
						_sp_t = float(_shot_h['speed']) if _shot_h is not None else 0.0
						_d_t = hit_dist if enemy_mock is not None else (world_dist if world_dist < 4900.0 else 0.0)
						if _sp_t > 0.0 and 0.0 < _d_t < 5000.0:
							_tof = min(_d_t / _sp_t, 5.0)
					except Exception:
						_tof = 0.0
					if _tof > 0.03:
						LOG_DEBUG('SHELL IN FLIGHT: %.0f m, impact in %.2f s' % (_d_t, _tof))
						BigWorld.callback(_tof, _offh_deliver_shot)
					else:
						_offh_deliver_shot()
					
					# --- GUNSHOT SOUND & EFFECTS ---
					try:
						_offh_player_notifications()   # built once, bind-proofed, before the first report

						td = loaded_models.get('td')
						# Barrel recoil animation on the player's gun
						_trigger_gun_recoil(getattr(BigWorld.player(), '_offhangar_gun_recoil', None))
						# Hull rock-back: impulse backward along the shot direction
						try:
							_trigger_shot_impulse(getattr(BigWorld.player(), '_offhangar_swinging', None), Math.Vector3(-dir_vec.x, -dir_vec.y, -dir_vec.z), td.gun['impulse'] if td else 0.0)
						except Exception:
							pass
						_mflash_played = False
						if td is not None:
							_mflash_played = _play_muzzle_flash(BigWorld.player(), loaded_models.get('gun'), td, is_player=True)
						# The gun's effects list plays the real per-gun shot sound (like the
						# live game); the forced caliber-bucket sound doubled it with a
						# generic one. Bucket kept only as a fallback.
						if not _mflash_played:
							_fallback_gun_sound(td, loaded_models.get('chassis') or loaded_models.get('hull') or loaded_models.get('turret') or loaded_models.get('gun'))
					except Exception as e: pass
						
					LOG_DEBUG('OfflineBattle: SHOOT HIT LOGIC RUN!')
				except Exception as e:
					import traceback
					LOG_DEBUG('Shoot ERROR:', traceback.format_exc())
				return


			# --- ENEMY CLONE SPAWNER (Key O) ---
			def _find_safe_spawn(want_pos):
				# Find a free, flat ground spot near want_pos:
				# not inside the player/other tanks, not against walls, not on roofs/steep slopes
				import math as _m
				import BigWorld, Math
				_pl = BigWorld.player()
				_sid = _offh_bspace()  # battle space, not empty player.spaceID (dedicated mode)
				
				def _ground_at(x, z, y_hint):
					# Probe ground just above the expected height (not from +1000: avoids roofs)
					try:
						c = BigWorld.wg_collideSegment(_sid, Math.Vector3(x, y_hint + 3.0, z), Math.Vector3(x, y_hint - 150.0, z), 128)
						if c is not None:
							return c[0].y
					except Exception:
						pass
					return None
				
				def _is_free(x, y, z):
					# 1) Keep distance: >=10 m to the player, >=8 m to other tanks/wrecks
					try:
						if (x - veh_pos[0]) ** 2 + (z - veh_pos[2]) ** 2 < 100.0:
							return False
					except Exception:
						pass
					try:
						for _sv in mock_vehicles.values():
							_svp = getattr(_sv, 'position', None)
							if _svp is not None and (x - _svp.x) ** 2 + (z - _svp.z) ** 2 < 64.0:
								return False
					except Exception:
						pass
					# 2) Clearance: 8 horizontal rays at hull height (no walls right next to us)
					for _i in range(8):
						_a = _i * _m.pi / 4.0
						try:
							if BigWorld.wg_collideSegment(_sid, Math.Vector3(x, y + 1.2, z), Math.Vector3(x + _m.sin(_a) * 3.5, y + 1.2, z + _m.cos(_a) * 3.5), 128) is not None:
								return False
						except Exception:
							pass
					# 3) No steep slope / roof edge: ground probes 2.5 m around
					for _dx, _dz in ((2.5, 0.0), (-2.5, 0.0), (0.0, 2.5), (0.0, -2.5)):
						_gy = _ground_at(x + _dx, z + _dz, y)
						if _gy is None or abs(_gy - y) > 1.5:
							return False
					return True
				
				# Candidates: the desired point itself, then rings (8 directions) up to 30 m
				_cands = [(want_pos.x, want_pos.z)]
				for _r in (4.0, 8.0, 13.0, 20.0, 30.0):
					for _i in range(8):
						_a = _i * _m.pi / 4.0
						_cands.append((want_pos.x + _m.sin(_a) * _r, want_pos.z + _m.cos(_a) * _r))
				for _cx, _cz in _cands:
					_gy = _ground_at(_cx, _cz, want_pos.y)
					if _gy is None:
						continue
					if _is_free(_cx, _gy, _cz):
						return Math.Vector3(_cx, _gy, _cz)
				# Fallback 1: force the desired point down to the ground (long ray)
				try:
					c = BigWorld.wg_collideSegment(_sid, Math.Vector3(want_pos.x, want_pos.y + 300.0, want_pos.z), Math.Vector3(want_pos.x, want_pos.y - 1000.0, want_pos.z), 128)
					if c is not None:
						return Math.Vector3(want_pos.x, c[0].y, want_pos.z)
				except Exception:
					pass
				# Fallback 2: 15 m in front of the player
				try:
					_fx = veh_pos[0] + _m.sin(veh_yaw[0]) * 15.0
					_fz = veh_pos[2] + _m.cos(veh_yaw[0]) * 15.0
					_gy = _ground_at(_fx, _fz, veh_pos[1])
					if _gy is not None:
						return Math.Vector3(_fx, _gy, _fz)
				except Exception:
					pass
				# Last resort: desired x/z at the PLAYER's ground height. Never return
				# want_pos unchanged - its y is the sky-high probe start (~300), exactly
				# what made bots spawn in the air and fall.
				try:
					return Math.Vector3(want_pos.x, float(veh_pos[1]), want_pos.z)
				except Exception:
					return Math.Vector3(want_pos)
			_orig_handleKeyEvent = g_offline_aih.handleKeyEvent
			_spawn_count = [0]
			def _mock_handleKeyEvent(event):
				import BigWorld, Keys, Math
				player = BigWorld.player()
				# X-ray overlay first, and only when it was actually armed by config.
				# It claims F8/F9/F10 and returns True for those, so nothing else in
				# this handler sees them; every other key falls straight through.
				_xr = globals().get('g_offh_internal_xray')
				if _xr is not None:
					try:
						if _xr.handle_key_event(event):
							return True
					except Exception as _xke:
						LOG_DEBUG('X-ray key handling failed, disabling overlay:', str(_xke))
						try: _xr.stop()
						except Exception: pass
						globals()['g_offh_internal_xray'] = None
				# Post-death spectator: left-click cycles to the next living ally, right-click
				# the previous one - but NOT while the ESC menu is up. Its clicks are meant for
				# the menu, and they were also switching the spectated tank underneath it.
				if getattr(player, '_offh_spectating', False) and not _offh_cursor_shown():
					try:
						if event.isKeyDown() and event.key == Keys.KEY_LEFTMOUSE:
							player._offh_spec_idx = getattr(player, '_offh_spec_idx', 0) + 1
							return
						if event.isKeyDown() and event.key == Keys.KEY_RIGHTMOUSE:
							# right-click = previous target; negative index wraps (Python % maps it)
							player._offh_spec_idx = getattr(player, '_offh_spec_idx', 0) - 1
							return
					except Exception:
						pass
				
				if event.key == Keys.KEY_RIGHTMOUSE:
					if event.isKeyDown():
						_gun_state['rmb_down'] = True
						bot = getattr(player, '_outlined_bot', None)
						prev_target = getattr(player, '_autoaim_target', None)
						if bot is not None:
							team = getattr(bot, '_bot_team', 2)
							player_team = getattr(player, '_offhangar_team', 1)
							if team != player_team and getattr(bot, 'health', 0) > 0:
								if prev_target == bot:
									player._autoaim_target = None
								else:
									player._autoaim_target = bot
							else:
								player._autoaim_target = None
						else:
							player._autoaim_target = None
							
						curr_target = getattr(player, '_autoaim_target', None)
						if prev_target != curr_target:
							import debug_utils
							debug_utils.LOG_DEBUG('Autoaim state changed:', prev_target, '->', curr_target)
							# target_captured / target_unlocked both set shouldBindToPlayer, so these
							# went the same way as the crew lines: play() raised on the account's
							# missing `vehicle` and the lock chirp never sounded.
							_offh_notify('target_captured' if curr_target is not None else 'target_unlocked')
						
						if getattr(player, '_autoaim_target', None) is None:
							_gun_state['locked_local_yaw'] = turret_yaw[0]
							_gun_state['locked_local_pitch'] = gun_pitch[0]
					else:
						_gun_state['rmb_down'] = False
						
				# An OPEN equipment fly-out owns the number keys while it is up. Route them to
				# the panel, but only when a fly-out is actually expanded and the key is bound
				# to one of its entities - a stale fly-out must not eat the shell keys forever.
				if event.isKeyDown() and event.key in (Keys.KEY_1, Keys.KEY_2, Keys.KEY_3, Keys.KEY_4, Keys.KEY_5, Keys.KEY_6):
					try:
						import gui.WindowsManager as _WMfk
						_bwfk = getattr(_WMfk.g_windowsManager, 'battleWindow', None)
						_panelfk = getattr(_bwfk, 'consumablesPanel', None) if _bwfk is not None else None
						if _panelfk is not None and getattr(_panelfk, '_ConsumablesPanel__expandEquipmentIdx', None) is not None:
							_kcmap = getattr(_panelfk, '_ConsumablesPanel__entitiesKCMap', None) or {}
							if event.key in _kcmap:
								_panelfk.handleKey(event.key)
								return
					except Exception as _fke:
						LOG_DEBUG('flyout key route err:', str(_fke))
				
				# Shell slots honor Controls->Equipment rebinds (CMD_AMMO_CHOICE_1..3)
				_ammo_bind = [Keys.KEY_1, Keys.KEY_2, Keys.KEY_3]
				try:
					import CommandMapping as _CMap
					_ammo_bind = [(_CMap.g_instance.get('CMD_AMMO_CHOICE_%d' % (_n + 1)) or _ammo_bind[_n]) for _n in range(3)]
				except Exception:
					pass
				if event.isKeyDown() and event.key in _ammo_bind:
					try:
						idx = _ammo_bind.index(event.key)
						from gui import WindowsManager
						bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
						panel = getattr(bw, 'consumablesPanel', None) if bw else None
						if panel and ('ammo_%d' % idx) in _gun_state:
							# Empty type: retail refuses the switch outright. Switching anyway set
							# clip = min(clip_size, 0) = 0 AND started a full reload animation for
							# ammunition that does not exist.
							if (_gun_state.get('ammo_%d' % idx, 0) or 0) <= 0:
								LOG_DEBUG('Ammo switch refused: slot %d is empty' % idx)
							elif _gun_state.get('shot_index', 0) == idx and _gun_state.get('next_shot_index', idx) == idx:
								pass   # already loaded and already selected - retail does nothing
							elif _gun_state.get('next_shot_index', _gun_state.get('shot_index', 0)) != idx:
								# FIRST press: queue the type for the next load. Retail's
								# Avatar.onAmmoButtonPressed sends NEXT_SHELLS here, which only calls
								# consumablesPanel.setNextShell - the round in the breech is NOT
								# thrown away and the gun does NOT start reloading. Pressing the same
								# slot again is what sends CURRENT_SHELLS and forces the swap.
								_gun_state['next_shot_index'] = idx
								try: panel.setNextShell(idx)
								except Exception as e:
									LOG_DEBUG('setNextShell error:', str(e))
								LOG_DEBUG('AMMO queued: slot %d loads after the current round' % idx)
							else:
								# SECOND press on the already-queued slot: swap now, dumping the
								# loaded round - the full reload below.
								_gun_state['shot_index'] = idx
								_gun_state['next_shot_index'] = idx
								# The magazine is EMPTY while the type is being changed. That reload is the
								# gun being cleared and refilled with the other shell, so leaving the clip
								# full meant the green rounds sat there through the whole animation. The
								# reload-complete handler puts them back.
								_gun_state['clip'] = 0
								_gun_state['reloadTime'] = _gun_state['reload'] # Full reload on switch
								panel.setCurrentShell(idx)
								panel.setShellQuantityInSlot(idx, _gun_state['ammo_%d' % idx], _gun_state['clip'])
								try: panel.setCoolDownTime(idx, 0.0)
								except Exception as e:
									import debug_utils; debug_utils.LOG_DEBUG('setCoolDownTime reset error switch:', str(e))
								try: panel.setCoolDownTime(idx, _gun_state['reloadTime'])
								except Exception as e:
									import debug_utils; debug_utils.LOG_DEBUG('setCoolDownTime error switch:', str(e))
								try:
									aim = getattr(g_offline_aih, 'aim', None)
									if aim:
										try: aim.setReloading(0.0, None)
										except: pass
										aim.setReloading(_gun_state['reloadTime'], None)
										aim.setAmmoStock(_gun_state['ammo_%d' % idx], _gun_state['clip'], False)
								except Exception as e:
									import debug_utils; debug_utils.LOG_DEBUG('aim error switch:', str(e))
					except Exception as e:
						import debug_utils
						debug_utils.LOG_DEBUG('Key ammo switch error:', str(e))
						
				# Consumable slots honor Controls->Equipment rebinds (CMD_AMMO_CHOICE_4..6)
				_cons_bind = [Keys.KEY_4, Keys.KEY_5, Keys.KEY_6]
				try:
					import CommandMapping as _CMap
					_cons_bind = [(_CMap.g_instance.get('CMD_AMMO_CHOICE_%d' % (_n + 4)) or _cons_bind[_n]) for _n in range(3)]
				except Exception:
					pass
				if event.isKeyDown() and event.key in _cons_bind:
					# One entry point for every consumable. The old inline handler repaired
					# EVERYTHING from any kit and offered no module/crew choice at all.
					try:
						_offh_activate_equipment(_cons_bind.index(event.key) + 3)
					except Exception as _eqe:
						import debug_utils
						debug_utils.LOG_DEBUG('Consumable hotkey error:', str(_eqe))
					return
				if event.isKeyDown() and event.key == Keys.KEY_K:
					try:
						import BigWorld
						player = BigWorld.player()
						if hasattr(player, 'arena'):
							p_team = getattr(player, '_offhangar_team', getattr(player, 'team', 1))
							p_name = getattr(player, 'name', 'Player')
							p_dbid = getattr(player, 'databaseID', 1)
							_td = None
							try: _td = loaded_models.get('td')
							except: pass
							if not _td: _td = getattr(player, 'vehicleTypeDescriptor', None)
							
							p_cd = getattr(getattr(_td, 'type', None), 'compactDescr', 0)
							
							LOG_DEBUG('BATTLE RESULTS LOCAL P_CD IS:', p_cd)
							
							import debug_utils
							debug_utils.LOG_DEBUG('BATTLE RESULTS P_CD IS:', p_cd)
							
							if p_cd == 0 and hasattr(player, 'arena') and player.playerVehicleID in player.arena.vehicles:
								_vinfo = player.arena.vehicles[player.playerVehicleID]
								_vtype = _vinfo.get('vehicleType', None)
								if _vtype:
									p_cd = getattr(getattr(_vtype, 'type', None), 'compactDescr', 0)
									debug_utils.LOG_DEBUG('BATTLE RESULTS FALLBACK P_CD IS:', p_cd)
							
							allied = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team', 2) == p_team)
							enemy = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team', 2) != p_team)
							# A capture win ends the battle through this same K flow and
							# forces the outcome instead of the frag comparison below.
							_forced_w = globals().pop('G_OFFH_FORCED_WINNER', None)
							if _forced_w is not None:
								if _forced_w == 0:
									# Draw: winnerTeam is derived from these two, and only EQUAL counts
									# map to 0. Forcing 0 straight through read as a defeat.
									allied = enemy = 0
								else:
									allied, enemy = (1, 0) if _forced_w == p_team else (0, 1)
							
							def _show_res():
								try:
									from gui.SystemMessages import SM_TYPE, pushMessage
									pushMessage('Offline battle finished. Returning to Hangar...'.encode('utf-8'), SM_TYPE.Information)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									import MusicController
									if hasattr(MusicController, 'g_musicController') and MusicController.g_musicController:
										_mc = MusicController.g_musicController
										try: _mc.stop()
										except: pass
										evt = None
										if allied > enemy:
											evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_VICTORY', getattr(MusicController, 'MUSIC_EVENT_VICTORY', 'music_victory'))
										elif allied < enemy:
											evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_LOSE', getattr(MusicController, 'MUSIC_EVENT_LOSE', 'music_lose'))
										else:
											evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_DRAW', getattr(MusicController, 'MUSIC_EVENT_DRAW', 'music_draw'))
										try: _mc.play(evt)
										except: pass
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY MUSIC:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									import battle_results_shared
									mock_arena_id = 999
									
									v_id = getattr(player, 'playerVehicleID', 1)
									p_max_health = getattr(getattr(player, 'vehicleTypeDescriptor', None), 'maxHealth', 1000)
									_player_mock = globals().get('G_MOCK_VEHICLES', {}).get(getattr(player, 'playerVehicleID', -1))
									# `player.vehicle` DOES NOT EXIST offline - the player is the ACCOUNT,
									# and that attribute raises - so this read always fell through to its
									# default and the results screen reported EVERY battle at full health:
									# damageReceived a flat 0 and killerID 0 even when the player had been
									# destroyed. The mock is the only thing carrying his real HP; the
									# capture tick already leans on it for exactly this reason.
									if _player_mock is not None:
										p_max_health = int(getattr(_player_mock, 'maxHealth', None) or p_max_health)
										# The real health, NOT _offh_hp_display: that one deliberately
										# reports the HP a drowned tank had when it went under (so the
										# damage panel does not flash to zero mid-sink), and a results
										# screen fed from it would list a drowned player as alive.
										p_health = max(0, min(int(getattr(_player_mock, 'health', None) or 0), p_max_health))
									else:
										p_health = p_max_health
									_p_killer_id = (getattr(_player_mock, 'last_killer_id', None) or 255) if p_health <= 0 else 0
									
									total_dmg_dealt = 0
									total_frags = 0
									total_hits = 0
									players_dict = {p_dbid: {'name': p_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': p_team, 'igrType': 0}}
									vehicles_dict = {v_id: {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': 10, 'hits': 8, 'he_hits': 0, 'pierced': 8, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': max(0, p_max_health - p_health), 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': {}, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0}}
									personal_details = {}
									# Ledger: everything below is summed out of the who-hit-whom record.
									# The team map lets totals_for() split friendly fire into
									# tdamageDealt/tkills, which the client prints in red, instead of
									# folding it into the normal totals.
									_led = None
									_led_team = {}
									try:
										from gui.mods.offhangar import battle_ledger as _BLED
										_led = _BLED.get()
										for _tvid, _tinfo in getattr(player.arena, 'vehicles', {}).items():
											_led_team[_tvid] = _tinfo.get('team', 0)
										_led_team[v_id] = p_team
										LOG_DEBUG('Ledger:', _led.summary())
									except Exception as _lde:
										LOG_DEBUG('Ledger unavailable, results fall back to zeros:', str(_lde))
										_led = None
									_led_mates = {}
									for _mvid, _mteam in _led_team.items():
										_led_mates.setdefault(_mteam, []).append(_mvid)
									def _led_totals(_vid):
										if _led is None:
											return None
										return _led.totals_for(_vid, _led_mates.get(_led_team.get(_vid, 0), ()))
									
									for vid, vinfo in getattr(player.arena, 'vehicles', {}).items():
										if vid == v_id: continue
										bot_team = vinfo.get('team', 2)
										
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											bot_team = getattr(_mock_vehicles[vid], '_bot_team', bot_team)
										bot_name = vinfo.get('name', 'Bot')
										# Force bot DBID to be its vehicle ID so it never overlaps the player's DBID!
										bot_dbid = vid
										td = vinfo.get('vehicleType', None)
										
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											_true_td = getattr(_mock_vehicles[vid], 'typeDescriptor', None)
											if _true_td: td = _true_td
										
										td_type = getattr(td, 'type', None)
										bot_cd = getattr(td_type, 'compactDescr', 0)
										
										players_dict[bot_dbid] = {'name': bot_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': bot_team, 'igrType': 0}
										
										is_killed = not vinfo.get('isAlive', True)
										bot_hp = getattr(td, 'maxHealth', 1000)
										bot_max_hp = bot_hp
										
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											bot_hp = max(0, getattr(_mock_vehicles[vid], 'health', 0))
											bot_max_hp = getattr(_mock_vehicles[vid], 'maxHealth', bot_max_hp)
											if bot_hp <= 0: is_killed = True
										
										if not 'mock_vehicles' in locals() and not '_mock_vehicles' in locals():
											bot_hp = 0 if is_killed else bot_max_hp
											
										# Stats for this bot, summed out of the ledger. The three old
										# counters lived on the VICTIM and could not say who fired, so
										# 'damageDealt' below used to be handed damage_from_bots - the
										# damage this bot had RECEIVED. Every bot row was wrong.
										_lt = _led_totals(vid)
										if _lt is None:
											_lt = {'shots': 0, 'hits': 0, 'he_hits': 0, 'pierced': 0,
												'damageDealt': 0, 'damageAssisted': 0, 'spotted': 0,
												'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0,
												'damageReceived': 0, 'shotsReceived': 0, 'isTeamKiller': False,
												'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 0}
										# Health tells the truth about what it took even when a damage
										# path forgot to report; never show less than actually came off.
										dmg_received = max(int(_lt['damageReceived']), bot_max_hp - bot_hp)
										# The frag belongs to whoever fired the killing blow. The old test
										# ('did the player deal at least half the damage') handed the kill
										# to the biggest contributor instead, which is not how WoT scores.
										killer_id = _led.killer_of(vid) if _led is not None else 0
										if not killer_id and is_killed:
											killer_id = getattr(_mock_vehicles.get(vid, None), 'last_killer_id', 255)
										vehicles_dict[vid] = {'health': bot_hp, 'credits': 100, 'xp': 100,
											'shots': _lt['shots'], 'hits': _lt['hits'], 'he_hits': _lt['he_hits'],
											'pierced': _lt['pierced'], 'damageDealt': _lt['damageDealt'],
											'damageAssisted': _lt['damageAssisted'], 'damageReceived': dmg_received,
											'shotsReceived': _lt['shotsReceived'], 'spotted': _lt['spotted'],
											'damaged': _lt['damaged'], 'kills': _lt['kills'],
											'tdamageDealt': _lt['tdamageDealt'], 'tkills': _lt['tkills'],
											'isTeamKiller': _lt['isTeamKiller'], 'capturePoints': _lt['capturePoints'],
											'droppedCapturePoints': _lt['droppedCapturePoints'],
											'mileage': _lt['mileage'], 'lifeTime': 300, 'killerID': killer_id,
											'achievements': [], 'repair': 0, 'freeXP': 5, 'details': {},
											'accountDBID': bot_dbid, 'team': bot_team, 'typeCompDescr': bot_cd,
											'gold': 0}
											
									# The player's own row, from the same ledger. personal_details is the
									# per-enemy breakdown the results screen turns into the efficiency
									# list, and it carries the nine fields the client expects verbatim.
									_pt = _led_totals(v_id)
									if _pt is not None:
										personal_details = _led.details_for(v_id)
										total_dmg_dealt = _pt['damageDealt']
										total_hits = _pt['hits']
										total_frags = _pt['kills']
										vehicles_dict[v_id].update({
											'shots': _pt['shots'], 'hits': _pt['hits'], 'he_hits': _pt['he_hits'],
											'pierced': _pt['pierced'], 'damageDealt': _pt['damageDealt'],
											'damageAssisted': _pt['damageAssisted'], 'spotted': _pt['spotted'],
											'damaged': _pt['damaged'], 'kills': _pt['kills'],
											'tdamageDealt': _pt['tdamageDealt'], 'tkills': _pt['tkills'],
											'isTeamKiller': _pt['isTeamKiller'],
											'capturePoints': _pt['capturePoints'],
											'droppedCapturePoints': _pt['droppedCapturePoints'],
											'mileage': _pt['mileage'],
											'shotsReceived': _pt['shotsReceived'],
											'damageReceived': max(int(_pt['damageReceived']), max(0, p_max_health - p_health)),
											'details': personal_details})
									else:
										vehicles_dict[v_id]['damageDealt'] = total_dmg_dealt
										vehicles_dict[v_id]['hits'] = total_hits
										vehicles_dict[v_id]['pierced'] = total_hits
										vehicles_dict[v_id]['shots'] = max(0, total_hits)
										vehicles_dict[v_id]['spotted'] = len(personal_details)
										vehicles_dict[v_id]['damaged'] = len(personal_details)
									vehicles_dict[v_id]['kills'] = total_frags
									
									# Frags come from the ledger now, which books them on the killing
									# blow. This loop re-derived them from killerID and would add a
									# SECOND count on top, so it only runs when the ledger is missing.
									if _led is None:
										for v_iter_id, v_iter_data in vehicles_dict.items():
											k_id = v_iter_data.get('killerID', 0)
											if k_id and k_id in vehicles_dict and k_id != v_iter_id:
												vehicles_dict[k_id]['kills'] = vehicles_dict[k_id].get('kills', 0) + 1
									
									mock_res = {
										'arenaUniqueID': mock_arena_id,
										'personal': {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': globals().get('G_OFFHANGAR_SHOTS_FIRED', max(0, total_hits)), 'hits': total_hits, 'he_hits': 0, 'pierced': total_hits, 'damageDealt': total_dmg_dealt, 'damageAssisted': 0, 'damageReceived': 0, 'shotsReceived': 0, 'spotted': len(personal_details), 'damaged': len(personal_details), 'kills': total_frags, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': personal_details, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0, 'xpPenalty': 0, 'creditsPenalty': 0, 'creditsContributionIn': 0, 'creditsContributionOut': 0, 'tmenXP': 0, 'eventCredits': 0, 'eventGold': 0, 'eventXP': 0, 'eventFreeXP': 0, 'eventTMenXP': 0, 'autoRepairCost': 0, 'autoLoadCost': (0, 0), 'autoEquipCost': (0, 0), 'isPremium': True, 'premiumXPFactor10': 15, 'premiumCreditsFactor10': 15, 'dailyXPFactor10': 10, 'aogasFactor10': 10, 'markOfMastery': 0, 'dossierPopUps': []},
										'common': {'arenaTypeID': getattr(player.arena, 'arenaTypeID', 1), 'arenaCreateTime': __import__('time').time(), 'winnerTeam': p_team if allied > enemy else (0 if allied==enemy else (3-p_team)), 'finishReason': 1, 'duration': 300, 'bonusType': 1, 'guiType': 1, 'vehLockMode': 0},
										'players': players_dict,
										'vehicles': vehicles_dict
									}
									# 'personal' repeats every field of the player's own vehicle row and
									# adds the account-only ones on top. The literal above still carries
									# the old placeholders (he_hits 0, mileage 100, shots read from a
									# counter that was never incremented), so copy the ledger-backed row
									# over them - only for keys 'personal' already has, which leaves the
									# account-only fields untouched.
									try:
										_pers = mock_res['personal']
										for _k, _v in vehicles_dict.get(v_id, {}).items():
											if _k in _pers:
												_pers[_k] = _v
									except Exception as _pfe:
										LOG_DEBUG('personal sync failed:', str(_pfe))
									# Medals. The panel on the personal tab is fed by dossierPopUps -
									# __populatePersonalMedals never looks at 'achievements' - while the
									# little icons on the team-table row come from 'achievements'. Same
									# medals, two shapes, so both get set.
									try:
										from gui.mods.offhangar import battle_medals as _BMED
										_alive_enemies = 0
										for _avid, _ainfo in getattr(player.arena, 'vehicles', {}).items():
											if _ainfo.get('team', 0) != p_team and _ainfo.get('isAlive', True):
												_alive_enemies += 1
										# Alone on the team: nobody else on your side is still breathing.
										_alive_mates = 0
										for _avid, _ainfo in getattr(player.arena, 'vehicles', {}).items():
											if _avid != v_id and _ainfo.get('team', 0) == p_team and _ainfo.get('isAlive', True):
												_alive_mates += 1
										_mctx = {'health': p_health, 'maxHealth': p_max_health,
											'survived': p_health > 0,
											'aloneOnTeam': _alive_mates == 0,
											'enemiesAlive': _alive_enemies}
										_pops, _achv = _BMED.evaluate(vehicles_dict.get(v_id, {}), _mctx)
										if _achv:
											vehicles_dict[v_id]['achievements'] = _achv
											mock_res['personal']['achievements'] = _achv
											mock_res['personal']['dossierPopUps'] = _pops
											LOG_DEBUG('Medals earned:', ', '.join(_BMED.names_for(_achv)))
										else:
											LOG_DEBUG('Medals: none earned this battle')
									except Exception as _mede:
										LOG_DEBUG('Medal evaluation failed:', str(_mede))
									# Economy. The per-vehicle multipliers, the repair bill and the shell
									# prices are real client data; the earning coefficients are not - they
									# ran on the server and were never published. See battle_economy.py.
									# These are BASE values: battleresults.py applies premium, the daily
									# double and aogas on top, so they must not be applied here as well.
									try:
										from gui.mods.offhangar import battle_economy as _BECO
										_etd = getattr(_player_mock, 'typeDescriptor', None)
										if _etd is None:
											_etd = getattr(player, 'vehicleTypeDescriptor', None)
										_eparams = _BECO.params_from_descriptor(_etd)
										# Tier of everything hit, and of everything killed, so damage
										# traded upwards pays more than farming the bottom of the list.
										_vt, _kt = [], []
										for _dvid, _ddet in personal_details.items():
											_dtd = getattr(_mock_vehicles.get(_dvid, None), 'typeDescriptor', None)
											_dtier = int(getattr(getattr(_dtd, 'type', None), 'level', 0) or 0)
											if not _dtier:
												continue
											if _ddet.get('damageDealt', 0) > 0:
												_vt.append(_dtier)
											if _ddet.get('killed', 0):
												_kt.append(_dtier)
										# Same verdict the 'common' block below writes into winnerTeam. There
										# is no `winner` variable in this scope - it is derived from the two
										# frag counts, so deriving it the same way keeps them from disagreeing.
										_ewin = p_team if allied > enemy else (0 if allied == enemy else (3 - p_team))
										_ectx = {'won': _ewin == p_team, 'draw': _ewin == 0,
											'survived': p_health > 0, 'health': p_health,
											'victimTiers': _vt, 'killTiers': _kt,
											'descriptor': _etd,
											# Rounds fired per shell slot, so a magazine of gold rounds is
											# billed in gold and an AP load in credits.
											'roundsUsed': _led.rounds_for(v_id) if _led is not None else {},
											# Every kit that was actually fired has to be bought again.
											'consumables': _gun_state.get('consumables', [])}
										_eco = _BECO.compute(vehicles_dict.get(v_id, {}), _eparams, _ectx)
										for _ek, _ev in _eco.items():
											if _ek in mock_res['personal']:
												mock_res['personal'][_ek] = _ev
										for _ek in ('credits', 'xp', 'freeXP'):
											if _ek in vehicles_dict[v_id]:
												vehicles_dict[v_id][_ek] = _eco[_ek]
										LOG_DEBUG(_BECO.summary(_eco))
									except Exception as _ecoe:
										LOG_DEBUG('Economy failed, results keep their placeholders:', str(_ecoe))
									
									if hasattr(battle_results_shared, 'VEH_FULL_RESULTS'):
										for k in battle_results_shared.VEH_FULL_RESULTS:
											if k not in mock_res['personal']: mock_res['personal'][k] = [] if 'list' in k or k == 'achievements' else (0 if k != 'details' else {})
									if hasattr(battle_results_shared, 'VEH_BASE_RESULTS'):
										for k in battle_results_shared.VEH_BASE_RESULTS:
											for v in mock_res['vehicles']:
												if k not in mock_res['vehicles'][v]: mock_res['vehicles'][v][k] = [] if 'list' in k or k == 'achievements' else (0 if k != 'details' else {})
									
									def _mock_get(arenaUniqueID, callback):
										import BigWorld
										BigWorld.callback(0.1, lambda: callback(1, mock_res))
									
									player_brc = getattr(player, 'battleResultsCache', None)
									if player_brc:
										orig_br_get = player_brc.get
										player_brc.get = _mock_get
									
									from gui import WindowsManager
									window = getattr(WindowsManager.g_windowsManager, 'window', None)
									if hasattr(window, 'onBattleResultsReceived'): window.onBattleResultsReceived(True, mock_arena_id)
									elif hasattr(window, 'battleResults') and hasattr(window.battleResults, 'show'): window.battleResults.show(mock_arena_id)
									elif hasattr(window, 'battleResults') and hasattr(window.battleResults, '_BattleResultsManager__showBattleResults'): window.battleResults._BattleResultsManager__showBattleResults(mock_arena_id)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
							
							BigWorld.callback(4.0, _show_res)
							player.leaveArena()
					except Exception: pass

				if event.isKeyDown() and event.key in (Keys.KEY_O, Keys.KEY_P, Keys.KEY_L):
					try:
						player = BigWorld.player()
						start_pos, dir_vec = player.gunRotator._VehicleGunRotator__getCurShotPosition()
						dir_vec.normalise()
						hit = BigWorld.wg_collideSegment(_offh_bspace(), start_pos, start_pos + dir_vec.scale(500.0), 128)
						target_pos = hit[0] if hit else start_pos + dir_vec.scale(50.0)
						
						# Auto-spawner forces a spawn location (original map spawn points)
						_forced_pos = getattr(player, '_forced_spawn_pos', None)
						if _forced_pos is not None:
							target_pos = Math.Vector3(_forced_pos[0], _forced_pos[1], _forced_pos[2])
						
						# Find a safe free ground spot near the aim point
						# (not inside player/tanks/walls, not on roofs, never floating in the air)
						target_pos = _find_safe_spawn(target_pos)
						
						td = None
						bot_name = 'Bot ' + str(_spawn_count[0])
						bot_team = 1 if event.key == Keys.KEY_L else 2
						# Auto-spawner overrides team/facing. Read them synchronously here:
						# the model-load callback below runs async and must not race the next spawn.
						_forced_team = getattr(player, '_forced_spawn_team', None)
						if _forced_team in (1, 2): bot_team = _forced_team
						_forced_yaw_local = getattr(player, '_forced_spawn_yaw', None)
						if bot_team == 1: bot_name = 'Ally ' + str(_spawn_count[0])
						if event.key == Keys.KEY_O:
							td = loaded_models.get('td')
							bot_name = 'Clone ' + str(_spawn_count[0])
						elif event.key in (Keys.KEY_P, Keys.KEY_L):
							try:
								import random
								from items import vehicles
								import nations
								cur_tier = loaded_models['td'].type.level
								candidates = []
								for nation in nations.AVAILABLE_NAMES:
									nationID = nations.INDICES[nation]
									for v in vehicles.g_list.getList(nationID).itervalues():
										if abs(v['level'] - cur_tier) <= 2 and not _offh_veh_excluded(v):
											candidates.append(v['name'])
								LOG_DEBUG('KEY P pressed! cur_tier=%d candidates=%d' % (cur_tier, len(candidates)))
								if candidates:
									chosen = random.choice(candidates)
									# Auto-spawner pre-picks vehicles (sorted by class for the line-up)
									_fv = getattr(player, '_forced_spawn_vehname', None)
									if _fv: chosen = _fv
									td = vehicles.VehicleDescr(typeName=chosen)
									bot_name = ('Ally ' if bot_team == 1 else 'Enemy ') + chosen.split(':')[-1] + ' ' + str(_spawn_count[0])
							except Exception as e:
								import traceback
								LOG_DEBUG('Random spawn error:', str(e), traceback.format_exc())
								td = loaded_models.get('td')
						
						if not td: return True

						# OOM guard: each bot is a full tank (models + textures +
						# per-component VehicleStickers + recoil). Spawning far past a
						# normal battle exhausts the 32-bit client and it crashes
						# NATIVELY mid-spawn (no Python traceback). Cap the live count
						# (player + bots); raise max_total_bots in config.json to allow more.
						try:
							from _constants import CONFIG_OPTIONS as _CFG_CAP
							_bot_cap = int(_CFG_CAP.get('max_total_bots', 50))
						except Exception:
							_bot_cap = 50
						if _bot_cap > 0 and len(globals().get('G_MOCK_VEHICLES', {}) or {}) >= _bot_cap:
							LOG_DEBUG('Bot spawn capped at %d live (raise max_total_bots in config.json)' % _bot_cap)
							return True

						try:
							for hitTester in td.getHitTesters():
								hitTester.loadBspModel()
						except Exception as e:
							LOG_DEBUG("Error loading hitTesters for bot:", str(e))
						
						e_id = 1000 + _spawn_count[0]
						_spawn_count[0] += 1
						
						# Load visual models
						
						def _on_bot_models_loaded(resourceRefs):
							try:
								ch = resourceRefs[td.chassis['models']['undamaged']]
								hu = resourceRefs[td.hull['models']['undamaged']]
								tu = resourceRefs[td.turret['models']['undamaged']]
								gu = resourceRefs[td.gun['models']['undamaged']]
							except Exception as e:
								# NOT debug_utils.LOG_DEBUG - that one writes nothing in the release
								# client, so a bot whose models failed to load vanished without a trace.
								LOG_DEBUG('Bot model unpack error (bot will not spawn):', str(e))
								return
							e_mock = _MockVeh()
							e_mock.id = e_id
							e_mock.position = target_pos
							# Face the player
							import math
							e_mock.yaw = math.atan2(start_pos.x - target_pos.x, start_pos.z - target_pos.z)
							if _forced_yaw_local is not None:
								e_mock.yaw = _forced_yaw_local
							e_mock.health = getattr(td, 'maxHealth', 1000)
							e_mock.maxHealth = e_mock.health
							_offh_set_alive(e_mock, True)
							e_mock.isStarted = True
							e_mock._bot_team = bot_team
							LOG_DEBUG('SPAWN BOT: bot_team=%s bot_name=%s player_team=%s' % (bot_team, bot_name, getattr(player, '_offhangar_team', -99)))
							e_mock.publicInfo = {
								'vehicleType': td,
								'name': bot_name,
								'team': bot_team,
								'isAlive': True,
								'isAvatarReady': True,
								'isTeamKiller': False,
								'accountDBID': 0,
								'clanAbbrev': '',
								'clanDBID': 0,
								'prebattleID': 0,
								'isPrebattleCreator': False,
							'events': {}
							}
							ch.position = e_mock.position
							ch.yaw = e_mock.yaw
							
							_eid = BigWorld.createEntity('OfflineEntity', _offh_bspace(), 0, e_mock.position, (0, 0, e_mock.yaw), dict())
							e_mock.bw_entity = None
							def _assign_model_when_ready(eid, model_to_add, retries=10, _e_mock=e_mock):
								if not getattr(_e_mock, 'isAlive', True) or getattr(_e_mock, '_wreck_done', False):
									return  # bot died meanwhile: never re-add the intact model over the wreck
								ent = BigWorld.entity(eid)
								if ent:
									ent.model = model_to_add  # Outline needs it!
									try:
										ent.filter = BigWorld.AvatarFilter()
									except: pass
									_e_mock.bw_entity = ent
								elif retries > 0:
									BigWorld.callback(0.1, lambda: _assign_model_when_ready(eid, model_to_add, retries - 1, _e_mock))
								else:
									_add_model(model_to_add)
							# world-add moved BELOW mock registration: a failure in between
							# must not leave a ghost model in the world
							h_mat = Math.Matrix(); h_mat.setIdentity()
							t_mat = Math.Matrix(); t_mat.setIdentity()
							g_mat = Math.Matrix(); g_mat.setIdentity()
							ch.node('V').attach(hu)
							e_mock._t_node = hu.node('HP_turretJoint', t_mat)
							e_mock._t_node.attach(tu)
							e_mock._g_node = tu.node('HP_gunJoint', g_mat)
							e_mock._g_node.attach(gu)
							e_mock._gun_recoil = _setup_gun_recoil(gu, td)
							e_mock._swinging = _setup_swinging(ch, td)
							e_mock.model = ch
							e_mock.typeDescriptor = td
							e_mock._chassis_model = ch
							e_mock._hull_model = hu
							e_mock._turret_model = tu
							e_mock._gun_model = gu
							e_mock._t_mat = t_mat
							# was a local only, so the barrel could never be elevated
							e_mock._g_mat = g_mat
							# Per-component VehicleStickers so shell-hole decals can land
							# on this bot (bots have no stickers otherwise). Empty emblem
							# slots - we only want the damage-sticker model.
							try:
								import VehicleStickers
								e_mock._sticker_map = {}
								_bot_sticker_setup = (
									('hull', hu, ch.node('V')),
									('turret', tu, e_mock._t_node),
									('gun', gu, e_mock._g_node),
								)
								for _cn, _cm, _cnode in _bot_sticker_setup:
									if _cm is not None and _cnode is not None:
										_st = VehicleStickers.VehicleStickers(td, [], _cn == 'hull', None)
										_st.attachStickers(_cm, _cnode, False)
										e_mock._sticker_map[_cn] = (_st, _cm, _cnode)
							except Exception as _se:
								LOG_DEBUG('Bot sticker setup error:', str(_se))
							# Scrolling-track animation for the bot (original fashion system);
							# attached slightly delayed so the model is in the world first
							def _attach_bot_fashion(_bch=ch, _btd=td, _bm=e_mock):
								# The ghost-fix delays the world-add (entity retries); a fashion
								# attached to a not-yet-inWorld model stays inert -> static tracks.
								# Wait for inWorld like the player path does.
								if not getattr(_bch, 'inWorld', False):
									_bm._fash_tries = (getattr(_bm, '_fash_tries', 0) or 0) + 1
									if _bm._fash_tries < 20 and getattr(_bm, 'isAlive', True):
										BigWorld.callback(0.5, lambda: _attach_bot_fashion(_bch, _btd, _bm))
									return
								try:
									_bf = BigWorld.WGVehicleFashion()
									try:
										_bf.maxMovement = _btd.physics['speedLimits'][0]
									except Exception:
										pass
									# Swinging node 'V' is mandatory for attaching the fashion
									try:
										_b_sw = _btd.hull['swinging']
										_b_pp = tuple(_p * _m for (_p, _m) in zip(_b_sw['pitchParams'], (0.9, 1.88, 0.3, 4.0, 1.0, 1.0)))
										_bf.setPitchSwinging('V', *_b_pp)
										_bf.setRollSwinging('V', *_b_sw['rollParams'])
										_bf.setShotSwinging('V', _b_sw['sensitivityToImpulse'])
									except Exception:
										pass
									_bt = _btd.chassis['tracks']
									try:
										# Bot detail, see _bot_lod - config keys bot_lod_scale,
										# bot_ground_traces, bot_hull_swinging.
										_bf.setLods(*_bot_lod(_btd))
									except Exception:
										pass
									_bf.setTracks(_bt['leftMaterial'], _bt['rightMaterial'], _bt['textureScale'])
									# Road wheels + scroll source, same as the player fashion
									try:
										_bwcfg = _btd.chassis['wheels']
										for _bg in _bwcfg['groups']:
											_bn = ['%s%d' % (_bg[1], _bi) for _bi in range(_bg[3], _bg[3] + _bg[2])]
											_bf.addWheelGroup(_bg[0], _bg[4], _bn)
										for _bwh in _bwcfg['wheels']:
											_bf.addWheel(_bwh[0], _bwh[2], _bwh[1])
									except Exception:
										pass
									try:
										_bf.movementInfo = Math.Vector4(0.0, 0.0, 0.0, 0.0)
									except Exception:
										pass
									_bch.wg_fashion = _bf
									_bm._fashion = _bf
									# see the note on the player fashion: wiring this to _trigger_shot_impulse
									# crashes the hangar load on a filter-less mock fashion.
									# Real half track gauge for the turn scroll split (see player feed)
									try:
										_btco = abs(float(_btd.physics.get('trackCenterOffset', 1.5)))
										_bm._tco = _btco if 0.3 <= _btco <= 3.0 else 1.5
									except Exception:
										_bm._tco = 1.5
								except Exception as _bfe:
									LOG_DEBUG('Bot track fashion failed:', str(_bfe))
							BigWorld.callback(1.5, _attach_bot_fashion)
							try:
								e_mock._collision_obstacle = BigWorld.PyModelObstacle(
									td.hull['models']['undamaged'],
									td.turret['models']['undamaged'],
									e_mock.matrix,
									True
								)
							except Exception as e:
								LOG_DEBUG('OfflineBattle PyModelObstacle Error:', e)
							class FakeEnemyAppearance(object):
								def __init__(self, tmat=None):
									from Event import Event
									self.onModelChanged = Event()
									# WG's TankIndicator._setup reads appearance.turretMatrix for EVERY
									# vehicle it follows and feeds it to wg_turretMatProv. Without it the
									# setup raises inside __update -> _waiting -> _setup the moment the GUI
									# follows a bot (postmortem), taking the rest of that update pass with
									# it. Reference the turret matrix the bot mutates in place each tick -
									# never a per-frame copy - so the needle tracks the real turret.
									if tmat is None:
										tmat = Math.Matrix()
										tmat.setIdentity()
									self.turretMatrix = tmat
								def changeVisibility(self, *a, **kw): pass
								def showDamageFromShot(self, *a, **kw): pass
								def showDamageFromExplosion(self, *a, **kw): pass
							e_mock.appearance = FakeEnemyAppearance(getattr(e_mock, '_t_mat', None))
							mock_vehicles[e_id] = e_mock
							try:
								_assign_model_when_ready(_eid, ch)
							except Exception:
								_add_model(ch)
							# Safety net for the invisible bot. _assign_model_when_ready can leave the
							# chassis out of the world entirely - its liveness guard returns without
							# scheduling a retry, and the entity path can silently not take. Everything
							# else about the bot works in that case (it drives, aims and shoots), which
							# is exactly what an invisible tank looks like. 2 s is well past the 1 s
							# retry budget, so a normal spawn never reaches this.
							# Safety net for the invisible bot: ONE shot, 2 s after spawn, for the case
							# where _assign_model_when_ready left the chassis out of the world entirely.
							#
							# It stays a one-shot on purpose. A periodic version (1.7.3) looked obvious and
							# was wrong: models added through _add_model report inWorld False in this client
							# even while they render perfectly, so the watchdog fired on 16 healthy bots in a
							# single battle and called _add_model on models that were already claimed - the
							# double-owner crash, dressed up as a fix. inWorld is only trustworthy as a
							# NEGATIVE signal at spawn time, before anything has been added.
							def _verify_bot_visible(_vch=ch, _vm=e_mock, _vid=e_id):
								try:
									if not getattr(_vm, 'isAlive', False) or getattr(_vm, '_wreck_done', False):
										return
									if getattr(_vch, 'inWorld', False):
										return
									LOG_DEBUG('BOT INVISIBLE: id=%s never reached the world - re-adding' % _vid)
									_add_model(_vch)
								except Exception as _vbe:
									LOG_DEBUG('bot visibility check err:', str(_vbe))
							try:
								BigWorld.callback(2.0, _verify_bot_visible)
							except Exception:
								pass
							import weakref
							e_mock.proxy = weakref.proxy(e_mock)
							
							from gui import WindowsManager
							player.arena.vehicles[e_id] = e_mock.publicInfo
							try:
								player.arena.onVehicleAdded(e_id)
							except: pass
							try:
								bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, '_Battle__updatePlayers'):
									bw._Battle__updatePlayers()
							except: pass
							
							try:
								if hasattr(WindowsManager.g_windowsManager.battleWindow, 'vMarkersManager'):
									e_mock.marker = WindowsManager.g_windowsManager.battleWindow.vMarkersManager.createMarker(e_mock.proxy)
								
								minimap = WindowsManager.g_windowsManager.battleWindow.minimap
								if minimap:
									minimap.notifyVehicleStart(e_mock.id)
							except Exception as e:
								LOG_DEBUG('GUI Add error:', str(e))
							LOG_DEBUG('Enemy Clone Spawned at:', target_pos)
						
						BigWorld.loadResourceListBG((
							td.chassis['models']['undamaged'],
							td.hull['models']['undamaged'],
							td.turret['models']['undamaged'],
							td.gun['models']['undamaged'],
						), _on_bot_models_loaded)
						return True
					except Exception as e:
						import traceback
						LOG_DEBUG('Clone spawn error:', traceback.format_exc())
				return _orig_handleKeyEvent(event)
			g_offline_aih.handleKeyEvent = _mock_handleKeyEvent
			
			# --- 15v15 AUTO-SPAWN like the original game ---
			# In 0.8.2 ctf the teams spawn AT the teamBasePositions (the arena_defs
			# only carry teamSpawnPoints for the domination mode). The line-up is a
			# 5x3 grid behind the flag, 9 m spacing, facing the enemy base, with the
			# heavies up front and artillery at the back. bots_per_team in config.json.
			def _formation_slot(t_id, slot):
				# Returns (x, z, yaw) for a line-up slot of the given team.
				# Slot 0 = front row centre (the player's own slot in his team).
				import math
				_sp = globals().get('g_offline_spawns', {}) or {}
				_bs = globals().get('g_offline_bases', {}) or {}
				pts = list(_sp.get(t_id, []) or [])
				# True when these are the arena's REAL spawn points (not the base-flag fallback):
				# the original game puts each vehicle ON its own spawn point, so the grid offset
				# below must not be applied while a distinct real point is still free.
				_real_pts = bool(pts)
				if not pts:
					pts = [(_b.x, _b.z) for _b in (_bs.get(t_id, []) or [])]
				if not pts:
					return (0.0, 0.0, 0.0)
				ax, az = pts[slot % len(pts)]
				_fb = globals().get('g_offline_bounds', None)
				# Face the enemy base (fallback: map centre)
				try:
					eb = _bs.get(2 if t_id == 1 else 1, [])
					yaw = math.atan2(eb[0].x - ax, eb[0].z - az) if eb else math.atan2(-ax, -az)
				except Exception:
					yaw = 0.0
				k = slot // len(pts)
				# Real spawn point still unshared on this pass -> stand exactly on it, like retail.
				if _real_pts and k == 0:
					return (ax, az, yaw)
				# Wide + shallow, like a retail spawn line. The old 5-wide/9 m grid packed 16
				# vehicles into a ~36 m x 27 m block right behind the flag - they visibly
				# clumped and clipped. 9 columns at 14 m spans ~112 m and needs only 2 rows.
				cols = (0, -1, 1, -2, 2, -3, 3, -4, 4)   # centre first, then fan out
				col = cols[k % len(cols)]
				row = k // len(cols)
				# Step rows TOWARD the enemy, i.e. into the map. Base flags commonly sit ON the
				# arena edge (Himmelsdorf team1: z=-302.6 while boundingBox stops at -300), so
				# the old 'behind the flag' offset pushed the whole line-up off the map - which
				# is why hulls ended up on roofs, inside edge buildings and stacked on each other.
				# Vehicles DO start inside their own base circle, as in retail - the line-up
				# only needs to stand off the flag itself and, crucially, in FRONT of it:
				# base flags sit at the arena edge (Himmelsdorf team1 z=-302.6 vs a -300
				# boundary), so there is no ground behind them to line up on.
				fwd = 20.0 + row * 12.0
				sx = ax + math.sin(yaw) * fwd + math.cos(yaw) * col * 14.0
				sz = az + math.cos(yaw) * fwd - math.sin(yaw) * col * 14.0
				# Safety net only (the anchor is already inside): a tight margin here so a
				# wide lateral slot cannot leave the arena, without flattening the rows.
				if _fb is not None:
					if sx < _fb[0] + 8.0: sx = _fb[0] + 8.0
					elif sx > _fb[2] - 8.0: sx = _fb[2] - 8.0
					if sz < _fb[1] + 8.0: sz = _fb[1] + 8.0
					elif sz > _fb[3] - 8.0: sz = _fb[3] - 8.0
				return (sx, sz, yaw)
			# Shared via globals: _aih_tick uses it for the player's spawn correction
			globals()['g_offline_formation_slot'] = _formation_slot

			def _auto_spawn_teams():
				import BigWorld, Keys, Math, math
				try:
					_pl = BigWorld.player()
					if _pl is None or _battle_finished[0]:
						return
					from _constants import CONFIG_OPTIONS as _CFG
					_n_per_team = int(_CFG.get('bots_per_team', 15))
					if _n_per_team <= 0:
						return
					_spawns = dict(globals().get('g_offline_spawns', {}) or {})
					_bases = globals().get('g_offline_bases', {}) or {}
					_p_team = getattr(_pl, '_offhangar_team', 1) or 1
					
					class _FakeSpawnEvent(object):
						def __init__(self, key):
							self.key = key
						def isKeyDown(self):
							return True
						def isRepeatedEvent(self):
							return False
						def isShiftDown(self):
							return False
						def isCtrlDown(self):
							return False
						def isAltDown(self):
							return False
					
					def _anchors(t_id):
						pts = list(_spawns.get(t_id, []) or [])
						if not pts:
							# Fall back to the team base flag if the map has no spawn points
							for _b in (_bases.get(t_id, []) or []):
								pts.append((_b.x, _b.z))
						return pts
					
					def _face_yaw(t_id, x, z):
						# Line the team up facing the enemy base (like the real line-up)
						try:
							_eb = _bases.get(2 if t_id == 1 else 1, [])
							if _eb:
								return math.atan2(_eb[0].x - x, _eb[0].z - z)
						except Exception:
							pass
						return math.atan2(-x, -z)
					
					_jobs = []
					for _t in (1, 2):
						_pts = _anchors(_t)
						if not _pts:
							LOG_DEBUG('AUTO-SPAWN: no spawn points for team', _t)
							continue
						# The player already occupies slot 0 (front row centre) of his team
						_count = _n_per_team - 1 if _t == _p_team else _n_per_team
						_slot0 = 1 if _t == _p_team else 0
						# Pick the bots' vehicles up front and sort heavy -> arty so the
						# front rows hold the heavies and artillery sits at the back
						_veh_names = []
						try:
							import random as _rnd
							from items import vehicles as _veh_items
							import nations as _nations
							_tier = loaded_models['td'].type.level
							_cand = []
							for _nat in _nations.AVAILABLE_NAMES:
								_nid = _nations.INDICES[_nat]
								for _v in _veh_items.g_list.getList(_nid).itervalues():
									if abs(_v['level'] - _tier) <= 2 and not _offh_veh_excluded(_v):
										_cand.append(_v)
							def _class_key(_v):
								try:
									_tg = _v['tags']
								except Exception:
									return 1
								if 'heavyTank' in _tg: return 0
								if 'mediumTank' in _tg: return 1
								if 'AT-SPG' in _tg: return 2
								if 'lightTank' in _tg: return 3
								if 'SPG' in _tg: return 4
								return 1
							if _cand:
								# Limit to a small STABLE per-tier pool reused across
								# battles so bot tank TEXTURES cache once instead of
								# loading ~30 fresh random tanks each battle (the leak
								# that climbed the baseline until map-load OOM).
								_pool = _offh_bot_pool(_cand, _tier)
								_picked = [_pool[_x % len(_pool)] for _x in range(_count)]
								_rnd.shuffle(_picked)
								_picked.sort(key=_class_key)
								_veh_names = [_p['name'] for _p in _picked]
						except Exception:
							import traceback
							LOG_DEBUG('AUTO-SPAWN vehicle pick failed:', traceback.format_exc())
						for _i in range(_count):
							_sx, _sz, _yw = _formation_slot(_t, _slot0 + _i)
							_vn = _veh_names[_i] if _i < len(_veh_names) else None
							_jobs.append((_t, _sx, _sz, _yw, _vn))
					
					# Interleave the teams so both sides build up evenly
					_t1 = [_j for _j in _jobs if _j[0] == 1]
					_t2 = [_j for _j in _jobs if _j[0] == 2]
					_jobs = []
					for _k in range(max(len(_t1), len(_t2))):
						if _k < len(_t1): _jobs.append(_t1[_k])
						if _k < len(_t2): _jobs.append(_t2[_k])
					
					def _spawn_next(_rest):
						if _battle_finished[0] or not _rest:
							return
						_t, _x, _z, _yw, _vn = _rest[0]
						try:
							_p2 = BigWorld.player()
							# Bot ground height. Probe the BATTLE space (_offh_bspace); if it misses
							# (collision not streamed in yet / odd city-map footprint) fall back to the
							# PLAYER's own ground height - never y=300, which rained bots from the sky.
							# Keep this slot clear of already-placed hulls: the grid spaces slots, but a
							# roof-corrected drop can still land on top of a neighbour.
							_taken = getattr(_p2, '_offh_spawn_taken', None)
							if _taken is None:
								_taken = []
								_p2._offh_spawn_taken = _taken
							for _nudge in range(4):
								_clash = False
								for _tx, _tz in _taken:
									if (_x - _tx) ** 2 + (_z - _tz) ** 2 < 81.0:   # < 9 m apart
										_clash = True
										break
								if not _clash: break
								import math as _mnu
								# Nudge FORWARD (toward the enemy, i.e. into the map). Pushing backwards ran
								# straight at the arena edge behind the base and its buildings.
								_x += _mnu.sin(_yw) * 11.0
								_z += _mnu.cos(_yw) * 11.0
							# Roof-safe ground probe: while something substantially lower sits below the
							# hit (roof / balcony / bridge), keep going down. Same walk the player uses.
							_gy = None
							try:
								_from_y = 1000.0
								for _ri in range(4):
									_gc = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_x, _from_y, _z), Math.Vector3(_x, -1000.0, _z), 128)
									if _gc is None: break
									_gy = _gc[0].y
									_gc2 = BigWorld.wg_collideSegment(_offh_bspace(), Math.Vector3(_x, _gy - 0.4, _z), Math.Vector3(_x, -1000.0, _z), 128)
									if _gc2 is None or (_gy - _gc2[0].y) < 2.5: break
									_from_y = _gy - 0.4
							except Exception:
								pass
							if _gy is None:
								try:
									_gy = float(veh_pos[1])
								except Exception:
									_gy = 100.0
							_taken.append((_x, _z))
							_p2._forced_spawn_pos = (_x, _gy, _z)
							_p2._forced_spawn_team = _t
							_p2._forced_spawn_yaw = _yw
							_p2._forced_spawn_vehname = _vn
							try:
								_mock_handleKeyEvent(_FakeSpawnEvent(Keys.KEY_P))
							finally:
								_p2._forced_spawn_pos = None
								_p2._forced_spawn_team = None
								_p2._forced_spawn_yaw = None
								_p2._forced_spawn_vehname = None
						except Exception as _se:
							LOG_DEBUG('AUTO-SPAWN error:', str(_se))
						BigWorld.callback(0.3, lambda: _spawn_next(_rest[1:]))
					
					LOG_DEBUG('AUTO-SPAWN: placing %d bots (%d per team incl. player)' % (len(_jobs), _n_per_team))
					# fresh occupancy list per battle - a stale one would nudge every new spawn
					try: BigWorld.player()._offh_spawn_taken = []
					except Exception: pass
					_spawn_next(_jobs)
				except Exception:
					import traceback
					LOG_DEBUG('AUTO-SPAWN failed:', traceback.format_exc())
			
			def _preload_bot_pool():
				# EXPERIMENTAL feature (config 'preload_bots'; may be unstable / change).
				# FPS: warm the bot pool's MODEL + BSP caches during the LOADING
				# screen so the later staggered auto-spawn is a cache hit instead
				# of decoding ~8 tank model-sets + BSP collision on the main thread
				# while the player is already driving (the FPS drop at bot spawns).
				# The pool is deterministic (g_offh_bot_pool[tier]) so we know
				# exactly which vehicles the bots will use. Safe: worst case a
				# redundant background load. Gated by config 'preload_bots'.
				try:
					from items import vehicles as _pv
					import nations as _pn
					_ptier = loaded_models['td'].type.level
					_pcand = []
					for _pnat in _pn.AVAILABLE_NAMES:
						_pnid = _pn.INDICES[_pnat]
						for _pvh in _pv.g_list.getList(_pnid).itervalues():
							if abs(_pvh['level'] - _ptier) <= 2 and not _offh_veh_excluded(_pvh):
								_pcand.append(_pvh)
					_ppool = _offh_bot_pool(_pcand, _ptier)
					if not _ppool:
						return
					_pnoop = lambda *a, **k: None
					_pwarm = 0
					for _pentry in _ppool:
						try:
							_ptd = _pv.VehicleDescr(typeName=_pentry['name'])
						except Exception:
							continue
						try:
							for _pht in _ptd.getHitTesters():
								_pht.loadBspModel()
						except Exception:
							pass
						try:
							BigWorld.loadResourceListBG((
								_ptd.chassis['models']['undamaged'],
								_ptd.hull['models']['undamaged'],
								_ptd.turret['models']['undamaged'],
								_ptd.gun['models']['undamaged'],
							), _pnoop)
							_pwarm += 1
						except Exception:
							pass
					LOG_DEBUG('AUTO-SPAWN preload: warmed %d/%d bot vehicles (tier %s)' % (_pwarm, len(_ppool), _ptier))
				except Exception:
					import traceback
					LOG_DEBUG('AUTO-SPAWN preload failed:', traceback.format_exc())
			try:
				from _constants import CONFIG_OPTIONS as _CFG_PLB
				if bool(_CFG_PLB.get('preload_bots', False)) and int(_CFG_PLB.get('bots_per_team', 15)) > 0:
					_preload_bot_pool()
			except Exception:
				pass
			try:
				from _constants import CONFIG_OPTIONS as _CFG_AS
				if int(_CFG_AS.get('bots_per_team', 15)) > 0:
					# Give the terrain chunks time to stream in before lining up the teams
					BigWorld.callback(float(_CFG_AS.get('auto_spawn_delay_seconds', 10.0)), _auto_spawn_teams)
			except Exception:
				import traceback
				LOG_DEBUG('AUTO-SPAWN schedule failed:', traceback.format_exc())
			# -------------------------------------------

			player.shoot = _mock_shoot
			
			# --- Central kill handling: keep players-panel/minimap icons in sync ---
			# Every kill path fires arena.onVehicleKilled(...). The bare event does not
			# flip arena.vehicles[id]['isAlive'], so panel icons stayed 'alive'. Wrap it
			# once so ALL kill paths (shots, fire, ramming) update the UI consistently.
			try:
				if hasattr(player, 'arena') and player.arena is not None and not getattr(player.arena, '_offh_kill_wrapped', False):
					class _KillEventWrapper(object):
						def __init__(self, orig):
							self._orig = orig
						def __iadd__(self, handler):
							try:
								self._orig += handler
							except Exception:
								pass
							return self
						def __isub__(self, handler):
							try:
								self._orig -= handler
							except Exception:
								pass
							return self
						def __getattr__(self, name):
							return getattr(self._orig, name)
						def __call__(self, victimID, killerID=-1, reason=0):
							import BigWorld
							_pl = BigWorld.player()
							# EVERY death path in this mod - shell, fire, drowning, ramming, a
							# fall - is routed through here, so this is the one place the frag
							# can be attributed from. The ledger keeps the FIRST report per
							# victim; later cleanup passes re-fire the same event.
							try:
								from gui.mods.offhangar import battle_ledger as _BLED
								_BLED.get().note_kill(victimID, killerID, reason)
							except Exception:
								pass
							_mv = globals().get('G_MOCK_VEHICLES', {}).get(victimID)
							try:
								if _pl is not None and victimID in getattr(_pl.arena, 'vehicles', {}):
									_pl.arena.vehicles[victimID]['isAlive'] = False
							except Exception:
								pass
							try:
								if _mv is not None:
									_offh_set_alive(_mv, False)
									if (getattr(_mv, 'health', None) or 0) > 0:
										# Drowning is not damage, so remember what it had before zeroing the
										# internal value that everything else treats as 'dead'.
										if getattr(_mv, '_drowned', False) and getattr(_mv, '_hp_display', None) is None:
											_mv._hp_display = _mv.health
										_mv.health = 0
									# Original behaviour (Vehicle.__onVehicleDeath): the marker is NOT
									# destroyed on death - it switches to the grey 'dead' state. Wrecks
									# are visible to everyone, so create the marker first if the victim
									# died unspotted. Central here for EVERY kill path.
									try:
										from gui import WindowsManager as _zwm
										_zbw = getattr(_zwm.g_windowsManager, 'battleWindow', None)
										_zvm = getattr(_zbw, 'vMarkersManager', None) if _zbw is not None else None
										if _zvm is not None:
											_zfresh = getattr(_mv, 'marker', None) in (None, -1)
											if _zfresh:
												try:
													_mv.marker = _zvm.createMarker(_mv.proxy)
												except Exception:
													_mv.marker = None
											if getattr(_mv, 'marker', None) not in (None, -1):
												try:
													# NOT a hard 0: a drowned hull still shows the HP it went under with.
													_zvm.onVehicleHealthChanged(_mv.marker, max(0, _offh_hp_display(_mv)), killerID, 0)
												except Exception:
													pass
												_zvm.updateMarkerState(_mv.marker, 'dead', not _zfresh)
									except Exception:
										pass
							except Exception:
								pass
							try:
								if self._orig is not None:
									self._orig(victimID, killerID, reason)
							except Exception:
								pass
							# Kill feed, ONCE per victim. Four separate sites used to post this, one of
							# them twice in a row, all with the key 'PlayerKilled' - which the panel does
							# not define, so Flash fell back to a default icon (the ammo rack) and printed
							# the key as the text. The real keys and their %(...)s arguments come from
							# gui/player_messages_panel.xml and ingame_gui player_messages/*.
							try:
								_seen_k = globals().setdefault('_offh_kill_msgs', set())
								if victimID not in _seen_k:
									_seen_k.add(victimID)
									from gui import WindowsManager as _kwm
									_kbw = getattr(_kwm.g_windowsManager, 'battleWindow', None)
									_kp = getattr(_kbw, '_Battle__pMsgsPanel', None) if _kbw is not None else None
									_pvid_early = getattr(_pl, 'playerVehicleID', -1)
									# Retail posts NOTHING to this panel when the player himself is the
									# victim (Avatar.__onArenaVehicleKilled returns early) - the post-mortem
									# already tells you. Match it.
									if _kp is not None and victimID != _pvid_early:
										_av = getattr(_pl.arena, 'vehicles', {}) or {}
										_vinfo = _av.get(victimID) or {}
										_kinfo = _av.get(killerID) or {}
										_vname = _vinfo.get('name', 'Unknown')
										_kname = _kinfo.get('name', 'Unknown')
										_pteam_k = getattr(_pl, '_offhangar_team', 1)
										_vteam = _vinfo.get('team', _pteam_k)
										_kteam = _kinfo.get('team', None)
										_pvid_k = getattr(_pl, 'playerVehicleID', -1)
										if killerID is None or killerID < 0 or killerID == victimID or not _kinfo:
											# Drowned, burned out, fell, rammed a rock: nobody gets the frag.
											_key = 'ally_suicide' if _vteam == _pteam_k else 'enemy_suicide'
											_args = {'entity': _vname}
										else:
											_ff = (_kteam == _vteam)
											if killerID == _pvid_k:
												_key = 'player_friendly_fire_frag' if _ff else 'player_frag'
												_args = {'target': _vname}
											elif _kteam == _pteam_k:
												_key = 'ally_friendly_fire_frag' if _ff else 'ally_frag'
												_args = {'attacker': _kname, 'target': _vname}
											else:
												_key = 'enemy_friendly_fire_frag' if _ff else 'enemy_frag'
												_args = {'attacker': _kname, 'target': _vname}
										LOG_DEBUG('KILL FEED: key=%s args=%s victim=%s killer=%s' % (_key, _args, victimID, killerID))
										_kp.showMessage(_key, _args)
							except Exception as _kfe:
								LOG_DEBUG('kill feed error:', str(_kfe))
							# Grey out the players-panel icon + drop the minimap marker
							try:
								from gui import WindowsManager
								_bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if _bw is not None:
									try:
										if hasattr(_bw, '_Battle__updatePlayers'):
											_bw._Battle__updatePlayers()
									except Exception:
										pass
									try:
										if getattr(_bw, 'minimap', None):
											_bw.minimap.notifyVehicleStop(victimID)
									except Exception:
										pass
									try:
										pass  # marker health + 'dead' state already handled centrally above
									except Exception:
										pass
							except Exception:
								pass
							# Fire deaths (reason 2) have no wreck-swap path of their own:
							# swap burnt-out bots to their destroyed models here
							try:
								# A drowned tank sank where it stood - it did not blow up. Swapping in the
								# crash models would reset the turret to its default bearing and level the
								# hull, throwing away exactly the last state that should be preserved.
								if getattr(_mv, '_drowned', False):
									_mv._wreck_done = True   # and never let a later path swap it either
								if (reason == 2 or reason == 3 or killerID == -1) and _mv is not None and _pl is not None and victimID != getattr(_pl, 'playerVehicleID', -1) and not getattr(_mv, '_wreck_done', False) and getattr(_mv, '_chassis_model', None) is not None:
									_mv._wreck_done = True
									_dtd = getattr(_mv, 'typeDescriptor', None)
									if _dtd is not None:
										_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
										_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
										_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
										_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
										_old_ch = _mv._chassis_model
										_old_pos = _old_ch.position
										_old_yaw = _old_ch.yaw
										# pitch/roll as well: a wreck used to snap dead level on any slope
										try: _old_pitch = _old_ch.pitch
										except Exception: _old_pitch = 0.0
										try: _old_roll = _old_ch.roll
										except Exception: _old_roll = 0.0
										def _fire_wreck_swap(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu, _old_ch=_old_ch, _old_pos=_old_pos, _old_yaw=_old_yaw, _mv=_mv):
											import BigWorld, Math
											# A Model only streams in once it is IN THE WORLD. This waited for
											# .loaded on four models that were never added, so whenever the assets
											# were not already resident the wait never ended - and the caller had
											# ALREADY hidden the tank, leaving a dead-tank marker floating over
											# empty ground with no wreck under it. The player-kill path
											# (_swap_destroyed_model) always did this right; these two did not.
											if not getattr(_mv, '_wreck_kicked', False):
												_mv._wreck_kicked = True
												try: _d_ch.position = _old_pos
												except Exception: pass
												for _wm in (_d_ch, _d_hu, _d_tu, _d_gu):
													try: _add_model(_wm)
													except Exception: pass
											if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
												BigWorld.callback(0.1, _fire_wreck_swap)
												return
											# Added above only to force the load; hull/turret/gun belong on nodes.
											for _wm in (_d_hu, _d_tu, _d_gu):
												try: BigWorld.delModel(_wm)
												except Exception: pass
											try: _old_ch.visible = False
											except Exception: pass
											try: _old_ch.visibleAttachments = False
											except Exception: pass
											try:
												if getattr(_mv, 'bw_entity', None) is not None:
													_mv.bw_entity.model = None  # chassis is entity-owned: delModel alone fails
											except Exception: pass
											try: BigWorld.delModel(_old_ch)
											except Exception: pass
											# Wreck must rest on the ground (mid-air kill would leave a floating
											# wreck). _wpos: NEVER rebind _old_pos - in the player-kill path this
											# code sits in a nested function where _old_pos is only a closure var;
											# assigning it made it local -> UnboundLocalError -> vanishing wrecks.
											_wpos = _old_pos
											try:
												import BigWorld as _bwx, Math as _mx
												_gw = _bwx.wg_collideSegment(_offh_bspace(), _mx.Vector3(_wpos.x, _wpos.y + 2.0, _wpos.z), _mx.Vector3(_wpos.x, _wpos.y - 500.0, _wpos.z), 128)
												if _gw is not None and _wpos.y > _gw[0].y + 0.5:
													_wpos = _mx.Vector3(_wpos.x, _gw[0].y, _wpos.z)
											except Exception:
												pass
											_d_ch.position = _wpos
											_d_ch.yaw = _old_yaw
											# Whole orientation in one go. Model.pitch/.roll assigned separately after
											# .yaw do NOT compose - each setter rebuilds the transform, which left the
											# wreck mis-oriented (turretless hulls like the Foch 155 worst of all).
											# A Servo on a prepared matrix is what the live chassis already uses.
											try:
												_wr_mat = Math.Matrix()
												_wr_mat.setRotateYPR((_old_yaw, _old_pitch, _old_roll))
												_wr_mat.translation = _wpos
												_d_ch.addMotor(BigWorld.Servo(_wr_mat))
												# _mv, not m_veh: this swap binds _mv=_mv in its signature and none of its
												# enclosing scopes has an m_veh at all, so the assignment raised NameError
												# every time a tank burned to death ('Wreck orientation failed: global name
												# m_veh is not defined') - taking with it the matrix reference this very line
												# exists to hold down. The two sibling swap paths bind m_veh properly and
												# were never affected.
												_mv._wreck_mat = _wr_mat   # hold a ref: a GC'd matrix drops the wreck
											except Exception as _wme:
												LOG_DEBUG('Wreck orientation failed:', str(_wme))
											try: _d_ch.node('V').attach(_d_hu)
											except Exception: pass
											try:
												# last aimed pose, like every other wreck path - identity snapped the turret forward,
												# which on TDs and arty twisted the casemate into an impossible default facing
												_tm = Math.Matrix(); _tm.setRotateYPR((float(getattr(_mv, '_turret_yaw', 0.0) or 0.0), 0, 0))
												_mv._wreck_t_mat = _tm   # hold a ref: a GC'd matrix drops the node back to identity
												_mv._d_t_node = _d_hu.node('HP_turretJoint', _tm)
												_mv._d_t_node.attach(_d_tu)
											except Exception: pass
											try:
												_gm = Math.Matrix(); _gm.setRotateYPR((0, float(getattr(_mv, '_gun_pitch', 0.0) or 0.0), 0))
												_mv._wreck_g_mat = _gm   # hold a ref: a GC'd matrix drops the node back to identity
												_mv._d_g_node = _d_tu.node('HP_gunJoint', _gm)
												_mv._d_g_node.attach(_d_gu)
											except Exception: pass
										BigWorld.callback(0.1, _fire_wreck_swap)
							except Exception:
								pass
					player.arena.onVehicleKilled = _KillEventWrapper(getattr(player.arena, 'onVehicleKilled', None))
					player.arena._offh_kill_wrapped = True
					LOG_DEBUG('OfflineBattle: kill-event wrapper installed')
					LOG_DEBUG('OfflineBattle BUILD %s' % _OFFH_BUILD)
			except Exception:
				import traceback
				LOG_DEBUG('OfflineBattle: kill wrapper failed:', traceback.format_exc())
			
			from Account import Account
			if not hasattr(Account, 'shoot'):
				Account.shoot = _mock_shoot
			if not hasattr(Account, 'autoAim'):
				Account.autoAim = lambda self, targetID: None
			if not hasattr(Account, 'isGuiVisible'):
				Account.isGuiVisible = True

			if hasattr(player, 'arena'):
				if player.arena.vehicles:
					player.playerVehicleID = player.arena.vehicles.keys()[0]
			
			Waiting.close()
			
			# ---- ZVUK: okamžitě zastavit garážové audio, spustit loading hudbu ----
			try:
				import MusicController as _MC
				
				
				if not hasattr(_MC, '_orig_play'):
					_MC._orig_play = _MC.MusicController.play
					def _mock_play(self, eventName):
						# NOTE: no traceback logging here - format_stack() built a
						# full stack dump string on EVERY music event, ungated.
						from debug_utils import LOG_DEBUG
						LOG_DEBUG('MusicController.play called with:', eventName)
						return _MC._orig_play(self, eventName)
					_MC.MusicController.play = _mock_play
				if not hasattr(_MC, '_orig_stopMusic'):
					_MC._orig_stopMusic = _MC.MusicController.stopMusic
					def _mock_stopMusic(self, *args, **kwargs):
						from debug_utils import LOG_DEBUG
						LOG_DEBUG('MusicController.stopMusic called!')
						return _MC._orig_stopMusic(self, *args, **kwargs)
				_mc = _MC.g_musicController
				try:
					import SoundGroups as _SG
					if getattr(_SG, 'g_instance', None) is not None:
						# applyPreferences(), NOT setVolume(cat, 1.0). setVolume defaults to
						# updatePrefs=True, so forcing 1.0 here wrote 1.0 into the saved sound
						# preferences - every battle start permanently reset the player's music
						# and ambient sliders to full. applyPreferences re-asserts the volumes the
						# player actually chose, which is what Avatar.__startGUI does.
						_SG.g_instance.applyPreferences()
				except Exception: pass
				
				# 1) Okamžitě zastavit staré FMOD sound eventy
				_snd_music = getattr(_mc, '_MusicController__sndEventMusic', None)
				if _snd_music is not None:
					try: _snd_music.stop()
					except Exception: pass
				_snd_ambient = getattr(_mc, '_MusicController__sndEventAmbient', None)
				if _snd_ambient is not None:
					try: _snd_ambient.stop()
					except Exception: pass
				
				# 2) Zastavit interní stav
				_mc.stopAmbient()
				_mc.stopMusic()
				
				# 3) Aplikovat patch přímo na instanci
				def _mock_mc_getArenaSoundEvent(self, eventId):
					from debug_utils import LOG_DEBUG
					import BigWorld
					player = BigWorld.player()
					if hasattr(player, 'arena') and hasattr(player.arena, 'arenaType'):
						sound_name = ''
						if eventId == _MC.MUSIC_EVENT_COMBAT:
							# Do NOT return None on a repeat call. MusicController.play does
							#     if prevSoundEvent == soundEvent: return
							#     if prevSoundEvent is not None: prevSoundEvent.stop()
							#     if soundEvent is not None: soundEvent.play()
							# so a None answer STOPS whatever is playing and starts nothing. The old
							# one-shot guard here did exactly that: the loading track was cut the
							# moment the bots finished loading and no combat music ever followed.
							# WG already prevents a restart through that equality check - it just
							# needs the SAME object back every time, hence the cache below.
							sound_name = getattr(player.arena.arenaType, 'music', '')
						elif eventId == _MC.MUSIC_EVENT_COMBAT_LOADING:
							sound_name = getattr(player.arena.arenaType, 'loadingMusic', '')
						elif eventId == _MC.AMBIENT_EVENT_COMBAT:
							sound_name = getattr(player.arena.arenaType, 'ambientSound', '')
						LOG_DEBUG('OfflineBattle.mock_getArenaSoundEvent DIRECT', eventId, sound_name)
						if sound_name:
							import FMOD
							# Cache per event id: FMOD.getSound hands back a NEW object each call,
							# and play()'s 'same event -> do nothing' check compares objects. Without
							# the cache every repeat call would restart the track from the top.
							_cache = globals().setdefault('g_offh_arena_snd', {})
							_snd = _cache.get(eventId)
							if _snd is None:
								_snd = FMOD.getSound(sound_name)
								# Retail's __getArenaSoundEvent stops a freshly resolved event before
								# returning it: FMOD hands back a handle to an event that may still be
								# running from the previous battle, and play() would then resume it
								# mid-track instead of starting it.
								if _snd is not None:
									try: _snd.stop()
									except Exception: pass
								_cache[eventId] = _snd
							return _snd
					return _MC.MusicController._MusicController__getArenaSoundEvent(self, eventId)

				import types
				_mc._MusicController__getArenaSoundEvent = types.MethodType(_mock_mc_getArenaSoundEvent, _mc)
				
				# 3) Spustit loading hudbu pro bitvu
				globals()['g_offh_combat_music_done'] = False
				# New battle: drop the cached sound objects, the arena (and its tracks) changed.
				globals()['g_offh_arena_snd'] = {}
				_mc.play(_MC.MUSIC_EVENT_COMBAT_LOADING)
				LOG_DEBUG('OfflineBattle.sounds.battle_start', 'COMBAT_LOADING OK')
			except Exception as _se:
				LOG_DEBUG('OfflineBattle.sounds.battle_start error', _se)
			# ---- konec zvuk ----
			
			WindowsManager.g_windowsManager.startBattle()
			WindowsManager.g_windowsManager.showBattleLoading()
			
			if hasattr(player, 'arena'):
				if hasattr(player.arena, 'onVehicleAdded'):
					for vID in player.arena.vehicles.keys():
						player.arena.onVehicleAdded(vID)
				
				def _finish_battle_load():
					try:
						try:
							import SoundGroups as _SG
							if getattr(_SG, 'g_instance', None) is not None:
								_SG.g_instance.enableLobbySounds(False)
								_SG.g_instance.enableArenaSounds(True)
								_SG.g_instance.applyPreferences()
							_offh_apply_sound_priority()
							# NO combat music here. This used to start it the moment loading finished,
							# and _do() - scheduled 0.1 s later, right below - then played
							# MUSIC_EVENT_NONE for the prebattle period, whose sound event is None:
							#     if prevSoundEvent is not None: prevSoundEvent.stop()
							#     if soundEvent is not None: soundEvent.play()
							# so the combat track was stopped 100 ms after it started and nothing ever
							# restarted it - the whole battle ran silent.
							#
							# Retail (Avatar.__startGUI) only calls onEnterArena() here, which fires
							# MusicController.__onArenaStateChanged once - and at PREBATTLE that method
							# does nothing at all, leaving the map's intro track playing through the
							# countdown. Combat music and the map ambience start on the
							# PREBATTLE -> BATTLE edge; _offh_battle_music() does that.
						except Exception as e: pass
						
						Waiting.close()
						WindowsManager.g_windowsManager.showBattle()
						BigWorld.worldDrawEnabled(True)
						
						import AvatarInputHandler.cameras
						AvatarInputHandler.cameras.SniperCamera._USE_SWINGING = False
						BigWorld.wg_isSniperModeSwingingEnabled = lambda *a, **kw: False
						
						if not hasattr(BigWorld, '_orig_serverTime'):
							BigWorld._orig_serverTime = BigWorld.serverTime
							BigWorld._offline_start_time = __import__('time').time()
							def _mock_serverTime():
								return __import__('time').time() - BigWorld._offline_start_time
							BigWorld.serverTime = _mock_serverTime
						
						def _do():
							try:
								from gui import WindowsManager
								from account_helpers.AccountSettings import AccountSettings
								_orig_getSettings = AccountSettings.getSettings
								# Unwrap first: re-wrapping every battle chained a new closure
								# over the previous one (leak + ever-longer call path).
								if getattr(_orig_getSettings, '_offh_wrapped', False):
									_orig_getSettings = _orig_getSettings._offh_orig
								def _mock_getSettings(name, *a, **kw):
									res = _orig_getSettings(name, *a, **kw)
									if name == 'sniper' or name == 'arcade':
										if res is None: res = {}
										if isinstance(res, dict):
											defaults = {
												'snpCentralTag': {'alpha': 100, 'type': 0},
												'snpNet': {'alpha': 100, 'type': 0},
												'snpReloader': {'alpha': 100, 'type': 0},
												'snpCondition': {'alpha': 100, 'type': 0},
												'snpCassette': {'alpha': 100, 'type': 0},
												'snpGunTag': {'alpha': 100, 'type': 0},
												'snpMixing': {'alpha': 100, 'type': 0},
												'centralTag': {'alpha': 100, 'type': 0},
												'net': {'alpha': 100, 'type': 0},
												'reloader': {'alpha': 100, 'type': 0},
												'condition': {'alpha': 100, 'type': 0},
												'cassette': {'alpha': 100, 'type': 0},
												'gunTag': {'alpha': 100, 'type': 0},
												'mixing': {'alpha': 100, 'type': 0}
											}
											for k, v in defaults.items():
												if k not in res:
													res[k] = v
									return res
								_mock_getSettings._offh_wrapped = True
								_mock_getSettings._offh_orig = _orig_getSettings
								AccountSettings.getSettings = staticmethod(_mock_getSettings)
								
								if hasattr(player.arena, 'onPeriodChange'):
									_battle_duration = 900

									# RESTORE PREBATTLE: original-style countdown - nobody moves
									# or shoots until it reaches 0 (see the period<3 guards)
									from _constants import CONFIG_OPTIONS as _CFG_PB
									_pb_len = float(_CFG_PB.get('prebattle_countdown_seconds', 30.0))
									player.arena.period = 2
									player.arena.periodLength = _pb_len
									player.arena.periodEndTime = BigWorld.serverTime() + _pb_len
									player.arena.onPeriodChange(2, player.arena.periodEndTime, _pb_len, {})
									# (removed) play(MUSIC_EVENT_NONE) used to silence the map's intro
									# track the instant the countdown began. MUSIC_EVENT_NONE maps to a
									# None sound event, so it only ever STOPS whatever is playing.
									# Retail plays no music event at PREBATTLE at all: the intro keeps
									# going until the battle starts and combat music replaces it.
									
								if hasattr(player.arena, 'onNewVehicleListReceived'):
									player.arena.onNewVehicleListReceived()
								if hasattr(player.arena, 'onVehicleAdded'):
									for vID in player.arena.vehicles.keys():
										player.arena.onVehicleAdded(vID)
								if hasattr(player.arena, 'onVehicleStatisticsUpdate'):
									for vID in player.arena.vehicles.keys():
										player.arena.onVehicleStatisticsUpdate(vID)
								if hasattr(WindowsManager.g_windowsManager.battleWindow, '_Battle__populateData'):
									WindowsManager.g_windowsManager.battleWindow._Battle__populateData()
									
								if not getattr(player, '_crosshair_init_done', False):
									player._crosshair_init_done = True
									# Remember the hangar FOV before any sniper zoom touches it, so the sweep
									# can put it back (see the snipercam stage).
									try:
										if globals().get('g_offh_base_fov') is None:
											globals()['g_offh_base_fov'] = BigWorld.projection().fov
									except Exception: pass
									# Silhouette palette - see _offh_push_edge_colors. Pushed here AND at every
									# outline change, because retail's Avatar.onEnterWorld never runs offline.
									LOG_DEBUG('EDGE DETECT colours pushed from %s' % (_offh_push_edge_colors(),))
									try:
										import AvatarInputHandler.aims
										try:
											AvatarInputHandler.aims.clearState()
											hs = AvatarInputHandler.aims._g_aimState.get('health')
											if hs is not None:
												hs['cur'] = getattr(player.vehicleTypeDescriptor, 'maxHealth', 400)
												hs['max'] = getattr(player.vehicleTypeDescriptor, 'maxHealth', 400)
										except Exception as e:
											LOG_DEBUG('OfflineBattle aims init error:', str(e))
											
										# Mock the startup to avoid crashing on missing Vehicle entity
										g_offline_aih._AvatarInputHandler__isStarted = True
										g_offline_aih._AvatarInputHandler__isGUIVisible = True
										g_offline_aih._AvatarInputHandler__isArenaStarted = True
										for control in g_offline_aih._AvatarInputHandler__ctrls.itervalues():
											try: control.create()
											except Exception as e: LOG_DEBUG('Control create error:', e)
											
											# Pre-warm the gunMarker state so dumpState() doesn't throw KeyError: 'startTime'
											try: control.setReloading(0.0, 0.0)
											except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
											
										try:
											g_offline_aih._AvatarInputHandler__isSPG = 'SPG' in td.type.tags
										except Exception:
											g_offline_aih._AvatarInputHandler__isSPG = False

										g_offline_aih.onControlModeChanged('arcade')
										g_offline_aih.setGUIVisible(True)
										if hasattr(g_offline_aih, 'ctrl'):
											pass
										try:
											import AvatarInputHandler.aims as aims
											if getattr(aims, '_g_aimState', None) is not None:
												aims._g_aimState['reload'] = {'isReloading': False, 'duration': 0.0, 'startTime': None, 'correction': None}
										except Exception:
											pass
										g_offline_aih.ctrl.showGunMarker(True)
										g_offline_aih.ctrl.showGunMarker2(True)
										_force_camera_to_model()
										LOG_DEBUG('OfflineBattle AIH enable SUCCESS')
									except Exception as e:
										import traceback
										LOG_DEBUG('OfflineBattle AIH enable ERROR:', traceback.format_exc())
							except Exception:
								import traceback
								LOG_DEBUG('Do error:', traceback.format_exc())
							return

						BigWorld.callback(0.1, _do)
						
					except Exception:
						LOG_CURRENT_EXCEPTION()
				from _constants import CONFIG_OPTIONS
				loading_time = float(CONFIG_OPTIONS.get('loading_screen_time_seconds', 5.0))
				BigWorld.callback(loading_time, _finish_battle_load)

		except Exception:
			LOG_CURRENT_EXCEPTION()
			WindowsManager.g_windowsManager.hideAll()
		LOG_DEBUG('OfflineBattle.camera started')
	except Exception:
		LOG_CURRENT_EXCEPTION()
	player._offline_allow_become_non_player = False
	LOG_DEBUG('OfflineBattle.spawnAvatar.fail', cmdName)
	return

def _step_on_enqueued(player, vehInvID, cmdName):
	try:
		_enable_offline_battle_transition(player)
		ctx = build_offline_battle_context(player, vehInvID, cmdName)
		player._offhangar_battle_ctx = ctx
		player._offhangar_player_vehicle_id = ctx.get('playerVehicleID', vehInvID)
		player._offhangar_team = 1
		arena = getattr(player, '_offhangar_arena', None)
		if arena is not None:
			arena.vehicles = ctx.get('vehicles', {})
			arena.guiType = 0
			arena.bonusType = 0
			arena.extraData = {'mapName': ctx.get('mapName'), 'mapID': ctx.get('mapID')}
			arena.period = 1
			arena.periodLength = 600
			arena.periodEndTime = BigWorld.serverTime() + 600
			map_name = ctx.get('mapName', '') or ''
			map_id = ctx.get('mapID', 0) or 0
			gameplay = 'ctf'
			real_arena_type = _resolve_real_arena_type(map_id, map_name, gameplay)
			if real_arena_type is not None:
				arena.arenaType = real_arena_type
				arena.arenaTypeID = map_id
				try:
					import ArenaType
					if hasattr(ArenaType, 'g_cache') and isinstance(ArenaType.g_cache, dict):
						for k, v in ArenaType.g_cache.iteritems():
							if v is real_arena_type:
								arena.arenaTypeID = k
								break
				except Exception: pass
				LOG_DEBUG('OfflineBattle.arenaType.real', map_name, 'arenaTypeID', arena.arenaTypeID, 'geomName', getattr(real_arena_type, 'geometryName', ''), 'minimap', hasattr(real_arena_type, 'minimap'))
			elif getattr(arena, 'arenaType', None) is not None:
				# Fallback: keep stub, but ensure required attrs exist.
				arena.arenaTypeID = map_id
				arena.arenaType.geometryName = map_name
				arena.arenaType.gameplayName = gameplay
				if not hasattr(arena.arenaType, 'minimap'):
					arena.arenaType.minimap = None
				LOG_DEBUG('OfflineBattle.arenaType.stub', map_name)
		queueType = _queue_type_randoms()
		LOG_DEBUG('OfflineBattle.onEnqueued', cmdName, 'queueType', queueType, 'vehInvID', vehInvID)
		onEnqueued = getattr(player, 'onEnqueued', None)
		if callable(onEnqueued):
			onEnqueued(queueType)
		else:
			onEnqueuedRandom = getattr(player, 'onEnqueuedRandom', None)
			if callable(onEnqueuedRandom):
				onEnqueuedRandom()
		if hasattr(player, 'isInRandomQueue'):
			player.isInRandomQueue = True
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _step_on_arena_created(player, cmdName):
	try:
		if player is None:
			return
		if getattr(player, '_offhangar_arena_created_once', False):
			LOG_DEBUG('OfflineBattle.onArenaCreated skip duplicate', cmdName)
			return
		player._offhangar_arena_created_once = True
		LOG_DEBUG('OfflineBattle.onArenaCreated', cmdName)
		onArenaCreated = getattr(player, 'onArenaCreated', None)
		if callable(onArenaCreated):
			onArenaCreated()
		BigWorld.callback(0.05, lambda: _try_spawn_battle_avatar_stub(BigWorld.player(), cmdName))
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _schedule_arena_created_resilient(cmdName, player):
	def _fire():
		# Cancel button pressed while the fake matchmaker was counting down
		# (Account.dequeueRandom sets this). Without the check the battle booted
		# anyway a few seconds after the player had left the queue.
		if getattr(player, '_offhangar_queue_cancelled', False):
			LOG_DEBUG('OfflineBattle.arenaCreated skipped: queue was cancelled')
			return
		if not getattr(player, '_offhangar_arena_created_once', False):
			_step_on_arena_created(player, cmdName)

	from _constants import CONFIG_OPTIONS
	queue_time = float(CONFIG_OPTIONS.get('queue_wait_time_seconds', 4.0))

	import BigWorld
	BigWorld.callback(queue_time, _fire)
	BigWorld.callback(queue_time + 0.03, _fire)
	BigWorld.callback(queue_time + 0.10, _fire)


def schedule_random_battle_flow_after_enqueue(cmd, cmdName, args):
	"""
	Call after RES_SUCCESS was delivered for an enqueue-like command.
	args: tuple from doCmdInt3 (int1, int2, int3) or similar.
	"""
	if not OFFLINE_BATTLE_ENABLED:
		LOG_DEBUG('OfflineBattle.disabled schedule', cmdName, cmd, args)
		return
	player = BigWorld.player()
	if player is not None:
		now = time.time()
		if now - getattr(player, '_offhangar_sched_debounce', 0) < 1.0:
			LOG_DEBUG('OfflineBattle.schedule debounce', cmdName, cmd, args)
			return
		player._offhangar_sched_debounce = now

	int1 = args[0] if args else 0
	# Never treat server-stats traffic as battle (same numeric cmd id can alias in AccountCommands index).
	if cmdName and ('SERVER_STATS' in cmdName or 'REQ_SERVER_STATS' in cmdName):
		if int1 == 0 and (len(args) < 2 or args[1] == 0) and (len(args) < 3 or args[2] == 0):
			LOG_DEBUG('OfflineBattle.skip stats-shaped packet', cmdName, cmd, args)
			return

	def _run():
		player = BigWorld.player()
		if player is None or not getattr(player, 'isOffline', False):
			return
		player._offhangar_arena_created_once = False
		player._offhangar_queue_cancelled = False
		vehInvID = int1
		if vehInvID == 0 and cmdName and 'ENQUEUE' in cmdName:
			vehInvID = _resolve_vehicle_inv_id(player, 0)
		if not vehInvID:
			LOG_DEBUG('OfflineBattle.skip no vehInvID', cmdName, cmd, args)
			return
		_step_on_enqueued(player, vehInvID, cmdName)
		_schedule_arena_created_resilient(cmdName, player)

	# Run after the current frame so onCmdResponse callbacks finish first.
	BigWorld.callback(0.05, _run)


def start_offline_random_from_hangar(player, vehInvID):
	import debug_utils
	try:
		from gui.battle_control import constants as bc_constants
		debug_utils.LOG_DEBUG("VEHICLE_VIEW_STATE:", dir(bc_constants.VEHICLE_VIEW_STATE))
		for k in dir(bc_constants.VEHICLE_VIEW_STATE):
			if not k.startswith('_'): debug_utils.LOG_DEBUG("VIEW_STATE", k, getattr(bc_constants.VEHICLE_VIEW_STATE, k))
	except Exception as e: debug_utils.LOG_DEBUG("DUMP ERR1", e)
	try:
		import constants
		debug_utils.LOG_DEBUG("VEHICLE_DEVICE_STATES:", dir(constants.VEHICLE_DEVICE_STATES))
		for k in dir(constants.VEHICLE_DEVICE_STATES):
			if not k.startswith('_'): debug_utils.LOG_DEBUG("DEV_STATE", k, getattr(constants.VEHICLE_DEVICE_STATES, k))
	except Exception as e: debug_utils.LOG_DEBUG("DUMP ERR2", e)
	
	import traceback
	LOG_DEBUG('OfflineBattle.start_offline_random_from_hangar CALLED', player, getattr(player, 'isOffline', None))
	"""
	0.8.x hangar may spam other doCmd ids before/instead of CMD_ENQUEUE_RANDOM (700).
	When the client calls PlayerAccount.enqueueRandom, short-circuit here so we still
	fire the same BW-side chain as a real matchmaker ack.
	"""
	if not OFFLINE_BATTLE_ENABLED:
		LOG_DEBUG('OfflineBattle.disabled start', vehInvID)
		return
	if player is None:
		LOG_DEBUG('OfflineBattle.disabled start player is None')
		return
	# Never boot a second battle on top of a running one: the account keeps
	# receiving key events in battle, so F12 mid-battle (or mid-loading)
	# re-entered the whole battle flow and left the battle aim GUI painted
	# over the hangar (user report: 'the bug occurs when pressing FN+F12').
	try:
		from gui import WindowsManager as _WMg
		if getattr(_WMg.g_windowsManager, 'battleWindow', None) is not None:
			LOG_DEBUG('OfflineBattle.hook skip: battle window active (F12 during battle/loading)')
			return
	except Exception:
		pass
	try:
		_ihg = getattr(player, 'inputHandler', None)
		if _ihg is not None and hasattr(_ihg, 'ctrls'):
			LOG_DEBUG('OfflineBattle.hook skip: battle input handler active')
			return
	except Exception:
		pass
	now = time.time()
	if now - getattr(player, '_offline_boot_time', 0.0) < 10.0:
		LOG_DEBUG('OfflineBattle.hook IGNORED AUTO-START inside start_offline')
		return
	last = getattr(player, '_offhangar_battle_last_boot', 0.0)
	if now - last < _BATTLE_BOOT_DEBOUNCE_SEC:
		LOG_DEBUG('OfflineBattle.hook debounce skip', vehInvID)
		return
	player._offhangar_battle_last_boot = now
	cmdName = 'offline.enqueueRandom'

	def _run():
		import BigWorld
		p = BigWorld.player()
		if p is None:
			LOG_DEBUG('OfflineBattle.hook skip p is None')
			return
		p._offhangar_arena_created_once = False
		p._offhangar_queue_cancelled = False
		vid = vehInvID or _resolve_vehicle_inv_id(p, 0)
		if not vid:
			LOG_DEBUG('OfflineBattle.hook skip no vehInvID', vehInvID)
			return
		LOG_DEBUG('OfflineBattle.hook start', cmdName, 'vehInvID', vid)
		_step_on_enqueued(p, vid, cmdName)
		_schedule_arena_created_resilient(cmdName, p)

	import BigWorld
	BigWorld.callback(0.05, _run)

try:
	import gui.Scaleform.battledispatcherinterface as bdi
	if hasattr(bdi, 'BattleDispatcherInterface'):
		orig_updateFightButton = bdi.BattleDispatcherInterface.updateFightButton
		def _new_updateFightButton(self):
			orig_updateFightButton(self)
			
			fightTypes = getattr(self, '_offhangar_fightTypes_temp', None)
			if fightTypes is None:
				# In case we can't capture it easily, we just call self.call again!
				pass
		
		# Better approach: monkey-patch self.call in BattleDispatcherInterface
		orig_call = bdi.BattleDispatcherInterface.call
		def _new_call(self, methodName, args=None):
			from gui.mods.offhangar.logging import LOG_DEBUG
			LOG_DEBUG("FLASH CALL:", methodName, args)
			if methodName == 'common.setFightButton' and isinstance(args, list):
				args.append('Bootcamp')
				args.append('tutorial')
				args.append(False)
				args.append('')
			return orig_call(self, methodName, args)
		bdi.BattleDispatcherInterface.call = _new_call

		orig_onFightButtonClick = bdi.BattleDispatcherInterface.onFightButtonClick
		def _new_onFightButtonClick(self, callbackId, mapId=None, queueType=0, confirm=False):
			import BigWorld
			p = BigWorld.player()
			from gui.mods.offhangar.logging import LOG_DEBUG
			LOG_DEBUG("FIGHT BUTTON CLICKED", "mapId:", mapId, "type:", type(mapId), "queueType:", queueType, "type:", type(queueType))
			
			if queueType == 'tutorial':
				if hasattr(p, 'enqueueTutorial'):
					p.enqueueTutorial()
				return
			
			if queueType == 'demonstrator':
				if mapId is not None:
					setattr(p, '_offhangar_selected_mapId', mapId)
				if hasattr(self, 'respond'):
					try: self.respond(callbackId, True)
					except: pass
				start_offline_random_from_hangar(p, 0)
				return
			
			# If it's a regular random battle, ensure we clear any demonstrator map override!
			if hasattr(p, '_offhangar_selected_mapId'):
				delattr(p, '_offhangar_selected_mapId')
				
			return orig_onFightButtonClick(self, callbackId, mapId, queueType, confirm)
		bdi.BattleDispatcherInterface.onFightButtonClick = _new_onFightButtonClick
except Exception:
	import traceback
	LOG_DEBUG('Failed to hook UI')
	LOG_DEBUG(traceback.format_exc())
