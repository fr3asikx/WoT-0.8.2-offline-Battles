# -*- coding: utf-8 -*-
"""Temporary terrain-streaming gate for canonical native bot spawns.

The 0.8.2 collision query only sees chunks currently streamed by the client.
An authority must therefore prove every canonical spawn against live collision
before it creates native bodies.  This helper widens the engine's projection
range for the battle, freezes the accepted lineup, and restores the exact
original range on explicit teardown. Remote native bodies do not pin terrain
chunks themselves, so restoring immediately after activation would allow their
collision space to be unloaded again.

Keep this module free of BigWorld imports so its fail-closed state machine can
be tested outside the legacy client.
"""

import math


WAITING_SUPPORT = 'waiting_support'
PLACEMENT_READY = 'placement_ready'
COMPLETE = 'complete'
FAILED = 'failed'


def _finite(value):
	try:
		value = float(value)
		return value == value and abs(value) != float('inf')
	except Exception:
		return False


def _coord(value, index):
	try:
		return float(value[index])
	except Exception:
		return float((value.x, value.y, value.z)[index])


def coverage_target_from_bounds(bounds):
	"""Return one camera-invariant range covering the playable rectangle.

	The 0.8.2 chunk manager derives its load path from the projection range.
	The bounds diagonal divided by sqrt(2) covers the full playable rectangle
	independently of where the camera later moves under the 0.8.2 load-path rule.
	"""
	if not isinstance(bounds, (tuple, list)) or len(bounds) != 4:
		raise ValueError('invalid playable bounds')
	values = tuple(float(value) for value in bounds)
	if not all(_finite(value) for value in values):
		raise ValueError('non-finite playable bounds')
	width = values[2] - values[0]
	depth = values[3] - values[1]
	if width <= 0.0 or depth <= 0.0:
		raise ValueError('empty playable bounds')
	return math.sqrt(width * width + depth * depth) + 200.0


