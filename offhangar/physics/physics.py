# -*- coding: utf-8 -*-
'''Uniform vehicle-physics laws for the offhangar mod (WoT 0.8.2 faithful).

ONE set of formulas, applied identically to the player and every bot. All
inputs come from the REAL vehicle descriptor (td.physics, built by
items/vehicles.pyc from the item_defs XML):

  weight            kg
  enginePower       W        (engine XML 'power' - reader converts)
  speedLimits       (fwd m/s, bwd m/s)
  terrainResistance (firm, medium, soft) multipliers
  specificFriction  mu*g in m/s^2 (chassis XML 0.07 x 9.81 = 0.6867 default)
  brakeForce        N (chassis XML kgf x 9.81; huge by design ->
                       braking is grip-limited, see BRAKE_GRIP_G)
  trackCenterOffset half track gauge in m (chassis topRightCarryingPoint.x)
  rotationSpeed     chassis, rad/s (reader applies radians())

This module must stay importable with nothing but the stdlib: it sits right
above paths.py at the bottom of the offhangar import graph.
'''

import math

# ---- Wargaming's own tuning, extracted from scripts/common/physics_shared.pyc
# (the module THIS client ships; it parameterizes the BigWorld rigid-body
# vehicle sim). Names kept 1:1 where they exist there. ----
G = 9.81
GRAVITY_FACTOR = 1.25          # WG: tanks live under 1.25 g 'arcade gravity'
GRAVITY = G * GRAVITY_FACTOR   # 12.26 m/s^2 - falls, slopes, all force laws
COHESION = 1.3                 # WG: physics.brakeFriction (brake/hold grip)
# Drive (pulling) traction coefficient - track-vs-ground grip for CLIMBING.
# Lower than the brake COHESION on purpose: a tank locks and holds a slope it
# cannot power up.
#
# CALIBRATION (do not 'simplify' this to atan(DRIVE_TRACTION)): the drive is cut
# when  DRIVE_TRACTION*g*cos(t) < g*sin(t) + rr,  so the EFFECTIVE limit is
#     tan(t_max) = DRIVE_TRACTION - rr/(g*cos t)
# and rolling resistance is NOT negligible: rr = specificFriction*GRAVITY_FACTOR
# = 0.6867*1.25 = 0.86 m/s^2 on firm ground, i.e. it eats ~3.7 deg. Measured:
#     0.43 -> 19.6 deg     0.47 -> 21.5 deg     0.54 -> 24.8 deg
# WG's own threshold is physics_shared.RISE_FRICTION_Y = 0.906 = cos(25.04 deg)
# (below that normal.y it applies RISE_FRICTION = 50.0, i.e. a wall), and 0.8.2
# chassis maxClimbAngle is 25 deg. So 0.54 reproduces the stock climb ceiling;
# lower values refuse slopes the map designers built to be drivable (Dragon Ridge
# became near-unplayable at 0.43 = 19.6 deg). Raise drive_traction in config to
# climb steeper.
DRIVE_TRACTION = 0.68
# Track-slip drag when rolling UP a grade past SLIP_THRESHOLD_TAN: extra
# deceleration = SLIP_DRAG * (tan(grade) - SLIP_THRESHOLD_TAN) * g. Bleeds the
# 'coast up a mountain on momentum' - real tracks slip and stop dead.
#
# The threshold must sit just above the EFFECTIVE climb limit (see DRIVE_TRACTION),
# NOT above the naive atan(DRIVE_TRACTION). Any gap between the two is a window
# where the drive is already cut but nothing bleeds speed - free momentum coasting.
# That window, not a high climb ceiling, was what let a tank rush the Serene Coast
# mountain: 0.47/0.50 left 21.5..26.6 deg (5.0 deg) wide open. 0.54/0.48 leaves
# 24.8..25.6 deg = 0.8 deg, so momentum can no longer cheat a slope the engine
# refuses.
# SLIP_DRAG shapes the ramp above the threshold. 20.0 was a cliff (20 m/s^2 by
# 28 deg) that killed every partial climb; 10.0 stays progressive - ~0.9 m/s^2 at
# 26 deg (a near-limit slope still feels dynamic), 11.9 at 30 deg, 27 at 35 deg.
SLIP_THRESHOLD_TAN = 0.48   # 25.6 deg: just above the 24.8 deg effective climb limit
SLIP_DRAG = 10.0
FORWARD_FRICTION = 0.07        # WG: physics.forwardFriction (rolling)
# WG scales enginePower by GRAVITY_FACTOR_SCALED (0.00125) against masses in
# tons (WEIGHT_SCALE 0.001): net effect, drive power acts 1.25x its SI value.
POWER_FACTOR = GRAVITY_FACTOR
BKWD_POWER_FRACTION = 1.0      # WG: reverse gets FULL engine power in 0.8.2
ANG_ACCELERATION_TIME = 0.05   # WG: hull traverse reaches full rate in 50 ms
SPEED_AFFECT_ROT_DECREASE = 0.0  # WG: driving speed does NOT slow the traverse
ROTATION_POWER_FRACTION = 0.85   # WG: rotation may use 85% of engine power
# WG slope-grip laws (physics_shared): track cohesion decays with the ground
# normal - cubically below normal.y 0.969 (~14 deg), an extra 0.25 beyond
# normal.y 0.72 (~44 deg), floored at 0.5. Per vehicle, chassis maxClimbAngle
# (25 deg across 0.8.2 -> minPlaneNormalY = cos = 0.906) is the HARD climb
# limit: steeper ground gives the engine no traction at all.
COH_DECAY_Y = 0.969
COH_DECAY_FACTOR = 5.78
COH_DECAY_POW = 3.0
COH_DECAY_BOUND = 0.5
SLOPE_COH_DECAY = 0.25
SLOPE_COH_DECAY_Y = 0.72
# ---- offline-model constants (no WG equivalent recoverable) ----
# Coasting (key released) = rolling resistance + this share of the full brake.
# At 0.35 the decel was rr + 0.35*grip ~ 6.5 m/s^2: 1.7 s and 9.5 m to stop from
# 40 km/h, which reads as a tank that will not settle. 0.55 -> ~9.7 m/s^2, i.e.
# ~1.1 s and 6 m. Tunable from config.json as coast_brake_share.
COAST_BRAKE_SHARE = 0.55
# Steering adds track-differential drag to the rolling resistance.
STEER_RESIST_MULT = 1.6
# Engine force F = P / max(|v|, ENGINE_MIN_V), capped by track cohesion.
ENGINE_MIN_V = 1.5
# Track scroll must stay strictly below fashion.maxMovement or the native
# scroll wraps to zero at exactly top speed.
SCROLL_CAP = 0.995
# Fall damage: free below this impact speed (~4 m drop under 1.25 g), then
# linear in the excess. 10 m fall ~ 17% HP, 20 m ~ 38%.
FALL_SAFE_SPEED = 10.0
FALL_DMG_PER_MS = 0.03
# Ramming hurts beyond this closing speed (m/s).
# Canonical WoT safe collision speed is ~1.5 m/s (~5.4 km/h).
RAM_SAFE_SPEED = 1.5
# Ram damage model constants. BOTH are a reconstruction - the 0.8.2 damage
# calculator was cell-side and never published - so they are tunable from
# config.json "physics_tuning" (ram_energy_const / ram_armor_absorb).
# RAM_ENERGY_CONST is the Cram of E = 0.5*Cram*mu*v_eff^2 (mu in tonnes).
# RAM_ARMOR_ABSORB is HP absorbed per mm of nominal armour at the contact,
# scaled by the spall liner. 1.25 was far too greedy: a 240 mm Maus ate 300 HP
# of ANY ram, so with equal 50 t hulls nothing scored below ~10 m/s closing -
# measured 0 damage in 51/51 ram events (session 02.10 15:01, median closing
# 2.0 m/s, max 7.0). See CONTEXT_AI §60.6 for the closing-speed x absorb table.
RAM_ENERGY_CONST = 0.58
RAM_ARMOR_ABSORB = 1.25
# Downhill slide on a slope the tracks cannot hold. The slide accelerates by the
# grip-excess g*(sin-coh*cos) but a track drag SLIDE_DRAG*v pulls it to a natural,
# terrain-dependent TERMINAL speed instead of ramping to a flat cap. SLIDE_MAX is
# just the hard safety ceiling for near-vertical faces.
SLIDE_MAX = 7.0
SLIDE_DRAG = 1.5
# Static hold limit (slope tangent) for a stationary hull, sideways AND fore/aft:
# the tracks perch only up to ~atan(SLIDE_HOLD_TAN); steeper ground slides off.
# Gentler than the brake COHESION (~52 deg) so a tank cannot cling to steep
# banks/cliffs. Deceleration (braking/coast) still uses the full COHESION grip.
SLIDE_HOLD_TAN = 0.50   # 26.6 deg static perch - must exceed the 24.8 deg climb limit, else a hull slides off the very slope it just drove up
# Kinetic (slipping) track drag while the hull slides BACK down a grade it could
# not climb - lower than the static hold so it does not hang mid-slope; it bleeds
# down to the foot at a controlled speed. Lower = slides faster/further.
SLIDE_KINETIC = 0.45
# Gravity overspeed: a steep descent / fall may carry the hull up to this
# multiple of its spec top speed, temporarily; OVERSPEED_DAMP (m/s^2) bleeds
# the surplus back to spec once the ground flattens.
OVERSPEED_MAX_FACTOR = 1.05   # gravity overspeed on a descent caps at 105% of spec
OVERSPEED_DAMP = 2.0
# Descent overspeed BUILDS UP gradually (m/s of surplus per sec, scaled by sin(grade))
# instead of snapping to the cap - the longer/steeper the descent, the more speed.
# ISOLATED to the overspeed clamp (>spec, descending only): does NOT touch the climb
# limit, slide-back, momentum-kill or flat driving. Higher = builds faster.
OVERSPEED_BUILD = 0.20


