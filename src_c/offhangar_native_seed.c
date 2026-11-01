/*
 * Version-locked World of Tanks 0.8.2 WGVehicleFilter2 bridge.
 *
 * This module is intentionally pinned to one verified WorldOfTanks.exe.  It
 * exposes three operations that the embedded Python 2.6 API does not:
 * injecting a complete timestamped pose, publishing one native
 * WGVehicleFilter2 output and transferring the solved WGVehiclePhysics2 root
 * matrix into that filter. No code or vtable is patched. Every executable,
 * type, object, vtable and function pointer check must pass before native
 * memory is read or a virtual method is called.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <float.h>
#include <stdint.h>


typedef struct _PyObject {
	long ob_refcnt;
	void *ob_type;
} PyObject;

typedef PyObject *(__cdecl *PyCFunction)(PyObject *, PyObject *);

typedef struct _PyMethodDef {
	const char *ml_name;
	PyCFunction ml_meth;
	int ml_flags;
	const char *ml_doc;
} PyMethodDef;

typedef PyObject *(__cdecl *PyInitModule4Fn)(
	const char *, PyMethodDef *, const char *, PyObject *, int);
typedef int (__cdecl *PyArgParseTupleFn)(PyObject *, const char *, ...);
typedef void (__cdecl *PyErrSetStringFn)(PyObject *, const char *);

typedef struct Vec3 {
	float x;
	float y;
	float z;
} Vec3;

typedef void (__attribute__((thiscall)) *FilterInputFn)(
	void *, double, int, int, const Vec3 *, const Vec3 *, const Vec3 *);
typedef void (__attribute__((thiscall)) *FilterOutputFn)(void *, double);
typedef float (__attribute__((thiscall)) *MatrixAngleFn)(void *);


#define PYTHON_API_VERSION_26 1013
#define METH_VARARGS 0x0001

#define EXPECTED_PE_TIMESTAMP 0x50b8eccfU
#define EXPECTED_IMAGE_SIZE 0x0140f000U

#define RVA_PY_INIT_MODULE4 0x00019800U
#define RVA_PY_ARG_PARSE_TUPLE 0x0001d580U
#define RVA_PY_ERR_SET_STRING 0x0000d620U
#define RVA_PY_EXC_TYPE_ERROR_SLOT 0x00e6f3c4U

#define RVA_WG_FILTER2_TYPE 0x00d77fc8U
#define RVA_WG_VEHICLE_PHYSICS2_TYPE 0x00d77690U
#define RVA_WG_FILTER2_VTABLE 0x00b67698U
#define RVA_WG_FILTER2_INPUT 0x0050a350U
#define RVA_WG_FILTER2_OUTPUT 0x0050dde0U
#define RVA_MATRIX_YAW 0x00130b40U
#define RVA_MATRIX_PITCH 0x00130e00U
#define RVA_MATRIX_ROLL 0x00149170U
#define OFFSET_FILTER_LAST_OUTPUT_TIME 0x000002a0U
#define OFFSET_FILTER_VEHICLE_PHYSICS 0x000004f0U
#define OFFSET_PHYSICS_ROOT_MATRIX 0x00000718U


typedef struct Matrix4 {
	float m[4][4];
} Matrix4;


static unsigned char *g_image_base = 0;


static int bytes_equal(const unsigned char *actual,
		const unsigned char *expected, unsigned int count)
{
	unsigned int index;
	for (index = 0; index < count; ++index) {
		if (actual[index] != expected[index]) {
			return 0;
		}
	}
	return 1;
}


static int readable_region(const void *address, SIZE_T bytes)
{
	MEMORY_BASIC_INFORMATION info;
	uintptr_t cursor = (uintptr_t)address;
	uintptr_t end;
	uintptr_t previous;
	DWORD protection;
	if (address == 0 || bytes == 0 ||
			bytes > (SIZE_T)((uintptr_t)-1 - cursor)) {
		return 0;
	}
	end = cursor + bytes;
	while (cursor < end) {
		if (VirtualQuery((const void *)cursor, &info, sizeof(info)) !=
				sizeof(info) ||
				info.State != MEM_COMMIT) {
			return 0;
		}
		if ((info.Protect & PAGE_GUARD) != 0) {
			return 0;
		}
		protection = info.Protect & 0xffU;
		if (protection != PAGE_READONLY &&
				protection != PAGE_READWRITE &&
				protection != PAGE_WRITECOPY &&
				protection != PAGE_EXECUTE_READ &&
				protection != PAGE_EXECUTE_READWRITE &&
				protection != PAGE_EXECUTE_WRITECOPY) {
			return 0;
		}
		if (info.RegionSize > (SIZE_T)((uintptr_t)-1 -
				(uintptr_t)info.BaseAddress)) {
			return 0;
		}
		previous = cursor;
		cursor = (uintptr_t)info.BaseAddress + info.RegionSize;
		if (cursor <= previous) {
			return 0;
		}
	}
	return 1;
}


static int executable_region(const void *address)
{
	MEMORY_BASIC_INFORMATION info;
	DWORD protection;
	if (VirtualQuery(address, &info, sizeof(info)) != sizeof(info) ||
			info.State != MEM_COMMIT) {
		return 0;
	}
	protection = info.Protect & 0xffU;
	return protection == PAGE_EXECUTE ||
		protection == PAGE_EXECUTE_READ ||
		protection == PAGE_EXECUTE_READWRITE ||
		protection == PAGE_EXECUTE_WRITECOPY;
}


static int validate_executable(unsigned char *base)
{
	IMAGE_DOS_HEADER *dos;
	IMAGE_NT_HEADERS32 *nt;
	static const unsigned char py_init_signature[] = {
		0x81, 0xec, 0x18, 0x02, 0x00, 0x00, 0xa1
	};
	static const unsigned char py_parse_signature[] = {
		0x51, 0x8b, 0x4c, 0x24, 0x08, 0x8b, 0x54, 0x24
	};
	static const unsigned char filter_input_signature[] = {
		0x83, 0xec, 0x14, 0x53, 0x55, 0x56, 0x8b, 0xf1
	};
	static const unsigned char filter_output_signature[] = {
		0x6a, 0xff, 0x68, 0x0e, 0xc6, 0xec, 0x00, 0x64
	};
	static const unsigned char matrix_yaw_signature[] = {
		0x83, 0xec, 0x0c, 0x8b, 0x41, 0x20, 0x8b, 0x51
	};
	static const unsigned char matrix_pitch_signature[] = {
		0x83, 0xec, 0x10, 0x8b, 0x41, 0x20, 0x8b, 0x51
	};
	static const unsigned char matrix_roll_signature[] = {
		0x83, 0xec, 0x1c, 0x8b, 0x01, 0x8b, 0x51, 0x04
	};
	if (!readable_region(base, sizeof(IMAGE_DOS_HEADER))) {
		return 0;
	}
	dos = (IMAGE_DOS_HEADER *)base;
	if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 ||
			dos->e_lfanew > 0x1000) {
		return 0;
	}
	nt = (IMAGE_NT_HEADERS32 *)(base + dos->e_lfanew);
	if (!readable_region(nt, sizeof(IMAGE_NT_HEADERS32)) ||
			nt->Signature != IMAGE_NT_SIGNATURE ||
			nt->FileHeader.Machine != IMAGE_FILE_MACHINE_I386 ||
			nt->FileHeader.TimeDateStamp != EXPECTED_PE_TIMESTAMP ||
			nt->OptionalHeader.SizeOfImage != EXPECTED_IMAGE_SIZE) {
		return 0;
	}
	if (!readable_region(base + RVA_PY_INIT_MODULE4,
			sizeof(py_init_signature)) ||
			!readable_region(base + RVA_PY_ARG_PARSE_TUPLE,
				sizeof(py_parse_signature)) ||
			!readable_region(base + RVA_WG_FILTER2_INPUT,
				sizeof(filter_input_signature)) ||
			!readable_region(base + RVA_WG_FILTER2_OUTPUT,
				sizeof(filter_output_signature)) ||
			!readable_region(base + RVA_MATRIX_YAW,
				sizeof(matrix_yaw_signature)) ||
			!readable_region(base + RVA_MATRIX_PITCH,
				sizeof(matrix_pitch_signature)) ||
			!readable_region(base + RVA_MATRIX_ROLL,
				sizeof(matrix_roll_signature)) ||
			!bytes_equal(base + RVA_PY_INIT_MODULE4, py_init_signature,
			sizeof(py_init_signature)) ||
			!bytes_equal(base + RVA_PY_ARG_PARSE_TUPLE, py_parse_signature,
				sizeof(py_parse_signature)) ||
			!bytes_equal(base + RVA_WG_FILTER2_INPUT, filter_input_signature,
				sizeof(filter_input_signature)) ||
			!bytes_equal(base + RVA_WG_FILTER2_OUTPUT, filter_output_signature,
				sizeof(filter_output_signature)) ||
			!bytes_equal(base + RVA_MATRIX_YAW, matrix_yaw_signature,
				sizeof(matrix_yaw_signature)) ||
			!bytes_equal(base + RVA_MATRIX_PITCH, matrix_pitch_signature,
				sizeof(matrix_pitch_signature)) ||
			!bytes_equal(base + RVA_MATRIX_ROLL, matrix_roll_signature,
				sizeof(matrix_roll_signature))) {
		return 0;
	}
	return executable_region(base + RVA_PY_INIT_MODULE4) &&
		executable_region(base + RVA_PY_ARG_PARSE_TUPLE) &&
		executable_region(base + RVA_WG_FILTER2_INPUT) &&
		executable_region(base + RVA_WG_FILTER2_OUTPUT) &&
		executable_region(base + RVA_MATRIX_YAW) &&
		executable_region(base + RVA_MATRIX_PITCH) &&
		executable_region(base + RVA_MATRIX_ROLL);
}


static PyObject *raise_type_error(const char *message)
{
	PyErrSetStringFn set_error;
	PyObject **exception_slot;
	if (g_image_base == 0) {
		return 0;
	}
	set_error = (PyErrSetStringFn)(g_image_base + RVA_PY_ERR_SET_STRING);
	exception_slot = (PyObject **)(g_image_base + RVA_PY_EXC_TYPE_ERROR_SLOT);
	if (readable_region(exception_slot, sizeof(*exception_slot)) &&
			*exception_slot != 0) {
		set_error(*exception_slot, message);
	}
	return 0;
}


static PyObject *seed_filter(PyObject *unused_self, PyObject *args)
{
	PyArgParseTupleFn parse_tuple;
	PyObject *filter_object = 0;
	void *filter_native;
	void **vtable;
	FilterInputFn input;
	double timestamp;
	double x;
	double y;
	double z;
	double yaw;
	double pitch;
	double roll;
	int space_id;
	int vehicle_id;
	Vec3 position;
	Vec3 position_error;
	Vec3 direction;
	(void)unused_self;

	if (g_image_base == 0) {
		return 0;
	}
	parse_tuple = (PyArgParseTupleFn)(
		g_image_base + RVA_PY_ARG_PARSE_TUPLE);
	if (!parse_tuple(args, "Odiidddddd:seed_filter", &filter_object,
			&timestamp, &space_id, &vehicle_id, &x, &y, &z,
			&yaw, &pitch, &roll)) {
		return 0;
	}
	if (filter_object == 0 ||
			!readable_region(filter_object, sizeof(PyObject))) {
		return raise_type_error("native bridge received an unreadable object");
	}
	if (filter_object->ob_type != (void *)(
			g_image_base + RVA_WG_FILTER2_TYPE)) {
		return raise_type_error(
			"native bridge requires an exact WGVehicleFilter2 object");
	}
	filter_native = (void *)((unsigned char *)filter_object - 4);
	if (!readable_region(filter_native, 16)) {
		return raise_type_error("native bridge filter body is unreadable");
	}
	vtable = *(void ***)filter_native;
	if (vtable != (void **)(g_image_base + RVA_WG_FILTER2_VTABLE) ||
			!readable_region(vtable, sizeof(void *) * 2)) {
		return raise_type_error("native bridge filter vtable mismatch");
	}
	input = (FilterInputFn)vtable[1];
	if ((void *)input != (void *)(g_image_base + RVA_WG_FILTER2_INPUT) ||
			!executable_region((void *)input)) {
		return raise_type_error("native bridge Filter::input mismatch");
	}

	position.x = (float)x;
	position.y = (float)y;
	position.z = (float)z;
	position_error.x = 0.0f;
	position_error.y = 0.0f;
	position_error.z = 0.0f;
	direction.x = (float)yaw;
	direction.y = (float)pitch;
	direction.z = (float)roll;
	input(filter_native, timestamp, space_id, vehicle_id,
		&position, &position_error, &direction);

	/* Return the exact filter object as an owned Python reference. */
	filter_object->ob_refcnt += 1;
	return filter_object;
}


