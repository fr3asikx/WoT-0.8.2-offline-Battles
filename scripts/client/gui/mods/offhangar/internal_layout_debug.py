import math

try:
    from gui.mods.offhangar.logging import LOG_EVENT, LOG_EXCEPTION
except Exception:
    def LOG_EVENT(category, event, **fields):
        return None
    def LOG_EXCEPTION(category='python', event='exception', *args, **fields):
        return None


UPDATE_INTERVAL = 1.0 / 24.0
NEARBY_DISTANCE = 50.0
MAX_NEARBY_VEHICLES = 1
NEARBY_DISTANCE_SQ = NEARBY_DISTANCE * NEARBY_DISTANCE
MAX_AIM_DRAW_DISTANCE = 1300.0
MAX_LINE_COMPONENTS = 8192
AIM_FILL_STEP_PX = 3.0
NEAR_FILL_STEP_PX = 4.5
AIM_FILL_ALPHA = 0.12
NEAR_FILL_ALPHA = 0.07
FILL_STRIP_OVERLAP = 2.05
AIM_SPHERE_LONGITUDE = 12
AIM_SPHERE_LATITUDE = 6
NEAR_SPHERE_LONGITUDE = 8
NEAR_SPHERE_LATITUDE = 4
MAX_LABEL_COMPONENTS = 32
SCREEN_CULL_MARGIN = 1.08
NEAR_PLANE = 0.05
LINE_TEXTURE = 'gui/maps/icons/offhangar/internal_debug_pixel.dds'
XRAY_TEXTURE_ROOT = 'gui/maps/icons/offhangar/internal_xray'
XRAY_TEXTURE_EXTENSION = '.dds'
MAX_RETAINED_LINE_COMPONENTS = MAX_LINE_COMPONENTS + 512

_TEXTURE_PALETTE = (
    ('engine', (255, 68, 58)),
    ('ammo', (255, 142, 24)),
    ('gun', (255, 230, 44)),
    ('turret_ring', (24, 222, 232)),
    ('track', (62, 236, 91)),
    ('optics', (255, 49, 196)),
    ('radio', (55, 200, 238)),
    ('fuel', (52, 126, 255)),
    ('commander', (157, 91, 245)),
    ('driver', (91, 232, 72)),
    ('gunner', (255, 72, 210)),
    ('gunner2', (212, 77, 245)),
    ('loader', (255, 222, 44)),
    ('loader2', (255, 179, 55)),
    ('radioman', (124, 151, 255)),
    ('crew', (236, 239, 84)),
    ('unknown', (255, 255, 255)),
    ('black', (0, 0, 0)),
)
_TEXTURE_STYLES = (
    'fill_dim', 'fill', 'glow', 'edge_dim', 'edge', 'shadow')

_VIEW_MODES = ('FRONT', 'ALL', 'MODULES', 'CREW', 'FOCUS')

_FRONT_HIDDEN_ENTITIES = ('leftTrack', 'rightTrack', 'gun')
FRONT_DEPTH_FRACTION = 0.78
FRONT_MIN_EXPOSURE = 0.34
FRONT_STRONG_EXPOSURE = 0.70
OCCLUSION_DEPTH_GAP = 0.12
MIN_PROJECTED_AREA = 0.000015

_LABEL_ORDER = (
    'OPTICS', 'AMMO', 'ENGINE', 'FUEL', 'GUN TRAVERSE', 'TURRET RING', 'RADIO',
    'COMMANDER', 'DRIVER', 'GUNNER', 'GUNNER 2', 'LOADER', 'LOADER 2',
    'RADIOMAN', 'GUN', 'LEFT TRACK', 'RIGHT TRACK')

_COLORS = {
    'engine': (255, 68, 58, 252),
    'ammoBay': (255, 142, 24, 252),
    'gun': (255, 230, 44, 250),
    'turretRotator': (24, 222, 232, 252),
    'leftTrack': (62, 236, 91, 245),
    'rightTrack': (62, 236, 91, 245),
    'surveyingDevice': (255, 49, 196, 252),
    'radio': (55, 200, 238, 250),
    'fuelTank': (52, 126, 255, 252),
    'commander': (157, 91, 245, 252),
    'driver': (91, 232, 72, 252),
    'gunner': (255, 72, 210, 252),
    'gunner1': (255, 72, 210, 252),
    'gunner2': (212, 77, 245, 252),
    'loader': (255, 222, 44, 252),
    'loader1': (255, 222, 44, 252),
    'loader2': (255, 179, 55, 252),
    'radioman': (124, 151, 255, 252),
    'crew': (236, 239, 84, 250),
    'unknown': (255, 255, 255, 240),
}


_DISPLAY_NAMES = {
    'engine': 'ENGINE',
    'ammoBay': 'AMMO',
    'gun': 'GUN',
    'turretRotator': 'TURRET RING',
    'leftTrack': 'LEFT TRACK',
    'rightTrack': 'RIGHT TRACK',
    'surveyingDevice': 'OPTICS',
    'radio': 'RADIO',
    'fuelTank': 'FUEL',
    'commander': 'COMMANDER',
    'driver': 'DRIVER',
    'gunner': 'GUNNER',
    'gunner1': 'GUNNER',
    'gunner2': 'GUNNER 2',
    'loader': 'LOADER',
    'loader1': 'LOADER',
    'loader2': 'LOADER 2',
    'radioman': 'RADIOMAN',
}

_BOX_EDGES = (
    (0, 1), (1, 3), (3, 2), (2, 0),
    (4, 5), (5, 7), (7, 6), (6, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)


_BOX_FACES = (
    (0, 1, 3, 2), (4, 6, 7, 5),
    (0, 4, 5, 1), (2, 3, 7, 6),
    (0, 2, 6, 4), (1, 5, 7, 3),
)

_COMPONENT_TYPE_TO_PARENT = {
    'vehicleChassis': 'chassis',
    'vehicleHull': 'hull',
    'vehicleTurret': 'turret',
    'vehicleGun': 'gun',
}
_FALLBACK_COMPONENT_ORDER = ('chassis', 'hull', 'turret', 'gun')


def _safe_set(obj, name, value):
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def _safe_set_gui_colour(obj, rgba):
    try:
        import Math
        try:
            value = Math.Vector4(float(rgba[0]), float(rgba[1]),
                float(rgba[2]), float(rgba[3]))
        except Exception:
            value = Math.Vector4((float(rgba[0]), float(rgba[1]),
                float(rgba[2]), float(rgba[3])))
        if _safe_set(obj, 'colour', value):
            return True
    except Exception:
        pass
    return _safe_set(obj, 'colour', tuple(rgba))


def _key(Keys, *names):
    for name in names:
        value = getattr(Keys, name, None)
        if value is not None:
            return value
    return -999999


def _value(value, key, default=None):
    if value is None:
        return default
    try:
        return value.get(key, default)
    except Exception:
        pass
    try:
        return value[key]
    except Exception:
        pass
    try:
        return getattr(value, key)
    except Exception:
        return default


def _vector_tuple(value):
    try:
        return (float(value.x), float(value.y), float(value.z))
    except Exception:
        try:
            return (float(value[0]), float(value[1]), float(value[2]))
        except Exception:
            return (0.0, 0.0, 0.0)


def _normalised_tuple(value, fallback):
    x, y, z = _vector_tuple(value)
    length = math.sqrt(x * x + y * y + z * z)
    if length <= 0.000001:
        return fallback
    inverse = 1.0 / length
    return (x * inverse, y * inverse, z * inverse)


def _dot(delta, axis):
    return (float(delta[0]) * float(axis[0]) +
        float(delta[1]) * float(axis[1]) +
        float(delta[2]) * float(axis[2]))


def _cross(a, b):
    return (float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
        float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
        float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]))


def _subtract(a, b):
    return (float(a[0]) - float(b[0]),
        float(a[1]) - float(b[1]),
        float(a[2]) - float(b[2]))


def _try_vector_tuple(value):
    if value is None:
        return None
    try:
        result = (float(value.x), float(value.y), float(value.z))
    except Exception:
        try:
            result = (float(value[0]), float(value[1]), float(value[2]))
        except Exception:
            return None
    for number in result:
        if number != number or abs(number) > 10000000.0:
            return None
    return result


def _lerp3(a, b, factor):
    return (a[0] + (b[0] - a[0]) * factor,
        a[1] + (b[1] - a[1]) * factor,
        a[2] + (b[2] - a[2]) * factor)


def _distance_sq(a, b):
    dx = float(a[0]) - float(b[0])
    dy = float(a[1]) - float(b[1])
    dz = float(a[2]) - float(b[2])
    return dx * dx + dy * dy + dz * dz


def _clamp(value, minimum, maximum):
    return max(float(minimum), min(float(maximum), float(value)))


def _cross_2d(origin, first, second):
    return ((float(first[0]) - float(origin[0])) *
        (float(second[1]) - float(origin[1])) -
        (float(first[1]) - float(origin[1])) *
        (float(second[0]) - float(origin[0])))


def _convex_hull(points):
    unique = {}
    for point in points:
        try:
            key = (round(float(point[0]), 6), round(float(point[1]), 6))
            unique[key] = (float(point[0]), float(point[1]))
        except Exception:
            continue
    ordered = sorted(unique.values())
    if len(ordered) <= 2:
        return tuple(ordered)
    lower = []
    for point in ordered:
        while len(lower) >= 2 and _cross_2d(
                lower[-2], lower[-1], point) <= 0.0000001:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(ordered):
        while len(upper) >= 2 and _cross_2d(
                upper[-2], upper[-1], point) <= 0.0000001:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _polygon_area(polygon):
    if polygon is None or len(polygon) < 3:
        return 0.0
    area = 0.0
    for index in range(len(polygon)):
        first = polygon[index]
        second = polygon[(index + 1) % len(polygon)]
        area += float(first[0]) * float(second[1])
        area -= float(second[0]) * float(first[1])
    return abs(area) * 0.5


def _polygon_center(polygon):
    if not polygon:
        return (0.0, 0.0)
    return (sum(float(point[0]) for point in polygon) /
        float(len(polygon)),
        sum(float(point[1]) for point in polygon) /
        float(len(polygon)))


def _point_in_polygon(point, polygon):
    if polygon is None or len(polygon) < 3:
        return False
    x_value = float(point[0])
    y_value = float(point[1])
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x0, y0 = float(previous[0]), float(previous[1])
        x1, y1 = float(current[0]), float(current[1])
        crosses = ((y0 > y_value) != (y1 > y_value))
        if crosses:
            denominator = y1 - y0
            if abs(denominator) > 0.0000001:
                x_intersection = x0 + (y_value - y0) * (x1 - x0) / denominator
                if x_value < x_intersection:
                    inside = not inside
        previous = current
    return inside


def _point_segment_distance_sq_2d(point, first, second):
    px, py = float(point[0]), float(point[1])
    x0, y0 = float(first[0]), float(first[1])
    x1, y1 = float(second[0]), float(second[1])
    dx = x1 - x0
    dy = y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0000000001:
        return (px - x0) * (px - x0) + (py - y0) * (py - y0)
    factor = ((px - x0) * dx + (py - y0) * dy) / length_sq
    factor = _clamp(factor, 0.0, 1.0)
    nearest_x = x0 + dx * factor
    nearest_y = y0 + dy * factor
    return ((px - nearest_x) * (px - nearest_x) +
        (py - nearest_y) * (py - nearest_y))


def _polygon_distance_sq(point, polygon):
    if polygon is None or len(polygon) < 2:
        return 999999.0
    if len(polygon) >= 3 and _point_in_polygon(point, polygon):
        return 0.0
    best = 999999.0
    for index in range(len(polygon)):
        distance_sq = _point_segment_distance_sq_2d(point,
            polygon[index], polygon[(index + 1) % len(polygon)])
        if distance_sq < best:
            best = distance_sq
    return best