# ---- Live tuning: config.json "physics_tuning" can override these WITHOUT a
# recompile (only a client restart). Key -> module global. Values you would
# dial to match original WoT feel. See apply_tuning(). ----
_TUNABLE = {
	'gravity_factor':      'GRAVITY_FACTOR',
	'cohesion':            'COHESION',
	'drive_traction':      'DRIVE_TRACTION',
	'slip_drag':           'SLIP_DRAG',
	'slip_threshold_tan':  'SLIP_THRESHOLD_TAN',
	'power_factor':        'POWER_FACTOR',
	'bkwd_power_fraction': 'BKWD_POWER_FRACTION',
	'traverse_accel_time': 'ANG_ACCELERATION_TIME',
	'traverse_speed_cost': 'SPEED_AFFECT_ROT_DECREASE',
	'coast_brake_share':   'COAST_BRAKE_SHARE',
	'steer_resist_mult':   'STEER_RESIST_MULT',
	'slide_max':           'SLIDE_MAX',
	'slide_drag':          'SLIDE_DRAG',
	'slide_hold_tan':      'SLIDE_HOLD_TAN',
	'slide_kinetic':       'SLIDE_KINETIC',
	'overspeed_max_factor': 'OVERSPEED_MAX_FACTOR',
	'overspeed_damp':      'OVERSPEED_DAMP',
	'overspeed_build':     'OVERSPEED_BUILD',
	'slope_coh_decay':     'SLOPE_COH_DECAY',
	'slope_coh_decay_y':   'SLOPE_COH_DECAY_Y',
	'coh_decay_bound':     'COH_DECAY_BOUND',
	'fall_safe_speed':     'FALL_SAFE_SPEED',
	'fall_dmg_per_ms':     'FALL_DMG_PER_MS',
	'ram_safe_speed':      'RAM_SAFE_SPEED',
	'ram_energy_const':    'RAM_ENERGY_CONST',
	'ram_armor_absorb':    'RAM_ARMOR_ABSORB',
}


