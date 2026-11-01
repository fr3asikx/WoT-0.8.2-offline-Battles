# -*- coding: utf-8 -*-
"""0.8.2 minimap postmortem binding for offline mock vehicles.

The stock Minimap.__resetCamera implementation indexes BigWorld.entities by
vehicle id. Offline battle vehicles are lightweight mocks, so reproduce the
same marker updates with the matrix that the spectator camera already follows.
"""


def _entry_flash(own_ui, entries, vehicle_id, method, args):
	entry = entries.get(vehicle_id)
	if entry is None or 'handle' not in entry:
		return
	own_ui.entryInvoke(entry['handle'], (method, args))


def _translation_only_mp(math_module, source_matrix):
	if source_matrix is None:
		return None
	try:
		mp = math_module.WGTranslationOnlyMP()
		mp.source = source_matrix
		return mp
	except Exception:
		return source_matrix


def follow_mock_vehicle(minimap, player_vehicle_id, vehicle_id,
		vehicle_matrix, own_matrix, camera_inverse_matrix, math_module):
	"""Move the stock postmortem and camera markers to an offline vehicle."""
	if minimap is None or vehicle_matrix is None:
		return False
	own_ui = getattr(minimap, '_Minimap__ownUI', None)
	own_entry = getattr(minimap, '_Minimap__ownEntry', None)
	entries = getattr(minimap, '_Minimap__entries', None)
	if own_ui is None or not isinstance(own_entry, dict) or not isinstance(entries, dict):
		return False

	previous = int(getattr(minimap, '_Minimap__observedVehicleId', -1) or -1)
	if previous > 0 and previous != int(vehicle_id):
		_entry_flash(own_ui, entries, previous, 'setPostmortem', [False])
		prev_entry = entries.get(previous)
		if isinstance(prev_entry, dict) and 'handle' in prev_entry:
			src_mat = prev_entry.get('source_matrix') or prev_entry.get('matrix')
			if src_mat is not None:
				emp_prev = _translation_only_mp(math_module, src_mat)
				prev_entry['matrix'] = emp_prev
				own_ui.entrySetMatrix(prev_entry['handle'], emp_prev)

	is_own = int(vehicle_id) == int(player_vehicle_id)
	if is_own:
		setattr(minimap, '_Minimap__observedVehicleId', -1)
		marker_matrix = _translation_only_mp(
			math_module, own_matrix if own_matrix is not None else vehicle_matrix)
		player_marker = 'postmortemCamera'
	else:
		setattr(minimap, '_Minimap__observedVehicleId', int(vehicle_id))
		_entry_flash(own_ui, entries, int(vehicle_id), 'setPostmortem', [True])
		entry = entries.get(int(vehicle_id))
		if entry is not None and 'handle' in entry:
			# When setPostmortem([True]) is active on an observed ally, Minimap.swf
			# displays the white directional arrow instead of the tank class symbol.
			# Pass the full vehicle_matrix (with hull rotation) so the white arrow
			# rotates in the ally tank's movement direction, and keep source_matrix
			# so switching away restores WGTranslationOnlyMP for the class symbol!
			entry['source_matrix'] = vehicle_matrix
			entry['matrix'] = vehicle_matrix
			own_ui.entrySetMatrix(entry['handle'], vehicle_matrix)
		marker_matrix = _translation_only_mp(
			math_module, own_matrix if own_matrix is not None else vehicle_matrix)
		player_marker = 'postmortemCamera'

	if 'handle' in own_entry and marker_matrix is not None:
		own_ui.entrySetMatrix(own_entry['handle'], marker_matrix)
		own_ui.entryInvoke(own_entry['handle'], ('init', ['player', player_marker]))

	old_camera = getattr(minimap, '_Minimap__cameraHandle', None)
	if old_camera is not None:
		own_ui.delEntry(old_camera)
	combined = math_module.WGCombinedMP()
	translation = math_module.WGTranslationOnlyMP()
	translation.source = vehicle_matrix
	combined.translationSrc = translation
	combined.rotationSrc = camera_inverse_matrix
	z_manager = getattr(minimap, 'zIndexManager', None)
	if z_manager is not None:
		camera_handle = own_ui.addEntry(
			combined, z_manager.getIndexByName('cameraNormal'))
		own_ui.entryInvoke(camera_handle, ('gotoAndStop', ['cursorNormal']))
		setattr(minimap, '_Minimap__cameraHandle', camera_handle)

	parent_ui = getattr(minimap, '_Minimap__parentUI', None)
	if parent_ui is not None:
		parent_ui.call('minimap.entryInited', [])
	return True