def _polygon_samples(polygon):
    if polygon is None or len(polygon) < 3:
        return ()
    center = _polygon_center(polygon)
    samples = [center, center]
    count = len(polygon)
    for index, point in enumerate(polygon):
        next_point = polygon[(index + 1) % count]
        samples.append((center[0] + (float(point[0]) - center[0]) * 0.62,
            center[1] + (float(point[1]) - center[1]) * 0.62))
        midpoint = ((float(point[0]) + float(next_point[0])) * 0.5,
            (float(point[1]) + float(next_point[1])) * 0.5)
        samples.append((center[0] + (midpoint[0] - center[0]) * 0.70,
            center[1] + (midpoint[1] - center[1]) * 0.70))
    minimum_x = min(float(point[0]) for point in polygon)
    maximum_x = max(float(point[0]) for point in polygon)
    minimum_y = min(float(point[1]) for point in polygon)
    maximum_y = max(float(point[1]) for point in polygon)
    for x_factor in (0.25, 0.5, 0.75):
        for y_factor in (0.25, 0.5, 0.75):
            candidate = (minimum_x + (maximum_x - minimum_x) * x_factor,
                minimum_y + (maximum_y - minimum_y) * y_factor)
            if _point_in_polygon(candidate, polygon):
                samples.append(candidate)
    return tuple(samples[:24])


def _with_alpha(colour, factor):
    alpha = int(round(float(colour[3]) * float(factor)))
    alpha = max(0, min(255, alpha))
    return (int(colour[0]), int(colour[1]), int(colour[2]), alpha)


def _shade_colour(colour, brightness, alpha_factor):
    brightness = max(0.0, min(1.25, float(brightness)))
    alpha_factor = max(0.0, min(1.0, float(alpha_factor)))
    return (
        max(0, min(255, int(round(float(colour[0]) * brightness)))),
        max(0, min(255, int(round(float(colour[1]) * brightness)))),
        max(0, min(255, int(round(float(colour[2]) * brightness)))),
        max(0, min(255, int(round(float(colour[3]) * alpha_factor)))),
    )


def _rotate_local_y(point, center, yaw_degrees):
    yaw = float(yaw_degrees or 0.0)
    if abs(yaw) <= 0.0001:
        return tuple(point)
    angle = math.radians(yaw)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    dx = float(point[0]) - float(center[0])
    dz = float(point[2]) - float(center[2])
    return (float(center[0]) + dx * cosine - dz * sine,
        float(point[1]), float(center[2]) + dx * sine + dz * cosine)


def _primitive_corners(primitive):
    shape = str(primitive.get('shape', 'aabb') or 'aabb').lower()
    center = primitive.get('center', (0.0, 0.0, 0.0))
    if shape in ('sphere', 'ellipsoid', 'capsule'):
        minimum = primitive.get('minimum')
        maximum = primitive.get('maximum')
        if minimum is None or maximum is None:
            half = primitive.get('half_extents', (0.1, 0.1, 0.1))
            minimum = tuple(float(center[i]) - float(half[i]) for i in range(3))
            maximum = tuple(float(center[i]) + float(half[i]) for i in range(3))
        x0, y0, z0 = tuple(float(value) for value in minimum)
        x1, y1, z1 = tuple(float(value) for value in maximum)
        return ((x0, y0, z0), (x1, y0, z0),
            (x0, y1, z0), (x1, y1, z0),
            (x0, y0, z1), (x1, y0, z1),
            (x0, y1, z1), (x1, y1, z1))
    half = primitive.get('half_extents', (0.1, 0.1, 0.1))
    x0 = float(center[0]) - float(half[0])
    y0 = float(center[1]) - float(half[1])
    z0 = float(center[2]) - float(half[2])
    x1 = float(center[0]) + float(half[0])
    y1 = float(center[1]) + float(half[1])
    z1 = float(center[2]) + float(half[2])
    corners = (
        (x0, y0, z0), (x1, y0, z0),
        (x0, y1, z0), (x1, y1, z0),
        (x0, y0, z1), (x1, y0, z1),
        (x0, y1, z1), (x1, y1, z1),
    )
    yaw = float(primitive.get('rotation_yaw_degrees', 0.0) or 0.0)
    return tuple(_rotate_local_y(point, center, yaw) for point in corners)