def apply_tuning(overrides):
	'''Overlay config.json "physics_tuning" onto the module constants. Returns
	the list of applied "key=value" strings (for logging). GRAVITY is derived
	from G*GRAVITY_FACTOR, so it is recomputed after any override.'''
	applied = []
	g = globals()
	if isinstance(overrides, dict):
		for k, gname in _TUNABLE.items():
			if k in overrides:
				try:
					g[gname] = float(overrides[k])
					applied.append('%s=%s' % (k, overrides[k]))
				except (TypeError, ValueError):
					pass
	g['GRAVITY'] = G * GRAVITY_FACTOR
	# brakeDecel default is COHESION*GRAVITY, frozen at load - refresh it so a
	# cohesion/gravity override reaches the no-brakeForce fallback too.
	_DEFAULTS['brakeDecel'] = COHESION * GRAVITY
	return applied


def snapshot(p, v, omega, throttle, slope_pitch, airborne, slide_speed, tank='',
             ground_kmh=None, pitch_deg=None, roll_deg=None, vert_ms=None,
             deflect=None, dy=None, slide_slope=None, dy_tick=None, y_src=None, terr_spread=None):
	'''One telemetry row: the live physics state + the force balance that
	decides hold/climb/slide, PLUS the observable extras that the pure force
	numbers miss - ACTUAL over-ground speed vs commanded v (catches wall-slide
	sideways drift / clipping), hull pitch & roll (catches orientation glitches),
	vertical speed & height delta (catches float/sink/teleport), and whether the
	wall-slide deflection fired this frame. Returns (name, value) pairs.'''
	grip = _grip_decel(p, slope_pitch)
	grav_a = GRAVITY * math.sin(slope_pitch)
	ef = engine_force(p, v, throttle, slope_pitch) / p['mass'] if throttle else 0.0
	rows = [
		('tank', tank),
		('mass_kg', round(p['mass'], 0)),
		('power_hp', round(p['powerW'] / 735.49875, 0)),
		('pw_ratio_hp_t', round((p['powerW'] / 735.49875) / max(p['mass'] / 1000.0, 0.01), 1)),
		('speedFwd_kmh', round(p['speedFwd'] * 3.6, 1)),
		('v_kmh', round(v * 3.6, 2)),
		('throttle', throttle),
		('slope_deg', round(math.degrees(slope_pitch), 1)),
		('grip_ms2', round(grip, 2)),
		('grav_along_ms2', round(grav_a, 2)),
		('hold_margin_ms2', round(grip - abs(grav_a), 2)),   # <0 => slides
		('drive_accel_ms2', round(ef, 2)),
		('turn_degs', round(math.degrees(omega), 1)),
		('rotSpd_max_degs', round(math.degrees(p['rotSpd']), 1)),
		('airborne', 1 if airborne else 0),
		('slide_ms', round(slide_speed, 2)),
		('terrain_r0', p['terrainResist'][0]),
		('spec_friction', round(p['specificFriction'], 3)),
		('brake_ms2', round(p['brakeDecel'], 2)),
	]
	# Observable extras (only when supplied by the caller):
	if ground_kmh is not None:
		# real over-ground speed; a big gap vs v_kmh = sideways wall-slide/clip
		rows.append(('ground_kmh', round(ground_kmh, 2)))
		rows.append(('drift_kmh', round(ground_kmh - abs(v * 3.6), 2)))
	if pitch_deg is not None:
		rows.append(('hull_pitch_deg', round(pitch_deg, 1)))
	if roll_deg is not None:
		rows.append(('hull_roll_deg', round(roll_deg, 1)))
	if vert_ms is not None:
		rows.append(('vert_ms', round(vert_ms, 2)))
	if dy is not None:
		rows.append(('dy_m', round(dy, 3)))   # per-tick height change (float/sink/teleport)
	if slide_slope is not None:
		# Raw ground gradient (a tangent) that DRIVES the slide gate - it includes
		# the lateral/roll component and gets no spike rejection. slope_deg above is
		# the hull-axis pitch the FORCE model actually integrated. When these two
		# disagree, a 'flat ground + sliding' row is the two probes differing, NOT a
		# physics contradiction. Log both so the CSV is honest about which is which.
		rows.append(('slide_slope_deg', round(math.degrees(math.atan(slide_slope)), 1)))
	if dy_tick is not None:
		rows.append(('dy_tick_m', round(dy_tick, 3)))   # biggest single 60fps-tick height jump this window
	if y_src is not None:
		rows.append(('y_src', y_src))                    # which path set veh_pos.y at that jump
	if terr_spread is not None:
		rows.append(('terr_spread_m', round(terr_spread, 2)))   # height spread of the 4 footprint samples = edge sharpness
	if deflect is not None:
		rows.append(('wall_deflect', 1 if deflect else 0))
	return rows


