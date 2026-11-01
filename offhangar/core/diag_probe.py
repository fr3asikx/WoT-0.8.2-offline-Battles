# -*- coding: utf-8 -*-
"""Aggregated diagnostics probe for the offline battle (config: diag_probe).

Why this module exists
----------------------
Three separate "the bots do nothing" reports cannot be told apart by reading
the code, because each has several plausible gates:

* bots never fire  - `_ai_ready_to_fire and _ai_shot_clear` is a conjunction of
  reload, order permission, range and gun alignment;
* no artillery     - is the roster missing SPGs, or are SPGs present but never
  given an artillery order?
* bots stay silent - `player_advisory()` is wired at offline_battle.py:16415 but
  its exceptions go to LOG_DEBUG, which is invisible unless debug_logging is on.

So the probe counts gates instead of guessing, and logs ONE line per category
every INTERVAL seconds (a battle adds a few dozen lines, not thousands). This is
the same method that found the `_SCFG` UnboundLocalError behind the invisible
enemies (see CONTEXT_AI.md section 9).

Everything here is inert unless `diag_probe` is true in config.json, every entry
point is exception-safe, and no game state is ever modified.
"""

from gui.mods.offhangar.logging import LOG_NOTE

INTERVAL = 3.0

# None = not resolved yet, so the config is read once per process.
_ON = [None]
_NEXT = [0.0]
_COUNTS = {}
_GAUGES = {}
_MIN = {}
_MAX = {}
# LOS collision-filter census: matKind -> [dropped, kept, last collFlags, name].
# Deliberately NOT reset by flush(): which materials the engine offers and which
# _offh_los_scene_only throws away is a property of the map, not of a 3 s window.
_MATKINDS = {}

# Per-bot fire-gate state for STALLPROBE. NOT reset with the 3 s window: a stall is
# by definition longer than one window. Rows carry the battle generation so a new
# battle cannot inherit the previous one's ids (bots are renumbered 1000.. each time).
_BOTS = {}
_NEXT_STALL = [0.0]
_STALL_GEN = [0]
STALL_INTERVAL = 15.0
STALL_QUIET = 20.0


def _enabled():
	if _ON[0] is None:
		try:
			from gui.mods.offhangar._constants import CONFIG_OPTIONS
			_ON[0] = bool(CONFIG_OPTIONS.get('diag_probe', False))
		except Exception:
			_ON[0] = False
		if _ON[0]:
			LOG_NOTE('DIAGPROBE on interval=%.1fs categories=FIREPROBE,ARTYPROBE,'
				'ADVPROBE (read-only, no game state touched)' % INTERVAL)
	return _ON[0]


def enabled():
	"""Public gate for callers that emit their own LOG_NOTE line (HANGARPROBE).

	`count`/`gauge` are inert when diag_probe is off, but a caller that formats and
	logs its own line has to ask first, or it would print with the probe disabled.
	"""
	return _enabled()


def count(name, amount=1):
	"""Accumulate an event counter for the current interval."""
	if not _enabled():
		return
	_COUNTS[name] = _COUNTS.get(name, 0) + amount


def gauge(name, value):
	"""Remember the latest value of a per-frame gauge."""
	if not _enabled():
		return
	_GAUGES[name] = value


def track_max(name, value):
	"""Remember the largest value seen this interval (reset on flush)."""
	if not _enabled():
		return
	try:
		value = float(value)
		previous = _MAX.get(name)
		if previous is None or value > float(previous):
			_MAX[name] = value
	except Exception:
		pass


def _track_min(name, value):
	previous = _MIN.get(name)
	if previous is None or value < previous:
		_MIN[name] = value


def matkind_census(mat_kind, coll_flags, kept, name=None):
	"""Record one decision of the LOS collision filter (read-only).

	Called from offline_battle._offh_los_scene_only, i.e. from inside the engine
	callback handed to BigWorld.wg_collideSegment, for every contact the engine
	offers - including the ones the filter rejects, which never reach the LOS
	counters and are therefore invisible in every other line of the log.
	`kept` is what the filter returned: True = the contact may veto LOS,
	False = the engine was told to march on through it.
	"""
	if not _enabled():
		return
	try:
		key = int(mat_kind)
	except Exception:
		return
	row = _MATKINDS.get(key)
	if row is None:
		row = [0, 0, -1, '?']
		_MATKINDS[key] = row
	if kept:
		row[1] = row[1] + 1
	else:
		row[0] = row[0] + 1
	try:
		row[2] = int(coll_flags)
	except Exception:
		pass
	if name is not None:
		row[3] = str(name)