static PyObject *output_filter(PyObject *unused_self, PyObject *args)
{
	PyArgParseTupleFn parse_tuple;
	PyObject *filter_object = 0;
	void *filter_native;
	void **vtable;
	FilterOutputFn output;
	double *last_output_time;
	double timestamp;
	(void)unused_self;

	if (g_image_base == 0) {
		return 0;
	}
	parse_tuple = (PyArgParseTupleFn)(
		g_image_base + RVA_PY_ARG_PARSE_TUPLE);
	if (!parse_tuple(args, "Od:output_filter", &filter_object, &timestamp)) {
		return 0;
	}
	if (filter_object == 0 ||
			!readable_region(filter_object, sizeof(PyObject))) {
		return raise_type_error("native bridge received an unreadable object");
	}
	if (filter_object->ob_type != (void *)(
			g_image_base + RVA_WG_FILTER2_TYPE)) {
		return raise_type_error(
			"native bridge requires an exact WGVehicleFilter2 object");
	}
	filter_native = (void *)((unsigned char *)filter_object - 4);
	if (!readable_region(filter_native, 16)) {
		return raise_type_error("native bridge filter body is unreadable");
	}
	vtable = *(void ***)filter_native;
	if (vtable != (void **)(g_image_base + RVA_WG_FILTER2_VTABLE) ||
			!readable_region(vtable, sizeof(void *) * 3)) {
		return raise_type_error("native bridge filter vtable mismatch");
	}
	output = (FilterOutputFn)vtable[2];
	if ((void *)output != (void *)(g_image_base + RVA_WG_FILTER2_OUTPUT) ||
			!executable_region((void *)output)) {
		return raise_type_error("native bridge Filter::output mismatch");
	}
	last_output_time = (double *)((unsigned char *)filter_native +
		OFFSET_FILTER_LAST_OUTPUT_TIME);
	if (!readable_region(last_output_time, sizeof(*last_output_time))) {
		return raise_type_error(
			"native bridge Filter::output timestamp is unreadable");
	}
	if (!(timestamp >= -DBL_MAX && timestamp <= DBL_MAX)) {
		return raise_type_error(
			"native bridge Filter::output timestamp is not finite");
	}
	if (!(timestamp > *last_output_time)) {
		return raise_type_error(
			"native bridge Filter::output timestamp is not newer");
	}

	output(filter_native, timestamp);
	if (*last_output_time != timestamp) {
		return raise_type_error(
			"native bridge Filter::output timestamp did not advance");
	}
	filter_object->ob_refcnt += 1;
	return filter_object;
}