_DEFAULTS = {
	'mass': 5730.0,
	'powerW': 45.0 * 735.49875,
	'speedFwd': 32.0 / 3.6,
	'speedBwd': 12.0 / 3.6,
	'rotSpd': math.radians(38.0),
	'terrainResist': (1.1, 1.4, 2.6),
	'specificFriction': 0.6867,
	'brakeDecel': COHESION * GRAVITY,
	'trackCenter': 1.5,
	'minPlaneNormalY': math.cos(math.radians(25.0)),
}


def derive_params(td):
	'''Real per-vehicle parameter set from a VehicleDescr. Every consumer
	(player tick, each bot) MUST source its numbers from here - this is the
	single place that knows the units of td.physics.'''
	p = dict(_DEFAULTS)
	try:
		tdp = getattr(td, 'physics', None) or {}
	except Exception:
		tdp = {}
	try:
		if 'weight' in tdp:
			p['mass'] = float(tdp['weight'])
		if 'enginePower' in tdp:
			p['powerW'] = float(tdp['enginePower'])
		if 'speedLimits' in tdp:
			p['speedFwd'] = float(tdp['speedLimits'][0])
			p['speedBwd'] = float(tdp['speedLimits'][1])
		if 'terrainResistance' in tdp:
			tr = tdp['terrainResistance']
			p['terrainResist'] = (float(tr[0]), float(tr[1]), float(tr[2]))
		if 'specificFriction' in tdp:
			p['specificFriction'] = float(tdp['specificFriction'])
		if 'brakeForce' in tdp:
			# WG: brake grip = COHESION (1.3) x normal force; the descriptor's
			# brakeForce (kgf x g) only ever binds for absurdly light hulls.
			p['brakeDecel'] = min(COHESION * GRAVITY,
			                      float(tdp['brakeForce']) / max(p['mass'], 1.0))
		if 'trackCenterOffset' in tdp:
			tc = abs(float(tdp['trackCenterOffset']))
			if 0.3 <= tc <= 3.0:
				p['trackCenter'] = tc
		if 'minPlaneNormalY' in tdp:
			# = cos(chassis maxClimbAngle); the vehicle's hard climb limit
			p['minPlaneNormalY'] = float(tdp['minPlaneNormalY'])
	except Exception:
		pass
	try:
		ch = getattr(td, 'chassis', None)
		if ch is not None and 'rotationSpeed' in ch:
			raw = float(ch['rotationSpeed'])
			# reader stores radians; tolerate a raw-degrees dump
			p['rotSpd'] = math.radians(raw) if raw > 6.3 else raw
	except Exception:
		pass
	return p


def engine_force(p, v, throttle, slope_pitch=0.0):
	'''F = P_eff / max(|v|, v_min), TRACTION-capped. P_eff = powerW x
	POWER_FACTOR (WG scales engine power by GRAVITY_FACTOR). Reverse gets
	BKWD_POWER_FRACTION (1.0 in 0.8.2 = full power).

	Drive force can never exceed the DRIVE TRACTION on the current slope:
	fmax = DRIVE_TRACTION * m * g * cos(theta) = drive traction x normal force.
	DRIVE_TRACTION (0.54) is the track-vs-ground grip for PULLING, distinct
	from and lower than the brake/hold COHESION (1.3): a tank can lock its
	tracks and hold a slope it cannot power UP. This makes the climb limit
	emergent at the stock ~25 deg boundary after rolling resistance: beyond it the
	grip-limited drive can't beat m*g*sin(theta), so the hull stalls - it does NOT
	climb 40-50 deg walls the way a cohesion(1.3) cap wrongly allowed.'''
	if throttle == 0:
		return 0.0
	pw = p['powerW'] * POWER_FACTOR
	if throttle < 0:
		pw *= BKWD_POWER_FRACTION
	f = pw / max(abs(v), ENGINE_MIN_V)
	ny = math.cos(slope_pitch)
	fmax = DRIVE_TRACTION * p['mass'] * GRAVITY * (ny if ny > 0.1 else 0.1)
	if f > fmax:
		f = fmax
	# The climb LIMIT is not a hard gate here (that oscillated at the boundary); it
	# lives in longitudinal_step as an rr-aware limit that cuts the drive on a grade
	# the tracks cannot pull up. engine_force stays purely power/traction limited.
	return f * throttle


def rolling_resist_force(p, terrainIdx=0, steering=False):
	'''Rolling drag = mu * N. The descriptor bakes mu*9.81 into
	specificFriction; the WG sim applies mu against the 1.25 g normal force,
	so the stored value gets the GRAVITY_FACTOR back. terrainResistance
	scales it; steering adds track-differential drag.'''
	f = p['mass'] * p['specificFriction'] * GRAVITY_FACTOR * p['terrainResist'][terrainIdx]
	if steering:
		f *= STEER_RESIST_MULT
	return f


def brake_force(p, active, terrainIdx=0, slope_pitch=0.0):
	'''Braking is GRIP-limited, like drive traction: the locked tracks can
	only hold cohesion x normal force, and the normal force shrinks with slope
	(cos theta) while cohesion decays on steep ground. So a hull braking on a
	slope past the grip limit CANNOT hold and slides - the same ~50 deg limit
	as the lateral fall-line slip, kept consistent on purpose.
	active=True: opposite-throttle / hold lock-up. active=False: coast.'''
	ny = math.cos(slope_pitch)
	grip_decel = slope_cohesion(ny) * GRAVITY * (ny if ny > 0.1 else 0.1)
	brake = p['brakeDecel'] if p['brakeDecel'] < grip_decel else grip_decel
	if active:
		return p['mass'] * brake
	return p['mass'] * (p['specificFriction'] * GRAVITY_FACTOR * p['terrainResist'][terrainIdx]
	                    + COAST_BRAKE_SHARE * brake)


