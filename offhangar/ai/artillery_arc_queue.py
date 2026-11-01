# -*- coding: utf-8 -*-
"""Bounded, engine-independent scheduler for artillery world-arc probes.

The BigWorld BSP query itself must run on the game thread.  This scheduler
keeps that requirement while spreading a complete low/high trajectory check
over several rendered frames, so one SPG observation cannot monopolise a
frame with dozens of synchronous collision rays.
"""

import math


def _coords(value):
	try:
		return (float(value[0]), float(value[1]), float(value[2]))
	except Exception:
		return (float(value.x), float(value.y), float(value.z))


def _distance(first, second):
	first = _coords(first)
	second = _coords(second)
	dx = first[0] - second[0]
	dy = first[1] - second[1]
	dz = first[2] - second[2]
	return math.sqrt(dx * dx + dy * dy + dz * dz)


class ArcProbeQueue(object):
	"""Resolve candidate trajectory paths under a strict per-frame ray quota."""

	def __init__(self, max_jobs=8, success_ttl=2.5, failure_ttl=0.75,
			max_job_age=4.0, target_slop=7.0):
		self.max_jobs = max(1, int(max_jobs))
		self.success_ttl = max(0.05, float(success_ttl))
		self.failure_ttl = max(0.05, float(failure_ttl))
		self.max_job_age = max(0.25, float(max_job_age))
		self.target_slop = max(0.0, float(target_slop))
		self.jobs = {}
		self.order = []
		self.results = {}

	def reset(self):
		self.jobs = {}
		self.order = []
		self.results = {}

	def _purge(self, now):
		now = float(now)
		for key, value in list(self.results.items()):
			if float(value[0]) <= now:
				self.results.pop(key, None)
		for key in list(self.order):
			job = self.jobs.get(key)
			if job is None or now - float(job['created']) > self.max_job_age:
				self.jobs.pop(key, None)
				try:
					self.order.remove(key)
				except ValueError:
					pass

	def result(self, key, now):
		"""Return ``(ready, solution)``; a ready ``None`` is a cached failure."""
		self._purge(now)
		value = self.results.get(key)
		if value is None:
			return False, None
		return True, value[1]

	def is_pending(self, key, now):
		self._purge(now)
		return key in self.jobs

	def request(self, key, candidates, target_position, now):
		"""Queue one low/high solution set without displacing active work."""
		ready, solution = self.result(key, now)
		if ready:
			return ready, solution
		if key in self.jobs:
			return False, None
		usable = []
		for candidate in candidates or ():
			path = candidate.get('path') if isinstance(candidate, dict) else None
			if path is not None and len(path) >= 2:
				usable.append(candidate)
		if not usable:
			self.results[key] = (float(now) + self.failure_ttl, None)
			return True, None
		if len(self.order) >= self.max_jobs:
			return False, None
		self.jobs[key] = {
			'created': float(now),
			'target': _coords(target_position),
			'candidates': usable,
			'candidate': 0,
			'chord': 0,
		}
		self.order.append(key)
		return False, None

	def request_lazy(self, key, candidate_factory, target_position, now):
		"""Build candidates only when this queue can accept the new job."""
		ready, solution = self.result(key, now)
		if ready:
			return ready, solution
		if key in self.jobs or len(self.order) >= self.max_jobs:
			return False, None
		return self.request(
			key, candidate_factory(), target_position, now)

	def _complete(self, key, now, solution):
		ttl = self.success_ttl if solution is not None else self.failure_ttl
		self.results[key] = (float(now) + ttl, solution)
		self.jobs.pop(key, None)
		try:
			self.order.remove(key)
		except ValueError:
			pass

	def advance(self, now, ray_budget, probe):
		"""Advance oldest work and return the exact number of probes performed.

		``probe(first, second)`` returns ``None`` for a clear chord or the world
		hit position.  A hit within ``target_slop`` of the target completes the
		trajectory exactly like the synchronous implementation.
		"""
		self._purge(now)
		budget = max(0, int(ray_budget))
		used = 0
		while used < budget and self.order:
			key = self.order[0]
			job = self.jobs.get(key)
			if job is None:
				self.order.pop(0)
				continue
			candidate_index = int(job['candidate'])
			if candidate_index >= len(job['candidates']):
				self._complete(key, now, None)
				continue
			solution = job['candidates'][candidate_index]
			path = solution['path']
			chord = int(job['chord'])
			if chord >= len(path) - 1:
				self._complete(key, now, solution)
				continue
			hit = probe(path[chord], path[chord + 1])
			used += 1
			if hit is None:
				job['chord'] = chord + 1
				continue
			if _distance(hit, job['target']) <= self.target_slop:
				self._complete(key, now, solution)
				continue
			job['candidate'] = candidate_index + 1
			job['chord'] = 0
		return used

	def diagnostics(self):
		return {
			'pending': len(self.order),
			'results': len(self.results),
		}