def matkind_report(limit=6):
	"""Return (dropped_top_N, kept_top_N) as compact strings, most frequent first.

	Format per entry: `<matKind>x<count>(name=<material>,cf=<last collFlags>)`.
	"""
	if not _enabled():
		return ('-', '-')
	dropped = []
	kept = []
	try:
		for key in list(_MATKINDS.keys()):
			row = _MATKINDS.get(key)
			if not row:
				continue
			if row[0] > 0:
				dropped.append((row[0], '%dx%d(name=%s,cf=%s)' % (
					key, row[0], row[3], row[2])))
			if row[1] > 0:
				kept.append((row[1], '%dx%d(name=%s,cf=%s)' % (
					key, row[1], row[3], row[2])))
		dropped.sort(reverse=True)
		kept.sort(reverse=True)
	except Exception:
		return ('-', '-')
	if not dropped and not kept:
		return ('-', '-')
	dropped_text = '-'
	kept_text = '-'
	if dropped:
		dropped_text = ', '.join([item[1] for item in dropped[:limit]])
	if kept:
		kept_text = ', '.join([item[1] for item in kept[:limit]])
	return (dropped_text, kept_text)


def fire_gate(has_target, timer_ok, allowed, in_range, aligned, ready, clear,
		target_yaw, hull_yaw, turret_yaw, desired_pitch, gun_pitch,
		yaw_tol, pitch_tol, distance, is_artillery):
	"""Record one bot-frame of the firing conjunction (offline_battle.py ~19207).

	Callers pass the raw aim values so the probe can report the actual yaw and
	pitch errors against the tolerances the difficulty tier produced. That is
	what separates "the tolerances are too tight for a live suspension" from
	"the order never allows fire".
	"""
	if not _enabled():
		return
	try:
		_COUNTS['fire_samples'] = _COUNTS.get('fire_samples', 0) + 1
		if has_target:
			_COUNTS['fire_with_target'] = _COUNTS.get('fire_with_target', 0) + 1
		if timer_ok:
			_COUNTS['fire_timer_ok'] = _COUNTS.get('fire_timer_ok', 0) + 1
		if allowed:
			_COUNTS['fire_allowed'] = _COUNTS.get('fire_allowed', 0) + 1
		if in_range:
			_COUNTS['fire_in_range'] = _COUNTS.get('fire_in_range', 0) + 1
		if aligned:
			_COUNTS['fire_aligned'] = _COUNTS.get('fire_aligned', 0) + 1
		if ready:
			_COUNTS['fire_ready'] = _COUNTS.get('fire_ready', 0) + 1
		if clear:
			_COUNTS['fire_clear'] = _COUNTS.get('fire_clear', 0) + 1
		if is_artillery:
			_COUNTS['fire_arty_orders'] = _COUNTS.get('fire_arty_orders', 0) + 1
		if has_target:
			import math
			yaw_error = abs((float(target_yaw) -
				(float(hull_yaw) + float(turret_yaw)) + math.pi) %
				(2.0 * math.pi) - math.pi)
			pitch_error = abs(float(desired_pitch) - float(gun_pitch))
			_track_min('yaw_err_min', yaw_error)
			_track_min('pitch_err_min', pitch_error)
			_GAUGES['yaw_tol'] = float(yaw_tol)
			_GAUGES['pitch_tol'] = float(pitch_tol)
			# Would it have fired with twice the tier tolerance?  Answers
			# "is the aim tolerance the blocker" without changing any code.
			if (yaw_error <= 2.0 * float(yaw_tol) and
					pitch_error <= 2.0 * float(pitch_tol)):
				_COUNTS['fire_aligned_2x_tol'] = _COUNTS.get(
					'fire_aligned_2x_tol', 0) + 1
			try:
				distance = float(distance)
				previous = _MIN.get('dist_min')
				if previous is None or distance < previous:
					_MIN['dist_min'] = distance
				previous = _MAX.get('dist_max')
				if previous is None or distance > previous:
					_MAX['dist_max'] = distance
			except Exception:
				pass
	except Exception:
		pass