def _grip_decel(p, slope_pitch):
	'''Max track hold as a deceleration (m/s^2): cohesion x normal-force,
	both shrinking with slope. This is the single grip limit that caps drive
	traction, braking AND the static parked hold, so climb/brake/slide all
	share one consistent ~50 deg threshold.'''
	ny = math.cos(slope_pitch)
	return slope_cohesion(ny) * GRAVITY * (ny if ny > 0.1 else 0.1)


def longitudinal_step(p, v, throttle, steering, slope_pitch, dt,
                      airborne=False, terrainIdx=0, handbrake=False):
	'''One integration step of forward (along-hull) speed. Returns the new v.
	slope_pitch: fore/aft ground pitch (BigWorld: nose-up negative).

	Gravity along the slope ALWAYS acts when grounded (even parked at v=0), so
	a hull pointed up/down a slope too steep for its tracks slides off from a
	standstill - the fix for "stuck where it should slide". The tracks resist
	up to the grip limit (_grip_decel): below it they hold, past it the excess
	drives the slide. The climb limit is emergent (grip-capped engine force vs
	slope gravity), so there is no dead can-neither-climb-nor-slide band.'''
	if airborne:
		return v  # no track grip in the air; horizontal momentum kept as-is

	grav_a = GRAVITY * math.sin(slope_pitch)      # signed accel along hull (+fwd downhill)
	if handbrake and not airborne:
		# Locked tracks: full grip opposes any motion, and at a standstill the hull
		# holds unless the slope beats the tracks outright. Grip-limited like every
		# other brake here, so a cliff still wins - it is a parking brake, not glue.
		_hb_grip = _grip_decel(p, slope_pitch)
		if abs(v) < 0.05:
			return 0.0 if abs(grav_a) <= _hb_grip else v + (grav_a - (_hb_grip if grav_a > 0.0 else -_hb_grip)) * dt
		_hb_a = grav_a - (_hb_grip if v > 0.0 else -_hb_grip)
		_hb_nv = v + _hb_a * dt
		if (v > 0.0) != (_hb_nv > 0.0):
			return 0.0            # braked through zero: stop, do not crawl backwards
		return _hb_nv
	grip = _grip_decel(p, slope_pitch)            # max track hold, m/s^2
	rr = rolling_resist_force(p, terrainIdx, steering) / p['mass']

	if throttle != 0:
		# Drive: engine force (power/traction limited) + slope gravity.
		_ef = engine_force(p, v, throttle, slope_pitch) / p['mass']
		# TRUE rolling-drag-aware climb limit: powering INTO a grade the pulling tracks
		# cannot overcome (peak drive accel < gravity-along + rolling drag). There the
		# drive is CUT and the hold dropped to the slide limit, so the hull slides BACK
		# instead of the engine pinning it just under a fixed gate (the 'stuck at the
		# foot of a descent' bug). Replaces the hard minPlaneNormalY gate the hull used
		# to oscillate across.
		_ny_c = math.cos(slope_pitch)
		_max_climb = DRIVE_TRACTION * GRAVITY * (_ny_c if _ny_c > 0.1 else 0.1)
		_cant_climb = throttle * grav_a < 0.0 and _max_climb < abs(grav_a) + rr * 0.45
		if _cant_climb:
			_ef = 0.0                                  # can't power up -> cut drive; momentum does not drive the hull up a too-steep grade
		accel = _ef + grav_a
		# Rolling drag ramped smoothly through v=0 - a hard abs(v)>0.01 threshold made
		# the hull judder / stick in a limit cycle wherever engine and gravity nearly
		# balanced (e.g. at the foot of a climb).
		_rrf = v / 0.08
		if _rrf > 1.0:
			_rrf = 1.0
		elif _rrf < -1.0:
			_rrf = -1.0
		accel -= rr * _rrf
		# (track-slip drag now runs for BOTH throttle states - see below)
		if _cant_climb:
			# Can't climb -> tracks slip; a LOW kinetic drag opposes the involuntary
			# slide-back so the hull bleeds down the grade at a controlled speed and does
			# NOT hang mid-slope. It is NOT held (no auto-brake) - it slides to the foot
			# where the grade becomes climbable again, carrying momentum onto the flat.
			if abs(v) > 0.05:
				_kin = SLIDE_KINETIC * GRAVITY * (_ny_c if _ny_c > 0.1 else 0.1)
				accel += _kin if v < 0.0 else -_kin
		else:
			# Auto-brake: intentional reverse, CLAMPED so grip never overshoots v past 0
			# in one tick (the raw +/-grip impulse limit-cycled ~1 km/h around v=0).
			if (throttle > 0 and v < -0.1) or (throttle < 0 and v > 0.1):
				_need = -v / dt - accel
				accel += _need if abs(_need) < grip else (grip if _need > 0.0 else -grip)
	else:
		# Parked / coasting: static grip tries to hold against slope gravity.
		if abs(v) < 0.02:
			_ny_h = math.cos(slope_pitch)
			_hold = SLIDE_HOLD_TAN * GRAVITY * (_ny_h if _ny_h > 0.1 else 0.1)   # static perch limit ~32 deg
			if abs(grav_a) <= _hold:
				return 0.0                        # tracks hold - no creep on ordinary hills
			accel = grav_a - (_hold if grav_a > 0.0 else -_hold)   # slides off a too-steep parked slope
		else:
			# rolling + partial grip brake oppose the motion; gravity still acts
			resist = rr + COAST_BRAKE_SHARE * grip
			accel = grav_a - (resist if v > 0.0 else -resist)

	# TRACK-SLIP DRAG: rolling UP a grade steeper than the tracks can pull, they
	# slip and momentum bleeds far faster than gravity alone would take it.
	# This used to sit inside the throttle != 0 branch only, so releasing the
	# throttle removed it entirely: build speed on the flat, let go, and coast
	# straight up a slope the engine flatly refuses. It must apply whenever the
	# hull is moving INTO the grade, powered or not.
	_grade = -slope_pitch if v > 0.0 else slope_pitch   # >0 = moving uphill
	if not airborne and abs(v) > 0.5 and _grade > 0.0:
		_tan = math.tan(_grade)
		if _tan > SLIP_THRESHOLD_TAN:
			_slip = SLIP_DRAG * (_tan - SLIP_THRESHOLD_TAN) * GRAVITY
			accel -= _slip if v > 0.0 else -_slip
	
	nv = v + accel * dt

	# Coast/brake must not yank the hull backwards through zero into a reverse
	# crawl (it should settle at rest) - but only when gravity itself can't hold
	# a slide going (gentle ground); on a steep slope let it cross into reverse.
	if throttle == 0 and abs(grav_a) <= grip and v != 0.0 and (v > 0.0) != (nv > 0.0):
		nv = 0.0

	# Speed limit with GRAVITY OVERSPEED: the engine can never push past the
	# spec limit, but gravity (steep descent / a fall's downhill momentum) may
	# carry the hull FASTER, temporarily, up to OVERSPEED_MAX_FACTOR x the limit.
	# Above the limit the engine stops contributing and an overspeed drag bleeds
	# the excess back to spec on flatter ground - the WoT 'downhill overspeed'
	# feel. Airborne already returned early, so a fall keeps its momentum and
	# this bleed only re-engages once the hull is back on the ground.
	_dir = 1.0 if nv >= 0.0 else -1.0
	_lim = p['speedFwd'] if nv >= 0.0 else p['speedBwd']
	if abs(nv) > _lim:
		# The overspeed drag ALWAYS bleeds the surplus back toward spec (rolling +
		# OVERSPEED_DAMP), so leaving a descent onto flat/uphill ground eases down
		# instead of a 1-tick snap to the limit. Gravity down THIS way is what lets
		# the surplus PERSIST up to the cap; without it the accel step adds no new
		# surplus, so the bleed just decays what is there, smoothly.
		_cap = _lim * (OVERSPEED_MAX_FACTOR - 1.0)
		# Build the surplus from the PREVIOUS speed at a slope-scaled rate so a descent
		# gains speed gradually toward the cap (not a 1-tick jump); bleed it off once the
		# ground stops helping.
		_prev_ex = abs(v) - _lim
		if _prev_ex < 0.0:
			_prev_ex = 0.0
		if (grav_a * _dir) > 0.05:
			_excess = _prev_ex + OVERSPEED_BUILD * math.sin(abs(slope_pitch)) * dt
		else:
			_excess = _prev_ex - (rr + OVERSPEED_DAMP) * dt
		if _excess < 0.0:
			_excess = 0.0
		if _excess > _cap:
			_excess = _cap
		nv = _dir * (_lim + _excess)
	return nv