static PyObject *filter_has_physics(PyObject *unused_self, PyObject *args)
{
	PyArgParseTupleFn parse_tuple;
	PyObject *filter_object = 0;
	PyObject *physics_object = 0;
	void *filter_native;
	void **vtable;
	void **attached_physics;
	(void)unused_self;

	if (g_image_base == 0) {
		return 0;
	}
	parse_tuple = (PyArgParseTupleFn)(
		g_image_base + RVA_PY_ARG_PARSE_TUPLE);
	if (!parse_tuple(args, "OO:filter_has_physics", &filter_object,
			&physics_object)) {
		return 0;
	}
	if (filter_object == 0 || physics_object == 0 ||
			!readable_region(filter_object, sizeof(PyObject)) ||
			!readable_region(physics_object, sizeof(PyObject))) {
		return raise_type_error(
			"native bridge received an unreadable owner object");
	}
	if (filter_object->ob_type != (void *)(
			g_image_base + RVA_WG_FILTER2_TYPE)) {
		return raise_type_error(
			"native bridge requires an exact WGVehicleFilter2 object");
	}
	filter_native = (void *)((unsigned char *)filter_object - 4);
	if (physics_object->ob_type != (void *)(
			g_image_base + RVA_WG_VEHICLE_PHYSICS2_TYPE)) {
		return raise_type_error(
			"native bridge requires an exact WGVehiclePhysics2 object");
	}
	if (!readable_region(filter_native, 16)) {
		return raise_type_error("native bridge owner body is unreadable");
	}
	vtable = *(void ***)filter_native;
	if (vtable != (void **)(g_image_base + RVA_WG_FILTER2_VTABLE)) {
		return raise_type_error("native bridge filter vtable mismatch");
	}
	attached_physics = (void **)((unsigned char *)filter_native +
		OFFSET_FILTER_VEHICLE_PHYSICS);
	if (!readable_region(attached_physics, sizeof(*attached_physics)) ||
			*attached_physics != (void *)physics_object) {
		return raise_type_error(
			"native bridge filter physics owner mismatch");
	}
	filter_object->ob_refcnt += 1;
	return filter_object;
}