def frame_tick(now, mock_vehicles=None):
	"""Flush one line per category every INTERVAL seconds. Call once per frame."""
	if not _enabled():
		return
	try:
		if float(now) < _NEXT[0]:
			return
		_NEXT[0] = float(now) + INTERVAL
	except Exception:
		return
	try:
		samples = int(_COUNTS.get('fire_samples', 0) or 0)
		LOG_NOTE('FIREPROBE samples=%d with_target=%d timer_ok=%d allowed=%d '
			'in_range=%d aligned=%d aligned_2x_tol=%d ready=%d clear=%d '
			'yaw_err_min=%s yaw_tol=%s pitch_err_min=%s pitch_tol=%s dist=%s..%s'
			% (samples,
				int(_COUNTS.get('fire_with_target', 0) or 0),
				int(_COUNTS.get('fire_timer_ok', 0) or 0),
				int(_COUNTS.get('fire_allowed', 0) or 0),
				int(_COUNTS.get('fire_in_range', 0) or 0),
				int(_COUNTS.get('fire_aligned', 0) or 0),
				int(_COUNTS.get('fire_aligned_2x_tol', 0) or 0),
				int(_COUNTS.get('fire_ready', 0) or 0),
				int(_COUNTS.get('fire_clear', 0) or 0),
				_fmt(_MIN.get('yaw_err_min')), _fmt(_GAUGES.get('yaw_tol')),
				_fmt(_MIN.get('pitch_err_min')), _fmt(_GAUGES.get('pitch_tol')),
				_fmt(_MIN.get('dist_min')), _fmt(_MAX.get('dist_max'))))
	except Exception:
		pass
	try:
		spg = {}
		bots = 0
		if mock_vehicles:
			for mock in mock_vehicles.values():
				bots += 1
				team = int(getattr(mock, '_bot_team', 0) or 0)
				tags = ()
				try:
					tags = getattr(getattr(mock, 'typeDescriptor', None),
						'type', None)
					tags = getattr(tags, 'tags', ()) or ()
				except Exception:
					tags = ()
				if 'SPG' in tags:
					spg[team] = spg.get(team, 0) + 1
		LOG_NOTE('ARTYPROBE bots=%d spg_by_team=%s arty_contacts_t1=%s '
			'arty_contacts_t2=%s arty_orders=%d' % (
				bots, spg or '{}',
				_GAUGES.get('arty_team1', '?'), _GAUGES.get('arty_team2', '?'),
				int(_COUNTS.get('fire_arty_orders', 0) or 0)))
	except Exception:
		pass
	try:
		LOG_NOTE('ADVPROBE advisory_calls=%d advisory_errors=%d '
			'advisory_results=%d blocked_shot_calls=%d last_error=%s' % (
				int(_COUNTS.get('adv_calls', 0) or 0),
				int(_COUNTS.get('adv_errors', 0) or 0),
				int(_COUNTS.get('adv_results', 0) or 0),
				int(_COUNTS.get('adv_blocked_calls', 0) or 0),
				_GAUGES.get('adv_last_error', '-')))
	except Exception:
		pass
	try:
		# Destructible passability census: why the corridor probe calls something
		# a wall.  structure_blocked high with structure_passable near zero is the
		# "bots are afraid of fences" signature; mat_info_none counts probes that
		# could not resolve a material at all (fail-closed => treated as a wall).
		LOG_NOTE('CRUSHPROBE structure_passable=%d structure_blocked=%d '
			'fragile_passable=%d fragile_blocked_big=%d mat_info_none=%d '
			'matkind_out_of_range=%d desc_missing=%d' % (
				int(_COUNTS.get('crush_structure_passable', 0) or 0),
				int(_COUNTS.get('crush_structure_blocked', 0) or 0),
				int(_COUNTS.get('crush_fragile_passable', 0) or 0),
				int(_COUNTS.get('crush_fragile_blocked_big', 0) or 0),
				int(_COUNTS.get('crush_mat_info_none', 0) or 0),
				int(_COUNTS.get('crush_matkind_out_of_range', 0) or 0),
				int(_COUNTS.get('crush_desc_missing', 0) or 0)))
	except Exception:
		pass
	try:
		# Line-of-sight census: every exit of _offh_ai_has_los is counted, so a
		# FIREPROBE with ready>0 and clear=0 says WHICH veto closed the lane.
		# blocked_solid dominates => static geometry or an unresolved material;
		# blocked_vehicle dominates => ally/wreck hulls on the firing line;
		# exception > 0 => the probe itself is broken, not the lane.
		LOG_NOTE('LOSPROBE true=%d pointblank=%d ray_clear=%d blocked_solid=%d '
			'blocked_vehicle=%d steps_exhausted=%d mat_info_none=%d '
			'step_transparent=%d exception=%d (fire=%d spot=%d) last_error=%s' % (
				int(_COUNTS.get('los_true', 0) or 0),
				int(_COUNTS.get('los_true_pointblank', 0) or 0),
				int(_COUNTS.get('los_ray_clear', 0) or 0),
				int(_COUNTS.get('los_blocked_solid', 0) or 0),
				int(_COUNTS.get('los_blocked_vehicle', 0) or 0),
				int(_COUNTS.get('los_steps_exhausted', 0) or 0),
				int(_COUNTS.get('los_mat_info_none', 0) or 0),
				int(_COUNTS.get('los_step_transparent', 0) or 0),
				int(_COUNTS.get('los_exception', 0) or 0),
				int(_COUNTS.get('los_exception_fire', 0) or 0),
				int(_COUNTS.get('los_exception_spot', 0) or 0),
				_GAUGES.get('los_last_error', '-')))
		LOG_NOTE('LOSPROBE last_solid_veto matkind=%s material=%s ground_dy=%s '
			'file=%s %s' % (
				_GAUGES.get('los_block_matkind', '-'),
				_GAUGES.get('los_block_matname', '-'),
				_GAUGES.get('los_block_ground_dy', '-'),
				_GAUGES.get('los_block_file', '-'),
				_GAUGES.get('los_block_sample', '-')))
	except Exception:
		pass
	try:
		# LOS collision-filter census.  The 02.10 ruinberg log vetoed LOS with
		# matKind 0 (terrain) and 73 (destructible props) ONLY - never a building
		# - while `LOS_CFG ignore_high_matkinds=on` makes _offh_los_scene_only
		# drop every matKind > 100 before it can veto.  These two lines say which
		# of the two it is: if buildings are dropped here they show up in
		# matkind_dropped with their real material name; if they are absent from
		# BOTH lines then the 128-mask query never offers them at all and the
		# next question is the mask, not the filter.
		_dropped, _kept = matkind_report(6)
		LOG_NOTE('LOSPROBE matkind_dropped(top6) %s' % _dropped)
		LOG_NOTE('LOSPROBE matkind_kept(top6) %s' % _kept)
	except Exception:
		pass
	try:
		# Wreck release census: which strategy ran when a bot died.  A client
		# crash right after 'wreck freeze' showed up as guard_ok with no further
		# lines; sleep_ok with no crash is the fix working.
		LOG_NOTE('WRECKPROBE mark_ok=%d mark_fail=%d mark_exc=%d sleep_ok=%d '
			'sleep_fail=%d sleep_exc=%d idle_ok=%d idle_fail=%d idle_exc=%d '
			'guard_ok=%d guard_exc=%d stop_mode=%d deferred=%d' % (
				int(_COUNTS.get('wreck_mark_ok', 0) or 0),
				int(_COUNTS.get('wreck_mark_fail', 0) or 0),
				int(_COUNTS.get('wreck_mark_exc', 0) or 0),
				int(_COUNTS.get('wreck_sleep_ok', 0) or 0),
				int(_COUNTS.get('wreck_sleep_fail', 0) or 0),
				int(_COUNTS.get('wreck_sleep_exc', 0) or 0),
				int(_COUNTS.get('wreck_idle_ok', 0) or 0),
				int(_COUNTS.get('wreck_idle_fail', 0) or 0),
				int(_COUNTS.get('wreck_idle_exc', 0) or 0),
				int(_COUNTS.get('wreck_guard_ok', 0) or 0),
				int(_COUNTS.get('wreck_guard_exc', 0) or 0),
				int(_COUNTS.get('wreck_stop_mode', 0) or 0),
				int(_COUNTS.get('wreck_deferred', 0) or 0)))
	except Exception:
		pass
	try:
		# Audibility gate: how many engine/track loops were stopped because the
		# owner was not spotted.  Zero while enemies are still heard means the
		# gate never runs (stale _spot_visible), not that the sounds are correct.
		LOG_NOTE('SOUNDPROBE stop_invisible=%d' % int(
			_COUNTS.get('snd_stop_invisible', 0) or 0))
	except Exception:
		pass
	try:
		# Stuck-bot census: how many bots entered a hazard hold, how many were
		# released by the timeout instead of reaching the escape endpoint.
		# enter >> timeout+recovered means bots are being parked permanently.
		LOG_NOTE('HAZARDPROBE enter=%d timeout=%d' % (
			int(_COUNTS.get('hazard_enter', 0) or 0),
			int(_COUNTS.get('hazard_timeout', 0) or 0)))
	except Exception:
		pass
	try:
		# Is bot gunnery actually 3D?  want_nonzero counts bot-frames whose
		# desired barrel elevation differs from level; clamped counts those
		# pressed into the gun's pitchLimits; src_* shows which solution drove
		# it (artillery / direct ballistic / geometric fallback).
		LOG_NOTE('GUNPROBE samples=%d want_nonzero=%d clamped=%d abs_max=%s '
			'src_arty=%d src_direct=%d src_fallback=%d' % (
				int(_COUNTS.get('gun_pitch_samples', 0) or 0),
				int(_COUNTS.get('gun_pitch_nonzero', 0) or 0),
				int(_COUNTS.get('gun_pitch_clamped', 0) or 0),
				_fmt(_MAX.get('gun_pitch_abs_max')),
				int(_COUNTS.get('gun_src_arty', 0) or 0),
				int(_COUNTS.get('gun_src_direct', 0) or 0),
				int(_COUNTS.get('gun_src_fallback', 0) or 0)))
	except Exception:
		pass
	try:
		if float(now) >= _NEXT_STALL[0]:
			_NEXT_STALL[0] = float(now) + STALL_INTERVAL
			for _stall_line in stall_report(now):
				LOG_NOTE(_stall_line)
	except Exception:
		pass
	# Reset the interval accumulators; gauges keep their last value.
	for key in list(_COUNTS.keys()):
		_COUNTS[key] = 0
	for key in list(_MIN.keys()):
		del _MIN[key]
	for key in list(_MAX.keys()):
		del _MAX[key]