def traverse_step(p, omega, steer_dir, v, dt, terrainIdx=0, drive_intent=0.0):
	'''One integration step of hull rotation speed (rad/s). WG 0.8.2: driving
	speed does NOT slow the traverse (SPEED_AFFECT_ROT_DECREASE = 0.0) and the
	rate ramps to full in ANG_ACCELERATION_TIME (50 ms). Medium/soft ground
	scales by the terrain-resistance ratio (the sim couples the limit to
	terrainResistance).

	Steering sign follows the explicit drive command, never signed velocity.
	A tank sliding backwards while W/forward is held must retain forward
	steering; S or reverse cruise flips steering immediately, even while the
	hull still has forward momentum. Neutral rolling keeps the forward sign.'''
	speed_ratio = abs(v) / max(p['speedFwd'], 0.1)
	rot_mod = 1.0 / (1.0 + speed_ratio * SPEED_AFFECT_ROT_DECREASE)
	ter_mod = p['terrainResist'][0] / p['terrainResist'][terrainIdx]
	max_rot = p['rotSpd'] * rot_mod * ter_mod

	# A tracked vehicle yaws the opposite way for the same steering input while
	# intentionally reversing. Velocity is not intent: gravity or a collision can
	# push the hull backwards while the driver is still commanding forward.
	intent_sign = -1.0 if drive_intent < 0.0 else 1.0
	target = steer_dir * intent_sign * max_rot
	diff = target - omega
	ramp = max_rot / ANG_ACCELERATION_TIME
	if abs(diff) < ramp * dt:
		omega = target
	else:
		omega += ramp * dt * (1 if diff > 0 else -1)
	if steer_dir == 0 and abs(omega) < 0.01:
		omega = 0.0
	return omega