static PyObject *publish_physics_root(PyObject *unused_self, PyObject *args)
{
	PyArgParseTupleFn parse_tuple;
	PyObject *filter_object = 0;
	PyObject *physics_object = 0;
	void *filter_native;
	void **vtable;
	void **attached_physics;
	double *last_output_time;
	FilterInputFn input;
	FilterOutputFn output;
	MatrixAngleFn matrix_yaw;
	MatrixAngleFn matrix_pitch;
	MatrixAngleFn matrix_roll;
	double timestamp;
	int space_id;
	Matrix4 *matrix;
	Vec3 position;
	Vec3 position_error;
	Vec3 direction;
	(void)unused_self;

	if (g_image_base == 0) {
		return 0;
	}
	parse_tuple = (PyArgParseTupleFn)(
		g_image_base + RVA_PY_ARG_PARSE_TUPLE);
	if (!parse_tuple(args, "OOdi:publish_physics_root", &filter_object,
			&physics_object, &timestamp, &space_id)) {
		return 0;
	}
	if (filter_object == 0 || physics_object == 0 ||
			!readable_region(filter_object, sizeof(PyObject)) ||
			!readable_region(physics_object, sizeof(PyObject))) {
		return raise_type_error(
			"native bridge received an unreadable owner object");
	}
	if (filter_object->ob_type != (void *)(
			g_image_base + RVA_WG_FILTER2_TYPE)) {
		return raise_type_error(
			"native bridge requires an exact WGVehicleFilter2 object");
	}
	if (physics_object->ob_type != (void *)(
			g_image_base + RVA_WG_VEHICLE_PHYSICS2_TYPE)) {
		return raise_type_error(
			"native bridge requires an exact WGVehiclePhysics2 object");
	}
	filter_native = (void *)((unsigned char *)filter_object - 4);
	if (!readable_region(filter_native, 16)) {
		return raise_type_error("native bridge owner body is unreadable");
	}
	vtable = *(void ***)filter_native;
	if (vtable != (void **)(g_image_base + RVA_WG_FILTER2_VTABLE) ||
			!readable_region(vtable, sizeof(void *) * 3)) {
		return raise_type_error("native bridge filter vtable mismatch");
	}
	input = (FilterInputFn)vtable[1];
	output = (FilterOutputFn)vtable[2];
	if ((void *)input != (void *)(g_image_base + RVA_WG_FILTER2_INPUT) ||
			!executable_region((void *)input) ||
			(void *)output != (void *)(g_image_base + RVA_WG_FILTER2_OUTPUT) ||
			!executable_region((void *)output)) {
		return raise_type_error(
			"native bridge filter input/output mismatch");
	}
	attached_physics = (void **)((unsigned char *)filter_native +
		OFFSET_FILTER_VEHICLE_PHYSICS);
	if (!readable_region(attached_physics, sizeof(*attached_physics)) ||
			*attached_physics != (void *)physics_object) {
		return raise_type_error(
			"native bridge filter physics owner mismatch");
	}
	matrix = (Matrix4 *)((unsigned char *)physics_object +
		OFFSET_PHYSICS_ROOT_MATRIX);
	if (!readable_region(matrix, sizeof(*matrix))) {
		return raise_type_error(
			"native bridge physics root matrix is unreadable");
	}
	if (!(timestamp >= -DBL_MAX && timestamp <= DBL_MAX)) {
		return raise_type_error(
			"native bridge physics root timestamp is not finite");
	}
	last_output_time = (double *)((unsigned char *)filter_native +
		OFFSET_FILTER_LAST_OUTPUT_TIME);
	if (!readable_region(last_output_time, sizeof(*last_output_time)) ||
			!(timestamp > *last_output_time)) {
		return raise_type_error(
			"native bridge physics root timestamp is not newer");
	}
	position.x = matrix->m[3][0];
	position.y = matrix->m[3][1];
	position.z = matrix->m[3][2];
	matrix_yaw = (MatrixAngleFn)(g_image_base + RVA_MATRIX_YAW);
	matrix_pitch = (MatrixAngleFn)(g_image_base + RVA_MATRIX_PITCH);
	matrix_roll = (MatrixAngleFn)(g_image_base + RVA_MATRIX_ROLL);
	direction.x = matrix_yaw(matrix);
	direction.y = matrix_pitch(matrix);
	direction.z = matrix_roll(matrix);
	position_error.x = 0.0f;
	position_error.y = 0.0f;
	position_error.z = 0.0f;
	if (!(position.x >= -FLT_MAX && position.x <= FLT_MAX &&
			position.y >= -FLT_MAX && position.y <= FLT_MAX &&
			position.z >= -FLT_MAX && position.z <= FLT_MAX &&
			direction.x >= -FLT_MAX && direction.x <= FLT_MAX &&
			direction.y >= -FLT_MAX && direction.y <= FLT_MAX &&
			direction.z >= -FLT_MAX && direction.z <= FLT_MAX)) {
		return raise_type_error(
			"native bridge physics root pose is not finite");
	}

	input(filter_native, timestamp, space_id, 0,
		&position, &position_error, &direction);
	output(filter_native, timestamp);
	if (*last_output_time != timestamp) {
		return raise_type_error(
			"native bridge physics root output did not advance");
	}
	filter_object->ob_refcnt += 1;
	return filter_object;
}


static PyMethodDef module_methods[] = {
	{
		"seed_filter", seed_filter, METH_VARARGS,
		"Inject one complete timestamped pose into WGVehicleFilter2."
	},
	{
		"output_filter", output_filter, METH_VARARGS,
		"Publish one WGVehicleFilter2 output after native batch simulation."
	},
	{
		"filter_has_physics", filter_has_physics, METH_VARARGS,
		"Verify WGVehicleFilter2 owns the exact WGVehiclePhysics2 body."
	},
	{
		"publish_physics_root", publish_physics_root, METH_VARARGS,
		"Publish the solved WGVehiclePhysics2 root through its filter."
	},
	{0, 0, 0, 0}
};


__declspec(dllexport) void initoffhangar_native_seed(void)
{
	PyInitModule4Fn init_module;
	unsigned char *base = (unsigned char *)GetModuleHandleA(0);
	if (base == 0 || !validate_executable(base)) {
		return;
	}
	g_image_base = base;
	init_module = (PyInitModule4Fn)(base + RVA_PY_INIT_MODULE4);
	init_module(
		"offhangar_native_seed", module_methods,
		"Version-locked World of Tanks 0.8.2 filter bridge.",
		0, PYTHON_API_VERSION_26);
}