def bot_gate(gen, bot_id, team, target_id, dist, speed, gate, now):
	"""Record one bot-frame of the fire conjunction PER BOT (feeds STALLPROBE).

	`fire_gate` sums each conjunct over ALL bots, so FIREPROBE can say "clear is the
	rare one" but not WHICH bot never fires. Session 03.10 16:53 (Himmelsdorf) had 28
	of 29 bots shoot and one - id=1020 - that never fired a single shot all battle
	while logging 35 `WEAKSPOT_AIM` lines from log line 814 to 6333, i.e. it was
	alive, had targets (1011/1003/1025) and computed aim points to the very end.
	Nothing in the log said where its gate stopped, and `hazard_recovery` /
	`hazard_timeout` were both 0 that battle, so it was not a hazard hold either.

	`gate` carries the conjuncts in the order
	(has_target, timer_ok, allowed, in_range, aligned, ready, clear); the FIRST False
	is the stage this bot stopped at this frame. Read-only for the game, inert unless
	diag_probe is on.
	"""
	if not _enabled():
		return
	try:
		key = int(bot_id)
	except Exception:
		return
	try:
		g = int(gen)
	except Exception:
		g = 0
	row = _BOTS.get(key)
	if row is None or row.get('gen') != g:
		row = {'gen': g, 'team': team, 'frames': 0, 'last_fire': 0.0,
			'last_target': None, 'stage': '-', 'dist': 0.0, 'speed': 0.0,
			'seen': 0.0}
		_BOTS[key] = row
	try:
		row['gen'] = g
		row['team'] = team
		row['frames'] = int(row.get('frames', 0)) + 1
		row['seen'] = float(now or 0.0)
		row['dist'] = float(dist or 0.0)
		row['speed'] = float(speed or 0.0)
		if target_id is not None:
			row['last_target'] = int(target_id)
		names = ('no_target', 'timer', 'allowed', 'in_range', 'aligned', 'ready', 'clear')
		stage = 'fire'
		for i in range(len(names)):
			if i < len(gate) and not gate[i]:
				stage = names[i]
				break
		row['stage'] = stage
		if stage == 'fire':
			row['last_fire'] = float(now or 0.0)
		_STALL_GEN[0] = g
	except Exception:
		pass