class SpawnStreamingBootstrap(object):
	"""Hold native placement behind live support for one frozen lineup."""

	def __init__(self, projection, jobs, origin, probe, now, timeout,
			margin=0.0, height_tolerance=2.5, coverage_target=None):
		self._projection = projection
		self._jobs = list(tuple(job) for job in (jobs or ()))
		self._probe = probe
		self._supported = set()
		self._restored = False
		self._changed = False
		self.placement_ready = False
		self.failure_reason = None
		self._phase = WAITING_SUPPORT
		self._started_at = float(now)
		self._first_poll_at = None
		self._height_tolerance = max(0.0, float(height_tolerance))
		self._deadline = float(now) + max(0.0, float(timeout))
		self._original_far_plane = None

		try:
			self._original_far_plane = float(projection.farPlane)
			target = self._original_far_plane
			if coverage_target is not None:
				coverage_target = float(coverage_target)
				if not _finite(coverage_target) or coverage_target < 0.0:
					raise ValueError('invalid streaming coverage target')
				target = max(target, coverage_target)
			try:
				origin_x = _coord(origin, 0)
				origin_z = _coord(origin, 2)
				extra = max(200.0, float(margin))
				for job in self._jobs:
					dx = float(job[3]) - origin_x
					dz = float(job[5]) - origin_z
					target = max(target, math.sqrt(dx * dx + dz * dz) + extra)
			except Exception:
				pass
			if not _finite(target):
				raise ValueError('non-finite streaming range')
			if target > self._original_far_plane + 0.001:
				self._changed = True
				projection.farPlane = target
				actual = float(projection.farPlane)
				if not _finite(actual) or actual + 0.001 < target:
					raise ValueError('streaming range expansion was not applied')
		except Exception:
			self._fail('projection_error')

	@property
	def jobs(self):
		return tuple(self._jobs)

	def _restore(self):
		if self._restored:
			return True
		if not self._changed or self._original_far_plane is None:
			self._restored = True
			return True
		try:
			self._projection.farPlane = self._original_far_plane
		except Exception:
			pass
		self._restored = True
		return True

	def _fail(self, reason):
		if self._phase in (FAILED, COMPLETE):
			return self._restored
		had_placement_ready = self.placement_ready
		self.failure_reason = str(reason)
		self.placement_ready = False
		self._phase = FAILED
		# Once placement has been unlocked, native owners may already depend on
		# the streamed terrain. Only their explicit teardown may restore range.
		if had_placement_ready:
			return False
		return self._restore()

	def poll(self, now, active_count):
		if self._phase in (FAILED, COMPLETE):
			return self._phase
		try:
			now_f = float(now)
			try:
				import BigWorld as _bw_ssb
				_sp_prog_ssb = float(_bw_ssb.spaceLoadStatus() or 0.0)
			except Exception:
				_sp_prog_ssb = 1.0
			if not self.placement_ready and _sp_prog_ssb < 0.85:
				self._first_poll_at = now_f
				self._deadline = max(self._deadline, now_f + 8.5)
				return self._phase
			if self._first_poll_at is None:
				self._first_poll_at = now_f
				self._deadline = max(self._deadline, now_f + 8.5)
			if now_f >= self._deadline:
				self._fail('timeout')
				return self._phase
		except Exception:
			self._fail('invalid_time')
			return self._phase

		poll_elapsed = max(0.0, float(now) - float(self._first_poll_at))
		just_became_ready = False
		if not self.placement_ready:
			for index, job in enumerate(self._jobs):
				if index in self._supported:
					continue
				try:
					hit_y = self._probe(job)
				except Exception:
					self._fail('support_probe_error')
					return self._phase
				if not _finite(hit_y):
					# Wait at least 5.5s after polling actually starts (post model-prefetch)
					# so distant chunks (e.g. 45_north_america SW corner) finish C++ octree streaming!
					if poll_elapsed >= 5.5:
						try:
							_fallback_y = float(job[4]) if job[4] is not None else 0.0
							self._jobs[index] = tuple(job[:4]) + (_fallback_y,) + tuple(job[5:])
						except Exception:
							pass
						self._supported.add(index)
					continue
				try:
					baked_y = None if job[4] is None else float(job[4])
				except Exception:
					self._fail('invalid_lineup')
					return self._phase
				if baked_y is None:
					# v4.38: grid slots past the 15 baked poses carry no height.
					# The live probe hit IS the support proof; write it back so
					# placement seeds the body at the streamed ground height.
					try:
						self._jobs[index] = tuple(job[:4]) + (float(hit_y),) + tuple(job[5:])
					except Exception:
						pass
					self._supported.add(index)
				elif abs(float(hit_y) - baked_y) <= self._height_tolerance or poll_elapsed >= 1.25:
					try:
						self._jobs[index] = tuple(job[:4]) + (float(hit_y),) + tuple(job[5:])
					except Exception:
						pass
					self._supported.add(index)
			if len(self._supported) == len(self._jobs):
				self.placement_ready = True
				self._phase = PLACEMENT_READY
				just_became_ready = True

		if self.placement_ready:
			# The active count sampled before this poll cannot describe bodies whose
			# placement was only unlocked by this poll. Require one later observation.
			if just_became_ready:
				return PLACEMENT_READY
			try:
				all_active = int(active_count) >= len(self._jobs)
			except Exception:
				all_active = False
			if all_active:
				self._phase = COMPLETE
				return self._phase
			return PLACEMENT_READY
		return WAITING_SUPPORT

	def stop(self):
		if self._phase == COMPLETE:
			return self._restore()
		if self._phase == PLACEMENT_READY:
			# Explicit stop is called only after the owner-release barrier. Unlike an
			# activation timeout, it is therefore safe to restore on this first call.
			self.failure_reason = 'stopped'
			self.placement_ready = False
			self._phase = FAILED
			return self._restore()
		if self._phase != FAILED:
			return self._fail('stopped')
		return self._restore()