class InternalLayoutDebugController(object):

    def __init__(self, vehicle_provider, player_provider, layout_module):
        self._vehicle_provider = vehicle_provider
        self._player_provider = player_provider
        self._layouts = layout_module
        self._active = False
        self._callback_id = None
        self._line_pools = {}
        self._line_usage = {}
        self._line_texture_cache = {}
        self._line_pool_changed = False
        self._label_pool = []
        self._text = None
        self._panel = None
        self._keys_down = set()
        self._view_mode_index = 0
        self._labels_enabled = True
        self._line_shader_supported = None
        self._draw_failures = 0
        self._last_draw_stats = {}
        self._camera_source = None
        self._anchor_log_cache = set()

    @property
    def active(self):
        return bool(self._active)

    def toggle(self):
        self._active = not self._active
        if self._active:
            self._camera_source = None
            self._anchor_log_cache.clear()
            self._ensure_gui()
            try:
                self._draw()
            except Exception:
                LOG_EXCEPTION('modules',
                    'internal_layout_filtered_esp_initial_draw_failed')
            self._schedule()
        else:
            self._cancel_callback()
            self._hide_all()
        LOG_EVENT('modules', 'internal_layout_visual_debug_toggled',
            enabled=self._active, nearby_distance=NEARBY_DISTANCE,
            battle_frozen=False, update_interval=UPDATE_INTERVAL)
        return self._active

    def stop(self):
        self._active = False
        self._cancel_callback()
        self._keys_down.clear()
        self._destroy_gui()

    def handle_key_event(self, event):
        if bool(getattr(event, '_offh_internal_spawn', False)):
            return False
        try:
            import Keys
        except Exception:
            return False
        key = getattr(event, 'key', -1)
        f8 = _key(Keys, 'KEY_F8')
        f9 = _key(Keys, 'KEY_F9')
        f10 = _key(Keys, 'KEY_F10')
        recognised = (f8, f9, f10)
        if key not in recognised:
            return False
        try:
            is_down = bool(event.isKeyDown())
        except Exception:
            return False
        if not is_down:
            self._keys_down.discard(key)
            return True
        if key in self._keys_down:
            return True
        self._keys_down.add(key)
        if key == f8:
            self.toggle()
        elif key == f9:
            self._view_mode_index = ((self._view_mode_index + 1) %
                len(_VIEW_MODES))
            LOG_EVENT('modules', 'internal_layout_view_mode_changed',
                mode=self._view_mode())
            if self._active:
                self._draw()
        elif key == f10:
            self._labels_enabled = not self._labels_enabled
            LOG_EVENT('modules', 'internal_layout_labels_toggled',
                enabled=self._labels_enabled)
            if self._active:
                self._draw()
        return True

    def _cancel_callback(self):
        if self._callback_id is None:
            return
        try:
            import BigWorld
            BigWorld.cancelCallback(self._callback_id)
        except Exception:
            pass
        self._callback_id = None

    def _schedule(self):
        if not self._active or self._callback_id is not None:
            return
        try:
            import BigWorld
            self._callback_id = BigWorld.callback(UPDATE_INTERVAL, self._tick)
        except Exception:
            self._callback_id = None
            LOG_EXCEPTION('modules',
                'internal_layout_filtered_esp_schedule_failed')

    def _tick(self):
        self._callback_id = None
        if not self._active:
            return
        try:
            self._draw()
            self._draw_failures = 0
        except Exception as error:
            self._draw_failures += 1
            self._ensure_gui()
            self._set_status(str(error))
            if self._draw_failures <= 3:
                LOG_EXCEPTION('modules',
                    'internal_layout_filtered_esp_draw_failed')
        self._schedule()

    def _resort_gui(self):
        try:
            import GUI
            method = getattr(GUI, 'reSort', None)
            if callable(method):
                method()
        except Exception:
            pass

    def _make_simple(self, texture_name=None):
        import GUI
        texture_name = texture_name or LINE_TEXTURE
        try:
            return GUI.Simple(texture_name)
        except Exception:
            try:
                return GUI.Simple(LINE_TEXTURE)
            except Exception:
                return GUI.Simple()

    def _make_line(self, texture_name):
        import GUI
        component = self._make_simple(texture_name)
        _safe_set(component, 'horizontalPositionMode', 'CLIP')
        _safe_set(component, 'verticalPositionMode', 'CLIP')
        _safe_set(component, 'widthMode', 'CLIP')
        _safe_set(component, 'heightMode', 'CLIP')
        _safe_set(component, 'horizontalAnchor', 'CENTER')
        _safe_set(component, 'verticalAnchor', 'CENTER')
        _safe_set(component, 'pixelSnap', False)
        _safe_set(component, 'materialFX', 'BLEND')
        _safe_set_gui_colour(component, (255, 255, 255, 255))
        _safe_set(component, 'visible', False)

        transform = None
        shader = None
        try:
            import Math
            transform = Math.Matrix()
            transform.setIdentity()
            shader = GUI.MatrixShader()
            shader.target = transform
            shader.blend = 0
            component.addShader(shader, 'offhOrientedLine')
            self._line_shader_supported = True
        except Exception:
            transform = None
            shader = None
            if self._line_shader_supported is None:
                self._line_shader_supported = False
                LOG_EXCEPTION('modules',
                    'internal_layout_matrix_shader_unavailable')
        GUI.addRoot(component)
        self._line_pool_changed = True
        return {
            'component': component,
            'matrix': transform,
            'shader': shader,
            'texture': texture_name,
        }

    def _line_component(self, entry):
        try:
            return entry.get('component')
        except Exception:
            return entry

    def _line_pool_size(self):
        total = 0
        for pool in self._line_pools.values():
            total += len(pool)
        return total

    def _begin_line_frame(self):
        self._line_usage = {}

    def _remove_line_entry(self, entry):
        try:
            import GUI
            GUI.delRoot(self._line_component(entry))
        except Exception:
            pass
        self._line_pool_changed = True

    def _discard_one_unused_line(self):
        best_key = None
        best_spare = 0
        for texture_name, pool in self._line_pools.items():
            used = int(self._line_usage.get(texture_name, 0))
            spare = len(pool) - used
            if spare > best_spare:
                best_key = texture_name
                best_spare = spare
        if best_key is None or best_spare <= 0:
            return False
        pool = self._line_pools.get(best_key, [])
        entry = pool.pop()
        self._remove_line_entry(entry)
        if not pool:
            try:
                del self._line_pools[best_key]
            except Exception:
                pass
        return True

    def _acquire_line(self, texture_name):
        pool = self._line_pools.get(texture_name)
        if pool is None:
            pool = []
            self._line_pools[texture_name] = pool
        used = int(self._line_usage.get(texture_name, 0))
        if used >= len(pool):
            if self._line_pool_size() >= MAX_RETAINED_LINE_COMPONENTS:
                self._discard_one_unused_line()
            pool.append(self._make_line(texture_name))
        entry = pool[used]
        self._line_usage[texture_name] = used + 1
        return entry

    def _hide_unused_lines(self):
        for texture_name, pool in self._line_pools.items():
            used = int(self._line_usage.get(texture_name, 0))
            for entry in pool[used:]:
                _safe_set(self._line_component(entry), 'visible', False)

    def _hide_all_lines(self):
        self._line_usage = {}
        self._hide_unused_lines()

    def _texture_for_colour(self, colour, thickness):
        try:
            red = max(0, min(255, int(colour[0])))
            green = max(0, min(255, int(colour[1])))
            blue = max(0, min(255, int(colour[2])))
            alpha = max(0, min(255, int(colour[3])))
        except Exception:
            red, green, blue, alpha = 255, 255, 255, 255
        fill_candidate = (float(thickness) >= 5.0 and alpha <= 48)
        cache_key = (red, green, blue, alpha, bool(fill_candidate))
        cached = self._line_texture_cache.get(cache_key)
        if cached is not None:
            return cached

        if max(red, green, blue) <= 10:
            palette_name = 'black'
        else:
            input_sum = float(red + green + blue)
            palette_name = 'unknown'
            best_error = None
            for candidate_name, candidate_rgb in _TEXTURE_PALETTE:
                if candidate_name == 'black':
                    continue
                candidate_sum = float(sum(candidate_rgb))
                scale = input_sum / max(1.0, candidate_sum)
                error = 0.0
                for axis, value in enumerate((red, green, blue)):
                    expected = float(candidate_rgb[axis]) * scale
                    delta = float(value) - expected
                    error += delta * delta
                if best_error is None or error < best_error:
                    best_error = error
                    palette_name = candidate_name

        if palette_name == 'black':
            style = 'shadow'
        elif fill_candidate:
            style = 'fill_dim' if alpha <= 22 else 'fill'
        elif alpha <= 112:
            style = 'glow'
        elif alpha < 228:
            style = 'edge_dim'
        else:
            style = 'edge'
        texture_name = '%s/%s_%s%s' % (
            XRAY_TEXTURE_ROOT, palette_name, style,
            XRAY_TEXTURE_EXTENSION)
        self._line_texture_cache[cache_key] = texture_name
        return texture_name

    def _set_line_transform(self, entry, center_x, center_y, z_order,
            angle):
        component = self._line_component(entry)
        try:
            matrix = entry.get('matrix')
        except Exception:
            matrix = None
        if matrix is not None:
            try:
                import Math
                matrix.setIdentity()
                matrix.setRotateZ(float(angle))
                matrix.translation = Math.Vector3(float(center_x),
                    float(center_y), 0.0)
                try:
                    shader = entry.get('shader')
                    if shader is not None:
                        shader.target = matrix
                        shader.blend = 0
                except Exception:
                    pass
                _safe_set(component, 'position', (0.0, 0.0,
                    float(z_order)))
                _safe_set(component, 'angle', 0.0)
                return True
            except Exception:
                pass
        _safe_set(component, 'position', (float(center_x), float(center_y),
            float(z_order)))
        return False

    def _make_label(self):
        import GUI
        component = GUI.Text()
        _safe_set(component, 'text', '')
        _safe_set(component, 'horizontalPositionMode', 'CLIP')
        _safe_set(component, 'verticalPositionMode', 'CLIP')
        _safe_set(component, 'horizontalAnchor', 'LEFT')
        _safe_set(component, 'verticalAnchor', 'CENTER')
        _safe_set(component, 'font', 'default_small.font')
        _safe_set_gui_colour(component, (255, 255, 255, 255))
        _safe_set(component, 'multiline', False)
        _safe_set(component, 'widthMode', 'CLIP')
        _safe_set(component, 'heightMode', 'CLIP')
        _safe_set(component, 'width', 0.42)
        _safe_set(component, 'height', 0.035)
        _safe_set(component, 'shadow', True)
        _safe_set(component, 'dropShadow', True)
        _safe_set(component, 'visible', False)
        GUI.addRoot(component)
        return component

    def _ensure_gui(self):
        try:
            import GUI
            created = False
            if self._panel is None:
                self._panel = self._make_simple()
                _safe_set(self._panel, 'horizontalPositionMode', 'CLIP')
                _safe_set(self._panel, 'verticalPositionMode', 'CLIP')
                _safe_set(self._panel, 'widthMode', 'CLIP')
                _safe_set(self._panel, 'heightMode', 'CLIP')
                _safe_set(self._panel, 'horizontalAnchor', 'LEFT')
                _safe_set(self._panel, 'verticalAnchor', 'TOP')
                _safe_set(self._panel, 'position', (-0.43, 0.91, 0.985))
                _safe_set(self._panel, 'width', 0.86)
                _safe_set(self._panel, 'height', 0.078)
                _safe_set(self._panel, 'materialFX', 'BLEND')
                _safe_set(self._panel, 'colour', (0, 0, 0, 184))
                _safe_set(self._panel, 'visible', True)
                GUI.addRoot(self._panel)
                created = True
            if self._text is None:
                self._text = GUI.Text()
                _safe_set(self._text, 'text', '')
                _safe_set(self._text, 'horizontalPositionMode', 'CLIP')
                _safe_set(self._text, 'verticalPositionMode', 'CLIP')
                _safe_set(self._text, 'horizontalAnchor', 'LEFT')
                _safe_set(self._text, 'verticalAnchor', 'TOP')
                _safe_set(self._text, 'position', (-0.415, 0.895, 0.995))
                _safe_set(self._text, 'font', 'default_small.font')
                _safe_set(self._text, 'colour', (255, 255, 255, 255))
                _safe_set(self._text, 'multiline', True)
                _safe_set(self._text, 'widthMode', 'CLIP')
                _safe_set(self._text, 'heightMode', 'CLIP')
                _safe_set(self._text, 'width', 0.83)
                _safe_set(self._text, 'height', 0.066)
                _safe_set(self._text, 'shadow', True)
                _safe_set(self._text, 'dropShadow', True)
                _safe_set(self._text, 'visible', True)
                GUI.addRoot(self._text)
                created = True
            if created:
                self._resort_gui()
        except Exception:
            self._panel = None
            self._text = None

    def _destroy_gui(self):
        try:
            import GUI
            for pool in self._line_pools.values():
                for entry in pool:
                    component = self._line_component(entry)
                    try:
                        GUI.delRoot(component)
                    except Exception:
                        pass
            for component in self._label_pool:
                try:
                    GUI.delRoot(component)
                except Exception:
                    pass
            for component in (self._text, self._panel):
                if component is not None:
                    try:
                        GUI.delRoot(component)
                    except Exception:
                        pass
        except Exception:
            pass
        self._line_pools = {}
        self._line_usage = {}
        self._line_texture_cache = {}
        self._line_pool_changed = False
        self._label_pool = []
        self._text = None
        self._panel = None

    def _hide_all(self):
        self._hide_lines_from(0)
        self._hide_labels_from(0)
        if self._text is not None:
            _safe_set(self._text, 'visible', False)
        if self._panel is not None:
            _safe_set(self._panel, 'visible', False)
    def _set_status(self, value):
        if self._panel is not None:
            _safe_set(self._panel, 'visible', True)
        if self._text is not None:
            _safe_set(self._text, 'visible', True)
            _safe_set(self._text, 'text', value)

    def _player(self):
        try:
            return self._player_provider()
        except Exception:
            return None

    def _vehicles(self):
        try:
            values = list((self._vehicle_provider() or {}).values())
        except Exception:
            values = []
        result = []
        seen = set()
        for vehicle in values:
            vehicle_id = getattr(vehicle, 'id', id(vehicle))
            if vehicle_id in seen:
                continue
            if getattr(vehicle, 'typeDescriptor', None) is None:
                continue
            if getattr(vehicle, 'matrix', None) is None:
                continue
            if float(getattr(vehicle, 'health', 1.0) or 0.0) <= 0.0:
                continue
            seen.add(vehicle_id)
            result.append(vehicle)
        return result

    def _layout(self, vehicle):
        layout = getattr(vehicle, '_offh_internal_hit_layout', None)
        expected_key = getattr(self._layouts, 'LAYOUT_KEY', None)
        if (layout is None or (expected_key is not None and
                layout.get('layout_key') != expected_key)):
            layout = self._layouts.build_layout(
                vehicle.typeDescriptor, log_build=False)
            vehicle._offh_internal_hit_layout = layout
        return layout

    def _component_parent(self, vehicle, component, index):
        item_type = str(_value(component, 'itemTypeName', '') or '')
        parent = _COMPONENT_TYPE_TO_PARENT.get(item_type)
        if parent is not None:
            return parent
        descriptor = getattr(vehicle, 'typeDescriptor', None)
        if descriptor is not None:
            for name in _FALLBACK_COMPONENT_ORDER:
                try:
                    if component is getattr(descriptor, name):
                        return name
                except Exception:
                    pass
        if index < len(_FALLBACK_COMPONENT_ORDER):
            return _FALLBACK_COMPONENT_ORDER[index]
        return None

    def _parent_matrices(self, vehicle):
        import Math
        result = {}
        try:
            components = vehicle.getComponents()
        except Exception:
            return result
        for index, pair in enumerate(components):
            try:
                component, vehicle_to_component = pair
                parent = self._component_parent(vehicle, component, index)
                if parent is None:
                    continue
                component_to_vehicle = Math.Matrix(vehicle_to_component)
                component_to_vehicle.invert()
                result[parent] = component_to_vehicle
            except Exception:
                continue
        return result

    def _camera_control_name(self):
        try:
            player = self._player()
            handler = getattr(player, 'inputHandler', None)
            control = getattr(handler, 'ctrl', None)
            if control is not None:
                return control.__class__.__name__
        except Exception:
            pass
        return 'unknown'

    def _raw_aimed_vehicle(self, vehicles, player):
        for attribute in ('_outlined_bot', '_autoaim_target'):
            candidate = getattr(player, attribute, None)
            if candidate in vehicles:
                return candidate
            candidate_id = getattr(candidate, 'id', None)
            if candidate_id is None:
                continue
            for vehicle in vehicles:
                if getattr(vehicle, 'id', None) == candidate_id:
                    return vehicle
        return None

    def _desired_camera_point(self):
        player = self._player()
        if player is None:
            return None
        try:
            handler = getattr(player, 'inputHandler', None)
            method = getattr(handler, 'getDesiredShotPoint', None)
            if callable(method):
                point = _try_vector_tuple(method())
                if point is not None:
                    return point
        except Exception:
            pass
        try:
            rotator = getattr(player, 'gunRotator', None)
            marker = getattr(rotator, 'markerInfo', None)
            if marker:
                point = _try_vector_tuple(marker[0])
                if point is not None:
                    return point
        except Exception:
            pass
        return None

    def _camera_position_and_direction(self, camera):
        position = None
        direction = None
        for name in ('position', 'worldPosition'):
            try:
                value = getattr(camera, name, None)
                if callable(value):
                    value = value()
                position = _try_vector_tuple(value)
                if position is not None:
                    break
            except Exception:
                pass
        for name in ('direction', 'worldDirection'):
            try:
                value = getattr(camera, name, None)
                if callable(value):
                    value = value()
                direction = _try_vector_tuple(value)
                if direction is not None:
                    break
            except Exception:
                pass

        source = 'camera.position+camera.direction'
        if position is None or direction is None:
            try:
                import Math
                raw = Math.Matrix(camera.matrix)
                inverse = Math.Matrix(raw)
                inverse.invert()
                if position is None:
                    position = _try_vector_tuple(inverse.translation)
                if direction is None:
                    direction = _try_vector_tuple(inverse.applyToAxis(2))
                source = 'inverse(camera.matrix)'
            except Exception:
                pass
        if position is None or direction is None:
            return None
        direction = _normalised_tuple(direction, (0.0, 0.0, 1.0))
        desired_point = self._desired_camera_point()
        if desired_point is not None:
            desired_direction = _subtract(desired_point, position)
            if _dot(desired_direction, direction) < 0.0:
                direction = (-direction[0], -direction[1], -direction[2])
                source += '+aim_sign_corrected'
        return position, direction, source

    def _projection_data(self, vehicles):
        import BigWorld
        camera = BigWorld.camera()
        if camera is None:
            return None
        camera_data = self._camera_position_and_direction(camera)
        if camera_data is None:
            return None
        camera_position, forward, camera_source = camera_data

        right = _normalised_tuple(_cross((0.0, 1.0, 0.0), forward),
            (1.0, 0.0, 0.0))
        up = _normalised_tuple(_cross(forward, right),
            (0.0, 1.0, 0.0))

        try:
            screen = BigWorld.screenSize()
            width = max(1.0, float(screen[0]))
            height = max(1.0, float(screen[1]))
        except Exception:
            width, height = 1280.0, 720.0
        fov = 0.0
        aspect = width / height
        try:
            projection = BigWorld.projection()
            fov = float(getattr(projection, 'fov', 0.0) or 0.0)
            projection_aspect = float(getattr(
                projection, 'aspectRatio', 0.0) or 0.0)
            if 0.2 < projection_aspect < 8.0:
                aspect = projection_aspect
        except Exception:
            pass
        if not fov or fov < 0.1 or fov > 3.0:
            try:
                fov = float(getattr(camera, 'fov', 0.0) or 0.0)
            except Exception:
                fov = 0.0
        if not fov or fov < 0.1 or fov > 3.0:
            fov = math.radians(60.0)
        tangent = math.tan(fov * 0.5)
        if tangent <= 0.0001:
            tangent = math.tan(math.radians(30.0))

        if self._camera_source != camera_source:
            previous = self._camera_source
            self._camera_source = camera_source
            LOG_EVENT('modules', 'internal_layout_camera_basis_bound',
                source=camera_source, previous_source=previous,
                control_mode=self._camera_control_name(),
                camera_position=camera_position,
                camera_direction=forward)
        return {
            'mode': 'camera_basis_pos_z',
            'camera_position': camera_position,
            'right': right,
            'up': up,
            'forward': forward,
            'width': width,
            'height': height,
            'aspect': aspect,
            'tangent': tangent,
        }

    def _world_to_camera(self, world_point, projection_data):
        world = _try_vector_tuple(world_point)
        if world is None:
            return (0.0, 0.0, -1.0)
        delta = _subtract(world, projection_data['camera_position'])
        return (_dot(delta, projection_data['right']),
            _dot(delta, projection_data['up']),
            _dot(delta, projection_data['forward']))

    def _project_camera(self, camera_point, projection_data):
        depth = float(camera_point[2])
        if depth <= NEAR_PLANE or depth > MAX_AIM_DRAW_DISTANCE:
            return None
        x = float(camera_point[0]) / max(0.0001,
            depth * projection_data['tangent'] * projection_data['aspect'])
        y = float(camera_point[1]) / max(0.0001,
            depth * projection_data['tangent'])
        return (x, y, depth)

    def _project_world(self, world_point, projection_data):
        return self._project_camera(
            self._world_to_camera(world_point, projection_data),
            projection_data)

    def _clip_camera_segment(self, point_a, point_b):
        depth_a = float(point_a[2])
        depth_b = float(point_b[2])
        if depth_a <= NEAR_PLANE and depth_b <= NEAR_PLANE:
            return None
        if depth_a <= NEAR_PLANE:
            denominator = depth_b - depth_a
            if abs(denominator) <= 0.000001:
                return None
            point_a = _lerp3(point_a, point_b,
                (NEAR_PLANE - depth_a) / denominator)
        elif depth_b <= NEAR_PLANE:
            denominator = depth_a - depth_b
            if abs(denominator) <= 0.000001:
                return None
            point_b = _lerp3(point_b, point_a,
                (NEAR_PLANE - depth_b) / denominator)
        return point_a, point_b

    def _clip_screen_segment(self, point_a, point_b):
        margin = SCREEN_CULL_MARGIN
        x0, y0 = float(point_a[0]), float(point_a[1])
        x1, y1 = float(point_b[0]), float(point_b[1])
        dx = x1 - x0
        dy = y1 - y0
        u1 = 0.0
        u2 = 1.0
        tests = ((-dx, x0 + margin), (dx, margin - x0),
            (-dy, y0 + margin), (dy, margin - y0))
        for p_value, q_value in tests:
            if abs(p_value) <= 0.000001:
                if q_value < 0.0:
                    return None
                continue
            ratio = q_value / p_value
            if p_value < 0.0:
                if ratio > u2:
                    return None
                if ratio > u1:
                    u1 = ratio
            else:
                if ratio < u1:
                    return None
                if ratio < u2:
                    u2 = ratio
        clipped_a = (x0 + dx * u1, y0 + dy * u1,
            point_a[2] + (point_b[2] - point_a[2]) * u1)
        clipped_b = (x0 + dx * u2, y0 + dy * u2,
            point_a[2] + (point_b[2] - point_a[2]) * u2)
        return clipped_a, clipped_b

    def _line_projected(self, index, projected_a, projected_b, colour,
            thickness, projection_data, z_order=0.977):
        if index >= MAX_LINE_COMPONENTS:
            return index
        clipped_screen = self._clip_screen_segment(projected_a, projected_b)
        if clipped_screen is None:
            return index
        projected_a, projected_b = clipped_screen
        width_px = max(1.0, float(projection_data['width']))
        height_px = max(1.0, float(projection_data['height']))
        dx_clip = float(projected_b[0]) - float(projected_a[0])
        dy_clip = float(projected_b[1]) - float(projected_a[1])
        dx_px = dx_clip * width_px * 0.5
        dy_px = dy_clip * height_px * 0.5
        pixel_length = math.sqrt(dx_px * dx_px + dy_px * dy_px)
        clip_length = math.sqrt(dx_clip * dx_clip + dy_clip * dy_clip)
        if pixel_length < 0.65 or clip_length < 0.000001:
            return index + 1

        texture_name = self._texture_for_colour(colour, thickness)
        entry = self._acquire_line(texture_name)
        component = self._line_component(entry)
        center_x = (float(projected_a[0]) + float(projected_b[0])) * 0.5
        center_y = (float(projected_a[1]) + float(projected_b[1])) * 0.5
        angle = math.atan2(dy_clip, dx_clip)
        has_matrix = False
        try:
            has_matrix = entry.get('matrix') is not None
        except Exception:
            has_matrix = False
        if has_matrix:
            normal_x = -dy_clip / clip_length
            normal_y = dx_clip / clip_length
            pixels_per_clip = math.sqrt(
                (normal_x * width_px * 0.5) ** 2 +
                (normal_y * height_px * 0.5) ** 2)
            height_clip = max(0.00001,
                float(thickness) / max(0.00001, pixels_per_clip))
            _safe_set(component, 'widthMode', 'CLIP')
            _safe_set(component, 'heightMode', 'CLIP')
            _safe_set(component, 'width', clip_length)
            _safe_set(component, 'height', height_clip)
            self._set_line_transform(entry, center_x, center_y, z_order,
                angle)
        else:
            _safe_set(component, 'widthMode', 'PIXEL')
            _safe_set(component, 'heightMode', 'PIXEL')
            _safe_set(component, 'width', pixel_length)
            _safe_set(component, 'height', max(1.0, float(thickness)))
            self._set_line_transform(entry, center_x, center_y, z_order, 0.0)
            angle_degrees = math.degrees(math.atan2(dy_px, dx_px))
            _safe_set(component, 'angle', angle_degrees)
        _safe_set_gui_colour(component, (255, 255, 255, 255))
        _safe_set(component, 'visible', True)
        return index + 1

    def _line(self, index, point_a, point_b, colour, thickness,
            projection_data, z_order=0.977):
        if index >= MAX_LINE_COMPONENTS:
            return index
        clipped_camera = self._clip_camera_segment(point_a, point_b)
        if clipped_camera is None:
            return index
        projected_a = self._project_camera(
            clipped_camera[0], projection_data)
        projected_b = self._project_camera(
            clipped_camera[1], projection_data)
        if projected_a is None or projected_b is None:
            return index
        return self._line_projected(index, projected_a, projected_b, colour,
            thickness, projection_data, z_order)

    def _screen_line(self, index, point_a, point_b, colour, thickness,
            projection_data, z_order=0.985):
        return self._line_projected(index, point_a, point_b, colour,
            thickness, projection_data, z_order)

    def _screen_strip(self, index, x0, x1, y, colour, height_px,
            projection_data, z_order=0.958):
        return self._screen_line(index, (float(x0), float(y), 1.0),
            (float(x1), float(y), 1.0), colour, max(1.0, float(height_px)),
            projection_data, z_order)

    def _wire_segment(self, index, point_a, point_b, colour, thickness,
            projection_data, emphasis=False, focused=False):
        glow_factor = 0.34 if focused else (0.22 if emphasis else 0.15)
        glow_thickness = float(thickness) + (2.8 if focused else 1.8)
        index = self._line(index, point_a, point_b,
            _with_alpha(colour, glow_factor), glow_thickness,
            projection_data, 0.974)
        if index < MAX_LINE_COMPONENTS:
            core_colour = colour if focused else _with_alpha(colour, 0.96)
            index = self._line(index, point_a, point_b, core_colour,
                float(thickness), projection_data, 0.982)
        return index

    def _draw_focus_marker(self, line_index, point, colour,
            projection_data):
        if point is None:
            return line_index
        if (abs(float(point[0])) > SCREEN_CULL_MARGIN or
                abs(float(point[1])) > SCREEN_CULL_MARGIN):
            return line_index
        width = max(1.0, float(projection_data['width']))
        height = max(1.0, float(projection_data['height']))
        dx = 15.0 / width
        dy = 15.0 / height
        center_x = float(point[0])
        center_y = float(point[1])
        corners = (
            (center_x, center_y + dy, 1.0),
            (center_x + dx, center_y, 1.0),
            (center_x, center_y - dy, 1.0),
            (center_x - dx, center_y, 1.0),
        )
        for index in range(4):
            start = corners[index]
            end = corners[(index + 1) % 4]
            line_index = self._screen_line(line_index, start, end,
                (0, 0, 0, 210), 4.2, projection_data, 0.986)
            line_index = self._screen_line(line_index, start, end,
                colour, 1.8, projection_data, 0.989)
        return line_index

    def _clip_camera_polygon(self, points):
        if not points:
            return []
        output = []
        previous = points[-1]
        previous_inside = float(previous[2]) > NEAR_PLANE
        for current in points:
            current_inside = float(current[2]) > NEAR_PLANE
            if current_inside != previous_inside:
                denominator = float(current[2]) - float(previous[2])
                if abs(denominator) > 0.000001:
                    factor = (NEAR_PLANE - float(previous[2])) / denominator
                    output.append(_lerp3(previous, current, factor))
            if current_inside:
                output.append(current)
            previous = current
            previous_inside = current_inside
        return output

    def _clip_projected_polygon(self, points):
        margin = SCREEN_CULL_MARGIN

        def clip_axis(polygon, axis, bound, keep_greater):
            if not polygon:
                return []
            clipped = []
            previous = polygon[-1]
            previous_inside = ((previous[axis] >= bound) if keep_greater
                else (previous[axis] <= bound))
            for current in polygon:
                current_inside = ((current[axis] >= bound) if keep_greater
                    else (current[axis] <= bound))
                if current_inside != previous_inside:
                    denominator = float(current[axis]) - float(previous[axis])
                    if abs(denominator) > 0.000001:
                        factor = (bound - float(previous[axis])) / denominator
                        clipped.append(_lerp3(previous, current, factor))
                if current_inside:
                    clipped.append(current)
                previous = current
                previous_inside = current_inside
            return clipped

        result = list(points)
        result = clip_axis(result, 0, -margin, True)
        result = clip_axis(result, 0, margin, False)
        result = clip_axis(result, 1, -margin, True)
        result = clip_axis(result, 1, margin, False)
        return result

    def _fill_projected_polygon(self, index, polygon, projection_data,
            colour, step_px, z_order=0.958):
        polygon = self._clip_projected_polygon(polygon)
        if len(polygon) < 3:
            return index, False
        minimum_y = max(-SCREEN_CULL_MARGIN,
            min(float(point[1]) for point in polygon))
        maximum_y = min(SCREEN_CULL_MARGIN,
            max(float(point[1]) for point in polygon))
        if maximum_y <= minimum_y:
            return index, False
        step_px = max(1.0, float(step_px))
        step_clip = (2.0 * step_px) / max(1.0, projection_data['height'])
        solid_height = max(2.0, step_px * FILL_STRIP_OVERLAP)
        y = minimum_y + step_clip * 0.5
        before = index
        while y <= maximum_y + 0.000001 and index < MAX_LINE_COMPONENTS:
            intersections = []
            count = len(polygon)
            for edge_index in range(count):
                first = polygon[edge_index]
                second = polygon[(edge_index + 1) % count]
                y0 = float(first[1])
                y1 = float(second[1])
                if abs(y1 - y0) <= 0.000001:
                    continue
                if not ((y0 <= y < y1) or (y1 <= y < y0)):
                    continue
                factor = (y - y0) / (y1 - y0)
                intersections.append(float(first[0]) +
                    (float(second[0]) - float(first[0])) * factor)
            intersections.sort()
            pair_index = 0
            while pair_index + 1 < len(intersections):
                index = self._screen_strip(index,
                    intersections[pair_index], intersections[pair_index + 1],
                    y, colour, solid_height, projection_data, z_order)
                pair_index += 2
                if index >= MAX_LINE_COMPONENTS:
                    break
            y += step_clip
        return index, index > before

    def _fill_camera_face(self, index, camera_points, projection_data,
            colour, step_px, z_order=0.958):
        clipped = self._clip_camera_polygon(camera_points)
        if len(clipped) < 3:
            return index, False
        projected = []
        for point in clipped:
            screen = self._project_camera(point, projection_data)
            if screen is None:
                return index, False
            projected.append(screen)
        return self._fill_projected_polygon(index, projected,
            projection_data, colour, step_px, z_order)

    def _fill_camera_hull(self, index, camera_points, projection_data,
            colour, opacity, emphasis, z_order=0.960):
        projected = []
        depths = []
        for point in camera_points:
            screen = self._project_camera(point, projection_data)
            if screen is None:
                continue
            projected.append((float(screen[0]), float(screen[1])))
            depths.append(float(screen[2]))
        hull = _convex_hull(projected)
        if len(hull) < 3:
            return index, False
        average_depth = (sum(depths) / float(len(depths)) if depths else 1.0)
        polygon = tuple((point[0], point[1], average_depth)
            for point in hull)
        alpha_factor = (AIM_FILL_ALPHA if emphasis else NEAR_FILL_ALPHA)
        alpha_factor *= _clamp(opacity, 0.0, 1.0)
        fill_colour = _shade_colour(colour, 0.96, alpha_factor)
        step_px = AIM_FILL_STEP_PX if emphasis else NEAR_FILL_STEP_PX
        return self._fill_projected_polygon(index, polygon,
            projection_data, fill_colour, step_px, z_order)

    def _fill_faces(self, index, faces, projection_data, colour, emphasis,
            opacity=1.0):
        valid_faces = []
        all_points = []
        for face in faces:
            if len(face) < 3:
                continue
            if max(float(point[2]) for point in face) <= NEAR_PLANE:
                continue
            valid_faces.append(face)
            all_points.extend(face)
        if not valid_faces or not all_points:
            return index, False

        object_center = (
            sum(float(point[0]) for point in all_points) / float(len(all_points)),
            sum(float(point[1]) for point in all_points) / float(len(all_points)),
            sum(float(point[2]) for point in all_points) / float(len(all_points)))
        visible_faces = []
        for face in valid_faces:
            first = face[0]
            second = face[1]
            third = face[2]
            normal = _cross(_subtract(second, first),
                _subtract(third, first))
            normal_length_sq = _dot(normal, normal)
            if normal_length_sq <= 0.00000001:
                continue
            face_center = (
                sum(float(point[0]) for point in face) / float(len(face)),
                sum(float(point[1]) for point in face) / float(len(face)),
                sum(float(point[2]) for point in face) / float(len(face)))
            radial = _subtract(face_center, object_center)
            if _dot(normal, radial) < 0.0:
                normal = (-normal[0], -normal[1], -normal[2])
            if _dot(normal, face_center) >= 0.0:
                continue
            depth = sum(float(point[2]) for point in face) / float(len(face))
            visible_faces.append((depth, face, normal))

        if not visible_faces:
            fallback = []
            for face in valid_faces:
                depth = sum(float(point[2]) for point in face) / float(len(face))
                fallback.append((depth, face, (0.0, 0.0, -1.0)))
            fallback.sort(key=lambda item: item[0])
            visible_faces = fallback[:2]

        visible_faces.sort(key=lambda item: item[0], reverse=True)
        nearest = min(item[0] for item in visible_faces)
        farthest = max(item[0] for item in visible_faces)
        depth_span = max(0.0001, farthest - nearest)
        alpha_factor = (AIM_FILL_ALPHA if emphasis else NEAR_FILL_ALPHA)
        alpha_factor *= _clamp(opacity, 0.0, 1.0)
        step_px = AIM_FILL_STEP_PX if emphasis else NEAR_FILL_STEP_PX
        before = index
        for depth, face, normal in visible_faces:
            near_factor = (farthest - depth) / depth_span
            light = 0.88 + 0.12 * min(1.0, abs(float(normal[2])) /
                max(0.0001, math.sqrt(_dot(normal, normal))))
            light *= 0.96 + 0.04 * near_factor
            face_colour = _shade_colour(colour, light, alpha_factor)
            depth_factor = 1.0 - min(1.0, max(0.0, depth) / 250.0)
            z_order = 0.944 + 0.026 * depth_factor + 0.004 * near_factor
            index, unused_drawn = self._fill_camera_face(index, face,
                projection_data, face_colour, step_px, z_order)
            if index >= MAX_LINE_COMPONENTS:
                break
        return index, index > before

    def _matrix_from_provider(self, provider):
        if provider is None:
            return None
        try:
            if callable(provider):
                provider = provider()
        except Exception:
            return None
        try:
            import Math
            return Math.Matrix(provider)
        except Exception:
            return None

    def _model_world_matrix(self, model):
        if model is None:
            return None
        matrix = self._matrix_from_provider(getattr(model, 'matrix', None))
        if matrix is not None:
            return matrix
        return self._matrix_from_provider(model)

    def _anchor_reference_position(self, vehicle):
        position = _try_vector_tuple(getattr(vehicle, 'position', None))
        if position is not None:
            return position
        try:
            return _try_vector_tuple(vehicle.matrix.translation)
        except Exception:
            return None

    def _validated_parent_matrix(self, vehicle, parent, matrix, source):
        if matrix is None:
            return None
        translation = _try_vector_tuple(getattr(matrix, 'translation', None))
        reference = self._anchor_reference_position(vehicle)
        if translation is None:
            return None
        if reference is not None:
            maximum_distance = 30.0 if parent == 'gun' else 20.0
            if _distance_sq(translation, reference) > maximum_distance * maximum_distance:
                LOG_EVENT('modules', 'internal_layout_vehicle_anchor_rejected',
                    vehicle_id=getattr(vehicle, 'id', -1), parent=parent,
                    source=source, translation=translation,
                    vehicle_position=reference,
                    reason='render_anchor_too_far_from_vehicle')
                return None
        return matrix

    def _cache_parent_node(self, vehicle, attribute, model, node_name,
            local_matrix=None):
        provider = getattr(vehicle, attribute, None)
        if provider is not None:
            return provider
        if model is None:
            return None
        try:
            if local_matrix is None:
                provider = model.node(node_name)
            else:
                provider = model.node(node_name, local_matrix)
            setattr(vehicle, attribute, provider)
            return provider
        except Exception:
            return None

    def _parent_world_matrices(self, vehicle):
        result = {}
        sources = {}
        chassis = getattr(vehicle, '_chassis_model', None)
        hull = getattr(vehicle, '_hull_model', None)
        turret = getattr(vehicle, '_turret_model', None)
        gun = getattr(vehicle, '_gun_model', None)
        if chassis is None:
            chassis = getattr(vehicle, 'model', None)
        if chassis is None:
            entity = getattr(vehicle, 'bw_entity', None)
            chassis = getattr(entity, 'model', None)

        chassis_matrix = self._validated_parent_matrix(vehicle, 'chassis',
            self._model_world_matrix(chassis), 'render_model.matrix')
        if chassis_matrix is not None:
            result['chassis'] = chassis_matrix
            sources['chassis'] = 'render_model.matrix'

        hull_provider = getattr(vehicle, '_hull_node', None)
        if hull_provider is None:
            hull_provider = self._cache_parent_node(vehicle, '_hull_node',
                chassis, 'V')
        hull_matrix = self._validated_parent_matrix(vehicle, 'hull',
            self._matrix_from_provider(hull_provider), 'chassis.node(V)')
        if hull_matrix is None:
            hull_matrix = self._validated_parent_matrix(vehicle, 'hull',
                self._model_world_matrix(hull), 'hull_model.matrix')
        if hull_matrix is not None:
            result['hull'] = hull_matrix
            sources['hull'] = ('chassis.node(V)' if hull_provider is not None
                else 'hull_model.matrix')

        turret_provider = getattr(vehicle, '_t_node', None)
        if turret_provider is None:
            turret_local = getattr(vehicle, '_t_mat', None)
            turret_provider = self._cache_parent_node(vehicle, '_t_node',
                hull, 'HP_turretJoint', turret_local)
        turret_matrix = self._validated_parent_matrix(vehicle, 'turret',
            self._matrix_from_provider(turret_provider),
            'hull.node(HP_turretJoint)')
        if turret_matrix is None:
            turret_matrix = self._validated_parent_matrix(vehicle, 'turret',
                self._model_world_matrix(turret), 'turret_model.matrix')
        if turret_matrix is not None:
            result['turret'] = turret_matrix
            sources['turret'] = ('hull.node(HP_turretJoint)' if
                turret_provider is not None else 'turret_model.matrix')

        gun_provider = getattr(vehicle, '_g_node', None)
        if gun_provider is None:
            gun_local = getattr(vehicle, '_g_mat', None)
            gun_provider = self._cache_parent_node(vehicle, '_g_node',
                turret, 'HP_gunJoint', gun_local)
        gun_matrix = self._validated_parent_matrix(vehicle, 'gun',
            self._matrix_from_provider(gun_provider),
            'turret.node(HP_gunJoint)')
        if gun_matrix is None:
            gun_matrix = self._validated_parent_matrix(vehicle, 'gun',
                self._model_world_matrix(gun), 'gun_model.matrix')
        if gun_matrix is not None:
            result['gun'] = gun_matrix
            sources['gun'] = ('turret.node(HP_gunJoint)' if
                gun_provider is not None else 'gun_model.matrix')

        vehicle_id = getattr(vehicle, 'id', id(vehicle))
        cache_key = (vehicle_id, tuple(sorted(sources.items())))
        if cache_key not in self._anchor_log_cache:
            self._anchor_log_cache.add(cache_key)
            translations = {}
            for parent, matrix in result.items():
                translations[parent] = _vector_tuple(matrix.translation)
            LOG_EVENT('modules', 'internal_layout_vehicle_anchor_bound',
                vehicle_id=vehicle_id, sources=sources,
                translations=translations,
                source='render_model_nodes_direct_world')
        return result

    def _local_to_camera(self, local_point, parent_world_matrix,
            projection_data):
        import Math
        local = Math.Vector3(local_point[0], local_point[1], local_point[2])
        world = parent_world_matrix.applyPoint(local)
        return self._world_to_camera(world, projection_data)

    def _draw_box(self, line_index, primitive, parent_world_matrix,
            projection_data, colour, thickness, emphasis, focused=False,
            opacity=1.0):
        points = []
        for corner in _primitive_corners(primitive):
            points.append(self._local_to_camera(
                corner, parent_world_matrix, projection_data))
        faces = []
        for face_indices in _BOX_FACES:
            faces.append(tuple(points[index] for index in face_indices))
        before = line_index
        fill_opacity = _clamp(opacity * (1.10 if focused else 1.0), 0.0, 1.0)
        line_index, unused_filled = self._fill_faces(line_index, faces,
            projection_data, colour, emphasis, fill_opacity)
        outline_colour = _with_alpha(colour,
            1.0 if focused else _clamp(0.70 + opacity * 0.30, 0.0, 1.0))
        edge_thickness = (2.8 if focused else
            (2.05 if emphasis else 1.25))
        for start_index, end_index in _BOX_EDGES:
            line_index = self._wire_segment(line_index,
                points[start_index], points[end_index], outline_colour,
                edge_thickness, projection_data, emphasis, focused)
            if line_index >= MAX_LINE_COMPONENTS:
                break
        return line_index, line_index > before

    def _sphere_local_point(self, center, radius, plane, angle):
        cosine = math.cos(angle) * radius
        sine = math.sin(angle) * radius
        if plane == 0:
            return (center[0] + cosine, center[1] + sine, center[2])
        if plane == 1:
            return (center[0] + cosine, center[1], center[2] + sine)
        return (center[0], center[1] + cosine, center[2] + sine)

    def _sphere_mesh_faces(self, center, radius, longitude_segments,
            latitude_segments):
        rings = []
        for latitude_index in range(latitude_segments + 1):
            latitude = (-math.pi * 0.5 + math.pi *
                float(latitude_index) / float(latitude_segments))
            ring_radius = math.cos(latitude) * radius
            height = math.sin(latitude) * radius
            ring = []
            for longitude_index in range(longitude_segments):
                longitude = (math.pi * 2.0 * float(longitude_index) /
                    float(longitude_segments))
                ring.append((center[0] + math.cos(longitude) * ring_radius,
                    center[1] + height,
                    center[2] + math.sin(longitude) * ring_radius))
            rings.append(ring)
        faces = []
        for latitude_index in range(latitude_segments):
            lower = rings[latitude_index]
            upper = rings[latitude_index + 1]
            for longitude_index in range(longitude_segments):
                next_index = (longitude_index + 1) % longitude_segments
                faces.append((lower[longitude_index],
                    lower[next_index], upper[next_index],
                    upper[longitude_index]))
        return faces

    def _ellipsoid_circle_point(self, center, radii, plane, angle,
            yaw_degrees):
        cosine = math.cos(angle)
        sine = math.sin(angle)
        if plane == 0:
            point = (center[0] + cosine * radii[0],
                center[1] + sine * radii[1], center[2])
        elif plane == 1:
            point = (center[0] + cosine * radii[0], center[1],
                center[2] + sine * radii[2])
        else:
            point = (center[0], center[1] + cosine * radii[1],
                center[2] + sine * radii[2])
        return _rotate_local_y(point, center, yaw_degrees)

    def _ellipsoid_mesh_faces(self, center, radii, yaw_degrees,
            longitude_segments, latitude_segments):
        rings = []
        for latitude_index in range(latitude_segments + 1):
            latitude = (-math.pi * 0.5 + math.pi *
                float(latitude_index) / float(latitude_segments))
            horizontal = math.cos(latitude)
            height = math.sin(latitude)
            ring = []
            for longitude_index in range(longitude_segments):
                longitude = (math.pi * 2.0 * float(longitude_index) /
                    float(longitude_segments))
                point = (center[0] + math.cos(longitude) * horizontal * radii[0],
                    center[1] + height * radii[1],
                    center[2] + math.sin(longitude) * horizontal * radii[2])
                ring.append(_rotate_local_y(point, center, yaw_degrees))
            rings.append(ring)
        faces = []
        for latitude_index in range(latitude_segments):
            lower = rings[latitude_index]
            upper = rings[latitude_index + 1]
            for longitude_index in range(longitude_segments):
                next_index = (longitude_index + 1) % longitude_segments
                faces.append((lower[longitude_index], lower[next_index],
                    upper[next_index], upper[longitude_index]))
        return faces

    def _draw_ellipsoid(self, line_index, primitive, parent_world_matrix,
            projection_data, colour, thickness, emphasis, focused=False,
            opacity=1.0):
        center = primitive.get('center', (0.0, 0.0, 0.0))
        radii = tuple(max(0.001, float(value)) for value in primitive.get(
            'radii', primitive.get('half_extents', (0.1, 0.1, 0.1))))
        yaw = float(primitive.get('rotation_yaw_degrees', 0.0) or 0.0)
        circle_segments = (AIM_SPHERE_LONGITUDE if emphasis else
            NEAR_SPHERE_LONGITUDE)
        latitude_segments = (AIM_SPHERE_LATITUDE if emphasis else
            NEAR_SPHERE_LATITUDE)
        local_faces = self._ellipsoid_mesh_faces(center, radii, yaw,
            circle_segments, latitude_segments)
        camera_faces = self._transform_local_faces(local_faces,
            parent_world_matrix, projection_data)
        camera_points = []
        for face in camera_faces:
            camera_points.extend(face)
        before = line_index
        line_index, unused_filled = self._fill_camera_hull(line_index,
            camera_points, projection_data, colour,
            _clamp(opacity * (1.08 if focused else 1.0), 0.0, 1.0),
            emphasis, 0.962)
        planes = (0, 1, 2) if emphasis else (0, 1)
        edge_thickness = (2.45 if focused else
            (1.85 if emphasis else 1.15))
        for plane in planes:
            previous = None
            first = None
            for segment_index in range(circle_segments):
                angle = (math.pi * 2.0 * float(segment_index) /
                    float(circle_segments))
                local = self._ellipsoid_circle_point(center, radii, plane,
                    angle, yaw)
                current = self._local_to_camera(local, parent_world_matrix,
                    projection_data)
                if first is None:
                    first = current
                if previous is not None:
                    line_index = self._wire_segment(line_index, previous,
                        current, colour if plane == 0 else _with_alpha(
                            colour, 0.68), edge_thickness, projection_data,
                        emphasis, focused)
                previous = current
                if line_index >= MAX_LINE_COMPONENTS:
                    return line_index, line_index > before
            if previous is not None and first is not None:
                line_index = self._wire_segment(line_index, previous, first,
                    colour if plane == 0 else _with_alpha(colour, 0.68),
                    edge_thickness, projection_data, emphasis, focused)
        return line_index, line_index > before

    def _transform_local_faces(self, local_faces, parent_world_matrix,
            projection_data):
        faces = []
        for local_face in local_faces:
            camera_face = []
            for local in local_face:
                camera_face.append(self._local_to_camera(local,
                    parent_world_matrix, projection_data))
            faces.append(tuple(camera_face))
        return faces

    def _draw_sphere(self, line_index, primitive, parent_world_matrix,
            projection_data, colour, thickness, emphasis, focused=False,
            opacity=1.0):
        center = primitive.get('center', (0.0, 0.0, 0.0))
        radius = max(0.001, float(primitive.get('radius', 0.1)))
        circle_segments = (AIM_SPHERE_LONGITUDE if emphasis else
            NEAR_SPHERE_LONGITUDE)
        latitude_segments = (AIM_SPHERE_LATITUDE if emphasis else
            NEAR_SPHERE_LATITUDE)
        local_faces = self._sphere_mesh_faces(center, radius,
            circle_segments, latitude_segments)
        camera_faces = self._transform_local_faces(local_faces,
            parent_world_matrix, projection_data)
        camera_points = []
        for face in camera_faces:
            camera_points.extend(face)
        before = line_index
        line_index, unused_filled = self._fill_camera_hull(line_index,
            camera_points, projection_data, colour,
            _clamp(opacity * (1.08 if focused else 1.0), 0.0, 1.0),
            emphasis, 0.962)

        planes = (0, 1, 2) if emphasis else (0, 1)
        for plane in planes:
            plane_colour = colour if plane == 0 else _with_alpha(colour, 0.68)
            previous = None
            first = None
            for segment_index in range(circle_segments):
                angle = (math.pi * 2.0 * float(segment_index) /
                    float(circle_segments))
                local = self._sphere_local_point(center, radius, plane,
                    angle)
                current = self._local_to_camera(local, parent_world_matrix,
                    projection_data)
                if first is None:
                    first = current
                if previous is not None:
                    line_index = self._wire_segment(line_index, previous,
                        current, plane_colour,
                        2.45 if focused else (1.85 if emphasis else 1.15),
                        projection_data, emphasis, focused)
                previous = current
                if line_index >= MAX_LINE_COMPONENTS:
                    return line_index, line_index > before
            if previous is not None and first is not None:
                line_index = self._wire_segment(line_index, previous, first,
                    plane_colour,
                    2.45 if focused else (1.85 if emphasis else 1.15),
                    projection_data, emphasis, focused)
        return line_index, line_index > before

    def _capsule_local_point(self, center, axis, axial, radial,
            longitude):
        cosine = math.cos(longitude) * radial
        sine = math.sin(longitude) * radial
        if axis == 'x':
            return (center[0] + axial, center[1] + cosine,
                center[2] + sine)
        if axis == 'z':
            return (center[0] + cosine, center[1] + sine,
                center[2] + axial)
        return (center[0] + cosine, center[1] + axial,
            center[2] + sine)

    def _capsule_mesh_faces(self, center, radius, half_length, axis,
            longitude_segments, hemisphere_segments, yaw_degrees=0.0):
        ring_specs = []
        for index in range(hemisphere_segments + 1):
            angle = (-math.pi * 0.5 + math.pi * 0.5 *
                float(index) / float(hemisphere_segments))
            ring_specs.append((-half_length + math.sin(angle) * radius,
                math.cos(angle) * radius))
        ring_specs.append((half_length, radius))
        for index in range(1, hemisphere_segments + 1):
            angle = (math.pi * 0.5 * float(index) /
                float(hemisphere_segments))
            ring_specs.append((half_length + math.sin(angle) * radius,
                math.cos(angle) * radius))
        rings = []
        for axial, radial in ring_specs:
            ring = []
            for longitude_index in range(longitude_segments):
                longitude = (math.pi * 2.0 * float(longitude_index) /
                    float(longitude_segments))
                ring.append(_rotate_local_y(self._capsule_local_point(
                    center, axis, axial, radial, longitude), center,
                    yaw_degrees))
            rings.append(ring)
        faces = []
        for ring_index in range(len(rings) - 1):
            first_ring = rings[ring_index]
            second_ring = rings[ring_index + 1]
            for longitude_index in range(longitude_segments):
                next_index = (longitude_index + 1) % longitude_segments
                faces.append((first_ring[longitude_index],
                    first_ring[next_index], second_ring[next_index],
                    second_ring[longitude_index]))
        return faces, rings

    def _draw_capsule(self, line_index, primitive, parent_world_matrix,
            projection_data, colour, thickness, emphasis, focused=False,
            opacity=1.0):
        center = primitive.get('center', (0.0, 0.0, 0.0))
        radius = max(0.001, float(primitive.get('radius', 0.1)))
        half_length = max(0.0, float(primitive.get('half_length',
            primitive.get('halfHeight', radius * 2.0))))
        axis = str(primitive.get('axis', 'y')).lower()
        longitude_segments = (AIM_SPHERE_LONGITUDE if emphasis else
            NEAR_SPHERE_LONGITUDE)
        hemisphere_segments = 4 if emphasis else 2
        yaw = float(primitive.get('rotation_yaw_degrees', 0.0) or 0.0)
        local_faces, local_rings = self._capsule_mesh_faces(center, radius,
            half_length, axis, longitude_segments, hemisphere_segments, yaw)
        camera_faces = self._transform_local_faces(local_faces,
            parent_world_matrix, projection_data)
        camera_surface_points = []
        for face in camera_faces:
            camera_surface_points.extend(face)
        camera_rings = []
        for local_ring in local_rings:
            camera_ring = []
            for local in local_ring:
                camera_ring.append(self._local_to_camera(local,
                    parent_world_matrix, projection_data))
            camera_rings.append(camera_ring)
        before = line_index
        if not camera_rings:
            return line_index, False
        line_index, unused_filled = self._fill_camera_hull(line_index,
            camera_surface_points, projection_data, colour,
            _clamp(opacity * (1.08 if focused else 1.0), 0.0, 1.0),
            emphasis, 0.962)

        selected_ring_indices = [hemisphere_segments,
            hemisphere_segments + 1]
        if emphasis and hemisphere_segments > 1:
            selected_ring_indices.append(max(1, hemisphere_segments // 2))
            selected_ring_indices.append(min(len(camera_rings) - 2,
                hemisphere_segments + 1 + hemisphere_segments // 2))
        unique_indices = []
        for ring_index in selected_ring_indices:
            if ring_index not in unique_indices:
                unique_indices.append(ring_index)
        unique_indices.sort()
        ring_thickness = (2.45 if focused else
            (1.85 if emphasis else 1.15))
        for order, ring_index in enumerate(unique_indices):
            ring = camera_rings[ring_index]
            ring_colour = (colour if order in (0, len(unique_indices) - 1)
                else _with_alpha(colour, 0.62))
            for segment_index in range(longitude_segments):
                line_index = self._wire_segment(line_index,
                    ring[segment_index],
                    ring[(segment_index + 1) % longitude_segments],
                    ring_colour, ring_thickness, projection_data,
                    emphasis, focused)
                if line_index >= MAX_LINE_COMPONENTS:
                    return line_index, line_index > before

        rail_indices = (0, longitude_segments // 4,
            longitude_segments // 2, (longitude_segments * 3) // 4)
        if not emphasis:
            rail_indices = (0, longitude_segments // 2)
        for longitude_index in rail_indices:
            previous = None
            for ring in camera_rings:
                current = ring[longitude_index]
                if previous is not None:
                    line_index = self._wire_segment(line_index, previous,
                        current, _with_alpha(colour, 0.80), ring_thickness,
                        projection_data, emphasis, focused)
                previous = current
                if line_index >= MAX_LINE_COMPONENTS:
                    return line_index, line_index > before
        return line_index, line_index > before

    def _target_colour(self, target, emphasis, opacity=1.0):
        entity = str(target.get('entity', 'unknown') or 'unknown')
        key = entity
        if target.get('kind') == 'crew' and key not in _COLORS:
            roles = tuple(target.get('roles', ()))
            key = str(roles[0]) if roles else 'crew'
        base = _COLORS.get(key, _COLORS['unknown'])
        alpha_factor = (1.0 if emphasis else 0.82)
        alpha_factor *= _clamp(0.78 + float(opacity) * 0.22, 0.0, 1.0)
        return _with_alpha(base, alpha_factor)

    def _target_name(self, target):
        entity = str(target.get('entity', 'unknown') or 'unknown')
        if (entity == 'turretRotator' and bool(target.get(
                'fixed_fighting_compartment', False))):
            return 'GUN TRAVERSE'
        if target.get('kind') == 'crew':
            return _DISPLAY_NAMES.get(entity, entity.upper())
        return _DISPLAY_NAMES.get(entity, entity.upper())

    def _target_size_text(self, target):
        minimum = target.get('minimum')
        maximum = target.get('maximum')
        try:
            if minimum is not None and maximum is not None:
                dimensions = tuple(max(0.0,
                    float(maximum[axis]) - float(minimum[axis]))
                    for axis in range(3))
            else:
                half_extents = target.get('half_extents',
                    (0.0, 0.0, 0.0))
                dimensions = tuple(float(half_extents[axis]) * 2.0
                    for axis in range(3))
            return '%.2f x %.2f x %.2f m' % dimensions
        except Exception:
            return ''

    def _target_depth_group(self, target):
        parent = str(target.get('parent', 'hull') or 'hull').lower()
        if parent in ('turret', 'gun'):
            return 'upper'
        return 'lower'

    def _target_projection_info(self, target, parent_world_matrix,
            projection_data):
        projected = []
        depths = []
        for primitive in target.get('primitives', ()):
            for corner in _primitive_corners(primitive):
                camera_point = self._local_to_camera(corner,
                    parent_world_matrix, projection_data)
                depth = float(camera_point[2])
                if depth > NEAR_PLANE:
                    depths.append(depth)
                screen = self._project_camera(camera_point, projection_data)
                if screen is not None:
                    projected.append((float(screen[0]), float(screen[1])))
        if not projected or not depths:
            return None
        hull = _convex_hull(projected)
        area = _polygon_area(hull)
        if len(hull) < 3 or area <= 0.0:
            return None
        local_center = target.get('center', (0.0, 0.0, 0.0))
        camera_center = self._local_to_camera(local_center,
            parent_world_matrix, projection_data)
        screen_center = self._project_camera(camera_center, projection_data)
        if screen_center is None:
            polygon_center = _polygon_center(hull)
            screen_center = (polygon_center[0], polygon_center[1],
                sum(depths) / float(len(depths)))
        return {
            'target': target,
            'parent_world_matrix': parent_world_matrix,
            'hull': hull,
            'samples': _polygon_samples(hull),
            'area': area,
            'min_depth': min(depths),
            'max_depth': max(depths),
            'center_depth': float(camera_center[2]),
            'screen': screen_center,
            'group': self._target_depth_group(target),
            'exposure': 1.0,
            'depth_fraction': 0.0,
            'front_visible': True,
            'opacity': 1.0,
        }

    def _classify_front_targets(self, infos):
        group_ranges = {}
        internal_infos = []
        for info in infos:
            entity = str(info['target'].get('entity', '') or '')
            if entity in _FRONT_HIDDEN_ENTITIES:
                info['front_visible'] = False
                info['opacity'] = 0.42
                continue
            internal_infos.append(info)
            group = info['group']
            record = group_ranges.get(group)
            if record is None:
                group_ranges[group] = [info['min_depth'], info['max_depth']]
            else:
                record[0] = min(record[0], info['min_depth'])
                record[1] = max(record[1], info['max_depth'])

        ordered = sorted(internal_infos,
            key=lambda item: item['center_depth'])
        nearer = []
        visible_count = 0
        for info in ordered:
            target = info['target']
            entity = str(target.get('entity', '') or '')
            group_range = group_ranges.get(info['group'],
                (info['min_depth'], info['max_depth']))
            depth_span = max(0.001,
                float(group_range[1]) - float(group_range[0]))
            depth_fraction = ((float(info['center_depth']) -
                float(group_range[0])) / depth_span)
            depth_fraction = _clamp(depth_fraction, 0.0, 1.0)
            info['depth_fraction'] = depth_fraction

            samples = info.get('samples', ())
            exposed = 0
            total = 0
            for sample in samples:
                total += 1
                blocked = False
                for front in nearer:
                    if (float(front['center_depth']) + OCCLUSION_DEPTH_GAP >=
                            float(info['center_depth'])):
                        continue
                    if _point_in_polygon(sample, front['hull']):
                        blocked = True
                        break
                if not blocked:
                    exposed += 1
            exposure = (float(exposed) / float(total) if total else 0.0)
            info['exposure'] = exposure

            strong_uncovered = (exposure >= FRONT_STRONG_EXPOSURE and
                depth_fraction <= 0.90)
            normal_front = (exposure >= FRONT_MIN_EXPOSURE and
                depth_fraction <= FRONT_DEPTH_FRACTION)
            front_visible = (entity not in _FRONT_HIDDEN_ENTITIES and
                info['area'] >= MIN_PROJECTED_AREA and
                (normal_front or strong_uncovered))
            info['front_visible'] = bool(front_visible)
            if front_visible:
                visible_count += 1
            if info['area'] >= MIN_PROJECTED_AREA * 0.45:
                nearer.append(info)

            depth_fade = 1.0 - depth_fraction * 0.16
            info['opacity'] = _clamp((0.70 + exposure * 0.30) * depth_fade,
                0.52, 1.0)

        if visible_count == 0 and internal_infos:
            fallback = sorted(internal_infos, key=lambda item:
                (item['depth_fraction'] - item['exposure'] * 0.85,
                -item['area']))
            for info in fallback[:4]:
                info['front_visible'] = True
                info['opacity'] = max(0.72, info.get('opacity', 0.72))

    def _draw_target(self, line_index, parent_world_matrix, target,
            projection_data, emphasis, focused=False, opacity=1.0):
        colour = self._target_colour(target, emphasis, opacity)
        thickness = 3.0 if emphasis else 1.8
        primitive_count = 0
        for primitive in target.get('primitives', ()):
            shape = str(primitive.get('shape', 'aabb') or 'aabb').lower()
            if shape == 'sphere':
                line_index, drawn = self._draw_sphere(line_index, primitive,
                    parent_world_matrix, projection_data, colour,
                    thickness, emphasis, focused, opacity)
            elif shape == 'ellipsoid':
                line_index, drawn = self._draw_ellipsoid(line_index, primitive,
                    parent_world_matrix, projection_data, colour,
                    thickness, emphasis, focused, opacity)
            elif shape == 'capsule':
                line_index, drawn = self._draw_capsule(line_index, primitive,
                    parent_world_matrix, projection_data, colour,
                    thickness, emphasis, focused, opacity)
            else:
                line_index, drawn = self._draw_box(line_index, primitive,
                    parent_world_matrix, projection_data, colour,
                    thickness, emphasis, focused, opacity)
            if drawn:
                primitive_count += 1
            if line_index >= MAX_LINE_COMPONENTS:
                break
        label_point = None
        try:
            local_center = target.get('center', (0.0, 0.0, 0.0))
            camera_center = self._local_to_camera(local_center,
                parent_world_matrix, projection_data)
            label_point = self._project_camera(camera_center, projection_data)
        except Exception:
            label_point = None
        if (focused and label_point is not None and
                self._view_mode() == 'FOCUS'):
            line_index = self._draw_focus_marker(line_index, label_point,
                colour, projection_data)
        return (line_index, primitive_count, label_point, colour,
            self._target_size_text(target))

    def _vehicle_world_matrix(self, vehicle):
        model = getattr(vehicle, '_chassis_model', None)
        if model is None:
            model = getattr(vehicle, 'model', None)
        if model is None:
            entity = getattr(vehicle, 'bw_entity', None)
            model = getattr(entity, 'model', None)
        matrix = self._model_world_matrix(model)
        if matrix is not None:
            return matrix
        import Math
        return Math.Matrix(vehicle.matrix)

    def _vehicle_position(self, vehicle):
        try:
            return _vector_tuple(self._vehicle_world_matrix(vehicle).translation)
        except Exception:
            return _vector_tuple(getattr(vehicle, 'position', None))

    def _player_vehicle(self, vehicles, player):
        player_id = getattr(player, 'playerVehicleID', -999999)
        for vehicle in vehicles:
            if getattr(vehicle, 'id', -1) == player_id:
                return vehicle
        return None

    def _aimed_vehicle(self, vehicles, player, projection_data,
            player_vehicle):
        candidate = getattr(player, '_outlined_bot', None)
        if candidate is None:
            candidate = getattr(player, '_autoaim_target', None)
        if candidate in vehicles:
            return candidate
        candidate_id = getattr(candidate, 'id', None)
        if candidate_id is not None:
            for vehicle in vehicles:
                if getattr(vehicle, 'id', None) == candidate_id:
                    return vehicle
        best = None
        best_score = 999999.0
        for vehicle in vehicles:
            if vehicle is player_vehicle:
                continue
            try:
                screen = self._project_world(
                    self._vehicle_world_matrix(vehicle).translation,
                    projection_data)
            except Exception:
                screen = None
            if screen is None:
                continue
            radius = math.sqrt(screen[0] * screen[0] + screen[1] * screen[1])
            if radius > 0.18:
                continue
            score = radius * 1000.0 + screen[2] * 0.001
            if score < best_score:
                best_score = score
                best = vehicle
        return best

    def _selected_vehicles(self, vehicles, projection_data):
        player = self._player()
        if player is None:
            return [], None, None
        player_vehicle = self._player_vehicle(vehicles, player)
        player_position = (self._vehicle_position(player_vehicle)
            if player_vehicle is not None else
            _vector_tuple(getattr(player, 'position', None)))
        aimed = self._raw_aimed_vehicle(vehicles, player)
        if aimed is None:
            aimed = self._aimed_vehicle(vehicles, player, projection_data,
                player_vehicle)
        if aimed is not None and aimed is not player_vehicle:
            return [aimed], aimed, player_vehicle
        nearby = []
        for vehicle in vehicles:
            if vehicle is player_vehicle:
                continue
            distance_sq = _distance_sq(self._vehicle_position(vehicle),
                player_position)
            if distance_sq <= NEARBY_DISTANCE_SQ:
                nearby.append((distance_sq, getattr(vehicle, 'id', 0),
                    vehicle))
        nearby.sort(key=lambda item: (item[0], item[1]))
        selected = [item[2] for item in nearby[:MAX_NEARBY_VEHICLES]]
        return selected, None, player_vehicle

    def _view_mode(self):
        try:
            return _VIEW_MODES[self._view_mode_index]
        except Exception:
            return _VIEW_MODES[0]

    def _target_allowed(self, info, focused):
        mode = self._view_mode()
        target = info['target']
        kind = str(target.get('kind', 'module') or 'module').lower()
        if mode == 'ALL':
            return True
        if not bool(info.get('front_visible', False)):
            return False
        if mode == 'MODULES':
            return kind != 'crew'
        if mode == 'CREW':
            return kind == 'crew'
        if mode == 'FOCUS':
            return bool(focused)
        return True

    def _focus_candidate_allowed(self, info):
        mode = self._view_mode()
        target = info['target']
        kind = str(target.get('kind', 'module') or 'module').lower()
        if mode != 'ALL' and not bool(info.get('front_visible', False)):
            return False
        if mode == 'MODULES':
            return kind != 'crew'
        if mode == 'CREW':
            return kind == 'crew'
        return True

    def _draw_vehicle(self, line_index, vehicle, projection_data, emphasis):
        layout = self._layout(vehicle)
        targets = tuple(layout.get('targets', ()))
        if not targets:
            return line_index, 0, 0, []
        parents = self._parent_world_matrices(vehicle)
        infos = []
        for target in targets:
            parent_world_matrix = parents.get(target.get('parent'))
            if parent_world_matrix is None:
                continue
            try:
                info = self._target_projection_info(target,
                    parent_world_matrix, projection_data)
            except Exception:
                info = None
            if info is not None:
                infos.append(info)
        if not infos:
            return line_index, 0, 0, []

        self._classify_front_targets(infos)
        focused_info = None
        focused_score = None
        for info in infos:
            if not self._focus_candidate_allowed(info):
                continue
            distance_sq = _polygon_distance_sq((0.0, 0.0), info['hull'])
            score = (distance_sq, float(info['center_depth']),
                -float(info['area']))
            if focused_score is None or score < focused_score:
                focused_score = score
                focused_info = info

        infos.sort(key=lambda item: item['center_depth'], reverse=True)
        zones_drawn = 0
        primitives_drawn = 0
        labels = []
        mode = self._view_mode()
        for info in infos:
            focused = info is focused_info
            if not self._target_allowed(info, focused):
                continue
            target = info['target']
            parent_world_matrix = info['parent_world_matrix']
            opacity = float(info.get('opacity', 1.0))
            if mode == 'ALL' and not focused:
                opacity = min(opacity, 0.72)
            if focused:
                opacity = 1.0
            before = line_index
            (line_index, target_primitives, label_point,
                target_colour, size_text) = self._draw_target(
                    line_index, parent_world_matrix, target,
                    projection_data, emphasis, focused, opacity)
            if line_index > before:
                zones_drawn += 1
                primitives_drawn += target_primitives
                if emphasis and label_point is not None:
                    labels.append({
                        'point': label_point,
                        'name': self._target_name(target),
                        'entity': str(target.get('entity', '') or ''),
                        'colour': target_colour,
                        'size': size_text,
                        'kind': str(target.get('kind', 'module') or 'module'),
                        'focused': bool(focused),
                        'exposure': float(info.get('exposure', 1.0)),
                        'depth_fraction': float(info.get(
                            'depth_fraction', 0.0)),
                    })
            if line_index >= MAX_LINE_COMPONENTS:
                break
        return line_index, zones_drawn, primitives_drawn, labels

    def _show_label(self, index, text, position, colour, anchor):
        if index >= MAX_LABEL_COMPONENTS:
            return index
        while len(self._label_pool) <= index:
            self._label_pool.append(self._make_label())
        label = self._label_pool[index]
        _safe_set(label, 'text', text)
        _safe_set(label, 'position', (position[0], position[1], 0.995))
        _safe_set(label, 'horizontalAnchor', anchor)
        _safe_set(label, 'colour', colour)
        _safe_set(label, 'visible', True)
        return index + 1

    def _draw_aim_labels(self, line_index, labels, projection_data):
        if not self._labels_enabled or not labels:
            self._hide_labels_from(0)
            return line_index, 0
        grouped = {}
        for item in labels:
            point = item['point']
            if (abs(point[0]) > SCREEN_CULL_MARGIN or
                    abs(point[1]) > SCREEN_CULL_MARGIN):
                continue
            kind = str(item.get('kind', 'module') or 'module').lower()
            key = (kind, item['name'])
            score = (float(point[0]) * float(point[0]) +
                float(point[1]) * float(point[1]))
            record = grouped.get(key)
            if record is None:
                record = {
                    'point': point,
                    'name': item['name'],
                    'entity': item.get('entity', ''),
                    'colour': item['colour'],
                    'kind': kind,
                    'focused': bool(item.get('focused', False)),
                    'count': 1,
                    'score': score,
                    'exposure': float(item.get('exposure', 1.0)),
                }
                grouped[key] = record
            else:
                record['count'] += 1
                record['focused'] = (record['focused'] or
                    bool(item.get('focused', False)))
                record['exposure'] = max(record['exposure'],
                    float(item.get('exposure', 1.0)))
                if score < record['score']:
                    record['point'] = point
                    record['colour'] = item['colour']
                    record['score'] = score
        visible = list(grouped.values())
        if not visible:
            self._hide_labels_from(0)
            return line_index, 0

        order = {}
        for index, name in enumerate(_LABEL_ORDER):
            order[name] = index

        def label_sort(item):
            return (order.get(item['name'], len(order) + 1),
                0 if item['focused'] else 1, item['name'])
        visible.sort(key=label_sort)
        if len(visible) > 14:
            visible = visible[:14]

        max_x = max(item['point'][0] for item in visible)
        min_x = min(item['point'][0] for item in visible)
        max_y = max(item['point'][1] for item in visible)
        use_right = max_x < 0.68
        label_x = (min(0.89, max_x + 0.085) if use_right else
            max(-0.89, min_x - 0.085))
        anchor = 'LEFT' if use_right else 'RIGHT'
        spacing = 0.044
        top = min(0.80, max_y + 0.11)
        bottom_required = top - spacing * float(max(0, len(visible) - 1))
        if bottom_required < -0.80:
            top += -0.80 - bottom_required

        label_index = 0
        for row, item in enumerate(visible):
            text = item['name']
            if item['count'] > 1:
                text += ' x%d' % item['count']
            label_y = top - spacing * float(row)
            label_colour = (_with_alpha(item['colour'], 1.0) if
                item['focused'] else _with_alpha(item['colour'], 0.94))
            label_index = self._show_label(label_index, text,
                (label_x, label_y), label_colour, anchor)

            endpoint_x = label_x - 0.010 if use_right else label_x + 0.010
            start = (float(item['point'][0]), float(item['point'][1]), 1.0)
            elbow_x = (endpoint_x - 0.030 if use_right else
                endpoint_x + 0.030)
            elbow = (elbow_x, label_y, 1.0)
            end = (endpoint_x, label_y, 1.0)
            line_index = self._screen_line(line_index, start, elbow,
                (0, 0, 0, 150), 2.4, projection_data, 0.986)
            line_index = self._screen_line(line_index, elbow, end,
                (0, 0, 0, 150), 2.4, projection_data, 0.986)
            line_index = self._screen_line(line_index, start, elbow,
                _with_alpha(label_colour, 0.82), 1.0,
                projection_data, 0.989)
            line_index = self._screen_line(line_index, elbow, end,
                _with_alpha(label_colour, 0.82), 1.0,
                projection_data, 0.989)
            if line_index >= MAX_LINE_COMPONENTS:
                break
        self._hide_labels_from(label_index)
        return line_index, label_index

    def _hide_lines_from(self, index):
        if index <= 0:
            self._hide_all_lines()
        else:
            self._hide_unused_lines()

    def _hide_labels_from(self, index):
        for component in self._label_pool[index:]:
            _safe_set(component, 'visible', False)

    def _draw(self):
        self._ensure_gui()
        self._line_pool_changed = False
        self._begin_line_frame()
        all_vehicles = self._vehicles()
        projection_data = self._projection_data(all_vehicles)
        if projection_data is None:
            self._hide_lines_from(0)
            self._hide_labels_from(0)
            self._set_status('F8 XRAY | F9 MODE | F10 LABELS' % self._view_mode())
            return
        selected, aimed, unused_player_vehicle = self._selected_vehicles(
            all_vehicles, projection_data)
        records = []
        for vehicle in selected:
            try:
                world_matrix = self._vehicle_world_matrix(vehicle)
                screen = self._project_world(world_matrix.translation,
                    projection_data)
                if screen is None:
                    continue
                records.append((screen[2], vehicle))
            except Exception:
                continue
        records.sort(key=lambda item: item[0], reverse=True)

        old_line_pool = self._line_pool_size()
        old_label_pool = len(self._label_pool)
        line_index = 0
        vehicles_drawn = 0
        zones_drawn = 0
        primitives_drawn = 0
        aimed_labels = []
        for unused_depth, vehicle in records:
            emphasis = vehicle is aimed
            before = line_index
            line_index, vehicle_zones, vehicle_primitives, labels = (
                self._draw_vehicle(line_index, vehicle, projection_data,
                    emphasis))
            if line_index > before:
                vehicles_drawn += 1
                zones_drawn += vehicle_zones
                primitives_drawn += vehicle_primitives
                if emphasis:
                    aimed_labels = labels
            if line_index >= MAX_LINE_COMPONENTS:
                break
        label_count = 0
        if aimed_labels:
            line_index, label_count = self._draw_aim_labels(
                line_index, aimed_labels, projection_data)
        else:
            self._hide_labels_from(0)
        self._hide_lines_from(line_index)
        if (self._line_pool_changed or
                self._line_pool_size() != old_line_pool or
                len(self._label_pool) != old_label_pool):
            self._resort_gui()

        aimed_name = 'none'
        if aimed is not None:
            try:
                aimed_name = self._layout(aimed).get('vehicle_type', '?')
            except Exception:
                aimed_name = str(getattr(aimed, 'id', '?'))
        limited = ' | LIMIT' if line_index >= MAX_LINE_COMPONENTS else ''
        label_state = 'LABELS ON' if self._labels_enabled else 'LABELS OFF'
        self._set_status(
            'F8 XRAY | F9 MODE | F10 %s' % (
                self._view_mode(), zones_drawn, aimed_name, limited,
                label_state))
        self._last_draw_stats = {
            'vehicles_total': len(all_vehicles),
            'vehicles_selected': len(selected),
            'vehicles_drawn': vehicles_drawn,
            'aimed_vehicle_id': getattr(aimed, 'id', None),
            'zones_drawn': zones_drawn,
            'primitives_drawn': primitives_drawn,
            'labels': label_count,
            'lines': line_index,
            'nearby_distance': NEARBY_DISTANCE,
            'nearby_vehicle_limit': MAX_NEARBY_VEHICLES,
            'line_limit': MAX_LINE_COMPONENTS,
            'projection_mode': projection_data.get('mode'),
            'camera_source': self._camera_source,
            'anchor_source': 'render_model_nodes_direct_world',
            'geometry_source': 'layout.targets.primitives+render_model_nodes',
            'dimension_source': 'primitive.minimum/maximum+live_parent_matrix',
            'exact_primitive_dimensions': True,
            'baked_rgba_textures': True,
            'texture_pool_components': self._line_pool_size(),
            'filled_3d': True,
            'wireframe_renderer': True,
            'translucent_surface_renderer': True,
            'front_layer_depth_filter': True,
            'oriented_line_shader': bool(self._line_shader_supported),
            'view_mode': self._view_mode(),
            'labels_enabled': bool(self._labels_enabled),
            'labels_grouped': True,
            'profile_owned_real_shapes': True,
        }