def stall_report(now, limit=6):
	"""Lines for bots that have had a target but not fired for STALL_QUIET seconds.

	Called from frame_tick every STALL_INTERVAL seconds. Sorted by the longest
	silence first, capped at `limit` lines so a bad battle cannot flood the log.
	"""
	if not _enabled():
		return []
	out = []
	try:
		rows = []
		for key in list(_BOTS.keys()):
			row = _BOTS.get(key)
			if row is None or row.get('gen') != _STALL_GEN[0]:
				continue
			if row.get('last_target') is None:
				continue
			quiet = float(now or 0.0) - float(row.get('last_fire') or 0.0)
			if quiet < STALL_QUIET:
				continue
			rows.append((quiet, key, row))
		rows.sort(key=lambda r: (-r[0], r[1]))
		for quiet, key, row in rows[:limit]:
			out.append(('STALLPROBE id=%d team=%s tgt=%s dist=%.1f spd=%.1f stage=%s '
				'quiet=%.0fs frames=%d') % (
					key, row.get('team'), row.get('last_target'),
					float(row.get('dist') or 0.0), float(row.get('speed') or 0.0),
					row.get('stage'), quiet, int(row.get('frames') or 0)))
	except Exception:
		pass
	return out


def _fmt(value):
	if value is None:
		return '-'
	try:
		return '%.4f' % float(value)
	except Exception:
		return str(value)