def track_scroll(p, v, omega):
	'''Per-track surface speeds for WGVehicleFashion.movementInfo:
	v_track = v -/+ omega * halfGauge, clamped strictly below maxMovement.'''
	tls = v - omega * p['trackCenter']
	trs = v + omega * p['trackCenter']
	cap = p['speedFwd'] * SCROLL_CAP
	if tls > cap: tls = cap
	elif tls < -cap: tls = -cap
	if trs > cap: trs = cap
	elif trs < -cap: trs = -cap
	return tls, trs


def slope_cohesion(ny):
	'''Effective track cohesion for a ground normal.y (WG physics_shared slope
	decay): full COHESION until normal.y drops below COH_DECAY_Y (~14 deg), then a
	cubic decay COH_DECAY_FACTOR*(COH_DECAY_Y-ny)**COH_DECAY_POW, plus an extra
	SLOPE_COH_DECAY step below SLOPE_COH_DECAY_Y (~44 deg), floored at
	COH_DECAY_BOUND. The cubic term smooths the old single 44 deg grip cliff so
	grip falls off gradually on steepening ground, as WG does.'''
	coh = COHESION
	if ny < COH_DECAY_Y:
		coh -= COH_DECAY_FACTOR * (COH_DECAY_Y - ny) ** COH_DECAY_POW
	if ny < SLOPE_COH_DECAY_Y:
		coh -= SLOPE_COH_DECAY
	return coh if coh > COH_DECAY_BOUND else COH_DECAY_BOUND


def slope_slide_speed(cur, slope_tan, dt):
	'''WG-faithful passive slope slide along the fall line. The tracks HOLD the
	hull while tan(theta) <= effective cohesion (~46 deg with slope decay,
	~52 deg on firm ground) - a parked tank does NOT creep down ordinary hills.
	Past the grip limit the hull accelerates down-slope by the EXCESS
	g*(sin - coh*cos), regardless of throttle: engine force acts along the hull
	heading (handled by longitudinal_step), it cannot stop the LATERAL fall-line
	slip. The caller applies only the cross-heading component so the two never
	double-count. Below the grip limit the residual bleeds off fast.'''
	theta = math.atan(slope_tan)
	# Lateral fall-line hold: tracks resist SIDEWAYS slip only up to SLIDE_HOLD_TAN
	# (gentler than the brake COHESION), so a hull cannot perch across a steep bank
	# it is only leaning on. Past it, slip down the excess.
	if slope_tan <= SLIDE_HOLD_TAN:
		cur -= COHESION * GRAVITY * dt   # holds: kill any residual slide fast
		return cur if cur > 0.0 else 0.0
	# accelerate by the grip-excess, minus a track drag proportional to slide
	# speed -> settles at a natural terrain-dependent terminal, not a flat cap.
	cur += (GRAVITY * (math.sin(theta) - SLIDE_HOLD_TAN * math.cos(theta)) - SLIDE_DRAG * cur) * dt
	if cur < 0.0:
		cur = 0.0
	elif cur > SLIDE_MAX:
		cur = SLIDE_MAX
	return cur


def fall_damage(maxHealth, impact_speed):
	'''HP cost of a landing. 0 below FALL_SAFE_SPEED (~4 m drop).'''
	iv = abs(impact_speed)
	if iv <= FALL_SAFE_SPEED:
		return 0
	return int(maxHealth * (iv - FALL_SAFE_SPEED) * FALL_DMG_PER_MS)


def ram_facing_dot(rdx, rdz, other_yaw):
	'''Directional ramming: dot(rammer->target, target forward), world XZ plane.

	rdx/rdz is what the call site computes as `_rdx = ox - x; _rdz = oz - z`
	(offline_battle.py:13689) = the vector FROM the rammer TO the target, and the
	target's forward vector is (sin(yaw), cos(yaw)) - the same convention the drive
	code uses (offline_battle.py:6288). Therefore:
	  fd = -1.0  the target's FRONT faces the rammer (head-on),
	  fd = +1.0  the rammer is behind the target (rear hit),
	  fd =  0.0  pure T-bone on the side.
	MIND THE SIGN: an earlier version of this docstring claimed the opposite and
	described rdx/rdz as target->rammer, contradicting the call site. ram_damage's
	directional multipliers are written for THIS convention (fd = -1 gives the
	target other_mult 1.45 and the rammer self_mult 0.50), but ram_nominal_armor's
	thresholds (> 0.3 -> pa[0], < -0.3 -> pa[2]) assume the OPPOSITE one, so the
	caller must pass -fd when picking the plate. That is what offline_battle's
	`ram_direction_fix` flag does; without it a head-on ram is absorbed using the
	target's REAR plate value and a rear ram using its FRONT plate.'''
	rdist = math.sqrt(rdx * rdx + rdz * rdz)
	if rdist <= 0.001:
		return 0.0
	fx = math.sin(other_yaw)
	fz = math.cos(other_yaw)
	return (rdx * fx + rdz * fz) / rdist


def ram_nominal_armor(td, facing_dot=0.0, is_rammer=False):
	'''Extract nominal armor thickness in mm from vehicle descriptor for ramming.
	is_rammer: if True, this vehicle is the rammer driving forward (front plate hits).
	facing_dot: dot product of the target's forward vector with the direction TO the
	rammer (i.e. target->rammer), which is the NEGATIVE of what ram_facing_dot
	returns:
	  > 0.3: front armor (pa[0])
	  < -0.3: rear armor (pa[2])
	  else: side armor (pa[1])
	pa is hull['primaryArmor'], a 3-tuple read by _readPrimaryArmor
	(dev_ref/stock_082/common/items/vehicles.py:1740-1760); the (front, side, rear)
	order is what the whole mod assumes (bot_ai._component_armor) but is NOT proven
	from stock, so the RAMPROBE log line dumps the triple next to a known tank.
	'''
	try:
		if td is None:
			return 50.0
		hull = getattr(td, 'hull', None)
		if hull is None or 'primaryArmor' not in hull:
			return 50.0
		pa = hull['primaryArmor']
		if is_rammer:
			return float(pa[0])
		if facing_dot > 0.3:
			return float(pa[0])
		elif facing_dot < -0.3:
			return float(pa[2])
		else:
			return float(pa[1])
	except Exception:
		return 50.0


