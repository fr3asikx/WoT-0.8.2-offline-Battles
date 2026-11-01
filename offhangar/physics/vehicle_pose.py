# -*- coding: utf-8 -*-
"""Canonical pose commit for locally constructed 0.8.2 vehicle proxies.

The offline battle has more consumers than the rendered model: the minimap and
markers read ``mock.matrix``, native entity users read ``entity.filter``, and
the chassis is normally driven by ``Servo(mock.matrix)``.  Updating any one of
those in isolation creates a split pose.  Keep all live-vehicle pose writes in
this small adapter; wreck models deliberately own a separate frozen matrix.
"""


def commit_pose(mock, position, yaw=None, pitch=None, roll=None,
		space_id=None, timestamp=None, sync_filter=True,
		attach_servo=True, prime_model=False):
	"""Commit one pose to Python state, matrix, native filter and model servo.

	``prime_model`` performs the one direct model write needed before a Servo is
	attached.  Once attached, the matrix is the only model writer.
	"""
	if mock is None or position is None:
		return False

	if (hasattr(position, 'x') and hasattr(position, 'y') and
			hasattr(position, 'z')):
		# The render loop already owns a Vector3. Reuse it instead of allocating
		# another native vector for every bot on every frame.
		world = position
	else:
		try:
			import Math
			world = Math.Vector3(position)
		except Exception:
			world = position
	if yaw is None:
		yaw = getattr(mock, 'yaw', 0.0) or 0.0
	if pitch is None:
		pitch = getattr(mock, 'pitch', 0.0) or 0.0
	if roll is None:
		roll = getattr(mock, 'roll', 0.0) or 0.0
	yaw = float(yaw)
	pitch = float(pitch)
	roll = float(roll)

	mock.position = world
	mock.yaw = yaw
	mock.pitch = pitch
	mock.roll = roll
	matrix = getattr(mock, 'matrix', None)
	if matrix is not None:
		target_matrix = getattr(matrix, 'a', matrix)
		target_matrix.setRotateYPR((yaw, pitch, roll))
		target_matrix.translation = world
		if hasattr(matrix, 'a'):
			matrix.a = target_matrix

	entity = getattr(mock, 'bw_entity', None)
	entity_filter = getattr(entity, 'filter', None) if entity is not None else None
	if sync_filter and entity_filter is not None and space_id is not None:
		try:
			import BigWorld
			when = BigWorld.time() if timestamp is None else float(timestamp)
			entity_filter.set(when, int(space_id), entity.id, world,
			                  (roll, pitch, yaw), 0)
			mock.filter = entity_filter
		except Exception:
			# Matrix/Servo remains a complete render path if a just-created entity
			# has not accepted its filter yet. The next commit retries it.
			pass

	chassis = (getattr(mock, '_chassis_model', None) or
	           getattr(mock, 'model', None))
	if chassis is not None and matrix is not None:
		if prime_model and not getattr(mock, '_servo_added', False):
			try:
				chassis.position = world
				chassis.yaw = yaw
			except Exception:
				pass
		if attach_servo and not getattr(mock, '_servo_added', False):
			servo = None
			try:
				import BigWorld
				# Read before mutating so a model without an ownership surface cannot
				# accumulate an untracked Servo after a silent native API failure.
				if list(chassis.motors):
					raise RuntimeError('model already has a root motor')
				servo = BigWorld.Servo(matrix)
				chassis.addMotor(servo)
				motors = list(chassis.motors)
				if len(motors) != 1 or motors[0] is not servo:
					raise RuntimeError('Servo attach readback mismatch')
				mock._pose_servo = servo
				mock._servo_added = True
			except Exception:
				try:
					if servo is not None and any(
							motor is servo for motor in list(chassis.motors)):
						chassis.delMotor(servo)
				except Exception:
					pass
				mock._pose_servo = None
				mock._servo_added = False
				return False

	return True