def vehicle_spall_factor(td):
	'''Returns spall liner absorption multiplier (1.0 = none, 1.2-1.5 = installed).'''
	try:
		if td is None:
			return 1.0
		try:
			_af = float(getattr(td, 'miscAttrs', {}).get('antifragmentationLiningFactor', 1.0) or 1.0)
			if _af > 1.01:
				return _af
		except Exception:
			pass
		opt = list(getattr(td, 'optionalDevices', None) or ()) + list(getattr(td, '_offh_opt_devices', None) or ())
		for dev in opt:
			if dev is not None:
				name = str(getattr(dev, 'name', '') or getattr(getattr(dev, 'descriptor', None), 'name', '') or dev).lower()
				if 'antifragmentation' in name or 'lining' in name:
					return 1.30
	except Exception:
		pass
	return 1.0


def vehicle_suspension_factor(td):
	'''Returns ram/fall suspension damage multiplier (0.70 when enhanced suspension is installed, else 1.0).'''
	try:
		if td is None:
			return 1.0
		opt = list(getattr(td, 'optionalDevices', None) or ()) + list(getattr(td, '_offh_opt_devices', None) or ())
		for dev in opt:
			if dev is not None:
				name = str(getattr(dev, 'name', '') or getattr(getattr(dev, 'descriptor', None), 'name', '') or dev).lower()
				if any(k in name for k in ('suspension', 'torsion', 'springs', 'levers', 'belleville', 'christie')):
					return 0.70
	except Exception:
		pass
	return 1.0


def ram_damage(closing_speed, mass_self, mass_other, facing_dot=0.0,
               armor_self=None, armor_other=None,
               spall_self=1.0, spall_other=1.0):
	'''(dmg_to_other, dmg_to_self) for a ram at the given closing speed.
	Canonical World of Tanks collision damage mechanics:
	1. Kinetic energy pool: E = 0.5 * Cram * (m1 * m2 / (m1 + m2)) * (v_rel - v_safe)^2
	   where masses are in metric tons and v_safe = 1.5 m/s (~5.4 km/h).
	   Cram = RAM_ENERGY_CONST (config "ram_energy_const"), a reconstruction.
	2. Mass-inversed damage distribution:
	   Each vehicle takes damage proportional to the OTHER vehicle's mass:
	   share_i = E * (m_other / (m_self + m_other)).
	   Heavier tank takes proportionally less, lighter tank takes bulk of damage.
	3. Directional contact multiplier based on facing_dot (front/side/rear).
	4. Armor mitigation at contact surface:
	   absorbed = RAM_ARMOR_ABSORB * armor * spall_factor  (config "ram_armor_absorb").
	   dmg = max(0, int(round(raw_share - absorbed))).
	5. Spall liner (antifragmentationLining) boosts armor absorption (+30%).
	'''
	rel = abs(closing_speed)
	v_safe = max(float(RAM_SAFE_SPEED), 2.2)
	if rel <= v_safe:
		return 0, 0

	v_eff = rel - v_safe
	# Smooth low-speed ramp between 2.2 m/s (~8 km/h) and 5.0 m/s (~18 km/h)
	low_speed_ramp = min(1.0, v_eff / 2.8)

	# Convert mass from kg to metric tons
	ms = max(1.0, float(mass_self) / 1000.0 if float(mass_self) > 500.0 else float(mass_self))
	mo = max(1.0, float(mass_other) / 1000.0 if float(mass_other) > 500.0 else float(mass_other))

	# Reduced mass (metric tons)
	mu = (ms * mo) / (ms + mo)

	# Calibrated canonical WoT kinetic energy constant Cram (tunable).
	total_energy = 0.5 * RAM_ENERGY_CONST * mu * (v_eff * v_eff) * low_speed_ramp

	# Mass-inverse damage shares
	share_other = total_energy * (ms / (ms + mo))
	share_self = total_energy * (mo / (ms + mo))

	# Directional contact adjustment
	fd = max(-1.0, min(1.0, float(facing_dot)))
	other_mult = max(0.65, min(1.45, 1.0 - 0.25 * fd + 0.20 * max(0.0, -fd)))
	self_mult = max(0.50, min(1.35, 1.0 + 0.20 * fd - 0.30 * max(0.0, -fd)))

	raw_other = share_other * other_mult
	raw_self = share_self * self_mult

	# Armor absorption (RAM_ARMOR_ABSORB * armor * spall_factor), tunable.
	if armor_other is not None:
		absorb_other = RAM_ARMOR_ABSORB * max(0.0, float(armor_other)) * max(1.0, float(spall_other))
	else:
		absorb_other = 0.0

	if armor_self is not None:
		absorb_self = RAM_ARMOR_ABSORB * max(0.0, float(armor_self)) * max(1.0, float(spall_self))
	else:
		absorb_self = 0.0

	dmg_other = max(0, int(round(raw_other - absorb_other)))
	dmg_self = max(0, int(round(raw_self - absorb_self)))

	return dmg_other, dmg_self
