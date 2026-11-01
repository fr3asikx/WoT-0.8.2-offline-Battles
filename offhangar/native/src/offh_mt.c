/*
 * offh_mt.c — 32-bit RVA-Bound Win32 Multithreaded PhysicsSpace Sub-Engine + Direct WorldOfTanks.exe RVA Hooks
 *
 * Implements:
 * 1. PhysicsSpace with parallel worker threads.
 * 2. Fixed 60 Hz physics tick (FIXED_DT = 0.0166667f) with mass, engine power, ground friction,
 *    4-corner terrain attitude, and continuous turret/gun slew with [-PI, PI] angle normalization.
 * 3. Single-threaded inter-tank collision resolution phase (resolve_inter_collisions) after the parallel step.
 * 4. Double-buffered TankState (previous_state Tick N-1 and current_state Tick N) with linear & angular
 *    LERP interpolation by alpha (0.0 .. 1.0) via get_interpolated_transform(id, alpha) and batch output.
 * 5. Direct WorldOfTanks.exe (0.8.2, TimeDateStamp=0x50B8ECCF) native hooks & crash guards:
 *    - In-memory binary crash guards for:
 *      * 0x00E78490 (std::map::find NULL dereference at 0x00E78498)
 *      * 0x0040BDD0 (PyObject_RichCompare NULL operand dereference at 0x0040BDDC)
 *      * 0x0090A3E3 (WGVehicleFilter2::input NULL pEntity_ dereference at 0x0090A3EA)
 *      * 0x0090DEDE (WGVehicleFilter2::output NULL pEntity_ dereference at 0x0090DEE1)
 *      * 0x00C66B40 (Scaleform GFx Value::GetMember NULL dereference at 0x00C66B43)
 *      * 0x0095AF91 (Post-battle Hangar reload / BattleResults NULL [ecx+0x148] dereference at 0x0095AF97)
 *    - Direct C++ ClientSpace::collide (0x0092D4B0) raycaster (native_collide_segment) with custom C vtable
 *      that skips SPT_MATKIND.LEAVES (72) in C++ with zero Python callbacks or Math.Vector3 allocations.
 *    - Direct C++ WGVehiclePhysics2 struct readback (speed @ +0x38C/+0x3B8, rspeed @ +0x380/+0x3A8,
 *      isFrozen @ +0x692) inside fast_publish_root to eliminate C++ std::string heap allocations per frame.
 */

typedef unsigned char      uint8_t;
typedef unsigned short     uint16_t;
typedef unsigned int       uint32_t;
typedef int                int32_t;
typedef unsigned int       size_t;
typedef int                intptr_t;
typedef unsigned int       uintptr_t;

#define NULL ((void *)0)
#define WINAPI   __attribute__((stdcall))
#define CDECL    __attribute__((cdecl))
#define THISCALL __attribute__((thiscall))

#define PI_F     3.14159265358979323846f
#define TWO_PI_F 6.28318530717958647692f
#define FIXED_DT 0.016666667f

static inline float fast_fabsf(float x) {
    float res;
    __asm__ __volatile__ ("fabs" : "=t"(res) : "0"(x));
    return res;
}

static inline float fast_sqrtf(float x) {
    if (x <= 0.0f) return 0.0f;
    float res;
    __asm__ __volatile__ ("fsqrt" : "=t"(res) : "0"(x));
    return res;
}

static inline float fast_sinf(float x) {
    float res;
    __asm__ __volatile__ ("fsin" : "=t"(res) : "0"(x));
    return res;
}

static inline float fast_cosf(float x) {
    float res;
    __asm__ __volatile__ ("fcos" : "=t"(res) : "0"(x));
    return res;
}

static inline float fast_atan2f(float y, float x) {
    float res;
    __asm__ __volatile__ ("fpatan" : "=t"(res) : "0"(x), "u"(y) : "st(1)");
    return res;
}

static inline float clampf(float v, float lo, float hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

/* Strict [-PI, PI] angle normalization so turrets never spin 360 deg the wrong way across 180 deg */
static inline float wrap_pi(float a) {
    while (a >  PI_F) a -= TWO_PI_F;
    while (a < -PI_F) a += TWO_PI_F;
    return a;
}

/* Shortest-arc angular LERP between prev_a and curr_a by alpha in [0, 1] */
static inline float lerp_angle(float prev_a, float curr_a, float alpha) {
    float diff = wrap_pi(curr_a - prev_a);
    return wrap_pi(prev_a + diff * alpha);
}

static inline float lerpf(float a, float b, float alpha) {
    return a + (b - a) * alpha;
}

static void fast_memcpy(void *dst, const void *src, size_t n) {
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;
    while (n >= 4) {
        *(uint32_t *)d = *(const uint32_t *)s;
        d += 4;
        s += 4;
        n -= 4;
    }
    while (n--) {
        *d++ = *s++;
    }
}

static void fast_memset_zero(void *dst, size_t n) {
    uint8_t *d = (uint8_t *)dst;
    while (n--) {
        *d++ = 0;
    }
}

static int fast_memcmp(const void *a, const void *b, size_t n) {
    const uint8_t *pa = (const uint8_t *)a;
    const uint8_t *pb = (const uint8_t *)b;
    while (n--) {
        if (*pa != *pb) return (int)(*pa) - (int)(*pb);
        pa++;
        pb++;
    }
    return 0;
}

typedef void *HANDLE;
typedef void *HWND;
typedef void *HMODULE;
typedef void *LPVOID;
typedef uint32_t DWORD;
typedef int BOOL;
typedef int LONG_VAL;
typedef volatile int LONG;

#define INFINITE 0xFFFFFFFFu
#define MEM_COMMIT  0x00001000u
#define MEM_RESERVE 0x00002000u
#define MEM_RELEASE 0x00008000u
#define PAGE_READWRITE 0x04u
#define PAGE_EXECUTE_READWRITE 0x40u
#define DLL_PROCESS_DETACH 0
#define DLL_PROCESS_ATTACH 1

typedef struct _RECT {
    int32_t left;
    int32_t top;
    int32_t right;
    int32_t bottom;
} RECT;

typedef struct _CRITICAL_SECTION {
    void *DebugInfo;
    LONG LockCount;
    LONG RecursionCount;
    HANDLE OwningThread;
    HANDLE LockSemaphore;
    uint32_t SpinCount;
} CRITICAL_SECTION;

typedef struct _SYSTEM_INFO {
    uint32_t dwOemId;
    uint32_t dwPageSize;
    void *lpMinimumApplicationAddress;
    void *lpMaximumApplicationAddress;
    uint32_t dwActiveProcessorMask;
    uint32_t dwNumberOfProcessors;
    uint32_t dwProcessorType;
    uint32_t dwAllocationGranularity;
    uint16_t wProcessorLevel;
    uint16_t wProcessorRevision;
} SYSTEM_INFO;

typedef DWORD (WINAPI *LPTHREAD_START_ROUTINE)(LPVOID);
typedef BOOL  (WINAPI *WNDENUMPROC)(HWND, intptr_t);

typedef HMODULE (WINAPI *fn_GetModuleHandleA_t)(const char *);
typedef void *  (WINAPI *fn_GetProcAddress_t)(HMODULE, const char *);
typedef void    (WINAPI *fn_InitializeCriticalSection_t)(CRITICAL_SECTION *);
typedef void    (WINAPI *fn_EnterCriticalSection_t)(CRITICAL_SECTION *);
typedef void    (WINAPI *fn_LeaveCriticalSection_t)(CRITICAL_SECTION *);
typedef void    (WINAPI *fn_GetSystemInfo_t)(SYSTEM_INFO *);
typedef HANDLE  (WINAPI *fn_CreateEventA_t)(void *, BOOL, BOOL, const char *);
typedef BOOL    (WINAPI *fn_SetEvent_t)(HANDLE);
typedef BOOL    (WINAPI *fn_ResetEvent_t)(HANDLE);
typedef DWORD   (WINAPI *fn_WaitForSingleObject_t)(HANDLE, DWORD);
typedef HANDLE  (WINAPI *fn_CreateThread_t)(void *, size_t, LPTHREAD_START_ROUTINE, LPVOID, DWORD, DWORD *);
typedef LPVOID  (WINAPI *fn_VirtualAlloc_t)(LPVOID, size_t, DWORD, DWORD);
typedef BOOL    (WINAPI *fn_VirtualFree_t)(LPVOID, size_t, DWORD);
typedef BOOL    (WINAPI *fn_VirtualProtect_t)(LPVOID, size_t, DWORD, DWORD *);
typedef BOOL    (WINAPI *fn_FlushInstructionCache_t)(HANDLE, const void *, size_t);
typedef HANDLE  (WINAPI *fn_GetCurrentProcess_t)(void);
typedef HANDLE  (WINAPI *fn_GetCurrentThread_t)(void);
typedef BOOL    (WINAPI *fn_SetPriorityClass_t)(HANDLE, DWORD);
typedef BOOL    (WINAPI *fn_SetThreadPriority_t)(HANDLE, int);
typedef BOOL    (WINAPI *fn_GetProcessAffinityMask_t)(HANDLE, uintptr_t *, uintptr_t *);
typedef BOOL    (WINAPI *fn_SetProcessAffinityMask_t)(HANDLE, uintptr_t);
typedef uintptr_t (WINAPI *fn_SetThreadAffinityMask_t)(HANDLE, uintptr_t);
typedef BOOL    (WINAPI *fn_SetProcessPriorityBoost_t)(HANDLE, BOOL);
typedef BOOL    (WINAPI *fn_SetThreadPriorityBoost_t)(HANDLE, BOOL);
typedef uint32_t (WINAPI *fn_timeBeginPeriod_t)(uint32_t);
typedef uint32_t (WINAPI *fn_timeEndPeriod_t)(uint32_t);

typedef HMODULE  (WINAPI *fn_LoadLibraryA_t)(const char *);
typedef DWORD    (WINAPI *fn_GetCurrentProcessId_t)(void);
typedef void     (WINAPI *fn_ExitProcess_t)(uint32_t);
typedef HWND     (WINAPI *fn_FindWindowA_t)(const char *, const char *);
typedef HWND     (WINAPI *fn_GetForegroundWindow_t)(void);
typedef HWND     (WINAPI *fn_GetActiveWindow_t)(void);
typedef DWORD    (WINAPI *fn_GetWindowThreadProcessId_t)(HWND, DWORD *);
typedef BOOL     (WINAPI *fn_EnumWindows_t)(WNDENUMPROC, intptr_t);
typedef BOOL     (WINAPI *fn_IsWindowVisible_t)(HWND);
typedef LONG_VAL (WINAPI *fn_GetWindowLongA_t)(HWND, int);
typedef LONG_VAL (WINAPI *fn_SetWindowLongA_t)(HWND, int, LONG_VAL);
typedef BOOL     (WINAPI *fn_SetWindowPos_t)(HWND, HWND, int, int, int, int, uint32_t);
typedef BOOL     (WINAPI *fn_AdjustWindowRectEx_t)(RECT *, DWORD, BOOL, DWORD);
typedef BOOL     (WINAPI *fn_GetClientRect_t)(HWND, RECT *);
typedef BOOL     (WINAPI *fn_SystemParametersInfoA_t)(uint32_t, uint32_t, void *, uint32_t);
typedef LONG_VAL (WINAPI *fn_ChangeDisplaySettingsA_t)(void *, DWORD);
typedef BOOL     (WINAPI *fn_ClipCursor_t)(const RECT *);
typedef BOOL     (WINAPI *fn_ReleaseCapture_t)(void);
typedef BOOL     (WINAPI *fn_SetForegroundWindow_t)(HWND);
typedef HWND     (WINAPI *fn_SetFocus_t)(HWND);
typedef BOOL     (WINAPI *fn_ShowWindow_t)(HWND, int);
typedef BOOL     (WINAPI *fn_DrawMenuBar_t)(HWND);

uint8_t                        *g_self_base                = NULL;
uint8_t                        *g_wot_base                 = NULL;
fn_GetModuleHandleA_t          p_GetModuleHandleA          = NULL;
fn_GetProcAddress_t            p_GetProcAddress            = NULL;
fn_InitializeCriticalSection_t p_InitializeCriticalSection = NULL;
fn_EnterCriticalSection_t      p_EnterCriticalSection      = NULL;
fn_LeaveCriticalSection_t      p_LeaveCriticalSection      = NULL;
fn_GetSystemInfo_t             p_GetSystemInfo             = NULL;
fn_CreateEventA_t              p_CreateEventA              = NULL;
fn_SetEvent_t                  p_SetEvent                  = NULL;
fn_ResetEvent_t                p_ResetEvent                = NULL;
fn_WaitForSingleObject_t       p_WaitForSingleObject       = NULL;
fn_CreateThread_t              p_CreateThread              = NULL;
fn_VirtualAlloc_t              p_VirtualAlloc              = NULL;
fn_VirtualFree_t               p_VirtualFree               = NULL;
fn_VirtualProtect_t            p_VirtualProtect            = NULL;
fn_FlushInstructionCache_t     p_FlushInstructionCache     = NULL;
fn_GetCurrentProcess_t         p_GetCurrentProcess         = NULL;
fn_GetCurrentThread_t          p_GetCurrentThread          = NULL;
fn_SetPriorityClass_t          p_SetPriorityClass          = NULL;
fn_SetThreadPriority_t         p_SetThreadPriority         = NULL;
fn_GetProcessAffinityMask_t    p_GetProcessAffinityMask    = NULL;
fn_SetProcessAffinityMask_t    p_SetProcessAffinityMask    = NULL;
fn_SetThreadAffinityMask_t     p_SetThreadAffinityMask     = NULL;
fn_SetProcessPriorityBoost_t   p_SetProcessPriorityBoost   = NULL;
fn_SetThreadPriorityBoost_t    p_SetThreadPriorityBoost    = NULL;
fn_timeBeginPeriod_t           p_timeBeginPeriod           = NULL;
fn_timeEndPeriod_t             p_timeEndPeriod             = NULL;
static int                     g_timer_period_active       = 0;
static uintptr_t               g_worker_affinity_mask      = 0;

fn_LoadLibraryA_t              p_LoadLibraryA              = NULL;
fn_GetCurrentProcessId_t       p_GetCurrentProcessId       = NULL;
fn_ExitProcess_t               p_ExitProcess               = NULL;
fn_FindWindowA_t               p_FindWindowA               = NULL;
fn_GetForegroundWindow_t       p_GetForegroundWindow       = NULL;
fn_GetActiveWindow_t           p_GetActiveWindow           = NULL;
fn_GetWindowThreadProcessId_t  p_GetWindowThreadProcessId  = NULL;
fn_EnumWindows_t               p_EnumWindows               = NULL;
fn_IsWindowVisible_t           p_IsWindowVisible           = NULL;
fn_GetWindowLongA_t            p_GetWindowLongA            = NULL;
fn_SetWindowLongA_t            p_SetWindowLongA            = NULL;
fn_SetWindowPos_t              p_SetWindowPos              = NULL;
fn_AdjustWindowRectEx_t        p_AdjustWindowRectEx        = NULL;
fn_GetClientRect_t             p_GetClientRect             = NULL;
fn_SystemParametersInfoA_t     p_SystemParametersInfoA     = NULL;
fn_ChangeDisplaySettingsA_t    p_ChangeDisplaySettingsA    = NULL;
fn_ClipCursor_t                p_ClipCursor                = NULL;
fn_ReleaseCapture_t            p_ReleaseCapture            = NULL;
fn_SetForegroundWindow_t       p_SetForegroundWindow       = NULL;
fn_SetFocus_t                  p_SetFocus                  = NULL;
fn_ShowWindow_t                p_ShowWindow                = NULL;
fn_DrawMenuBar_t               p_DrawMenuBar               = NULL;

static int resolve_win32_apis(void) {
    if (p_InitializeCriticalSection && p_EnterCriticalSection &&
        p_LeaveCriticalSection && p_CreateEventA && p_SetEvent &&
        p_ResetEvent && p_WaitForSingleObject && p_CreateThread &&
        p_VirtualAlloc && p_VirtualFree && p_VirtualProtect) {
        return 1;
    }

    if (g_self_base && *(uint16_t *)g_self_base == 0x5A4Du) {
        uint32_t e_lfanew = *(uint32_t *)(g_self_base + 0x3C);
        uint8_t *nt = g_self_base + e_lfanew;
        if (*(uint32_t *)nt == 0x00004550u) {
            uint32_t iat_rva = *(uint32_t *)(nt + 0x78 + 12 * 8);
            if (iat_rva) {
                void **iat = (void **)(g_self_base + iat_rva);
                p_GetModuleHandleA          = (fn_GetModuleHandleA_t)iat[0];
                p_GetProcAddress            = (fn_GetProcAddress_t)iat[1];
                p_InitializeCriticalSection = (fn_InitializeCriticalSection_t)iat[2];
                p_EnterCriticalSection      = (fn_EnterCriticalSection_t)iat[3];
                p_LeaveCriticalSection      = (fn_LeaveCriticalSection_t)iat[4];
                p_GetSystemInfo             = (fn_GetSystemInfo_t)iat[5];
                p_CreateEventA              = (fn_CreateEventA_t)iat[6];
                p_SetEvent                  = (fn_SetEvent_t)iat[7];
                p_ResetEvent                = (fn_ResetEvent_t)iat[8];
                p_WaitForSingleObject       = (fn_WaitForSingleObject_t)iat[9];
                p_CreateThread              = (fn_CreateThread_t)iat[10];
                p_VirtualAlloc              = (fn_VirtualAlloc_t)iat[11];
                p_VirtualFree               = (fn_VirtualFree_t)iat[12];
            }
        }
    }

    if (p_GetModuleHandleA && p_GetProcAddress) {
        HMODULE k32 = p_GetModuleHandleA("kernel32.dll");
        if (k32) {
            p_LoadLibraryA          = (fn_LoadLibraryA_t)p_GetProcAddress(k32, "LoadLibraryA");
            p_GetCurrentProcessId   = (fn_GetCurrentProcessId_t)p_GetProcAddress(k32, "GetCurrentProcessId");
            p_ExitProcess           = (fn_ExitProcess_t)p_GetProcAddress(k32, "ExitProcess");
            p_VirtualProtect        = (fn_VirtualProtect_t)p_GetProcAddress(k32, "VirtualProtect");
            p_FlushInstructionCache   = (fn_FlushInstructionCache_t)p_GetProcAddress(k32, "FlushInstructionCache");
            p_GetCurrentProcess       = (fn_GetCurrentProcess_t)p_GetProcAddress(k32, "GetCurrentProcess");
            p_GetCurrentThread        = (fn_GetCurrentThread_t)p_GetProcAddress(k32, "GetCurrentThread");
            p_SetPriorityClass        = (fn_SetPriorityClass_t)p_GetProcAddress(k32, "SetPriorityClass");
            p_SetThreadPriority       = (fn_SetThreadPriority_t)p_GetProcAddress(k32, "SetThreadPriority");
            p_GetProcessAffinityMask  = (fn_GetProcessAffinityMask_t)p_GetProcAddress(k32, "GetProcessAffinityMask");
            p_SetProcessAffinityMask  = (fn_SetProcessAffinityMask_t)p_GetProcAddress(k32, "SetProcessAffinityMask");
            p_SetThreadAffinityMask   = (fn_SetThreadAffinityMask_t)p_GetProcAddress(k32, "SetThreadAffinityMask");
            p_SetProcessPriorityBoost = (fn_SetProcessPriorityBoost_t)p_GetProcAddress(k32, "SetProcessPriorityBoost");
            p_SetThreadPriorityBoost  = (fn_SetThreadPriorityBoost_t)p_GetProcAddress(k32, "SetThreadPriorityBoost");
        }
        HMODULE winmm = p_GetModuleHandleA("winmm.dll");
        if (!winmm && p_LoadLibraryA) {
            winmm = p_LoadLibraryA("winmm.dll");
        }
        if (winmm) {
            p_timeBeginPeriod = (fn_timeBeginPeriod_t)p_GetProcAddress(winmm, "timeBeginPeriod");
            p_timeEndPeriod   = (fn_timeEndPeriod_t)p_GetProcAddress(winmm, "timeEndPeriod");
        }
        HMODULE u32 = p_GetModuleHandleA("user32.dll");
        if (!u32 && p_LoadLibraryA) {
            u32 = p_LoadLibraryA("user32.dll");
        }
        if (u32) {
            p_FindWindowA              = (fn_FindWindowA_t)p_GetProcAddress(u32, "FindWindowA");
            p_GetForegroundWindow      = (fn_GetForegroundWindow_t)p_GetProcAddress(u32, "GetForegroundWindow");
            p_GetActiveWindow          = (fn_GetActiveWindow_t)p_GetProcAddress(u32, "GetActiveWindow");
            p_GetWindowThreadProcessId = (fn_GetWindowThreadProcessId_t)p_GetProcAddress(u32, "GetWindowThreadProcessId");
            p_EnumWindows              = (fn_EnumWindows_t)p_GetProcAddress(u32, "EnumWindows");
            p_IsWindowVisible          = (fn_IsWindowVisible_t)p_GetProcAddress(u32, "IsWindowVisible");
            p_GetWindowLongA           = (fn_GetWindowLongA_t)p_GetProcAddress(u32, "GetWindowLongA");
            p_SetWindowLongA           = (fn_SetWindowLongA_t)p_GetProcAddress(u32, "SetWindowLongA");
            p_SetWindowPos             = (fn_SetWindowPos_t)p_GetProcAddress(u32, "SetWindowPos");
            p_AdjustWindowRectEx       = (fn_AdjustWindowRectEx_t)p_GetProcAddress(u32, "AdjustWindowRectEx");
            p_GetClientRect            = (fn_GetClientRect_t)p_GetProcAddress(u32, "GetClientRect");
            p_SystemParametersInfoA    = (fn_SystemParametersInfoA_t)p_GetProcAddress(u32, "SystemParametersInfoA");
            p_ChangeDisplaySettingsA   = (fn_ChangeDisplaySettingsA_t)p_GetProcAddress(u32, "ChangeDisplaySettingsA");
            p_ClipCursor               = (fn_ClipCursor_t)p_GetProcAddress(u32, "ClipCursor");
            p_ReleaseCapture           = (fn_ReleaseCapture_t)p_GetProcAddress(u32, "ReleaseCapture");
            p_SetForegroundWindow      = (fn_SetForegroundWindow_t)p_GetProcAddress(u32, "SetForegroundWindow");
            p_SetFocus                 = (fn_SetFocus_t)p_GetProcAddress(u32, "SetFocus");
            p_ShowWindow               = (fn_ShowWindow_t)p_GetProcAddress(u32, "ShowWindow");
            p_DrawMenuBar              = (fn_DrawMenuBar_t)p_GetProcAddress(u32, "DrawMenuBar");
        }
    }

    return (p_InitializeCriticalSection && p_EnterCriticalSection &&
            p_LeaveCriticalSection && p_CreateEventA && p_SetEvent &&
            p_ResetEvent && p_WaitForSingleObject && p_CreateThread &&
            p_VirtualAlloc && p_VirtualFree);
}

typedef int Py_ssize_t;

typedef struct _object {
    Py_ssize_t ob_refcnt;
    void *ob_type;
} PyObject;

typedef PyObject *(CDECL *PyCFunction)(PyObject *self, PyObject *args);

typedef struct PyMethodDef {
    const char *ml_name;
    PyCFunction ml_meth;
    int ml_flags;
    const char *ml_doc;
} PyMethodDef;

#define METH_VARARGS 0x0001
#define PYTHON_API_VERSION_26 1013

#define RVA_PY_INITMODULE4      0x00019800u
#define RVA_PYARG_PARSETUPLE    0x0001D580u
#define RVA_PYERR_SETSTRING     0x0000D620u
#define RVA_PYEXC_RUNTIMEERROR  0x00E6F3C4u
#define RVA_PY_NONE_STRUCT      0x00E7C8ECu

#define RVA_FILTER_TYPE         0x00D77FC8u
#define RVA_PHYSICS_TYPE        0x00D77690u
#define RVA_FILTER_VTABLE       0x00B67698u
#define RVA_FILTER_INPUT        0x0050A350u
#define RVA_FILTER_OUTPUT       0x0050DDE0u
#define RVA_MATRIX_YAW          0x00130B40u
#define RVA_MATRIX_PITCH        0x00130E00u
#define RVA_MATRIX_ROLL         0x00149170u

/* Direct BigWorld C++ ClientSpace::collide (wg_collideSegment core) RVAs */
#define RVA_BW_SPACE_COLLIDE    0x0052D4B0u
#define RVA_WG_COLLIDE_CB_OP    0x0052D610u
#define RVA_WG_COLLIDE_VTABLE   0x00B683B0u

typedef PyObject *(CDECL *fn_Py_InitModule4_t)(
    const char *name, PyMethodDef *methods, const char *doc,
    PyObject *self, int apiver);
typedef int (CDECL *fn_PyArg_ParseTuple_t)(
    PyObject *args, const char *format, ...);
typedef void (CDECL *fn_PyErr_SetString_t)(
    PyObject *exception, const char *string);

typedef float (THISCALL *fn_Matrix_Angle_t)(const void *matrix_this);
typedef void  (THISCALL *fn_Filter_Input_t)(
    void *filter_this, double timestamp, int space_id, int vehicle_id,
    const float *pos, const float *err, const float *dir);
typedef void  (THISCALL *fn_Filter_Output_t)(
    void *filter_this, double timestamp);

typedef void (CDECL *fn_BW_SpaceCollide_t)(
    int space_id,
    const float *start_xyz,
    const float *end_xyz,
    void *cb_ptr);

typedef int (THISCALL *fn_WGCollideCallback_op_t)(
    void *cb_this,
    const void *obstacle,
    const void *triangle,
    float dist);

typedef void PyThreadState;
typedef PyThreadState *(CDECL *fn_PyEval_SaveThread_t)(void);
typedef void           (CDECL *fn_PyEval_RestoreThread_t)(PyThreadState *);

fn_PyEval_SaveThread_t    p_PyEval_SaveThread    = NULL;
fn_PyEval_RestoreThread_t p_PyEval_RestoreThread = NULL;

#undef Py_BEGIN_ALLOW_THREADS
#undef Py_END_ALLOW_THREADS
#define Py_BEGIN_ALLOW_THREADS { \
    PyThreadState *_py_gil_save = (p_PyEval_SaveThread ? p_PyEval_SaveThread() : NULL);
#define Py_END_ALLOW_THREADS \
    if (_py_gil_save && p_PyEval_RestoreThread) { p_PyEval_RestoreThread(_py_gil_save); } \
}

fn_Py_InitModule4_t       p_Py_InitModule4       = NULL;
fn_PyArg_ParseTuple_t     p_PyArg_ParseTuple     = NULL;
fn_PyErr_SetString_t      p_PyErr_SetString      = NULL;
PyObject                **pp_PyExc_RuntimeError  = NULL;

fn_Matrix_Angle_t         p_Matrix_yaw           = NULL;
fn_Matrix_Angle_t         p_Matrix_pitch         = NULL;
fn_Matrix_Angle_t         p_Matrix_roll          = NULL;
fn_Filter_Input_t         p_Filter_input         = NULL;
fn_Filter_Output_t        p_Filter_output        = NULL;

fn_BW_SpaceCollide_t      p_BW_SpaceCollide      = NULL;
fn_WGCollideCallback_op_t p_WGCollideCallback_op = NULL;
void                     *g_custom_collide_vtable[6];
int                       g_collide_hook_ready   = 0;
int                       g_exe_patches_applied  = 0;
uint8_t                  *g_trampoline_page      = NULL;

int g_array_ptr_offset = 12;

const uint8_t SIG_PY_INITMODULE4[7]   = {0x81, 0xEC, 0x18, 0x02, 0x00, 0x00, 0xA1};
const uint8_t SIG_PYARG_PARSETUPLE[8] = {0x51, 0x8B, 0x4C, 0x24, 0x08, 0x8B, 0x54, 0x24};
const uint8_t SIG_BW_SPACE_COLLIDE[8] = {0x6A, 0xFF, 0x68, 0xB8, 0x72, 0xE9, 0x00, 0x64};
const uint8_t SIG_WG_COLLIDE_CB_OP[8] = {0x6A, 0xFF, 0x68, 0x80, 0xE1, 0xEC, 0x00, 0x64};

/*
 * Custom C++ CollisionCallback::operator() (vtable[1]) for native_collide_segment:
 * Filters SPT_MATKIND.LEAVES (matKind == 72) and optional max_matkind directly in C++
 * in 2 CPU instructions without allocating Python tuples or invoking Python callbacks!
 */
static int THISCALL offh_collide_cb_operator(
    void *cb_this,
    const void *obstacle,
    const void *triangle,
    float dist
) {
    if (!triangle) return 3;
    const uint8_t *tri_bytes = (const uint8_t *)triangle;
    uint8_t coll_flags = tri_bytes[0x24];
    uint8_t mat_kind   = tri_bytes[0x25];
    int max_mk = *(const int *)((const uint8_t *)cb_this + 0x50);
    /* Terrain (coll_flags & 8) ALWAYS blocks LOS, aiming, and outlines -- never skip! */
    if ((coll_flags & 8u) != 0u) {
        return p_WGCollideCallback_op(cb_this, obstacle, triangle, dist);
    }
    if (mat_kind == 72u) {
        return 3; /* COLLIDE_ALL: march straight through tree crowns (SPT_MATKIND.LEAVES) in C++ */
    }
    if (max_mk == -1 && mat_kind >= 71u && mat_kind <= 130u) {
        return 3; /* Ground-probe mode: skip fences, tree trunks, and destructible roofs so ground rays hit real ground! */
    }
    /* Standing destructible houses/walls (71..100 except 72) and terrain (>100) block LOS, spotting, and outlines! */
    return p_WGCollideCallback_op(cb_this, obstacle, triangle, dist);
}

static void write_rel32_le(uint8_t *dst, uintptr_t from_end, uintptr_t to_addr) {
    int32_t rel = (int32_t)(to_addr - from_end);
    *(int32_t *)dst = rel;
}

static int patch_code_bytes(uint8_t *target, const uint8_t *new_bytes, size_t len) {
    if (!p_VirtualProtect) return 0;
    DWORD old_prot = 0;
    if (!p_VirtualProtect((LPVOID)target, len, PAGE_EXECUTE_READWRITE, &old_prot)) {
        return 0;
    }
    fast_memcpy(target, new_bytes, len);
    DWORD dummy = 0;
    p_VirtualProtect((LPVOID)target, len, old_prot, &dummy);
    if (p_FlushInstructionCache && p_GetCurrentProcess) {
        p_FlushInstructionCache(p_GetCurrentProcess(), (const void *)target, len);
    }
    return 1;
}

/*
 * Install hardware-verified in-memory crash guards on WorldOfTanks.exe (0.8.2, TimeDateStamp=0x50B8ECCF):
 *   1. 0x00E78490 (RVA 0x00A78490): std::map::find NULL deref at 0x00E78498 -> jmp short 0x00E784C0
 *   2. 0x0040BDD0 (RVA 0x0000BDD0): PyObject_RichCompare NULL v/w deref at 0x0040BDDC -> substitute Py_None
 *   3. 0x0090A3E3 (RVA 0x0050A3E3): WGVehicleFilter2::input NULL pEntity_ deref at 0x0090A3EA -> clean ret
 *   4. 0x0090DEDE (RVA 0x0050DEDE): WGVehicleFilter2::output NULL pEntity_ deref at 0x0090DEE1 -> clean ret
 *   5. 0x00C66B40 (RVA 0x00866B40): Scaleform GFx GetMember NULL deref at 0x00C66B43 -> clean fail exit
 */
static void install_wot_exe_crash_guards(uint8_t *base) {
    if (!base || !p_VirtualProtect || !p_VirtualAlloc) return;
    if (g_exe_patches_applied > 0) return;

    /* Verify exact WoT 0.8.2 PE TimeDateStamp (0x50B8ECCF) and SizeOfImage (0x0140F000) */
    uint32_t e_lfanew = *(uint32_t *)(base + 0x3C);
    uint8_t *nt = base + e_lfanew;
    uint32_t timestamp  = *(uint32_t *)(nt + 8);
    uint32_t image_size = *(uint32_t *)(nt + 0x50);
    if (timestamp != 0x50B8ECCFu || image_size != 0x0140F000u) {
        return;
    }

    int applied = 0;

    /* Guard 1: 0x00E78490 (RVA 0x00A78490) -> 2-byte inline jump EB 2E to 0x00E784C0 */
    const uint8_t sig_map_null[10] = {0x33, 0xC9, 0x8B, 0x44, 0x24, 0x0C, 0x8B, 0x00, 0x8B, 0x11};
    const uint8_t sig_map_ret[4]   = {0x59, 0xC2, 0x08, 0x00};
    if (fast_memcmp(base + 0x00A78490u, sig_map_null, 10) == 0 &&
        fast_memcmp(base + 0x00A784C0u, sig_map_ret, 4) == 0) {
        const uint8_t patch_eb2e[2] = {0xEB, 0x2E};
        if (patch_code_bytes(base + 0x00A78490u, patch_eb2e, 2)) {
            applied++;
        }
    } else if (base[0x00A78490u] == 0xEBu && base[0x00A78491u] == 0x2Eu) {
        applied++;
    }

    if (!g_trampoline_page) {
        g_trampoline_page = (uint8_t *)p_VirtualAlloc(
            NULL, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    }
    if (!g_trampoline_page) {
        g_exe_patches_applied = applied;
        return;
    }

    uintptr_t py_none_va = (uintptr_t)(base + RVA_PY_NONE_STRUCT);

    /* Guard 2: 0x0040BDD0 (RVA 0x0000BDD0) — PyObject_RichCompare NULL v/w guard */
    const uint8_t sig_richcmp[15] = {
        0x8B, 0x75, 0x08, 0x8B, 0x46, 0x04, 0x8B, 0x7D, 0x0C, 0x8B, 0x5D, 0x10, 0x3B, 0x47, 0x04
    };
    if (fast_memcmp(base + 0x0000BDD0u, sig_richcmp, 15) == 0) {
        uint8_t *s1 = g_trampoline_page + 0;
        int k = 0;
        /* mov esi, [ebp+0x8] */
        s1[k++] = 0x8B; s1[k++] = 0x75; s1[k++] = 0x08;
        /* cmp esi, 0x10000; jae +5 */
        s1[k++] = 0x81; s1[k++] = 0xFE; s1[k++] = 0x00; s1[k++] = 0x00; s1[k++] = 0x01; s1[k++] = 0x00;
        s1[k++] = 0x73; s1[k++] = 0x05;
        /* mov esi, py_none_va */
        s1[k++] = 0xBE; *(uint32_t *)(s1 + k) = (uint32_t)py_none_va; k += 4;
        /* mov eax, [esi+0x4] */
        s1[k++] = 0x8B; s1[k++] = 0x46; s1[k++] = 0x04;
        /* mov edi, [ebp+0xc] */
        s1[k++] = 0x8B; s1[k++] = 0x7D; s1[k++] = 0x0C;
        /* cmp edi, 0x10000; jae +5 */
        s1[k++] = 0x81; s1[k++] = 0xFF; s1[k++] = 0x00; s1[k++] = 0x00; s1[k++] = 0x01; s1[k++] = 0x00;
        s1[k++] = 0x73; s1[k++] = 0x05;
        /* mov edi, py_none_va */
        s1[k++] = 0xBF; *(uint32_t *)(s1 + k) = (uint32_t)py_none_va; k += 4;
        /* jmp base + 0x0000BDD9 */
        s1[k++] = 0xE9;
        write_rel32_le(s1 + k, (uintptr_t)(s1 + k + 4), (uintptr_t)(base + 0x0000BDD9u));
        k += 4;

        uint8_t jmp_s1[6] = {0xE9, 0, 0, 0, 0, 0x90};
        write_rel32_le(jmp_s1 + 1, (uintptr_t)(base + 0x0000BDD0u + 5), (uintptr_t)s1);
        if (patch_code_bytes(base + 0x0000BDD0u, jmp_s1, 6)) {
            applied++;
        }
    } else if (base[0x0000BDD0u] == 0xE9u) {
        applied++;
    }

    /* Guard 3: 0x0090A3E3 (RVA 0x0050A3E3) — WGVehicleFilter2::input NULL pEntity_ guard */
    const uint8_t sig_flt_in[7] = {0x8B, 0x4E, 0x0C, 0xF3, 0x0F, 0x10, 0x07};
    if (fast_memcmp(base + 0x0050A3E3u, sig_flt_in, 7) == 0) {
        uint8_t *s2 = g_trampoline_page + 64;
        int k = 0;
        /* mov ecx, [esi+0xc] */
        s2[k++] = 0x8B; s2[k++] = 0x4E; s2[k++] = 0x0C;
        /* cmp ecx, 0x10000 */
        s2[k++] = 0x81; s2[k++] = 0xF9; s2[k++] = 0x00; s2[k++] = 0x00; s2[k++] = 0x01; s2[k++] = 0x00;
        /* jb 0x0090A535 */
        s2[k++] = 0x0F; s2[k++] = 0x82;
        write_rel32_le(s2 + k, (uintptr_t)(s2 + k + 4), (uintptr_t)(base + 0x0050A535u));
        k += 4;
        /* movss xmm0, [edi] */
        s2[k++] = 0xF3; s2[k++] = 0x0F; s2[k++] = 0x10; s2[k++] = 0x07;
        /* jmp 0x0090A3EA */
        s2[k++] = 0xE9;
        write_rel32_le(s2 + k, (uintptr_t)(s2 + k + 4), (uintptr_t)(base + 0x0050A3EAu));
        k += 4;

        uint8_t jmp_s2[7] = {0xE9, 0, 0, 0, 0, 0x90, 0x90};
        write_rel32_le(jmp_s2 + 1, (uintptr_t)(base + 0x0050A3E3u + 5), (uintptr_t)s2);
        if (patch_code_bytes(base + 0x0050A3E3u, jmp_s2, 7)) {
            applied++;
        }
    } else if (base[0x0050A3E3u] == 0xE9u) {
        applied++;
    }

    /* Guard 4: 0x0090DEDE (RVA 0x0050DEDE) — WGVehicleFilter2::output NULL pEntity_ guard */
    const uint8_t sig_flt_out[6] = {0x8B, 0x46, 0x0C, 0x8B, 0x58, 0x10};
    if (fast_memcmp(base + 0x0050DEDEu, sig_flt_out, 6) == 0) {
        uint8_t *s3 = g_trampoline_page + 128;
        int k = 0;
        /* mov eax, [esi+0xc] */
        s3[k++] = 0x8B; s3[k++] = 0x46; s3[k++] = 0x0C;
        /* cmp eax, 0x10000 */
        s3[k++] = 0x81; s3[k++] = 0xF8; s3[k++] = 0x00; s3[k++] = 0x00; s3[k++] = 0x01; s3[k++] = 0x00;
        /* jb 0x0090E522 */
        s3[k++] = 0x0F; s3[k++] = 0x82;
        write_rel32_le(s3 + k, (uintptr_t)(s3 + k + 4), (uintptr_t)(base + 0x0050E522u));
        k += 4;
        /* mov ebx, [eax+0x10] */
        s3[k++] = 0x8B; s3[k++] = 0x58; s3[k++] = 0x10;
        /* jmp 0x0090DEE4 */
        s3[k++] = 0xE9;
        write_rel32_le(s3 + k, (uintptr_t)(s3 + k + 4), (uintptr_t)(base + 0x0050DEE4u));
        k += 4;

        uint8_t jmp_s3[6] = {0xE9, 0, 0, 0, 0, 0x90};
        write_rel32_le(jmp_s3 + 1, (uintptr_t)(base + 0x0050DEDEu + 5), (uintptr_t)s3);
        if (patch_code_bytes(base + 0x0050DEDEu, jmp_s3, 6)) {
            applied++;
        }
    } else if (base[0x0050DEDEu] == 0xE9u) {
        applied++;
    }

    /* Guard 5: 0x00C66B40 (RVA 0x00866B40) — Scaleform GFx GetMember NULL pointer guard */
    const uint8_t sig_gfx_null[8] = {0x8B, 0x4E, 0x30, 0x8B, 0x11, 0x8B, 0x52, 0x10};
    if (fast_memcmp(base + 0x00866B40u, sig_gfx_null, 8) == 0) {
        uint8_t *s4 = g_trampoline_page + 192;
        int k = 0;
        /* mov ecx, [esi+0x30] */
        s4[k++] = 0x8B; s4[k++] = 0x4E; s4[k++] = 0x30;
        /* cmp ecx, 0x10000 */
        s4[k++] = 0x81; s4[k++] = 0xF9; s4[k++] = 0x00; s4[k++] = 0x00; s4[k++] = 0x01; s4[k++] = 0x00;
        /* jb 0x00C66BAB */
        s4[k++] = 0x0F; s4[k++] = 0x82;
        write_rel32_le(s4 + k, (uintptr_t)(s4 + k + 4), (uintptr_t)(base + 0x00866BABu));
        k += 4;
        /* mov edx, [ecx] */
        s4[k++] = 0x8B; s4[k++] = 0x11;
        /* cmp edx, 0x10000 */
        s4[k++] = 0x81; s4[k++] = 0xFA; s4[k++] = 0x00; s4[k++] = 0x00; s4[k++] = 0x01; s4[k++] = 0x00;
        /* jb 0x00C66BAB */
        s4[k++] = 0x0F; s4[k++] = 0x82;
        write_rel32_le(s4 + k, (uintptr_t)(s4 + k + 4), (uintptr_t)(base + 0x00866BABu));
        k += 4;
        /* jmp 0x00C66B45 */
        s4[k++] = 0xE9;
        write_rel32_le(s4 + k, (uintptr_t)(s4 + k + 4), (uintptr_t)(base + 0x00866B45u));
        k += 4;

        uint8_t jmp_s4[5] = {0xE9, 0, 0, 0, 0};
        write_rel32_le(jmp_s4 + 1, (uintptr_t)(base + 0x00866B40u + 5), (uintptr_t)s4);
        if (patch_code_bytes(base + 0x00866B40u, jmp_s4, 5)) {
            applied++;
        }
    } else if (base[0x00866B40u] == 0xE9u) {
        applied++;
    }

    /* Guard 6: 0x0095AF91 (RVA 0x0055AF91) — Post-battle Hangar / BattleResults NULL pointer guard
     * Prevents EXCEPTION_ACCESS_VIOLATION : 0xC0000005 @ 0x0095AF97 (Read @ 0x00000008) when [ecx+0x148] is NULL! */
    const uint8_t sig_hangar_95af91[9] = {0x8B, 0x91, 0x48, 0x01, 0x00, 0x00, 0x8B, 0x42, 0x08};
    if (fast_memcmp(base + 0x0055AF91u, sig_hangar_95af91, 9) == 0) {
        uint8_t *s5 = g_trampoline_page + 256;
        int k = 0;
        /* cmp ecx, 0x10000; jb 0x0095B075 */
        s5[k++] = 0x81; s5[k++] = 0xF9; s5[k++] = 0x00; s5[k++] = 0x00; s5[k++] = 0x01; s5[k++] = 0x00;
        s5[k++] = 0x0F; s5[k++] = 0x82;
        write_rel32_le(s5 + k, (uintptr_t)(s5 + k + 4), (uintptr_t)(base + 0x0055B075u));
        k += 4;
        /* mov edx, [ecx+0x148] */
        s5[k++] = 0x8B; s5[k++] = 0x91; s5[k++] = 0x48; s5[k++] = 0x01; s5[k++] = 0x00; s5[k++] = 0x00;
        /* cmp edx, 0x10000; jb 0x0095B075 */
        s5[k++] = 0x81; s5[k++] = 0xFA; s5[k++] = 0x00; s5[k++] = 0x00; s5[k++] = 0x01; s5[k++] = 0x00;
        s5[k++] = 0x0F; s5[k++] = 0x82;
        write_rel32_le(s5 + k, (uintptr_t)(s5 + k + 4), (uintptr_t)(base + 0x0055B075u));
        k += 4;
        /* mov eax, [edx+0x8] */
        s5[k++] = 0x8B; s5[k++] = 0x42; s5[k++] = 0x08;
        /* cmp eax, 0x10000; jb 0x0095B075 */
        s5[k++] = 0x81; s5[k++] = 0xF8; s5[k++] = 0x00; s5[k++] = 0x00; s5[k++] = 0x01; s5[k++] = 0x00;
        s5[k++] = 0x0F; s5[k++] = 0x82;
        write_rel32_le(s5 + k, (uintptr_t)(s5 + k + 4), (uintptr_t)(base + 0x0055B075u));
        k += 4;
        /* jmp 0x0095AF9A (RVA 0x0055AF9A) */
        s5[k++] = 0xE9;
        write_rel32_le(s5 + k, (uintptr_t)(s5 + k + 4), (uintptr_t)(base + 0x0055AF9Au));
        k += 4;

        uint8_t jmp_s5[6] = {0xE9, 0, 0, 0, 0, 0x90};
        write_rel32_le(jmp_s5 + 1, (uintptr_t)(base + 0x0055AF91u + 5), (uintptr_t)s5);
        if (patch_code_bytes(base + 0x0055AF91u, jmp_s5, 6)) {
            applied++;
        }
    } else if (base[0x0055AF91u] == 0xE9u) {
        applied++;
    }

    if (p_FlushInstructionCache && p_GetCurrentProcess) {
        p_FlushInstructionCache(p_GetCurrentProcess(), (const void *)g_trampoline_page, 384);
    }
    g_exe_patches_applied = applied;
}

static int g_is_exact_082 = 0;

static uint8_t *scan_pe_range_for_sig(uint8_t *start, size_t len, const uint8_t *sig, size_t sig_len) {
    if (!start || len < sig_len || sig_len == 0) return NULL;
    size_t limit = len - sig_len;
    uint8_t b0 = sig[0];
    for (size_t i = 0; i <= limit; ++i) {
        if (start[i] == b0 && fast_memcmp(start + i, sig, sig_len) == 0) {
            return start + i;
        }
    }
    return NULL;
}

static int resolve_wot_python_api(void) {
    resolve_win32_apis();

    uint8_t *base = p_GetModuleHandleA ? (uint8_t *)p_GetModuleHandleA(NULL) : (uint8_t *)0x00400000u;
    if (!base) return 0;

    if (*(uint16_t *)base != 0x5A4Du) return 0;
    uint32_t e_lfanew = *(uint32_t *)(base + 0x3C);
    uint8_t *nt = base + e_lfanew;
    if (*(uint32_t *)nt != 0x00004550u) return 0;

    /* Fast path: Exact WoT 0.8.2 executable (TimeDateStamp=0x50B8ECCF) */
    if (fast_memcmp(base + RVA_PY_INITMODULE4, SIG_PY_INITMODULE4, 7) == 0 &&
        fast_memcmp(base + RVA_PYARG_PARSETUPLE, SIG_PYARG_PARSETUPLE, 8) == 0) {
        g_is_exact_082        = 1;
        g_wot_base            = base;
        p_Py_InitModule4      = (fn_Py_InitModule4_t)(base + RVA_PY_INITMODULE4);
        p_PyArg_ParseTuple    = (fn_PyArg_ParseTuple_t)(base + RVA_PYARG_PARSETUPLE);
        p_PyErr_SetString     = (fn_PyErr_SetString_t)(base + RVA_PYERR_SETSTRING);
        pp_PyExc_RuntimeError = (PyObject **)(base + RVA_PYEXC_RUNTIMEERROR);

        p_Matrix_yaw          = (fn_Matrix_Angle_t)(base + RVA_MATRIX_YAW);
        p_Matrix_pitch        = (fn_Matrix_Angle_t)(base + RVA_MATRIX_PITCH);
        p_Matrix_roll         = (fn_Matrix_Angle_t)(base + RVA_MATRIX_ROLL);
        p_Filter_input        = (fn_Filter_Input_t)(base + RVA_FILTER_INPUT);
        p_Filter_output       = (fn_Filter_Output_t)(base + RVA_FILTER_OUTPUT);

        /* Bind native C++ BigWorld ClientSpace::collide (wg_collideSegment core) */
        if (fast_memcmp(base + RVA_BW_SPACE_COLLIDE, SIG_BW_SPACE_COLLIDE, 8) == 0 &&
            fast_memcmp(base + RVA_WG_COLLIDE_CB_OP, SIG_WG_COLLIDE_CB_OP, 8) == 0) {
            p_BW_SpaceCollide      = (fn_BW_SpaceCollide_t)(base + RVA_BW_SPACE_COLLIDE);
            p_WGCollideCallback_op = (fn_WGCollideCallback_op_t)(base + RVA_WG_COLLIDE_CB_OP);
            void **orig_vt = (void **)(base + RVA_WG_COLLIDE_VTABLE);
            for (int i = 0; i < 6; ++i) {
                g_custom_collide_vtable[i] = orig_vt[i];
            }
            g_custom_collide_vtable[1] = (void *)offh_collide_cb_operator;
            g_collide_hook_ready = 1;
        }
        install_wot_exe_crash_guards(base);
    } else {
        /* Universal 0.8.x (0.8.0..0.8.11) Dynamic Resolver:
         * 1. Check exports on WorldOfTanks.exe / python26.dll / python27.dll
         * 2. Scan the embedded Python 2.6 .text region (RVA 0x00001000..0x00090000) for byte signatures */
        g_is_exact_082 = 0;
        g_wot_base     = base;
        if (p_GetProcAddress) {
            HMODULE mods[3];
            mods[0] = (HMODULE)base;
            mods[1] = p_GetModuleHandleA ? p_GetModuleHandleA("python26.dll") : NULL;
            mods[2] = p_GetModuleHandleA ? p_GetModuleHandleA("python27.dll") : NULL;
            for (int m = 0; m < 3; ++m) {
                if (!mods[m]) continue;
                if (!p_Py_InitModule4)
                    p_Py_InitModule4 = (fn_Py_InitModule4_t)p_GetProcAddress(mods[m], "Py_InitModule4");
                if (!p_PyArg_ParseTuple)
                    p_PyArg_ParseTuple = (fn_PyArg_ParseTuple_t)p_GetProcAddress(mods[m], "PyArg_ParseTuple");
                if (!p_PyErr_SetString)
                    p_PyErr_SetString = (fn_PyErr_SetString_t)p_GetProcAddress(mods[m], "PyErr_SetString");
                if (!pp_PyExc_RuntimeError)
                    pp_PyExc_RuntimeError = (PyObject **)p_GetProcAddress(mods[m], "PyExc_RuntimeError");
                if (!p_PyEval_SaveThread)
                    p_PyEval_SaveThread = (fn_PyEval_SaveThread_t)p_GetProcAddress(mods[m], "PyEval_SaveThread");
                if (!p_PyEval_RestoreThread)
                    p_PyEval_RestoreThread = (fn_PyEval_RestoreThread_t)p_GetProcAddress(mods[m], "PyEval_RestoreThread");
            }
        }
        uint32_t size_of_code = *(uint32_t *)(nt + 0x1C);
        size_t scan_len = (size_of_code > 0x00010000u && size_of_code < 0x00180000u) ? (size_t)size_of_code : 0x00090000u;
        if (!p_Py_InitModule4) {
            uint8_t *hit = scan_pe_range_for_sig(base + 0x1000u, scan_len, SIG_PY_INITMODULE4, 7);
            if (hit) p_Py_InitModule4 = (fn_Py_InitModule4_t)hit;
        }
        if (!p_PyArg_ParseTuple) {
            const uint8_t sig_parse_14[14] = {
                0x51, 0x8B, 0x4C, 0x24, 0x08, 0x8B, 0x54, 0x24, 0x0C, 0x8D, 0x44, 0x24, 0x10, 0x50
            };
            uint8_t *hit = scan_pe_range_for_sig(base + 0x1000u, scan_len, sig_parse_14, 14);
            if (hit) p_PyArg_ParseTuple = (fn_PyArg_ParseTuple_t)hit;
        }
        if (!p_Py_InitModule4 || !p_PyArg_ParseTuple) {
            return 0;
        }
    }

    /* Resolve PyEval_SaveThread & PyEval_RestoreThread for GIL-free parallel & raycast execution */
    if (p_GetProcAddress && (!p_PyEval_SaveThread || !p_PyEval_RestoreThread)) {
        p_PyEval_SaveThread    = (fn_PyEval_SaveThread_t)p_GetProcAddress((HMODULE)base, "PyEval_SaveThread");
        p_PyEval_RestoreThread = (fn_PyEval_RestoreThread_t)p_GetProcAddress((HMODULE)base, "PyEval_RestoreThread");
        if ((!p_PyEval_SaveThread || !p_PyEval_RestoreThread) && p_GetModuleHandleA) {
            HMODULE py_dll = p_GetModuleHandleA("python26.dll");
            if (py_dll) {
                p_PyEval_SaveThread    = (fn_PyEval_SaveThread_t)p_GetProcAddress(py_dll, "PyEval_SaveThread");
                p_PyEval_RestoreThread = (fn_PyEval_RestoreThread_t)p_GetProcAddress(py_dll, "PyEval_RestoreThread");
            }
        }
    }

    return 1;
}

static PyObject *return_borrowed_arg(PyObject *obj) {
    if (obj) {
        obj->ob_refcnt += 1;
    }
    return obj;
}

static void *get_array_buffer(PyObject *arr_obj, int *out_len) {
    if (!arr_obj) return NULL;
    uint8_t *raw = (uint8_t *)arr_obj;
    if (out_len) {
        *out_len = *(int *)(raw + 8);
    }
    return *(void **)(raw + g_array_ptr_offset);
}

/* ============================================================================
 * PhysicsSpace: Parallel 60 Hz Sub-Engine + Double-Buffered LERP
 * ============================================================================ */

#define MAX_VEHICLES           32
#define PHYSICS_WORKER_THREADS 4
#define IN_STRIDE              16
#define OUT_STRIDE             8
#define SUB_IN_STRIDE          24
#define SUB_OUT_STRIDE         16
#define MAX_FOLIAGE            8192

typedef struct _TankState {
    int   id;
    int   alive;
    int   team;
    int   mode;         /* 0 = external/WGVehiclePhysics2 synced, 1 = C-subengine kinematic drive */
    float x, y, z;
    float yaw, pitch, roll;
    float vel, turn_vel;
    float turret_yaw, gun_pitch;
    float want_tyaw, want_gpitch;
    float hw, hl;
    float mass, inv_mass;
    float engine_power, speed_fwd, speed_bwd, turn_rate;
    float throttle, steer;
    float look_x, look_y, look_z;
    int   has_look_target;
    float turret_speed, gun_speed;
    int   has_lim_yaw;
    float min_yaw, max_yaw;
    float min_pitch, max_pitch;
    float gun_h, hull_h;
    float ground_y, t_pitch, t_roll;
    float push_x, push_z;
    float ram_id, closing_speed;
    uint32_t los_mask;
    uint32_t corridor_mask;
    float    nearest_enemy_dist;
    float    pitch_vel, roll_vel, vert_vel, prev_vel;
    int      aim_status;
    int      difficulty; /* 0 = easy, 1 = medium, 2 = hard */
    int      has_combat_target;
    int      is_arty;
    float    proj_speed;
    float    aim_phase;
} TankState;

CRITICAL_SECTION g_cs;
int g_cs_inited = 0;

float *g_heights = NULL;
size_t g_heights_bytes = 0;
int g_grid_nx = 0;
int g_grid_nz = 0;
float g_min_x = -500.0f;
float g_min_z = -500.0f;
float g_cell_sz = 18.0f;

float *g_foliage = NULL;
int g_foliage_count = 0;

/* Legacy buffers for compatibility with step_frame */
float g_in_buf[MAX_VEHICLES * IN_STRIDE];
float g_out_buf[MAX_VEHICLES * OUT_STRIDE];

/* PhysicsSpace lock-free double-buffered snapshots:
 * - Workers step `g_curr_state` (back buffer) and resolve inter-tank collisions on the worker thread.
 * - On completion, the last worker copies `g_curr_state` into the inactive snapshot bank `(1 - g_front_bank)`
 *   and atomically flips `g_front_bank`, then clears `g_workers_busy = 0`.
 * - Main Python thread calls `get_last_results_nonblocking` (reads `g_front_bank` with ZERO waiting!)
 *   and `submit_step_async` (stages inputs into `g_staging_in_buf` and wakes workers without blocking!). */
TankState g_prev_state[MAX_VEHICLES];
TankState g_curr_state[MAX_VEHICLES];
TankState g_snap_prev[2][MAX_VEHICLES];
TankState g_snap_curr[2][MAX_VEHICLES];
volatile LONG g_front_bank    = 0;
volatile LONG g_snap_count    = 0;
volatile LONG g_completed_seq = 0;
float     g_staging_in_buf[MAX_VEHICLES * SUB_IN_STRIDE];
volatile LONG g_staging_count = 0;
volatile LONG g_staging_ready = 0;
float     g_extrap_dt_accum   = 0.0f;

int       g_space_count = 0;
float     g_time_accum  = 0.0f;
float     g_last_alpha  = 1.0f;
int       g_worker_mode = 0; /* 0 = legacy step_frame, 1 = PhysicsSpace 60Hz split */

int g_veh_count = 0;
LONG g_next_veh_idx = 0;
LONG g_active_workers = 0;
volatile LONG g_workers_busy = 0;
volatile int g_shutdown = 0;

int g_num_threads = 0;
HANDLE g_threads[PHYSICS_WORKER_THREADS];
HANDLE g_start_events[PHYSICS_WORKER_THREADS];
HANDLE g_done_event = NULL;

static float sample_height_nolock(float x, float z, float fallback_y) {
    if (!g_heights || g_grid_nx < 2 || g_grid_nz < 2) {
        return fallback_y;
    }
    float fx = (x - g_min_x) / g_cell_sz;
    float fz = (z - g_min_z) / g_cell_sz;
    int ix = (int)fx;
    int iz = (int)fz;
    if (ix < 0) ix = 0;
    if (iz < 0) iz = 0;
    if (ix >= g_grid_nx - 1) ix = g_grid_nx - 2;
    if (iz >= g_grid_nz - 1) iz = g_grid_nz - 2;

    float tx = fx - (float)ix;
    float tz = fz - (float)iz;
    if (tx < 0.0f) tx = 0.0f;
    if (tx > 1.0f) tx = 1.0f;
    if (tz < 0.0f) tz = 0.0f;
    if (tz > 1.0f) tz = 1.0f;

    int row0 = iz * g_grid_nx;
    int row1 = (iz + 1) * g_grid_nx;
    float h00 = g_heights[row0 + ix];
    float h10 = g_heights[row0 + ix + 1];
    float h01 = g_heights[row1 + ix];
    float h11 = g_heights[row1 + ix + 1];
    if (h00 > 15000.0f) h00 -= 20000.0f; else if (h00 > 5000.0f) h00 -= 10000.0f;
    if (h10 > 15000.0f) h10 -= 20000.0f; else if (h10 > 5000.0f) h10 -= 10000.0f;
    if (h01 > 15000.0f) h01 -= 20000.0f; else if (h01 > 5000.0f) h01 -= 10000.0f;
    if (h11 > 15000.0f) h11 -= 20000.0f; else if (h11 > 5000.0f) h11 -= 10000.0f;

    if (h00 < -1000.0f && h10 < -1000.0f && h01 < -1000.0f && h11 < -1000.0f) {
        return fallback_y;
    }
    float valid_h = fallback_y;
    if (valid_h < -1000.0f) {
        if (h00 > -1000.0f) valid_h = h00;
        else if (h10 > -1000.0f) valid_h = h10;
        else if (h01 > -1000.0f) valid_h = h01;
        else valid_h = h11;
    }
    if (h00 < -1000.0f) h00 = valid_h;
    if (h10 < -1000.0f) h10 = valid_h;
    if (h01 < -1000.0f) h01 = valid_h;
    if (h11 < -1000.0f) h11 = valid_h;

    float h0 = h00 + (h10 - h00) * tx;
    float h1 = h01 + (h11 - h01) * tx;
    return h0 + (h1 - h0) * tz;
}

static int segment_hits_obb_2d(
    float ax, float az, float bx, float bz,
    float cx, float cz, float yaw, float half_w, float half_l
) {
    float dx = bx - ax;
    float dz = bz - az;
    float seg_len_sq = dx * dx + dz * dz;
    if (seg_len_sq < 1e-4f) return 0;

    float rel_x = cx - ax;
    float rel_z = cz - az;
    float proj = (rel_x * dx + rel_z * dz) / seg_len_sq;
    if (proj < 0.02f || proj > 0.98f) return 0;

    float near_x = ax + dx * proj - cx;
    float near_z = az + dz * proj - cz;
    float bound_r = half_l + half_w + 0.5f;
    if (near_x * near_x + near_z * near_z > bound_r * bound_r) return 0;

    float c = fast_cosf(yaw);
    float s = fast_sinf(yaw);
    float a_lx = (ax - cx) * c - (az - cz) * s;
    float a_lz = (ax - cx) * s + (az - cz) * c;
    float b_lx = (bx - cx) * c - (bz - cz) * s;
    float b_lz = (bx - cx) * s + (bz - cz) * c;

    float d_lx = b_lx - a_lx;
    float d_lz = b_lz - a_lz;
    float tmin = 0.0f;
    float tmax = 1.0f;

    if (fast_fabsf(d_lx) < 1e-6f) {
        if (a_lx < -half_w || a_lx > half_w) return 0;
    } else {
        float t1 = (-half_w - a_lx) / d_lx;
        float t2 = ( half_w - a_lx) / d_lx;
        if (t1 > t2) { float tmp = t1; t1 = t2; t2 = tmp; }
        if (t1 > tmin) tmin = t1;
        if (t2 < tmax) tmax = t2;
        if (tmin > tmax) return 0;
    }

    if (fast_fabsf(d_lz) < 1e-6f) {
        if (a_lz < -half_l || a_lz > half_l) return 0;
    } else {
        float t1 = (-half_l - a_lz) / d_lz;
        float t2 = ( half_l - a_lz) / d_lz;
        if (t1 > t2) { float tmp = t1; t1 = t2; t2 = tmp; }
        if (t1 > tmin) tmin = t1;
        if (t2 < tmax) tmax = t2;
        if (tmin > tmax) return 0;
    }
    return (tmax >= tmin && tmax >= 0.0f && tmin <= 1.0f);
}

static int is_cell_obstacle_nolock(float x, float z) {
    if (!g_heights || g_grid_nx < 2 || g_grid_nz < 2) return 0;
    int ix = (int)((x - g_min_x) / g_cell_sz);
    int iz = (int)((z - g_min_z) / g_cell_sz);
    if (ix < 0 || iz < 0 || ix >= g_grid_nx || iz >= g_grid_nz) return 1;
    float h = g_heights[iz * g_grid_nx + ix];
    return (h < -1000.0f || h > 5000.0f);
}

static int is_cell_open_halo_nolock(float x, float z) {
    if (!g_heights || g_grid_nx < 3 || g_grid_nz < 3) return 0;
    int cx = (int)((x - g_min_x) / g_cell_sz);
    int cz = (int)((z - g_min_z) / g_cell_sz);
    if (cx <= 0 || cz <= 0 || cx >= g_grid_nx - 1 || cz >= g_grid_nz - 1) return 0;
    for (int dz = -1; dz <= 1; ++dz) {
        int row = (cz + dz) * g_grid_nx + cx;
        for (int dx = -1; dx <= 1; ++dx) {
            float h = g_heights[row + dx];
            if (h < -1000.0f || h > 5000.0f) return 0;
        }
    }
    return 1;
}

static int raycast_heightfield_clear(
    float ax, float ay, float az,
    float bx, float by, float bz
) {
    float dx = bx - ax;
    float dy = by - ay;
    float dz = bz - az;
    float dist = fast_sqrtf(dx * dx + dz * dz);
    if (dist <= 4.0f) return 1;

    float trim_start = 4.0f / dist;
    float trim_end   = 1.0f - (2.0f / dist);
    if (trim_start >= trim_end) return 1;

    int steps = (int)(dist * 0.25f);
    if (steps < 6)  steps = 6;
    if (steps > 64) steps = 64;

    for (int s = 1; s < steps; ++s) {
        float u = (float)s / (float)steps;
        float t = trim_start + (trim_end - trim_start) * u;
        float px = ax + dx * t;
        float py = ay + dy * t;
        float pz = az + dz * t;
        float terr_y = sample_height_nolock(px, pz, -9999.0f);
        if (terr_y > -1000.0f && terr_y > py) {
            return 0;
        }
    }
    return 1;
}

/*
 * Transform world-space aim direction (dx, dy, dz) into the tilted hull frame (yaw, pitch, roll)
 * and return (desired_turret_yaw, desired_gun_pitch).
 */
static void compute_hull_local_aim(
    float dx, float dy, float dz,
    float hull_yaw, float hull_pitch, float hull_roll,
    float *out_tyaw, float *out_gpitch
) {
    float len = fast_sqrtf(dx * dx + dy * dy + dz * dz);
    if (len < 0.1f) {
        *out_tyaw   = 0.0f;
        *out_gpitch = 0.0f;
        return;
    }
    float wx = dx / len;
    float wy = dy / len;
    float wz = dz / len;

    float sy = fast_sinf(hull_yaw),   cy = fast_cosf(hull_yaw);
    float sp = fast_sinf(hull_pitch), cp = fast_cosf(hull_pitch);
    float sr = fast_sinf(hull_roll),  cr = fast_cosf(hull_roll);

    /* Hull basis axes in BigWorld YPR convention */
    float rx =  cy * cr + sy * sp * sr;
    float ry =  cp * sr;
    float rz = -sy * cr + cy * sp * sr;

    float ux = -cy * sr + sy * sp * cr;
    float uy =  cp * cr;
    float uz =  sy * sr + cy * sp * cr;

    float fx =  sy * cp;
    float fy = -sp;
    float fz =  cy * cp;

    float lx = wx * rx + wy * ry + wz * rz;
    float ly = wx * ux + wy * uy + wz * uz;
    float lz = wx * fx + wy * fy + wz * fz;

    float flat = fast_sqrtf(lx * lx + lz * lz);
    *out_tyaw   = (flat > 1e-5f) ? fast_atan2f(lx, lz) : 0.0f;
    /* BigWorld convention: negative gun pitch = barrel up */
    *out_gpitch = (flat > 1e-5f) ? -fast_atan2f(ly, flat) : 0.0f;
}

/*
 * Step one tank inside PhysicsSpace at fixed 60 Hz (dt = FIXED_DT = 0.016667f).
 */
static void step_tank_60hz(int i, float dt) {
    TankState *st = &g_curr_state[i];
    float x    = st->x;
    float y    = st->y;
    float z    = st->z;
    float yaw  = st->yaw;
    float hw   = st->hw > 0.5f ? st->hw : 1.65f;
    float hl   = st->hl > 0.5f ? st->hl : 3.20f;

    /* 1. 4-corner terrain height & attitude sampling */
    float s = fast_sinf(yaw);
    float c = fast_cosf(yaw);

    float fl_x = x + s * hl - c * hw;
    float fl_z = z + c * hl + s * hw;
    float fr_x = x + s * hl + c * hw;
    float fr_z = z + c * hl - s * hw;
    float rl_x = x - s * hl - c * hw;
    float rl_z = z - c * hl + s * hw;
    float rr_x = x - s * hl + c * hw;
    float rr_z = z - c * hl - s * hw;

    float h_fl = sample_height_nolock(fl_x, fl_z, y);
    float h_fr = sample_height_nolock(fr_x, fr_z, y);
    float h_rl = sample_height_nolock(rl_x, rl_z, y);
    float h_rr = sample_height_nolock(rr_x, rr_z, y);
    float h_c  = sample_height_nolock(x, z, y);

    float avg_corners = 0.25f * (h_fl + h_fr + h_rl + h_rr);
    float ground_y = (h_c > avg_corners) ? (0.6f * h_c + 0.4f * avg_corners) : avg_corners;
    float front_h  = 0.5f * (h_fl + h_fr);
    float rear_h   = 0.5f * (h_rl + h_rr);
    float left_h   = 0.5f * (h_fl + h_rl);
    float right_h  = 0.5f * (h_fr + h_rr);

    float t_pitch = -fast_atan2f(front_h - rear_h, hl * 2.0f);
    float t_roll  = fast_atan2f(left_h - right_h, hw * 2.0f);
    t_pitch = clampf(t_pitch, -0.45f, 0.45f);
    t_roll  = clampf(t_roll,  -0.32f, 0.32f);

    st->ground_y = ground_y;
    st->t_pitch  = t_pitch;
    st->t_roll   = t_roll;

    /* 2. Smooth 60 Hz Hull Kinematics (mass, engine traction, ground friction, slope gravity) */
    if (st->alive && st->mode == 1) {
        float mass  = st->mass > 1000.0f ? st->mass : 35000.0f;
        float pwr   = st->engine_power > 50.0f ? st->engine_power : 500.0f;
        float sfwd  = st->speed_fwd > 2.0f ? st->speed_fwd : 14.0f;
        float sbwd  = st->speed_bwd > 1.0f ? st->speed_bwd : 5.0f;
        float trate = st->turn_rate > 0.1f ? st->turn_rate : 0.65f;

        /* Specific power (hp/ton) determines acceleration */
        float hp_per_ton = pwr / (mass * 0.001f);
        float base_accel = clampf(hp_per_ton * 0.26f, 1.5f, 6.5f);
        float slope_acc  = -9.81f * fast_sinf(st->pitch) * 0.38f;

        float vel = st->vel;
        if (st->throttle > 0.05f) {
            float target_v = sfwd * st->throttle;
            if (vel < 0.0f) {
                vel += 12.0f * dt; /* braking from reverse */
            } else {
                float ratio = 1.0f - (vel / (sfwd + 0.1f)) * 0.65f;
                vel += (base_accel * ratio + slope_acc) * dt;
                if (vel > target_v) vel = target_v;
            }
        } else if (st->throttle < -0.05f) {
            float target_v = -sbwd * (-st->throttle);
            if (vel > 0.0f) {
                vel -= 12.0f * dt; /* braking from forward */
            } else {
                float ratio = 1.0f - ((-vel) / (sbwd + 0.1f)) * 0.65f;
                vel += (-base_accel * 0.85f * ratio + slope_acc) * dt;
                if (vel < target_v) vel = target_v;
            }
        } else {
            /* Ground rolling friction decel */
            float decel = 5.5f * dt;
            if (fast_fabsf(vel) <= decel) vel = 0.0f;
            else if (vel > 0.0f) vel -= decel;
            else vel += decel;
        }
        st->vel = clampf(vel, -sbwd, sfwd);

        /* Smooth angular traverse */
        if (fast_fabsf(st->steer) > 0.05f) {
            float target_w = st->steer * trate;
            st->turn_vel = lerpf(st->turn_vel, target_w, clampf(dt * 12.0f, 0.0f, 1.0f));
        } else {
            st->turn_vel = lerpf(st->turn_vel, 0.0f, clampf(dt * 15.0f, 0.0f, 1.0f));
        }

        st->yaw = wrap_pi(st->yaw + st->turn_vel * dt);
        float ns = fast_sinf(st->yaw);
        float nc = fast_cosf(st->yaw);
        st->x += ns * st->vel * dt;
        st->z += nc * st->vel * dt;
        float accel = (st->vel - st->prev_vel) / (dt > 1e-4f ? dt : 0.016667f);
        st->prev_vel = st->vel;
        float squat_pitch = clampf(-accel * 0.0085f, -0.055f, 0.055f);
        float want_p = clampf(t_pitch + squat_pitch, -0.46f, 0.46f);
        float cent_roll = clampf(-st->vel * st->turn_vel * 0.012f, -0.05f, 0.05f);
        float want_r = clampf(t_roll + cent_roll, -0.34f, 0.34f);

        /* Spring-damper hull suspension integration */
        float kp = 55.0f, dp = 12.5f;
        float kr = 62.0f, dr = 13.5f;
        st->pitch_vel += ((want_p - st->pitch) * kp - st->pitch_vel * dp) * dt;
        st->roll_vel  += ((want_r - st->roll)  * kr - st->roll_vel  * dr) * dt;
        st->pitch = clampf(st->pitch + st->pitch_vel * dt, -0.46f, 0.46f);
        st->roll  = clampf(st->roll  + st->roll_vel  * dt, -0.34f, 0.34f);

        if (st->y > ground_y + 0.15f) {
            st->vert_vel -= 9.81f * dt;
            st->y += st->vert_vel * dt;
            if (st->y <= ground_y) {
                st->y = ground_y;
                st->vert_vel = 0.0f;
            }
        } else {
            st->y = lerpf(st->y, ground_y, clampf(dt * 18.0f, 0.0f, 1.0f));
            st->vert_vel = 0.0f;
        }
    }

    /* 3. Continuous 60 Hz Turret & Gun Slew with Difficulty Separation (0=easy, 1=medium, 2=hard) */
    st->aim_phase += dt;
    if (st->aim_phase > 628.3185f) st->aim_phase -= 628.3185f;
    if (st->alive) {
        int diff = st->difficulty;
        if (diff < 0) diff = 0;
        if (diff > 2) diff = 2;

        float slew_mult = 1.0f;
        float grav_comp_scale = 1.0f;
        float lock_tol_yaw = 0.035f;
        float lock_tol_pitch = 0.025f;

        float aim_tx = st->look_x;
        float aim_ty = st->look_y;
        float aim_tz = st->look_z;
        float dx0 = aim_tx - st->x;
        float dz0 = aim_tz - st->z;
        float dist_xz = fast_sqrtf(dx0 * dx0 + dz0 * dz0);
        float phase = st->aim_phase + (float)(st->id & 63) * 0.73f;

        if (diff == 0) {
            slew_mult = 0.72f;
            grav_comp_scale = 0.0f;
            lock_tol_yaw = 0.065f;
            lock_tol_pitch = 0.045f;
            if (st->has_combat_target && dist_xz > 4.0f) {
                float scale_d = clampf(dist_xz * 0.012f, 0.4f, 1.8f);
                float perp_x = -dz0 / dist_xz;
                float perp_z =  dx0 / dist_xz;
                aim_tx += perp_x * (fast_sinf(phase * 1.35f) * 0.85f * scale_d);
                aim_tz += perp_z * (fast_sinf(phase * 1.35f) * 0.85f * scale_d);
                aim_ty += fast_cosf(phase * 0.95f) * 0.28f * scale_d;
            }
        } else if (diff == 1) {
            slew_mult = 1.00f;
            grav_comp_scale = 0.65f;
            lock_tol_yaw = 0.035f;
            lock_tol_pitch = 0.025f;
            if (st->has_combat_target && dist_xz > 4.0f) {
                float scale_d = clampf(dist_xz * 0.010f, 0.35f, 1.35f);
                float perp_x = -dz0 / dist_xz;
                float perp_z =  dx0 / dist_xz;
                aim_tx += perp_x * (fast_sinf(phase * 1.15f) * 0.22f * scale_d);
                aim_tz += perp_z * (fast_sinf(phase * 1.15f) * 0.22f * scale_d);
                aim_ty += fast_cosf(phase * 0.85f) * 0.08f * scale_d;
            }
        } else {
            slew_mult = 1.22f;
            grav_comp_scale = 1.00f;
            lock_tol_yaw = 0.018f;
            lock_tol_pitch = 0.014f;
        }

        float want_tyaw   = 0.0f;
        float want_gpitch = 0.0f;
        if (st->has_look_target) {
            float gun_ox = st->x;
            float gun_oy = st->y + (st->gun_h > 0.5f ? st->gun_h : 2.15f);
            float gun_oz = st->z;
            float adx = aim_tx - gun_ox;
            float ady = aim_ty - gun_oy;
            float adz = aim_tz - gun_oz;
            float adist_xz = fast_sqrtf(adx * adx + adz * adz);

            if (st->has_combat_target && grav_comp_scale > 0.01f && adist_xz > 8.0f) {
                float v = (st->proj_speed > 50.0f && st->proj_speed < 3000.0f) ? st->proj_speed : 720.0f;
                float g = 9.81f;
                float v2 = v * v;
                float disc = v2 * v2 - g * (g * adist_xz * adist_xz + 2.0f * ady * v2);
                float drop_y = 0.5f * g * (adist_xz * adist_xz) / v2;
                if (disc > 0.0f && !st->is_arty) {
                    float tan_theta = (v2 - fast_sqrtf(disc)) / (g * adist_xz);
                    float ball_dy = tan_theta * adist_xz;
                    if (ball_dy > ady) drop_y = ball_dy - ady;
                }
                ady += clampf(drop_y, 0.0f, 35.0f) * grav_comp_scale;
            }

            compute_hull_local_aim(
                adx, ady, adz,
                st->yaw, st->pitch, st->roll,
                &want_tyaw, &want_gpitch
            );
        }
        want_tyaw = wrap_pi(want_tyaw);
        if (st->has_lim_yaw) {
            want_tyaw = clampf(want_tyaw, st->min_yaw, st->max_yaw);
        }
        want_gpitch = clampf(want_gpitch, st->min_pitch, st->max_pitch);

        st->want_tyaw   = want_tyaw;
        st->want_gpitch = want_gpitch;

        float tspd = (st->turret_speed > 0.05f ? st->turret_speed : 0.60f) * slew_mult;
        float gspd = (st->gun_speed > 0.05f    ? st->gun_speed    : (tspd * 0.75f)) * slew_mult;

        float dyaw = wrap_pi(want_tyaw - st->turret_yaw);
        float max_dyaw = tspd * dt;
        if (st->has_lim_yaw) {
            float lin_dyaw = want_tyaw - st->turret_yaw;
            if (fast_fabsf(lin_dyaw) <= max_dyaw) st->turret_yaw = want_tyaw;
            else st->turret_yaw += (lin_dyaw > 0.0f ? max_dyaw : -max_dyaw);
            st->turret_yaw = clampf(st->turret_yaw, st->min_yaw, st->max_yaw);
        } else {
            if (fast_fabsf(dyaw) <= max_dyaw) {
                st->turret_yaw = want_tyaw;
            } else {
                st->turret_yaw = wrap_pi(st->turret_yaw + (dyaw > 0.0f ? max_dyaw : -max_dyaw));
            }
        }

        float dpitch = want_gpitch - st->gun_pitch;
        float max_dpitch = gspd * dt;
        if (fast_fabsf(dpitch) <= max_dpitch) {
            st->gun_pitch = want_gpitch;
        } else {
            st->gun_pitch += (dpitch > 0.0f ? max_dpitch : -max_dpitch);
        }
        st->gun_pitch = clampf(st->gun_pitch, st->min_pitch, st->max_pitch);

        float rem_yaw   = fast_fabsf(wrap_pi(want_tyaw - st->turret_yaw));
        float rem_pitch = fast_fabsf(want_gpitch - st->gun_pitch);
        if (rem_yaw <= lock_tol_yaw && rem_pitch <= lock_tol_pitch) {
            st->aim_status = 2; /* Locked & ready to fire */
        } else if (rem_yaw <= 0.18f && rem_pitch <= 0.10f) {
            st->aim_status = 1; /* Tracking closely */
        } else {
            st->aim_status = 0; /* Slewing */
        }
    }

    /* 4. 3D LiDAR 16-Direction Multi-Ring Lookahead Scanner + Parallel 32x32 LOS Matrix */
    int count = g_space_count;
    uint32_t los_mask = 0u;
    uint32_t corridor_mask = 0u;

    if (st->alive && is_cell_open_halo_nolock(st->x, st->z)) {
        float spd_abs = fast_fabsf(st->vel);
        int max_rings = (spd_abs > 6.0f) ? 6 : 5;
        for (int d = 0; d < 16; ++d) {
            float ang = -3.14159265f + (float)d * (2.0f * 3.14159265f / 16.0f);
            float dx = fast_sinf(ang);
            float dz = fast_cosf(ang);
            float lx = dz;
            float lz = -dx;
            float prev_h = st->ground_y;
            int clear_dir = 1;
            for (int step = 1; step <= max_rings; ++step) {
                float dist = (float)step * 4.0f;
                float px = st->x + dx * dist;
                float pz = st->z + dz * dist;
                if (!is_cell_open_halo_nolock(px, pz) ||
                    is_cell_obstacle_nolock(px + lx * 1.8f, pz + lz * 1.8f) ||
                    is_cell_obstacle_nolock(px - lx * 1.8f, pz - lz * 1.8f)) {
                    clear_dir = 0;
                    break;
                }
                float h_step = sample_height_nolock(px, pz, prev_h);
                float dh = h_step - prev_h;
                /* Reject >38% uphill wall or >32% downhill cliff drop-off */
                if (dh > 4.0f * 0.38f || dh < -4.0f * 0.32f) {
                    clear_dir = 0;
                    break;
                }
                prev_h = h_step;
            }
            if (clear_dir) {
                corridor_mask |= (1u << d);
            }
        }
    }
    st->corridor_mask = corridor_mask;

    if (st->alive) {
        float obs_gun_y  = st->y + (st->gun_h > 0.5f ? st->gun_h : 2.15f);
        float obs_hull_y = st->y + (st->hull_h > 0.4f ? st->hull_h : 1.45f);

        for (int j = 0; j < count; ++j) {
            if (j == i) {
                los_mask |= (1u << j);
                continue;
            }
            const TankState *tj = &g_curr_state[j];
            if (!tj->alive) continue;

            float jx = tj->x;
            float jy = tj->y;
            float jz = tj->z;
            float dx = jx - st->x;
            float dz = jz - st->z;
            float dist_sq = dx * dx + dz * dz;
            if (dist_sq > 520.0f * 520.0f) continue;
            if (dist_sq < 9.0f) {
                los_mask |= (1u << j);
                continue;
            }

            /* v6.8: Vehicle hulls never block spotting LOS in WoT; line-of-fire checks vehicles separately */

            float tgt_gun_y  = jy + (tj->gun_h > 0.5f ? tj->gun_h : 2.15f);
            float tgt_hull_y = jy + (tj->hull_h > 0.4f ? tj->hull_h : 1.45f);
            if (raycast_heightfield_clear(st->x, obs_gun_y, st->z, jx, tgt_gun_y, jz) ||
                raycast_heightfield_clear(st->x, obs_gun_y, st->z, jx, tgt_hull_y, jz) ||
                raycast_heightfield_clear(st->x, obs_hull_y, st->z, jx, tgt_gun_y, jz)) {
                los_mask |= (1u << j);
            }
        }
    }
    st->los_mask = los_mask;
}

/*
 * Single-threaded Inter-Tank Collision Resolution phase (runs on Main Thread after the 2 worker threads finish).
 */
static void resolve_inter_collisions(void) {
    int count = g_space_count;
    for (int i = 0; i < count; ++i) {
        g_curr_state[i].push_x        = 0.0f;
        g_curr_state[i].push_z        = 0.0f;
        g_curr_state[i].ram_id        = -1.0f;
        g_curr_state[i].closing_speed = 0.0f;
    }

    for (int i = 0; i < count; ++i) {
        TankState *a = &g_curr_state[i];
        for (int j = i + 1; j < count; ++j) {
            TankState *b = &g_curr_state[j];
            if (!a->alive && !b->alive) continue;

            float dy = fast_fabsf(a->y - b->y);
            if (dy > 3.8f) continue;

            float dx = a->x - b->x;
            float dz = a->z - b->z;
            float dist_sq = dx * dx + dz * dz;
            float rad_a = (a->hw + a->hl) * 0.48f;
            float rad_b = (b->hw + b->hl) * 0.48f;
            float min_dist = rad_a + rad_b;
            if (dist_sq >= min_dist * min_dist) continue;

            float dist = fast_sqrtf(dist_sq);
            float nx = (dist > 1e-4f) ? (dx / dist) : 1.0f;
            float nz = (dist > 1e-4f) ? (dz / dist) : 0.0f;
            float overlap = min_dist - dist;

            /* Mass-weighted impulse distribution */
            float inv_a = a->alive ? (a->inv_mass > 0.0f ? a->inv_mass : (1.0f / 35000.0f)) : 0.0f;
            float inv_b = b->alive ? (b->inv_mass > 0.0f ? b->inv_mass : (1.0f / 35000.0f)) : 0.0f;
            float inv_sum = inv_a + inv_b;
            if (inv_sum < 1e-9f) continue;

            float share_a = inv_a / inv_sum;
            float share_b = inv_b / inv_sum;

            float sep_a = overlap * share_a * 0.60f;
            float sep_b = overlap * share_b * 0.60f;

            a->push_x += nx * sep_a;
            a->push_z += nz * sep_a;
            b->push_x -= nx * sep_b;
            b->push_z -= nz * sep_b;

            if (a->alive && a->mode == 1) {
                a->x += nx * sep_a;
                a->z += nz * sep_a;
                a->vel *= 0.85f;
            }
            if (b->alive && b->mode == 1) {
                b->x -= nx * sep_b;
                b->z -= nz * sep_b;
                b->vel *= 0.85f;
            }

            float vax = fast_sinf(a->yaw) * a->vel;
            float vaz = fast_cosf(a->yaw) * a->vel;
            float vbx = fast_sinf(b->yaw) * b->vel;
            float vbz = fast_cosf(b->yaw) * b->vel;
            float rel_vn = (vbx - vax) * nx + (vbz - vaz) * nz;
            if (rel_vn < 0.0f) rel_vn = 0.0f;

            if (overlap > 0.05f) {
                if (a->ram_id < 0.0f || rel_vn > a->closing_speed) {
                    a->ram_id = (float)b->id;
                    a->closing_speed = rel_vn;
                }
                if (b->ram_id < 0.0f || rel_vn > b->closing_speed) {
                    b->ram_id = (float)a->id;
                    b->closing_speed = rel_vn;
                }
            }
        }
    }
}

/* Legacy vehicle processor for step_frame backward compatibility */
static void process_one_vehicle(int i) {
    const float *vi = &g_in_buf[i * IN_STRIDE];
    float *vo = &g_out_buf[i * OUT_STRIDE];

    int self_id = (int)vi[0];
    float x     = vi[1];
    float y     = vi[2];
    float z     = vi[3];
    float yaw   = vi[4];
    float hw    = vi[8] > 0.5f ? vi[8] : 1.65f;
    float hl    = vi[9] > 0.5f ? vi[9] : 3.20f;
    int alive   = ((int)vi[10]) != 0;

    float s = fast_sinf(yaw);
    float c = fast_cosf(yaw);

    float fl_x = x + s * hl - c * hw;
    float fl_z = z + c * hl + s * hw;
    float fr_x = x + s * hl + c * hw;
    float fr_z = z + c * hl - s * hw;
    float rl_x = x - s * hl - c * hw;
    float rl_z = z - c * hl + s * hw;
    float rr_x = x - s * hl + c * hw;
    float rr_z = z - c * hl - s * hw;

    float h_fl = sample_height_nolock(fl_x, fl_z, y);
    float h_fr = sample_height_nolock(fr_x, fr_z, y);
    float h_rl = sample_height_nolock(rl_x, rl_z, y);
    float h_rr = sample_height_nolock(rr_x, rr_z, y);
    float h_c  = sample_height_nolock(x, z, y);

    float avg_corners = 0.25f * (h_fl + h_fr + h_rl + h_rr);
    float ground_y = (h_c > avg_corners) ? (0.6f * h_c + 0.4f * avg_corners) : avg_corners;
    float front_h  = 0.5f * (h_fl + h_fr);
    float rear_h   = 0.5f * (h_rl + h_rr);
    float left_h   = 0.5f * (h_fl + h_rl);
    float right_h  = 0.5f * (h_fr + h_rr);

    float pitch = -fast_atan2f(front_h - rear_h, hl * 2.0f);
    float roll  = fast_atan2f(left_h - right_h, hw * 2.0f);
    pitch = clampf(pitch, -0.45f, 0.45f);
    roll  = clampf(roll,  -0.32f, 0.32f);

    float push_x = 0.0f;
    float push_z = 0.0f;
    int ram_id = -1;
    int count = g_veh_count;

    if (alive) {
        for (int j = 0; j < count; ++j) {
            if (j == i) continue;
            const float *vj = &g_in_buf[j * IN_STRIDE];
            float jx = vj[1];
            float jy = vj[2];
            float jz = vj[3];
            if (fast_fabsf(y - jy) > 3.8f) continue;

            float dx = x - jx;
            float dz = z - jz;
            float dist_sq = dx * dx + dz * dz;
            float j_hw = vj[8] > 0.5f ? vj[8] : 1.65f;
            float j_hl = vj[9] > 0.5f ? vj[9] : 3.20f;
            float min_dist = (hw + hl + j_hw + j_hl) * 0.48f;
            if (dist_sq < min_dist * min_dist && dist_sq > 1e-4f) {
                float dist = fast_sqrtf(dist_sq);
                float overlap = min_dist - dist;
                float nx = dx / dist;
                float nz = dz / dist;
                push_x += nx * overlap * 0.40f;
                push_z += nz * overlap * 0.40f;
                if (ram_id < 0) {
                    ram_id = (int)vj[0];
                }
            }
        }
    }

    uint32_t los_mask = 0u;
    if (alive) {
        float obs_gun_y  = y + (vi[12] > 0.5f ? vi[12] : 2.15f);
        float obs_hull_y = y + (vi[13] > 0.4f ? vi[13] : 1.45f);

        for (int j = 0; j < count; ++j) {
            if (j == i) {
                los_mask |= (1u << j);
                continue;
            }
            const float *vj = &g_in_buf[j * IN_STRIDE];
            if (((int)vj[10]) == 0) continue;

            float jx = vj[1];
            float jy = vj[2];
            float jz = vj[3];
            float dx = jx - x;
            float dz = jz - z;
            float dist_sq = dx * dx + dz * dz;
            if (dist_sq > 520.0f * 520.0f) continue;
            if (dist_sq < 9.0f) {
                los_mask |= (1u << j);
                continue;
            }

            int blocked_by_hull = 0;
            for (int k = 0; k < count; ++k) {
                if (k == i || k == j) continue;
                const float *vk = &g_in_buf[k * IN_STRIDE];
                float kx = vk[1];
                float ky = vk[2];
                float kz = vk[3];
                float kyaw = vk[4];
                float khw = (vk[8] > 0.5f ? vk[8] : 1.65f) * 0.92f;
                float khl = (vk[9] > 0.5f ? vk[9] : 3.20f) * 0.92f;

                float max_ray_y = (obs_gun_y > jy + 2.2f) ? obs_gun_y : (jy + 2.2f);
                float min_ray_y = (obs_hull_y < jy + 1.0f) ? obs_hull_y : (jy + 1.0f);
                if (ky + 2.6f < min_ray_y || ky - 0.5f > max_ray_y) continue;

                if (segment_hits_obb_2d(x, z, jx, jz, kx, kz, kyaw, khw, khl)) {
                    blocked_by_hull = 1;
                    break;
                }
            }
            if (blocked_by_hull) continue;

            float tgt_gun_y  = jy + (vj[12] > 0.5f ? vj[12] : 2.15f);
            float tgt_hull_y = jy + (vj[13] > 0.4f ? vj[13] : 1.45f);

            if (raycast_heightfield_clear(x, obs_gun_y, z, jx, tgt_gun_y, jz) ||
                raycast_heightfield_clear(x, obs_gun_y, z, jx, tgt_hull_y, jz) ||
                raycast_heightfield_clear(x, obs_hull_y, z, jx, tgt_gun_y, jz)) {
                los_mask |= (1u << j);
            }
        }
    }

    vo[0] = (float)self_id;
    vo[1] = ground_y;
    vo[2] = pitch;
    vo[3] = roll;
    vo[4] = push_x;
    vo[5] = push_z;
    vo[6] = (float)ram_id;
    vo[7] = (float)(los_mask & 0xFFFFu);
    (void)los_mask;
}

static float compute_foliage_bonus_nolock(
    float ox, float oy, float oz,
    float tx, float ty, float tz,
    int fired_recently
) {
    if (!g_foliage || g_foliage_count <= 0) return 0.0f;

    float sx = ox;
    float sy = oy + 2.0f; /* OBSERVER_EYE_HEIGHT */
    float sz = oz;
    float ex = tx;
    float ey = ty + 1.5f; /* TARGET_CHECK_HEIGHT */
    float ez = tz;

    float seg_dx = ex - sx;
    float seg_dz = ez - sz;
    if (seg_dx * seg_dx + seg_dz * seg_dz < 0.01f) return 0.0f;

    float min_x = (sx < ex ? sx : ex) - 18.0f;
    float max_x = (sx > ex ? sx : ex) + 18.0f;
    float min_z = (sz < ez ? sz : ez) - 18.0f;
    float max_z = (sz > ez ? sz : ez) + 18.0f;
    float min_y = (sy < ey ? sy : ey) - 8.0f;
    float max_y = (sy > ey ? sy : ey) + 8.0f;

    float bonus = 0.0f;
    int count = g_foliage_count;

    for (int i = 0; i < count; ++i) {
        const float *inst = &g_foliage[i * 10];
        float cx = inst[0];
        if (cx < min_x || cx > max_x) continue;
        float cz = inst[2];
        if (cz < min_z || cz > max_z) continue;
        float y0 = inst[1] - 0.6f;
        float y1 = inst[3] + 0.6f;
        if (y1 < min_y || y0 > max_y) continue;

        float strength = inst[8];
        float radius   = inst[9];

        /* v6.8: Only count foliage within 25m of target or 12m of observer */
        float fdx = tx - cx;
        float fdz = tz - cz;
        float t_d2 = fdx * fdx + fdz * fdz;
        float odx = ox - cx;
        float odz = oz - cz;
        float o_d2 = odx * odx + odz * odz;
        float max_tr = 25.0f + radius;
        float max_or = 12.0f + radius;
        if (t_d2 > max_tr * max_tr && o_d2 > max_or * max_or) continue;

        if (fired_recently) {
            float r_fire = 15.0f + radius;
            if (t_d2 <= r_fire * r_fire) {
                /* Within 15m of firing gun: bush loses 80% of its concealment */
                strength *= 0.20f;
            }
        }

        float dx0 = sx - cx;
        float dz0 = sz - cz;
        float dx1 = ex - cx;
        float dz1 = ez - cz;

        float u0 = inst[4] * dx0 + inst[5] * dz0;
        float v0 = inst[6] * dx0 + inst[7] * dz0;
        float u1 = inst[4] * dx1 + inst[5] * dz1;
        float v1 = inst[6] * dx1 + inst[7] * dz1;

        float du = u1 - u0;
        float dv = v1 - v0;
        float low = 0.0f;
        float high = 1.0f;

        if (fast_fabsf(du) <= 1e-7f) {
            if (u0 < -1.15f || u0 > 1.15f) continue;
        } else {
            float t1 = (-1.15f - u0) / du;
            float t2 = ( 1.15f - u0) / du;
            if (t1 > t2) { float tmp = t1; t1 = t2; t2 = tmp; }
            if (t1 > low)  low = t1;
            if (t2 < high) high = t2;
            if (low > high) continue;
        }

        if (fast_fabsf(dv) <= 1e-7f) {
            if (v0 < -1.15f || v0 > 1.15f) continue;
        } else {
            float t1 = (-1.15f - v0) / dv;
            float t2 = ( 1.15f - v0) / dv;
            if (t1 > t2) { float tmp = t1; t1 = t2; t2 = tmp; }
            if (t1 > low)  low = t1;
            if (t2 < high) high = t2;
            if (low > high) continue;
        }

        float mid = 0.5f * (low + high);
        float y_mid = sy + (ey - sy) * mid;
        if (y_mid >= y0 && y_mid <= y1) {
            bonus += strength;
            if (bonus >= 0.65f) {
                return 0.65f;
            }
        }
    }
    return (bonus > 0.48f) ? 0.48f : bonus;
}

static void sync_staging_inputs_to_curr_state_nolock(const float *in_ptr, int count, int first_init) {
    for (int i = 0; i < count; ++i) {
        const float *vi = &in_ptr[i * SUB_IN_STRIDE];
        TankState *st = &g_curr_state[i];
        int id       = (int)vi[0];
        int flags    = (int)vi[23];
        int alive    = (flags & 1) != 0;
        int mode     = (flags & 2) != 0 ? 1 : 0;
        int has_look = (flags & 4) != 0;
        int has_lim  = (flags & 8) != 0;

        int reseeded = (first_init || st->id != id);
        if (!reseeded) {
            float dx = vi[1] - st->x;
            float dz = vi[3] - st->z;
            if (dx * dx + dz * dz > 25.0f) {
                reseeded = 1;
            }
        }

        int diff       = (flags >> 4) & 3;
        if (diff > 2) diff = 2;
        int has_combat = (flags & 64) != 0;
        int is_arty    = (flags & 128) != 0;

        st->id                = id;
        st->alive             = alive;
        st->mode              = mode;
        st->has_look_target   = has_look;
        st->has_lim_yaw       = has_lim;
        st->difficulty        = diff;
        st->has_combat_target = has_combat;
        st->is_arty           = is_arty;
        st->hw                = vi[11] > 0.5f ? vi[11] : 1.65f;
        st->hl                = vi[12] > 0.5f ? vi[12] : 3.20f;
        st->look_x            = vi[19];
        st->look_y            = vi[20];
        st->look_z            = vi[21];
        st->turret_speed      = vi[22] > 0.05f ? vi[22] : 0.60f;
        st->gun_h             = 2.15f;
        st->hull_h            = 1.45f;

        if (mode == 0) {
            /* Mode 0 (external WGVehiclePhysics2 drive): vi[13..18] carry gun limits & projectile speed */
            st->mass         = 35000.0f;
            st->inv_mass     = 1.0f / 35000.0f;
            st->engine_power = 500.0f;
            st->speed_fwd    = 14.0f;
            st->speed_bwd    = 5.0f;
            st->turn_rate    = 0.65f;
            st->throttle     = 0.0f;
            st->steer        = 0.0f;
            st->proj_speed   = (vi[13] > 50.0f && vi[13] < 3000.0f) ? vi[13] : 720.0f;
            st->gun_speed    = (vi[14] > 0.05f && vi[14] < 10.0f) ? vi[14] : (st->turret_speed * 0.75f);
            st->min_yaw      = has_lim ? vi[15] : -0.45f;
            st->max_yaw      = has_lim ? vi[16] :  0.45f;
            st->min_pitch    = (vi[17] < -0.01f && vi[17] > -1.55f) ? vi[17] : -0.35f;
            st->max_pitch    = (vi[18] > -0.05f && vi[18] <  0.85f) ? vi[18] :  0.18f;
        } else {
            st->mass         = vi[13] > 500.0f ? vi[13] : 35000.0f;
            st->inv_mass     = 1.0f / st->mass;
            st->engine_power = vi[14] > 20.0f ? vi[14] : 500.0f;
            st->speed_fwd    = vi[15] > 1.0f ? vi[15] : 14.0f;
            st->speed_bwd    = vi[16] > 1.0f ? vi[16] : 5.0f;
            st->turn_rate    = 0.65f;
            st->throttle     = clampf(vi[17], -1.0f, 1.0f);
            st->steer        = clampf(vi[18], -1.0f, 1.0f);
            st->proj_speed   = 720.0f;
            st->gun_speed    = st->turret_speed * 0.75f;
            st->min_yaw      = -0.45f;
            st->max_yaw      =  0.45f;
            st->min_pitch    = -0.35f;
            st->max_pitch    =  0.18f;
        }

        if (reseeded) {
            st->x          = vi[1];
            st->y          = vi[2];
            st->z          = vi[3];
            st->yaw        = wrap_pi(vi[4]);
            st->pitch      = vi[5];
            st->roll       = vi[6];
            st->vel        = vi[7];
            st->turn_vel   = vi[8];
            st->turret_yaw = wrap_pi(vi[9]);
            st->gun_pitch  = vi[10];
            st->want_tyaw   = st->turret_yaw;
            st->want_gpitch = st->gun_pitch;
            st->ground_y   = vi[2];
            st->t_pitch    = vi[5];
            st->t_roll     = vi[6];
            g_prev_state[i] = *st;
        } else {
            g_prev_state[i] = *st;
            if (mode == 0) {
                st->x        = vi[1];
                st->y        = vi[2];
                st->z        = vi[3];
                st->yaw      = wrap_pi(vi[4]);
                st->pitch    = vi[5];
                st->roll     = vi[6];
                st->vel      = vi[7];
                st->turn_vel = vi[8];
                if (!st->has_look_target) {
                    st->turret_yaw = wrap_pi(vi[9]);
                    st->gun_pitch  = vi[10];
                }
            }
        }
    }
}

static void publish_double_buffer_snapshot_nolock(int count) {
    LONG cur_bank  = g_front_bank;
    LONG next_bank = (cur_bank == 0) ? 1 : 0;
    for (int i = 0; i < count; ++i) {
        g_snap_prev[next_bank][i] = g_prev_state[i];
        g_snap_curr[next_bank][i] = g_curr_state[i];
    }
    __sync_synchronize();
    g_snap_count = count;
    g_front_bank = next_bank;
    g_extrap_dt_accum = 0.0f;
    __sync_fetch_and_add(&g_completed_seq, 1);
}

static DWORD WINAPI worker_thread_proc(LPVOID param) {
    int tid = (int)(intptr_t)param;
    HANDLE my_start = g_start_events[tid];

    while (!g_shutdown) {
        p_WaitForSingleObject(my_start, INFINITE);
        if (g_shutdown) break;

        if (g_worker_mode == 1) {
            int count = g_space_count;
            int nthr  = g_num_threads > 0 ? g_num_threads : 1;
            int chunk = (count + nthr - 1) / nthr;
            int start_idx = tid * chunk;
            int end_idx   = start_idx + chunk;
            if (end_idx > count) end_idx = count;
            for (int i = start_idx; i < end_idx; ++i) {
                step_tank_60hz(i, FIXED_DT);
            }
        } else {
            for (;;) {
                int idx = (int)__sync_fetch_and_add(&g_next_veh_idx, 1);
                if (idx >= g_veh_count) break;
                process_one_vehicle(idx);
            }
        }

        if (__sync_sub_and_fetch(&g_active_workers, 1) == 0) {
            if (g_worker_mode == 1) {
                /* Resolve inter-tank OBB collisions and publish double-buffer bank on worker thread! */
                resolve_inter_collisions();
                publish_double_buffer_snapshot_nolock(g_space_count);
            }
            __sync_synchronize();
            g_workers_busy = 0;
            p_SetEvent(g_done_event);
        }
    }
    return 0;
}

static void init_thread_pool(void) {
    if (g_cs_inited) return;
    if (!resolve_win32_apis()) return;

    p_InitializeCriticalSection(&g_cs);
    g_cs_inited = 1;

    fast_memset_zero(g_prev_state, sizeof(g_prev_state));
    fast_memset_zero(g_curr_state, sizeof(g_curr_state));

    g_done_event = p_CreateEventA(NULL, 1, 0, NULL);
    g_num_threads = 0;

    int target_threads = PHYSICS_WORKER_THREADS;
    if (p_GetSystemInfo) {
        SYSTEM_INFO sys_info;
        fast_memset_zero(&sys_info, sizeof(sys_info));
        p_GetSystemInfo(&sys_info);
        if (sys_info.dwNumberOfProcessors >= 4) {
            target_threads = 4;
        } else if (sys_info.dwNumberOfProcessors >= 2) {
            target_threads = 2;
        }
    }
    /* Temporary in-memory runtime boost while WorldOfTanks.exe is running:
     * 1. Unpin process CPU affinity across all logical cores so D3D9, FMOD, and worker threads never fight Core 0.
     * 2. Elevate process priority to ABOVE_NORMAL_PRIORITY_CLASS (0x8000) and main render thread to ABOVE_NORMAL (+1).
     * 3. Isolate background physics worker threads to Cores 1..N (off Core 0) on >=4-core CPUs.
     * 4. Enable 1 ms Windows kernel scheduler resolution (timeBeginPeriod(1)). */
    g_worker_affinity_mask = 0;
    if (p_GetCurrentProcess) {
        HANDLE hProc = p_GetCurrentProcess();
        if (p_GetProcessAffinityMask && p_SetProcessAffinityMask) {
            uintptr_t proc_mask = 0, sys_mask = 0;
            if (p_GetProcessAffinityMask(hProc, &proc_mask, &sys_mask) && sys_mask != 0) {
                /* Unclamp affinity: allow process access to all logical cores for D3D9 / FMOD / physics */
                if (proc_mask != sys_mask) {
                    p_SetProcessAffinityMask(hProc, sys_mask);
                    proc_mask = sys_mask;
                }
                if ((proc_mask & ~((uintptr_t)1u)) != 0) {
                    g_worker_affinity_mask = proc_mask & ~((uintptr_t)1u);
                }
            }
        }
        if (p_SetPriorityClass) {
            p_SetPriorityClass(hProc, 0x00000080u); /* HIGH_PRIORITY_CLASS (was 0x8000 ABOVE_NORMAL) */
        }
        if (p_SetProcessPriorityBoost) {
            p_SetProcessPriorityBoost(hProc, 0);
        }
    }
    if (p_GetCurrentThread && p_SetThreadPriority) {
        HANDLE hMainTh = p_GetCurrentThread();
        p_SetThreadPriority(hMainTh, 2); /* THREAD_PRIORITY_HIGHEST */
        if (p_SetThreadPriorityBoost) {
            p_SetThreadPriorityBoost(hMainTh, 0);
        }
    }
    if (p_timeBeginPeriod && !g_timer_period_active) {
        p_timeBeginPeriod(1);
        g_timer_period_active = 1;
    }

    for (int i = 0; i < target_threads; ++i) {
        HANDLE ev = p_CreateEventA(NULL, 0, 0, NULL);
        if (!ev) break;
        g_start_events[i] = ev;
        DWORD thread_id = 0;
        HANDLE th = p_CreateThread(NULL, 65536, worker_thread_proc, (LPVOID)(intptr_t)i, 0, &thread_id);
        if (!th) break;
        if (g_worker_affinity_mask != 0 && p_SetThreadAffinityMask) {
            p_SetThreadAffinityMask(th, g_worker_affinity_mask);
        }
        g_threads[i] = th;
        g_num_threads++;
    }
}

static PyObject * CDECL py_calibrate_array_offset(PyObject *self, PyObject *args) {
    PyObject *probe_obj = NULL;
    PyObject *status_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "OO", &probe_obj, &status_obj)) {
        return NULL;
    }

    uint8_t *raw = (uint8_t *)probe_obj;
    g_array_ptr_offset = 12;
    float *cand = *(float **)(raw + 12);
    if ((uintptr_t)cand >= 0x00010000u && ((uintptr_t)cand & 3u) == 0u) {
        if (cand[0] == 12345.0f && cand[1] == -6789.0f) {
            g_array_ptr_offset = 12;
        }
    }

    int st_len = 0;
    float *st = (float *)get_array_buffer(status_obj, &st_len);
    if ((uintptr_t)st >= 0x00010000u && ((uintptr_t)st & 3u) == 0u) {
        if (st_len >= 1) st[0] = (float)(g_num_threads > 0 ? g_num_threads : PHYSICS_WORKER_THREADS);
        if (st_len >= 2) st[1] = (float)g_array_ptr_offset;
        if (st_len >= 3) st[2] = (float)g_exe_patches_applied;
        if (st_len >= 4) st[3] = (float)g_collide_hook_ready;
    }

    return return_borrowed_arg(status_obj);
}

static PyObject * CDECL py_upload_heightfield(PyObject *self, PyObject *args) {
    int nx = 0, nz = 0;
    double min_x = 0.0, min_z = 0.0, cell_sz = 18.0;
    PyObject *arr_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "iidddO", &nx, &nz, &min_x, &min_z, &cell_sz, &arr_obj)) {
        return NULL;
    }
    if (nx < 2 || nz < 2 || nx > 1024 || nz > 1024) {
        return return_borrowed_arg(arr_obj);
    }

    size_t count = (size_t)(nx * nz);
    size_t bytes = count * sizeof(float);
    float *src = (float *)get_array_buffer(arr_obj, NULL);
    if (!src) {
        if (p_PyErr_SetString && pp_PyExc_RuntimeError && *pp_PyExc_RuntimeError) p_PyErr_SetString(*pp_PyExc_RuntimeError, "invalid heightfield array buffer");
        return NULL;
    }

    if (!g_cs_inited) init_thread_pool();
    if (!g_cs_inited) return return_borrowed_arg(arr_obj);

    p_EnterCriticalSection(&g_cs);
    if (g_workers_busy) {
        p_WaitForSingleObject(g_done_event, INFINITE);
        g_workers_busy = 0;
    }
    float *new_h = (float *)p_VirtualAlloc(NULL, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (new_h) {
        fast_memcpy(new_h, src, bytes);
        if (g_heights) {
            p_VirtualFree(g_heights, 0, MEM_RELEASE);
        }
        g_heights       = new_h;
        g_heights_bytes = bytes;
        g_grid_nx       = nx;
        g_grid_nz       = nz;
        g_min_x         = (float)min_x;
        g_min_z         = (float)min_z;
        g_cell_sz       = (cell_sz > 0.5) ? (float)cell_sz : 18.0f;
    }
    g_time_accum = 0.0f;
    g_space_count = 0;
    p_LeaveCriticalSection(&g_cs);

    return return_borrowed_arg(arr_obj);
}

static PyObject * CDECL py_upload_foliage(PyObject *self, PyObject *args) {
    int count = 0;
    PyObject *arr_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "iO", &count, &arr_obj)) {
        return NULL;
    }
    if (count < 0) count = 0;
    if (count > MAX_FOLIAGE) count = MAX_FOLIAGE;

    size_t bytes = (size_t)(count * 10) * sizeof(float);
    float *src = (float *)get_array_buffer(arr_obj, NULL);
    if (count > 0 && !src) {
        if (p_PyErr_SetString && pp_PyExc_RuntimeError && *pp_PyExc_RuntimeError) p_PyErr_SetString(*pp_PyExc_RuntimeError, "invalid foliage array buffer");
        return NULL;
    }

    if (!g_cs_inited) init_thread_pool();
    if (!g_cs_inited) return return_borrowed_arg(arr_obj);

    p_EnterCriticalSection(&g_cs);
    if (g_workers_busy) {
        p_WaitForSingleObject(g_done_event, INFINITE);
        g_workers_busy = 0;
    }
    if (count > 0) {
        float *new_f = (float *)p_VirtualAlloc(NULL, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if (new_f) {
            fast_memcpy(new_f, src, bytes);
            if (g_foliage) p_VirtualFree(g_foliage, 0, MEM_RELEASE);
            g_foliage = new_f;
            g_foliage_count = count;
        }
    } else {
        g_foliage_count = 0;
    }
    p_LeaveCriticalSection(&g_cs);

    return return_borrowed_arg(arr_obj);
}

static PyObject * CDECL py_lidar_scan(PyObject *self, PyObject *args) {
    double x_d = 0.0, y_d = 0.0, z_d = 0.0, yaw_d = 0.0, spd_d = 0.0;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "dddddO", &x_d, &y_d, &z_d, &yaw_d, &spd_d, &out_obj)) {
        return NULL;
    }
    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (!out_ptr || out_len < 8) {
        return return_borrowed_arg(out_obj);
    }

    Py_BEGIN_ALLOW_THREADS
    float x = (float)x_d;
    float y = (float)y_d;
    float z = (float)z_d;
    float yaw = (float)yaw_d;
    float spd = fast_fabsf((float)spd_d);
    float ground_y = sample_height_nolock(x, z, y);

    uint32_t mask = 0u;
    float fwd_grade = 0.0f;
    float fwd_clear_dist = 24.0f;
    float best_left_yaw = yaw - 0.65f;
    float best_right_yaw = yaw + 0.65f;
    float best_left_score = -1e9f;
    float best_right_score = -1e9f;
    float mode_hint = 0.0f; /* 0 = normal, 1 = hill contour assist, -1 = hazard brake/turn */

    for (int d = 0; d < 16; ++d) {
        float ang = -3.14159265f + (float)d * (2.0f * 3.14159265f / 16.0f);
        float dx = fast_sinf(ang);
        float dz = fast_cosf(ang);
        float prev_h = ground_y;
        int clear_dir = 1;
        float max_up_grade = 0.0f;
        for (int step = 1; step <= 6; ++step) {
            float dist = (float)step * 4.0f;
            float px = x + dx * dist;
            float pz = z + dz * dist;
            if (is_cell_obstacle_nolock(px, pz)) {
                clear_dir = 0;
                break;
            }
            float h_step = sample_height_nolock(px, pz, prev_h);
            float dh = h_step - prev_h;
            float gr = dh * 0.25f;
            if (gr > max_up_grade) max_up_grade = gr;
            if (dh > 4.0f * 0.55f || dh < -4.0f * 0.45f) {
                clear_dir = 0;
                break;
            }
            prev_h = h_step;
        }
        if (clear_dir) {
            mask |= (1u << d);
            float diff = wrap_pi(ang - yaw);
            float abs_diff = fast_fabsf(diff);
            if (abs_diff <= 1.35f) {
                float sc = 2.0f - abs_diff - max_up_grade * 2.5f;
                if (diff < -0.15f && sc > best_left_score) {
                    best_left_score = sc;
                    best_left_yaw = ang;
                } else if (diff > 0.15f && sc > best_right_score) {
                    best_right_score = sc;
                    best_right_yaw = ang;
                }
            }
        }
    }

    /* Exact forward ray along current yaw */
    float fdx = fast_sinf(yaw);
    float fdz = fast_cosf(yaw);
    float prev_fy = ground_y;
    for (int step = 1; step <= 6; ++step) {
        float dist = (float)step * 4.0f;
        float px = x + fdx * dist;
        float pz = z + fdz * dist;
        float h_step = sample_height_nolock(px, pz, prev_fy);
        if (step == 2) {
            fwd_grade = (h_step - ground_y) / 8.0f;
        }
        if (is_cell_obstacle_nolock(px, pz) || (h_step - prev_fy) < -4.0f * 0.32f) {
            if (dist - 4.0f < fwd_clear_dist) {
                fwd_clear_dist = dist - 4.0f;
            }
            break;
        }
        prev_fy = h_step;
    }

    if (fwd_clear_dist < 8.0f + spd * 0.8f) {
        mode_hint = -1.0f;
    } else if (fwd_grade > 0.32f && spd < 1.5f) {
        mode_hint = 1.0f;
    }

    out_ptr[0] = (float)mask;
    out_ptr[1] = fwd_grade;
    out_ptr[2] = fwd_clear_dist;
    out_ptr[3] = best_left_yaw;
    out_ptr[4] = best_right_yaw;
    out_ptr[5] = mode_hint;
    out_ptr[6] = ground_y;
    out_ptr[7] = 1.0f;
    Py_END_ALLOW_THREADS
    return return_borrowed_arg(out_obj);
}

static PyObject * CDECL py_foliage_bonus(PyObject *self, PyObject *args) {
    double ox = 0.0, oy = 0.0, oz = 0.0;
    double tx = 0.0, ty = 0.0, tz = 0.0;
    int fired = 0;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "ddddddiO", &ox, &oy, &oz, &tx, &ty, &tz, &fired, &out_obj)) {
        return NULL;
    }
    float *out_ptr = (float *)get_array_buffer(out_obj, NULL);
    if (out_ptr) {
        Py_BEGIN_ALLOW_THREADS
        out_ptr[0] = compute_foliage_bonus_nolock(
            (float)ox, (float)oy, (float)oz,
            (float)tx, (float)ty, (float)tz, fired);
        Py_END_ALLOW_THREADS
    }
    return return_borrowed_arg(out_obj);
}

static void export_front_snapshot_nonblocking(float render_dt, float *out_ptr, int *out_count) {
    LONG bank  = g_front_bank;
    int  count = (int)g_snap_count;
    if (count <= 0 || count > MAX_VEHICLES) {
        if (out_count) *out_count = 0;
        return;
    }

    /* Advance smooth client render phase (extrapolating up to 1.35 ticks if workers are mid-flight) */
    g_extrap_dt_accum += clampf(render_dt, 0.0005f, 0.05f);
    float alpha = clampf(g_extrap_dt_accum / FIXED_DT, 0.0f, 1.35f);
    g_last_alpha = alpha;

    const TankState *prv_bank = g_snap_prev[bank];
    const TankState *cur_bank = g_snap_curr[bank];

    for (int i = 0; i < count; ++i) {
        const TankState *prv = &prv_bank[i];
        const TankState *cur = &cur_bank[i];
        float *vo = &out_ptr[i * SUB_OUT_STRIDE];

        float ex_x, ex_y, ex_z, ex_yaw, ex_tyaw, ex_gpitch;
        if (alpha <= 1.0f) {
            ex_x      = lerpf(prv->x, cur->x, alpha);
            ex_y      = lerpf(prv->y, cur->y, alpha);
            ex_z      = lerpf(prv->z, cur->z, alpha);
            ex_yaw    = lerp_angle(prv->yaw,        cur->yaw,        alpha);
            ex_tyaw   = lerp_angle(prv->turret_yaw, cur->turret_yaw, alpha);
            ex_gpitch = lerp_angle(prv->gun_pitch,  cur->gun_pitch,  alpha);
        } else {
            /* Smooth client dead-reckoning beyond Tick N when 10Hz/60Hz step is in flight */
            float over_dt = (alpha - 1.0f) * FIXED_DT;
            ex_yaw    = wrap_pi(cur->yaw + cur->turn_vel * over_dt);
            ex_x      = cur->x + fast_sinf(ex_yaw) * cur->vel * over_dt;
            ex_y      = cur->y;
            ex_z      = cur->z + fast_cosf(ex_yaw) * cur->vel * over_dt;
            ex_tyaw   = lerp_angle(prv->turret_yaw, cur->turret_yaw, alpha);
            ex_gpitch = lerp_angle(prv->gun_pitch,  cur->gun_pitch,  alpha);
        }

        vo[0]  = (float)cur->id;
        vo[1]  = ex_x;
        vo[2]  = ex_y;
        vo[3]  = ex_z;
        vo[4]  = ex_yaw;
        vo[5]  = lerp_angle(prv->t_pitch, cur->t_pitch, (alpha <= 1.0f ? alpha : 1.0f));
        vo[6]  = lerp_angle(prv->t_roll,  cur->t_roll,  (alpha <= 1.0f ? alpha : 1.0f));
        vo[7]  = ex_tyaw;
        vo[8]  = ex_gpitch;
        vo[9]  = (cur->mode == 0) ? cur->want_tyaw   : cur->push_x;
        vo[10] = (cur->mode == 0) ? cur->want_gpitch : cur->push_z;
        vo[11] = (float)(cur->los_mask & 0xFFFFu);
        vo[12] = (float)((cur->los_mask >> 16) & 0xFFFFu);
        vo[13] = cur->ground_y;
        vo[14] = (float)((cur->corridor_mask & 0xFFFFu) | ((uint32_t)(cur->aim_status & 3) << 16));
        vo[15] = cur->ram_id;
    }
    if (out_count) *out_count = count;
}

/*
 * Non-blocking harvest of the latest completed C++ worker results at the start of the Python frame:
 *   get_last_results_nonblocking(render_dt, out_array) -> returns out_array (with count in g_snap_count)
 * NEVER calls WaitForSingleObject and NEVER blocks the main Python thread!
 */
static PyObject * CDECL py_get_last_results_nonblocking(PyObject *self, PyObject *args) {
    double render_dt_d = 0.0166667;
    PyObject *out_obj  = NULL;
    PyObject *meta_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "dOO", &render_dt_d, &out_obj, &meta_obj)) {
        return NULL;
    }
    float *out_ptr  = (float *)get_array_buffer(out_obj, NULL);
    float *meta_ptr = (float *)get_array_buffer(meta_obj, NULL);
    if (!out_ptr) {
        return return_borrowed_arg(out_obj);
    }

    Py_BEGIN_ALLOW_THREADS
    int exported_count = 0;
    export_front_snapshot_nonblocking((float)render_dt_d, out_ptr, &exported_count);
    if (meta_ptr) {
        meta_ptr[0] = (float)exported_count;
        meta_ptr[1] = (float)g_completed_seq;
        meta_ptr[2] = (float)g_workers_busy;
    }
    Py_END_ALLOW_THREADS

    return return_borrowed_arg(out_obj);
}

/*
 * Non-blocking asynchronous submission of fresh vehicle coordinates & inputs:
 *   submit_step_async(count, render_dt, in_array) -> returns in_array immediately
 * Stages inputs into `g_staging_in_buf` and wakes the Win32 worker pool if idle.
 * NEVER blocks the main Python thread waiting for C worker threads!
 */
static PyObject * CDECL py_submit_step_async(PyObject *self, PyObject *args) {
    int count = 0;
    double render_dt_d = 0.0166667;
    PyObject *in_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "idO", &count, &render_dt_d, &in_obj)) {
        return NULL;
    }
    if (count <= 0) {
        return return_borrowed_arg(in_obj);
    }
    if (count > MAX_VEHICLES) count = MAX_VEHICLES;

    float *in_ptr = (float *)get_array_buffer(in_obj, NULL);
    if (!in_ptr) {
        return return_borrowed_arg(in_obj);
    }

    if (!g_cs_inited) init_thread_pool();

    Py_BEGIN_ALLOW_THREADS
    /* Copy fresh coordinates/inputs into atomic staging buffer */
    fast_memcpy(g_staging_in_buf, in_ptr, (size_t)(count * SUB_IN_STRIDE) * sizeof(float));
    g_staging_count = count;
    __sync_synchronize();
    g_staging_ready = 1;

    float render_dt = clampf((float)render_dt_d, 0.0005f, 0.10f);
    g_time_accum += render_dt;
    if (g_time_accum > FIXED_DT * 2.5f) {
        g_time_accum = FIXED_DT * 2.5f;
    }

    /* Try to acquire the worker pool non-blockingly (__sync_bool_compare_and_swap) */
    if (__sync_bool_compare_and_swap(&g_workers_busy, 0, 1)) {
        int first_init = (g_snap_count == 0 || g_space_count != count);
        g_space_count = count;
        g_staging_ready = 0;
        sync_staging_inputs_to_curr_state_nolock(g_staging_in_buf, count, first_init);

        if (first_init) {
            /* Seed initial double-buffer snapshot immediately so frame 1 has valid data,
             * then let background workers compute the full 60Hz tick asynchronously! */
            publish_double_buffer_snapshot_nolock(count);
        }

        if (g_time_accum >= FIXED_DT || first_init) {
            if (g_time_accum >= FIXED_DT) g_time_accum -= FIXED_DT;
            else g_time_accum = 0.0f;

            g_worker_mode = 1;
            if (g_num_threads >= 2 && count >= 4) {
                p_ResetEvent(g_done_event);
                g_active_workers = g_num_threads;
                for (int t = 0; t < g_num_threads; ++t) {
                    p_SetEvent(g_start_events[t]);
                }
            } else {
                for (int i = 0; i < count; ++i) {
                    step_tank_60hz(i, FIXED_DT);
                }
                resolve_inter_collisions();
                publish_double_buffer_snapshot_nolock(count);
                g_workers_busy = 0;
            }
        } else {
            g_workers_busy = 0;
        }
    }
    Py_END_ALLOW_THREADS

    return return_borrowed_arg(in_obj);
}

static PyObject * CDECL py_step_physics_space(PyObject *self, PyObject *args) {
    int count = 0;
    double render_dt_d = 0.0166667;
    PyObject *in_obj  = NULL;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "idOO", &count, &render_dt_d, &in_obj, &out_obj)) {
        return NULL;
    }
    if (count <= 0) {
        return return_borrowed_arg(out_obj);
    }
    if (count > MAX_VEHICLES) count = MAX_VEHICLES;

    float *in_ptr  = (float *)get_array_buffer(in_obj, NULL);
    float *out_ptr = (float *)get_array_buffer(out_obj, NULL);
    if (!in_ptr || !out_ptr) {
        if (p_PyErr_SetString && pp_PyExc_RuntimeError && *pp_PyExc_RuntimeError) p_PyErr_SetString(*pp_PyExc_RuntimeError, "null array buffer in step_physics_space");
        return NULL;
    }

    if (!g_cs_inited) init_thread_pool();

    Py_BEGIN_ALLOW_THREADS
    /* 1. If first frame (no snapshot published yet), seed initial state */
    if (g_snap_count == 0 && __sync_bool_compare_and_swap(&g_workers_busy, 0, 1)) {
        g_space_count = count;
        sync_staging_inputs_to_curr_state_nolock(in_ptr, count, 1);
        for (int i = 0; i < count; ++i) {
            step_tank_60hz(i, FIXED_DT);
        }
        resolve_inter_collisions();
        publish_double_buffer_snapshot_nolock(count);
        g_workers_busy = 0;
    }

    /* 2. Harvest latest completed double-buffer snapshot NON-BLOCKINGLY */
    int exported_count = 0;
    export_front_snapshot_nonblocking((float)render_dt_d, out_ptr, &exported_count);

    /* 3. Stage fresh inputs and launch next worker step asynchronously without waiting */
    fast_memcpy(g_staging_in_buf, in_ptr, (size_t)(count * SUB_IN_STRIDE) * sizeof(float));
    g_staging_count = count;
    g_staging_ready = 1;

    float render_dt = clampf((float)render_dt_d, 0.0005f, 0.10f);
    g_time_accum += render_dt;
    if (g_time_accum > FIXED_DT * 2.5f) {
        g_time_accum = FIXED_DT * 2.5f;
    }

    if (__sync_bool_compare_and_swap(&g_workers_busy, 0, 1)) {
        int first_init = (g_space_count != count);
        g_space_count = count;
        g_staging_ready = 0;
        sync_staging_inputs_to_curr_state_nolock(g_staging_in_buf, count, first_init);

        if (g_time_accum >= FIXED_DT || first_init) {
            if (g_time_accum >= FIXED_DT) g_time_accum -= FIXED_DT;
            else g_time_accum = 0.0f;

            g_worker_mode = 1;
            if (g_num_threads >= 2 && count >= 4) {
                p_ResetEvent(g_done_event);
                g_active_workers = g_num_threads;
                for (int t = 0; t < g_num_threads; ++t) {
                    p_SetEvent(g_start_events[t]);
                }
            } else {
                for (int i = 0; i < count; ++i) {
                    step_tank_60hz(i, FIXED_DT);
                }
                resolve_inter_collisions();
                publish_double_buffer_snapshot_nolock(count);
                g_workers_busy = 0;
            }
        } else {
            g_workers_busy = 0;
        }
    }
    Py_END_ALLOW_THREADS

    return return_borrowed_arg(out_obj);
}

static PyObject * CDECL py_get_interpolated_transform(PyObject *self, PyObject *args) {
    int target_id  = 0;
    double alpha_d = -1.0;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "idO", &target_id, &alpha_d, &out_obj)) {
        return NULL;
    }
    float *out_ptr = (float *)get_array_buffer(out_obj, NULL);
    if (!out_ptr) {
        return return_borrowed_arg(out_obj);
    }
    out_ptr[0] = 0.0f;

    float alpha = (alpha_d >= 0.0 && alpha_d <= 1.0) ? (float)alpha_d : g_last_alpha;
    int count = g_space_count;
    for (int i = 0; i < count; ++i) {
        if (g_curr_state[i].id == target_id) {
            const TankState *prv = &g_prev_state[i];
            const TankState *cur = &g_curr_state[i];
            out_ptr[0]  = 1.0f;
            out_ptr[1]  = lerpf(prv->x, cur->x, alpha);
            out_ptr[2]  = lerpf(prv->y, cur->y, alpha);
            out_ptr[3]  = lerpf(prv->z, cur->z, alpha);
            out_ptr[4]  = lerp_angle(prv->yaw,        cur->yaw,        alpha);
            out_ptr[5]  = lerp_angle(prv->pitch,      cur->pitch,      alpha);
            out_ptr[6]  = lerp_angle(prv->roll,       cur->roll,       alpha);
            out_ptr[7]  = lerp_angle(prv->turret_yaw, cur->turret_yaw, alpha);
            out_ptr[8]  = lerp_angle(prv->gun_pitch,  cur->gun_pitch,  alpha);
            out_ptr[9]  = lerpf(prv->vel,      cur->vel,      alpha);
            out_ptr[10] = lerpf(prv->turn_vel, cur->turn_vel, alpha);
            break;
        }
    }
    return return_borrowed_arg(out_obj);
}

static PyObject * CDECL py_step_frame(PyObject *self, PyObject *args) {
    int count = 0;
    PyObject *in_obj = NULL;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "iOO", &count, &in_obj, &out_obj)) {
        return NULL;
    }
    if (count <= 0) {
        return return_borrowed_arg(out_obj);
    }
    if (count > MAX_VEHICLES) count = MAX_VEHICLES;

    float *in_ptr  = (float *)get_array_buffer(in_obj, NULL);
    float *out_ptr = (float *)get_array_buffer(out_obj, NULL);
    if (!in_ptr || !out_ptr) {
        if (p_PyErr_SetString && pp_PyExc_RuntimeError && *pp_PyExc_RuntimeError) p_PyErr_SetString(*pp_PyExc_RuntimeError, "null array buffer in step_frame");
        return NULL;
    }

    if (!g_cs_inited) init_thread_pool();
    if (!g_cs_inited) {
        for (int i = 0; i < count; ++i) {
            process_one_vehicle(i);
        }
        fast_memcpy(out_ptr, g_out_buf, (size_t)(count * OUT_STRIDE) * sizeof(float));
        return return_borrowed_arg(out_obj);
    }

    p_EnterCriticalSection(&g_cs);
    if (g_workers_busy) {
        p_WaitForSingleObject(g_done_event, INFINITE);
        g_workers_busy = 0;
    }
    fast_memcpy(g_in_buf, in_ptr, (size_t)(count * IN_STRIDE) * sizeof(float));
    g_veh_count = count;
    g_next_veh_idx = 0;
    g_worker_mode = 0;

    if (g_num_threads > 1 && count >= 4) {
        p_ResetEvent(g_done_event);
        g_active_workers = g_num_threads;
        for (int t = 0; t < g_num_threads; ++t) {
            p_SetEvent(g_start_events[t]);
        }
        p_WaitForSingleObject(g_done_event, INFINITE);
    } else {
        for (int i = 0; i < count; ++i) {
            process_one_vehicle(i);
        }
    }

    fast_memcpy(out_ptr, g_out_buf, (size_t)(count * OUT_STRIDE) * sizeof(float));
    p_LeaveCriticalSection(&g_cs);

    return return_borrowed_arg(out_obj);
}

/*
 * Direct C++ publisher + WGVehiclePhysics2 struct reader:
 *   - Verifies filter->pEntity_ ([filter_this + 0x0C]) != NULL to prevent 0x0090A3EA / 0x0090DEE1 crashes.
 *   - Calls WGVehicleFilter2::input (0x0090A350) + WGVehicleFilter2::output (0x0090DDE0).
 *   - Reads speed (0x008E61A0), rspeed (0x008E61E0), and isFrozen (+0x692) directly from WGVehiclePhysics2
 *     C++ struct offsets, eliminating 90+ C++ std::string heap allocations + std::map lookups per frame!
 */
static PyObject * CDECL py_fast_publish_root(PyObject *self, PyObject *args) {
    PyObject *filter_obj  = NULL;
    PyObject *physics_obj = NULL;
    double timestamp      = 0.0;
    int space_id          = 0;
    PyObject *out_obj     = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "OOdiO", &filter_obj, &physics_obj, &timestamp, &space_id, &out_obj)) {
        return NULL;
    }

    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (out_ptr && out_len > 0) out_ptr[0] = 0.0f;

    if (!g_is_exact_082 || !g_wot_base || !filter_obj || !physics_obj || !out_ptr || out_len < 7) {
        return return_borrowed_arg(out_obj);
    }

    uint8_t *flt_raw  = (uint8_t *)filter_obj;
    uint8_t *phys_raw = (uint8_t *)physics_obj;

    if (*(uint32_t *)(flt_raw + 4)  != (uint32_t)(g_wot_base + RVA_FILTER_TYPE))  return return_borrowed_arg(out_obj);
    if (*(uint32_t *)(phys_raw + 4) != (uint32_t)(g_wot_base + RVA_PHYSICS_TYPE)) return return_borrowed_arg(out_obj);
    if (*(uint8_t **)(flt_raw + 0x4EC) != phys_raw) return return_borrowed_arg(out_obj);

    uint8_t *filter_this = flt_raw - 4;
    if (*(uint32_t *)filter_this != (uint32_t)(g_wot_base + RVA_FILTER_VTABLE)) return return_borrowed_arg(out_obj);

    /* Ensure filter->pEntity_ ([filter_this + 0x0C]) is attached and valid */
    if (*(uintptr_t *)(filter_this + 0x0C) < 0x00010000u) return return_borrowed_arg(out_obj);

    int skip_if_frozen = 0;
    int skip_filter_io = 0;
    if (space_id < 0) {
        skip_if_frozen = 1;
        space_id = -space_id;
        if (space_id > 100000) {
            skip_filter_io = 1;
            space_id -= 100000;
        }
    }
    uint8_t is_frozen_early = *(const uint8_t *)(phys_raw + 0x692);
    if (skip_if_frozen && is_frozen_early != 0) {
        out_ptr[0] = 2.0f;
        return return_borrowed_arg(out_obj);
    }

    double last_ts = *(double *)(flt_raw + 0x29C);
    if (!(timestamp > last_ts)) return return_borrowed_arg(out_obj);

    const void *mat_this = (const void *)(phys_raw + 0x718);
    float pos[3];
    pos[0] = *(const float *)(phys_raw + 0x748);
    pos[1] = *(const float *)(phys_raw + 0x74C);
    pos[2] = *(const float *)(phys_raw + 0x750);

    if (fast_fabsf(pos[0]) > 12000.0f || fast_fabsf(pos[1]) > 12000.0f || fast_fabsf(pos[2]) > 12000.0f) {
        return return_borrowed_arg(out_obj);
    }

    float dir[3];
    dir[0] = p_Matrix_yaw(mat_this);
    dir[1] = p_Matrix_pitch(mat_this);
    dir[2] = p_Matrix_roll(mat_this);

    if (dir[0] != dir[0] || dir[1] != dir[1] || dir[2] != dir[2]) {
        return return_borrowed_arg(out_obj);
    }

    if (!skip_filter_io) {
        float err_vec[3] = {0.0f, 0.0f, 0.0f};
        p_Filter_input((void *)filter_this, timestamp, space_id, 0, pos, err_vec, dir);
        p_Filter_output((void *)filter_this, timestamp);
    }

    /* Direct C++ struct extraction of speed (0x008E61A0), rspeed (0x008E61E0), and isFrozen (+0x692)
     * WGVehiclePhysics2 single-inherits PyObjectPlus, so C++ `this` is `phys_raw` (+0x00 vtable)! */
    const uint8_t *phys_this = phys_raw;
    const float *fwd_vec = (const float *)(phys_this + 0x38C);
    const float *lin_vel = (const float *)(phys_this + 0x3B8);
    float speed = fwd_vec[0] * lin_vel[0] + fwd_vec[1] * lin_vel[1] + fwd_vec[2] * lin_vel[2];

    const float *up_vec  = (const float *)(phys_this + 0x380);
    const float *ang_vel = (const float *)(phys_this + 0x3A8);
    float rspeed = up_vec[0] * ang_vel[0] + up_vec[1] * ang_vel[1] + up_vec[2] * ang_vel[2];

    uint8_t is_frozen = *(const uint8_t *)(phys_this + 0x692);

    float f_spd  = (speed == speed) ? speed : 0.0f;
    float f_rspd = (rspeed == rspeed) ? rspeed : 0.0f;
    float clamped_flag = 0.0f;

    /* Native 1:1 WGVehiclePhysics2 attitude: pass true solved (yaw, pitch, roll) directly without artificial rate/roll clamping */

    out_ptr[0] = 1.0f;
    out_ptr[1] = pos[0];
    out_ptr[2] = pos[1];
    out_ptr[3] = pos[2];
    out_ptr[4] = dir[0];
    out_ptr[5] = dir[1];
    out_ptr[6] = dir[2];
    if (out_len >= 9) {
        out_ptr[7] = f_spd;
        out_ptr[8] = f_rspd;
    }
    if (out_len >= 10) {
        out_ptr[9] = (float)is_frozen;
    }
    if (out_len >= 14) {
        out_ptr[10] = clamped_flag;
    }
    return return_borrowed_arg(out_obj);
}

static PyObject * CDECL py_fast_seed_and_output(PyObject *self, PyObject *args) {
    PyObject *filter_obj = NULL;
    double timestamp     = 0.0;
    int space_id         = 0;
    double x = 0.0, y = 0.0, z = 0.0;
    double yaw = 0.0, pitch = 0.0, roll = 0.0;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "Odidddddd", &filter_obj, &timestamp, &space_id, &x, &y, &z, &yaw, &pitch, &roll)) {
        return NULL;
    }
    if (!g_is_exact_082 || !g_wot_base || !filter_obj) {
        return return_borrowed_arg(filter_obj);
    }
    uint8_t *flt_raw = (uint8_t *)filter_obj;
    if (*(uint32_t *)(flt_raw + 4) != (uint32_t)(g_wot_base + RVA_FILTER_TYPE)) {
        return return_borrowed_arg(filter_obj);
    }
    uint8_t *filter_this = flt_raw - 4;
    if (*(uint32_t *)filter_this != (uint32_t)(g_wot_base + RVA_FILTER_VTABLE)) {
        return return_borrowed_arg(filter_obj);
    }
    if (*(uintptr_t *)(filter_this + 0x0C) < 0x00010000u) {
        return return_borrowed_arg(filter_obj);
    }
    float pos[3] = {(float)x, (float)y, (float)z};
    float err_vec[3] = {0.0f, 0.0f, 0.0f};
    float dir[3] = {(float)yaw, (float)pitch, (float)roll};
    p_Filter_input((void *)filter_this, timestamp, space_id, 0, pos, err_vec, dir);
    double last_ts = *(double *)(flt_raw + 0x29C);
    if (timestamp > last_ts) {
        p_Filter_output((void *)filter_this, timestamp);
    }
    return return_borrowed_arg(filter_obj);
}

/*
 * Direct C++ BigWorld ClientSpace::collide (0x0092D4B0) raycaster:
 *   native_collide_segment(space_id, sx, sy, sz, ex, ey, ez, skip_flags, max_matkind, out_10floats)
 *
 * Marches through SPT_MATKIND.LEAVES (matKind == 72) and materials > max_matkind directly in C++
 * via offh_collide_cb_operator with ZERO Python tuple allocations and ZERO Math.Vector3 objects!
 * Writes to out_10floats:
 *   [0] = 1.0f if hit else 0.0f (2.0f if hook unavailable so caller falls back)
 *   [1] = hit_x, [2] = hit_y, [3] = hit_z
 *   [4] = norm_x, [5] = norm_y, [6] = norm_z
 *   [7] = (float)mat_kind
 *   [8] = (float)coll_flags  (coll_flags & 8 != 0 means solid terrain!)
 *   [9] = hit_dist
 */
static PyObject * CDECL py_native_collide_segment(PyObject *self, PyObject *args) {
    int space_id       = 0;
    double sx = 0.0, sy = 0.0, sz = 0.0;
    double ex = 0.0, ey = 0.0, ez = 0.0;
    int skip_flags     = 128;
    int max_matkind    = 0;
    PyObject *out_obj  = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(
            args, "iddddddiiO",
            &space_id, &sx, &sy, &sz, &ex, &ey, &ez,
            &skip_flags, &max_matkind, &out_obj)) {
        return NULL;
    }

    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (!out_ptr || out_len < 10) {
        return return_borrowed_arg(out_obj);
    }
    if (!g_collide_hook_ready || !p_BW_SpaceCollide || !p_WGCollideCallback_op || space_id <= 0) {
        out_ptr[0] = 2.0f; /* Signal caller to use Python fallback */
        return return_borrowed_arg(out_obj);
    }

    float start_xyz[3] = {(float)sx, (float)sy, (float)sz};
    float end_xyz[3]   = {(float)ex, (float)ey, (float)ez};
    float dx = end_xyz[0] - start_xyz[0];
    float dy = end_xyz[1] - start_xyz[1];
    float dz = end_xyz[2] - start_xyz[2];
    float seg_len = fast_sqrtf(dx * dx + dy * dy + dz * dz);
    if (seg_len <= 0.05f) {
        out_ptr[0] = 0.0f;
        return return_borrowed_arg(out_obj);
    }

    uint8_t cb_buf[0x60];
    fast_memset_zero(cb_buf, sizeof(cb_buf));
    *(void **)(cb_buf + 0x00)     = (void *)g_custom_collide_vtable;
    *(uint16_t *)(cb_buf + 0x04)  = (uint16_t)(skip_flags & 0xFFFF);
    *(void **)(cb_buf + 0x08)     = NULL;        /* callback = NULL (pure C++ execution!) */
    cb_buf[0x0C]                  = 0;
    *(uint32_t *)(cb_buf + 0x10)  = 0x7F7FFFFFu; /* +FLT_MAX */
    cb_buf[0x20]                  = 0;           /* checkWater = 0 */
    cb_buf[0x21]                  = 0;
    *(uint32_t *)(cb_buf + 0x24)  = 0x7F7FFFFFu; /* +FLT_MAX */
    cb_buf[0x4C]                  = 1;           /* hasNoHit = 1 */
    *(int *)(cb_buf + 0x50)       = max_matkind;

    /* 0x0092D4B0(space_id, start_xyz, end_xyz, &cb_buf) — release GIL during C++ BSP tree traversal */
    Py_BEGIN_ALLOW_THREADS
    p_BW_SpaceCollide(space_id, start_xyz, end_xyz, (void *)cb_buf);
    Py_END_ALLOW_THREADS

    if (cb_buf[0x4C] != 0) {
        out_ptr[0] = 0.0f; /* Ray is 100% clear! */
        return return_borrowed_arg(out_obj);
    }

    float hit_dist = *(const float *)(cb_buf + 0x24);
    float inv_len  = 1.0f / seg_len;
    float hx = start_xyz[0] + dx * inv_len * hit_dist;
    float hy = start_xyz[1] + dy * inv_len * hit_dist;
    float hz = start_xyz[2] + dz * inv_len * hit_dist;
    float nx = *(const float *)(cb_buf + 0x28);
    float ny = *(const float *)(cb_buf + 0x2C);
    float nz = *(const float *)(cb_buf + 0x30);
    uint32_t mat_kind = *(const uint32_t *)(cb_buf + 0x34);
    const uint8_t *hit_tri = *(const uint8_t **)(cb_buf + 0x48);
    uint32_t coll_flags = ((uintptr_t)hit_tri >= 0x00010000u) ? (uint32_t)hit_tri[0x24] : 0u;

    out_ptr[0] = 1.0f;
    out_ptr[1] = hx;
    out_ptr[2] = hy;
    out_ptr[3] = hz;
    out_ptr[4] = nx;
    out_ptr[5] = ny;
    out_ptr[6] = nz;
    out_ptr[7] = (float)mat_kind;
    out_ptr[8] = (float)coll_flags;
    out_ptr[9] = hit_dist;
    return return_borrowed_arg(out_obj);
}


/*
 * Real 3D C++ Eyes for bots (outside of navigation & local combat):
 *   native_eyes_scan(space_id, x, y, z, yaw, lookahead, half_width, out_10floats)
 *
 * Performs a complete 3D corridor & terrain profile check in ONE C call (~0.012 ms, zero Python objects):
 *   1. Probes 3D ground height at mid-point (0.5 * lookahead) and endpoint (lookahead), plus left/right tracks.
 *   2. Checks steep grade / cliff drop and overhangs.
 *   3. Casts 3 terrain-following hull-height rays (center, left track, right track at y + 1.05m) via
 *      ClientSpace::collide (0x0092D4B0), skipping leaves (matKind 72) and fragile props (matKind >= 100).
 *   4. Writes to out_10floats:
 *      [0] = 1.0f if corridor is 100% clear, 0.0f if blocked (2.0f if hook unavailable)
 *      [1] = end_ground_y
 *      [2] = mid_ground_y
 *      [3] = min_obstacle_dist (or lookahead if clear)
 *      [4] = obstacle_matkind (0 if clear, -1 if terrain/cliff reject)
 *      [5] = left_track_y
 *      [6] = right_track_y
 *      [7] = max_slope_ratio
 */
static int c_ray_hit(int space_id, float sx, float sy, float sz, float ex, float ey, float ez,
                     int skip_flags, int max_matkind, float *out_dist, float *out_hy, uint32_t *out_mat) {
    float dx = ex - sx, dy = ey - sy, dz = ez - sz;
    float seg_len = fast_sqrtf(dx * dx + dy * dy + dz * dz);
    if (seg_len <= 0.05f) return 0;
    uint8_t cb_buf[0x60];
    fast_memset_zero(cb_buf, sizeof(cb_buf));
    *(void **)(cb_buf + 0x00)     = (void *)g_custom_collide_vtable;
    *(uint16_t *)(cb_buf + 0x04)  = (uint16_t)(skip_flags & 0xFFFF);
    *(void **)(cb_buf + 0x08)     = NULL;
    cb_buf[0x0C]                  = 0;
    *(uint32_t *)(cb_buf + 0x10)  = 0x7F7FFFFFu;
    cb_buf[0x20]                  = 0;
    cb_buf[0x21]                  = 0;
    *(uint32_t *)(cb_buf + 0x24)  = 0x7F7FFFFFu;
    cb_buf[0x4C]                  = 1;
    *(int *)(cb_buf + 0x50)       = max_matkind;
    float s_xyz[3] = {sx, sy, sz};
    float e_xyz[3] = {ex, ey, ez};
    Py_BEGIN_ALLOW_THREADS
    p_BW_SpaceCollide(space_id, s_xyz, e_xyz, (void *)cb_buf);
    Py_END_ALLOW_THREADS
    if (cb_buf[0x4C] != 0) return 0;
    float hdist = *(const float *)(cb_buf + 0x24);
    if (out_dist) *out_dist = hdist;
    if (out_hy)   *out_hy   = sy + (dy / seg_len) * hdist;
    if (out_mat)  *out_mat  = *(const uint32_t *)(cb_buf + 0x34);
    return 1;
}

static PyObject * CDECL py_native_eyes_scan(PyObject *self, PyObject *args) {
    int space_id = 0;
    double x = 0.0, y = 0.0, z = 0.0;
    double yaw = 0.0, lookahead = 9.0, half_width = 1.35;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "iddddddO", &space_id, &x, &y, &z, &yaw, &lookahead, &half_width, &out_obj)) {
        return NULL;
    }
    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (!out_ptr || out_len < 8) {
        return return_borrowed_arg(out_obj);
    }
    if (!g_collide_hook_ready || !p_BW_SpaceCollide || !p_WGCollideCallback_op || space_id <= 0) {
        out_ptr[0] = 2.0f;
        return return_borrowed_arg(out_obj);
    }

    float fx = (float)x, fy = (float)y, fz = (float)z;
    float fyaw = (float)yaw;
    float flook = (float)lookahead;
    float fhw = (float)half_width;
    float s = fast_sinf(fyaw), c = fast_cosf(fyaw);
    float lx = c, lz = -s;

    /* 1. Midpoint ground probe (max_matkind = -1 skips destructible fences/roofs so ground probe only hits ground) */
    float mid_d = flook * 0.5f;
    float mx = fx + s * mid_d, mz = fz + c * mid_d;
    float mid_y = fy;
    if (!c_ray_hit(space_id, mx, fy + 4.8f, mz, mx, fy - 7.5f, mz, 128, -1, NULL, &mid_y, NULL)) {
        out_ptr[0] = 0.0f; out_ptr[4] = -1.0f;
        return return_borrowed_arg(out_obj);
    }
    float dy1 = mid_y - fy;
    if (dy1 > mid_d * 0.58f || dy1 < -(mid_d * 0.48f + 0.85f)) {
        out_ptr[0] = 0.0f; out_ptr[4] = -1.0f;
        return return_borrowed_arg(out_obj);
    }

    /* 2. Endpoint ground probe */
    float ex = fx + s * flook, ez = fz + c * flook;
    float end_y = mid_y;
    if (!c_ray_hit(space_id, ex, mid_y + 4.8f, ez, ex, mid_y - 7.5f, ez, 128, -1, NULL, &end_y, NULL)) {
        out_ptr[0] = 0.0f; out_ptr[4] = -1.0f;
        return return_borrowed_arg(out_obj);
    }
    float dy2 = end_y - mid_y;
    if (dy2 > mid_d * 0.58f || dy2 < -(mid_d * 0.48f + 0.85f)) {
        out_ptr[0] = 0.0f; out_ptr[4] = -1.0f;
        return return_borrowed_arg(out_obj);
    }

    /* 3. Left & Right track ground probes at endpoint */
    float l_y = end_y, r_y = end_y;
    if (!c_ray_hit(space_id, ex - lx * fhw, end_y + 5.0f, ez - lz * fhw, ex - lx * fhw, end_y - 9.0f, ez - lz * fhw, 128, -1, NULL, &l_y, NULL) ||
        !c_ray_hit(space_id, ex + lx * fhw, end_y + 5.0f, ez + lz * fhw, ex + lx * fhw, end_y - 9.0f, ez + lz * fhw, 128, -1, NULL, &r_y, NULL)) {
        out_ptr[0] = 0.0f; out_ptr[4] = -1.0f;
        return return_borrowed_arg(out_obj);
    }

    /* 4. 3D Obstacle Sweep along terrain-following pieces (center, left track, right track at +0.62m and +1.15m)
     *    skip_flags = 136 (128 | 8) on the low +0.62m ray so terrain micro-lips do not false-trigger while
     *    catching low steel train frames / foundations, and 128 on the +1.15m hull ray! */
    float offsets[3] = {0.0f, -fhw, fhw};
    float heights[2] = {1.15f, 0.62f};
    int   sflags[2]  = {128, 136};
    float min_hit_d = flook;
    uint32_t hit_mat = 0;
    for (int h_i = 0; h_i < 2; ++h_i) {
        float h_off = heights[h_i];
        int sf = sflags[h_i];
        for (int i = 0; i < 3; ++i) {
            float off = offsets[i];
            float ox = lx * off, oz = lz * off;
            float h_d = 0.0f;
            uint32_t h_m = 0;
            /* Piece 1: start -> mid */
            if (c_ray_hit(space_id, fx + ox, fy + h_off, fz + oz, mx + ox, mid_y + h_off, mz + oz, sf, 118, &h_d, NULL, &h_m)) {
                out_ptr[0] = 0.0f;
                out_ptr[1] = end_y; out_ptr[2] = mid_y;
                out_ptr[3] = h_d;   out_ptr[4] = (float)h_m;
                return return_borrowed_arg(out_obj);
            }
            /* Piece 2: mid -> end */
            if (c_ray_hit(space_id, mx + ox, mid_y + h_off, mz + oz, ex + ox, end_y + h_off, ez + oz, sf, 118, &h_d, NULL, &h_m)) {
                out_ptr[0] = 0.0f;
                out_ptr[1] = end_y; out_ptr[2] = mid_y;
                out_ptr[3] = mid_d + h_d; out_ptr[4] = (float)h_m;
                return return_borrowed_arg(out_obj);
            }
        }
    }

    out_ptr[0] = 1.0f;
    out_ptr[1] = end_y;
    out_ptr[2] = mid_y;
    out_ptr[3] = min_hit_d;
    out_ptr[4] = (float)hit_mat;
    out_ptr[5] = l_y;
    out_ptr[6] = r_y;
    out_ptr[7] = (fast_fabsf(dy1) > fast_fabsf(dy2) ? fast_fabsf(dy1) : fast_fabsf(dy2)) / (mid_d > 0.1f ? mid_d : 1.0f);
    return return_borrowed_arg(out_obj);
}


/*
 * C++ Parabolic Ballistic Trajectory Raycaster (v7.9):
 *   native_ballistic_impact(space_id, sx, sy, sz, vx, vy, vz, gy, sub_dt, max_t, max_dist, out_10floats)
 *
 * Marches the shell parabola P(t) = S + V*t + 0.5*G*t^2 chord-by-chord directly in C++ via
 * ClientSpace::collide (0x0092D4B0) with g_custom_collide_vtable (skipping leaves matKind 72
 * and fragile props >= 119) with GIL released and zero Python allocations!
 * Writes to out_10floats:
 *   [0] = 1.0f if hit world geometry, 0.0f if clear to max_dist (2.0f if hook unavailable)
 *   [1] = impact_x, [2] = impact_y, [3] = impact_z
 *   [4] = dir_x,    [5] = dir_y,    [6] = dir_z
 *   [7] = total_travelled_distance
 *   [8] = impact_flight_time
 *   [9] = (float)hit_matkind
 */
static PyObject * CDECL py_native_ballistic_impact(PyObject *self, PyObject *args) {
    int space_id = 0;
    double sx = 0.0, sy = 0.0, sz = 0.0;
    double vx = 0.0, vy = 0.0, vz = 0.0;
    double gy = -9.81, sub_dt = 0.12, max_t = 6.0, max_dist = 720.0;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(
            args, "iddddddddddO",
            &space_id, &sx, &sy, &sz, &vx, &vy, &vz,
            &gy, &sub_dt, &max_t, &max_dist, &out_obj)) {
        return NULL;
    }
    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (!out_ptr || out_len < 10) {
        return return_borrowed_arg(out_obj);
    }
    if (!g_collide_hook_ready || !p_BW_SpaceCollide || !p_WGCollideCallback_op || space_id <= 0) {
        out_ptr[0] = 2.0f;
        return return_borrowed_arg(out_obj);
    }

    float fsx = (float)sx, fsy = (float)sy, fsz = (float)sz;
    float fvx = (float)vx, fvy = (float)vy, fvz = (float)vz;
    float fgy = (float)gy;
    float fdt = (float)sub_dt;
    if (fdt < 0.02f) fdt = 0.02f;
    float fmax_t = (float)max_t;
    if (fmax_t < 0.05f) fmax_t = 0.05f;
    if (fmax_t > 20.0f) fmax_t = 20.0f;
    float fmax_d = (float)max_dist;
    if (fmax_d < 1.0f) fmax_d = 720.0f;

    float travelled = 0.0f;
    float cur_t = 0.0f;
    float p0[3] = {fsx, fsy, fsz};
    float last_dir[3] = {fvx, fvy, fvz};
    float vlen = fast_sqrtf(fvx * fvx + fvy * fvy + fvz * fvz);
    if (vlen > 0.0001f) {
        last_dir[0] /= vlen; last_dir[1] /= vlen; last_dir[2] /= vlen;
    } else {
        last_dir[0] = 0.0f; last_dir[1] = 0.0f; last_dir[2] = 1.0f;
    }

    int hit_found = 0;
    float hit_pt[3] = {fsx, fsy, fsz};
    float hit_time = fmax_t;
    uint32_t hit_mk = 0;

    Py_BEGIN_ALLOW_THREADS
    while (cur_t < fmax_t - 0.0001f && travelled < fmax_d) {
        float next_t = cur_t + fdt;
        if (next_t > fmax_t) next_t = fmax_t;
        float p1[3];
        p1[0] = fsx + fvx * next_t;
        p1[1] = fsy + fvy * next_t + 0.5f * fgy * next_t * next_t;
        p1[2] = fsz + fvz * next_t;

        float dx = p1[0] - p0[0];
        float dy = p1[1] - p0[1];
        float dz = p1[2] - p0[2];
        float seg_len = fast_sqrtf(dx * dx + dy * dy + dz * dz);
        if (seg_len > 0.001f) {
            float inv_l = 1.0f / seg_len;
            last_dir[0] = dx * inv_l;
            last_dir[1] = dy * inv_l;
            last_dir[2] = dz * inv_l;

            float h_d = 0.0f;
            uint32_t h_m = 0;
            /* Use max_matkind = 118 so leaves (72) and fragile wooden fences (>=119) are skipped in C++ */
            if (c_ray_hit(space_id, p0[0], p0[1], p0[2], p1[0], p1[1], p1[2], 128, 118, &h_d, NULL, &h_m)) {
                hit_found = 1;
                hit_pt[0] = p0[0] + last_dir[0] * h_d;
                hit_pt[1] = p0[1] + last_dir[1] * h_d;
                hit_pt[2] = p0[2] + last_dir[2] * h_d;
                travelled += h_d;
                hit_time = cur_t + (next_t - cur_t) * (h_d * inv_l);
                hit_mk = h_m;
                break;
            }
            travelled += seg_len;
        }
        p0[0] = p1[0]; p0[1] = p1[1]; p0[2] = p1[2];
        cur_t = next_t;
    }
    Py_END_ALLOW_THREADS

    if (!hit_found) {
        float off_x = p0[0] - fsx;
        float off_y = p0[1] - fsy;
        float off_z = p0[2] - fsz;
        float no_hit_d = fast_sqrtf(off_x * off_x + off_y * off_y + off_z * off_z);
        if (no_hit_d > fmax_d && no_hit_d > 0.001f) {
            float inv_d = 1.0f / no_hit_d;
            last_dir[0] = off_x * inv_d;
            last_dir[1] = off_y * inv_d;
            last_dir[2] = off_z * inv_d;
            p0[0] = fsx + last_dir[0] * fmax_d;
            p0[1] = fsy + last_dir[1] * fmax_d;
            p0[2] = fsz + last_dir[2] * fmax_d;
            no_hit_d = fmax_d;
        }
        out_ptr[0] = 0.0f;
        out_ptr[1] = p0[0];
        out_ptr[2] = p0[1];
        out_ptr[3] = p0[2];
        out_ptr[4] = last_dir[0];
        out_ptr[5] = last_dir[1];
        out_ptr[6] = last_dir[2];
        out_ptr[7] = no_hit_d;
        out_ptr[8] = cur_t;
        out_ptr[9] = 0.0f;
        return return_borrowed_arg(out_obj);
    }

    out_ptr[0] = 1.0f;
    out_ptr[1] = hit_pt[0];
    out_ptr[2] = hit_pt[1];
    out_ptr[3] = hit_pt[2];
    out_ptr[4] = last_dir[0];
    out_ptr[5] = last_dir[1];
    out_ptr[6] = last_dir[2];
    out_ptr[7] = travelled;
    out_ptr[8] = hit_time;
    out_ptr[9] = (float)hit_mk;
    return return_borrowed_arg(out_obj);
}

static HWND g_cached_hwnd = NULL;

static BOOL WINAPI find_wot_hwnd_cb(HWND hwnd, intptr_t lparam) {
    DWORD target_pid = (DWORD)lparam;
    DWORD win_pid = 0;
    if (p_GetWindowThreadProcessId) {
        p_GetWindowThreadProcessId(hwnd, &win_pid);
        if (win_pid == target_pid) {
            if (!p_IsWindowVisible || p_IsWindowVisible(hwnd)) {
                g_cached_hwnd = hwnd;
                return 0;
            }
        }
    }
    return 1;
}

static HWND get_wot_hwnd(void) {
    resolve_win32_apis();
    if (p_GetCurrentProcessId && p_EnumWindows) {
        DWORD pid = p_GetCurrentProcessId();
        g_cached_hwnd = NULL;
        p_EnumWindows(find_wot_hwnd_cb, (intptr_t)pid);
        if (g_cached_hwnd) return g_cached_hwnd;
    }
    if (p_FindWindowA) {
        HWND h = p_FindWindowA("World of Tanks", NULL);
        if (!h) h = p_FindWindowA(NULL, "W.o.T. Client");
        if (!h) h = p_FindWindowA(NULL, "World of Tanks");
        if (h) return h;
    }
    if (p_GetActiveWindow) {
        HWND h = p_GetActiveWindow();
        if (h) return h;
    }
    if (p_GetForegroundWindow) {
        return p_GetForegroundWindow();
    }
    return NULL;
}

static void do_fix_window_mode(int windowed, int req_w, int req_h, float *out_ptr) {
    HWND hwnd = get_wot_hwnd();
    if (!hwnd) {
        return;
    }

    if (p_ClipCursor) p_ClipCursor(NULL);
    if (p_ReleaseCapture) p_ReleaseCapture();

    if (windowed) {
        if (p_ChangeDisplaySettingsA) {
            p_ChangeDisplaySettingsA(NULL, 0);
        }

        LONG_VAL style   = p_GetWindowLongA ? p_GetWindowLongA(hwnd, -16) : 0;
        LONG_VAL exstyle = p_GetWindowLongA ? p_GetWindowLongA(hwnd, -20) : 0;

        LONG_VAL new_style   = (style & ~((LONG_VAL)0x80000000)) | (LONG_VAL)0x16CF0000;
        LONG_VAL new_exstyle = (exstyle & ~((LONG_VAL)0x00000008)) | (LONG_VAL)0x00040100;

        if (p_SetWindowLongA) {
            p_SetWindowLongA(hwnd, -16, new_style);
            p_SetWindowLongA(hwnd, -20, new_exstyle);
        }

        RECT work = {0, 0, 1920, 1040};
        if (p_SystemParametersInfoA) {
            p_SystemParametersInfoA(0x0030u, 0, &work, 0);
        }
        int work_w = work.right - work.left;
        int work_h = work.bottom - work.top;
        if (work_w < 640) work_w = 1920;
        if (work_h < 480) work_h = 1040;

        RECT client_rc = {0, 0, 1280, 720};
        if (req_w >= 640 && req_h >= 480) {
            client_rc.right  = req_w;
            client_rc.bottom = req_h;
        } else if (p_GetClientRect) {
            p_GetClientRect(hwnd, &client_rc);
            if (client_rc.right - client_rc.left < 640) client_rc.right = 1280;
            if (client_rc.bottom - client_rc.top < 480) client_rc.bottom = 720;
        }

        int cw = client_rc.right - client_rc.left;
        int ch = client_rc.bottom - client_rc.top;

        if (cw > work_w - 16) cw = work_w - 16;
        if (ch > work_h - 42) ch = work_h - 42;

        RECT adj = {0, 0, cw, ch};
        if (p_AdjustWindowRectEx) {
            p_AdjustWindowRectEx(&adj, (DWORD)new_style, 0, (DWORD)new_exstyle);
        }
        int outer_w = adj.right - adj.left;
        int outer_h = adj.bottom - adj.top;
        if (outer_w > work_w) outer_w = work_w;
        if (outer_h > work_h) outer_h = work_h;

        int win_x = work.left + (work_w - outer_w) / 2;
        int win_y = work.top  + (work_h - outer_h) / 2;
        if (win_x < work.left) win_x = work.left;
        if (win_y < work.top)  win_y = work.top;

        if (p_SetWindowPos) {
            p_SetWindowPos(hwnd, (HWND)(intptr_t)-2, win_x, win_y, outer_w, outer_h, 0x0060u);
        }
        if (p_DrawMenuBar) p_DrawMenuBar(hwnd);
        if (p_ShowWindow)  p_ShowWindow(hwnd, 5);
    }

    if (p_SetForegroundWindow) p_SetForegroundWindow(hwnd);
    if (p_SetFocus)            p_SetFocus(hwnd);

    if (out_ptr && p_GetClientRect) {
        RECT final_rc = {0, 0, 0, 0};
        p_GetClientRect(hwnd, &final_rc);
        out_ptr[0] = (float)(final_rc.right - final_rc.left);
        out_ptr[1] = (float)(final_rc.bottom - final_rc.top);
    }
}

static PyObject * CDECL py_fix_window_mode(PyObject *self, PyObject *args) {
    int windowed = 1;
    int req_w    = 0;
    int req_h    = 0;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "iiiO", &windowed, &req_w, &req_h, &out_obj)) {
        return NULL;
    }
    float *out_ptr = (float *)get_array_buffer(out_obj, NULL);
    do_fix_window_mode(windowed, req_w, req_h, out_ptr);
    return return_borrowed_arg(out_obj);
}

static PyObject * CDECL py_ensure_windowed_style(PyObject *self, PyObject *args) {
    int req_w = 0, req_h = 0;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "iiO", &req_w, &req_h, &out_obj)) {
        return NULL;
    }
    float *out_ptr = (float *)get_array_buffer(out_obj, NULL);
    HWND hwnd = get_wot_hwnd();
    if (hwnd && p_GetWindowLongA) {
        LONG_VAL style = p_GetWindowLongA(hwnd, -16);
        if ((style & 0x00C80000) != 0x00C80000 || (style & (LONG_VAL)0x80000000) != 0) {
            do_fix_window_mode(1, req_w, req_h, out_ptr);
        }
    }
    return return_borrowed_arg(out_obj);
}

static PyObject * CDECL py_shutdown_process(PyObject *self, PyObject *args) {
    PyObject *arg_obj = NULL;
    (void)self;
    p_PyArg_ParseTuple(args, "O", &arg_obj);

    g_shutdown = 1;
    if (g_cs_inited && p_SetEvent) {
        for (int i = 0; i < g_num_threads; ++i) {
            if (g_start_events[i]) p_SetEvent(g_start_events[i]);
        }
    }
    resolve_win32_apis();
    if (g_timer_period_active && p_timeEndPeriod) {
        p_timeEndPeriod(1);
        g_timer_period_active = 0;
    }
    if (p_ChangeDisplaySettingsA) {
        p_ChangeDisplaySettingsA(NULL, 0);
    }
    if (p_ClipCursor) {
        p_ClipCursor(NULL);
    }
    if (p_ExitProcess) {
        p_ExitProcess(0);
    }
    return return_borrowed_arg(arg_obj);
}

/*
 * C++ Multi-Point 3D Spotting Contour + Heightfield Mountain LOS Check (v6.2):
 *   native_spot_3d_los(space_id, ox, oy, oz, tx, ty, tz, obs_roof, tgt_roof, tgt_hull, t_yaw, fl, full_contour, out_array)
 *
 * Performs the complete spotting visibility raycast in ONE GIL-free C call:
 *   1. If 2D distance > 165m and heightfield is loaded, checks heightfield crest blockage first.
 *   2. Trims 4.0m (up to 35% of segment length) off both vehicle hulls and tests (obs_roof -> tgt_roof)
 *      and (obs_roof -> tgt_hull) via ClientSpace::collide (0x0092D4B0) with max_matkind = 70
 *      (ignoring leaves=72 and destructibles=71..100 while solid terrain coll_flags & 8 always blocks).
 *   3. When full_contour != 0 and distance <= 260m, tests front and rear hull contour points along t_yaw.
 *   4. Writes to out_array[0]: 1.0f if LOS is clear, 0.0f if blocked, 2.0f if hook unavailable.
 */
static int c_spot_ray_clear_nogil(int space_id, float ax, float ay, float az, float bx, float by, float bz) {
    float dx = bx - ax, dy = by - ay, dz = bz - az;
    float seg_len = fast_sqrtf(dx * dx + dy * dy + dz * dz);
    if (seg_len <= 0.1f) return 1;
    float trim = 4.0f;
    if (trim > seg_len * 0.35f) trim = seg_len * 0.35f;
    if (seg_len > trim * 2.0f + 0.5f) {
        float inv_l = 1.0f / seg_len;
        ax += dx * inv_l * trim;
        ay += dy * inv_l * trim;
        az += dz * inv_l * trim;
        bx -= dx * inv_l * trim;
        by -= dy * inv_l * trim;
        bz -= dz * inv_l * trim;
    }
    uint8_t cb_buf[0x60];
    fast_memset_zero(cb_buf, sizeof(cb_buf));
    *(void **)(cb_buf + 0x00)     = (void *)g_custom_collide_vtable;
    *(uint16_t *)(cb_buf + 0x04)  = 128u;
    *(void **)(cb_buf + 0x08)     = NULL;
    cb_buf[0x0C]                  = 0;
    *(uint32_t *)(cb_buf + 0x10)  = 0x7F7FFFFFu;
    cb_buf[0x20]                  = 0;
    cb_buf[0x21]                  = 0;
    *(uint32_t *)(cb_buf + 0x24)  = 0x7F7FFFFFu;
    cb_buf[0x4C]                  = 1;
    *(int *)(cb_buf + 0x50)       = 70; /* skip destructibles 71..100; solid terrain & buildings block */
    float s_xyz[3] = {ax, ay, az};
    float e_xyz[3] = {bx, by, bz};
    p_BW_SpaceCollide(space_id, s_xyz, e_xyz, (void *)cb_buf);
    return (cb_buf[0x4C] != 0) ? 1 : 0;
}

static PyObject * CDECL py_native_spot_3d_los(PyObject *self, PyObject *args) {
    int space_id = 0;
    double ox = 0.0, oy = 0.0, oz = 0.0;
    double tx = 0.0, ty = 0.0, tz = 0.0;
    double obs_roof = 2.15, tgt_roof = 2.15, tgt_hull = 1.45;
    double t_yaw = 0.0, fl = 2.5;
    int full_contour = 1;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(
            args, "idddddddddddiO",
            &space_id, &ox, &oy, &oz, &tx, &ty, &tz,
            &obs_roof, &tgt_roof, &tgt_hull, &t_yaw, &fl, &full_contour, &out_obj)) {
        return NULL;
    }
    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (!out_ptr || out_len < 1) {
        return return_borrowed_arg(out_obj);
    }
    if (!g_collide_hook_ready || !p_BW_SpaceCollide || !p_WGCollideCallback_op || space_id <= 0) {
        out_ptr[0] = 2.0f;
        return return_borrowed_arg(out_obj);
    }

    float fox = (float)ox, foy = (float)oy, foz = (float)oz;
    float ftx = (float)tx, fty = (float)ty, ftz = (float)tz;
    float fo_roof = (float)obs_roof;
    float ft_roof = (float)tgt_roof;
    float ft_hull = (float)tgt_hull;
    float ftyaw   = (float)t_yaw;
    float ffl     = (float)fl;

    int is_clear = 0;
    Py_BEGIN_ALLOW_THREADS
    float dx = ftx - fox;
    float dz = ftz - foz;
    float d2 = dx * dx + dz * dz;
    int mountain_blocked = 0;
    if (d2 > 165.0f * 165.0f && g_heights && g_grid_nx >= 2 && g_grid_nz >= 2) {
        if (!raycast_heightfield_clear(fox, foy + fo_roof, foz, ftx, fty + ft_roof, ftz) &&
            !raycast_heightfield_clear(fox, foy + fo_roof, foz, ftx, fty + ft_hull, ftz)) {
            mountain_blocked = 1;
        }
    }
    if (!mountain_blocked) {
        if (c_spot_ray_clear_nogil(space_id, fox, foy + fo_roof, foz, ftx, fty + ft_roof, ftz) ||
            c_spot_ray_clear_nogil(space_id, fox, foy + fo_roof, foz, ftx, fty + ft_hull, ftz)) {
            is_clear = 1;
        } else if (full_contour && d2 <= 260.0f * 260.0f) {
            float sy = fast_sinf(ftyaw) * ffl;
            float cy = fast_cosf(ftyaw) * ffl;
            if (c_spot_ray_clear_nogil(space_id, fox, foy + fo_roof, foz, ftx + sy, fty + ft_hull, ftz + cy) ||
                c_spot_ray_clear_nogil(space_id, fox, foy + fo_roof, foz, ftx - sy, fty + ft_hull, ftz - cy)) {
                is_clear = 1;
            }
        }
    }
    Py_END_ALLOW_THREADS

    out_ptr[0] = is_clear ? 1.0f : 0.0f;
    return return_borrowed_arg(out_obj);
}

/*
 * C++ Player & Bot Ballistic + Tilted-Hull Shot Angle Solver (v6.2):
 *   native_solve_shot_angles(in_18floats, out_8floats)
 *
 * Input buffer (18 floats):
 *   [0..2]   = hull_x, hull_y, hull_z
 *   [3..5]   = hull_yaw, hull_pitch, hull_roll
 *   [6]      = cur_turret_yaw
 *   [7..9]   = turret_off_x, turret_off_y, turret_off_z
 *   [10..12] = gun_off_x, gun_off_y, gun_off_z
 *   [13..15] = shot_x, shot_y, shot_z
 *   [16]     = proj_speed (v)
 *   [17]     = proj_gravity (g > 0)
 *
 * Output buffer (8 floats):
 *   [0]      = 1.0f (valid)
 *   [1..3]   = last_true_gun_pos (gx, gy, gz)
 *   [4]      = local_target_yaw (in [-PI, PI])
 *   [5]      = target_pitch (BigWorld convention: negative = elevation up)
 *   [6]      = horiz_dist
 */
static PyObject * CDECL py_native_solve_shot_angles(PyObject *self, PyObject *args) {
    PyObject *in_obj  = NULL;
    PyObject *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "OO", &in_obj, &out_obj)) {
        return NULL;
    }
    int in_len = 0, out_len = 0;
    const float *in_ptr = (const float *)get_array_buffer(in_obj, &in_len);
    float *out_ptr      = (float *)get_array_buffer(out_obj, &out_len);
    if (!in_ptr || !out_ptr || in_len < 18 || out_len < 7) {
        if (out_ptr && out_len >= 1) out_ptr[0] = 0.0f;
        return return_borrowed_arg(out_obj);
    }

    float hx = in_ptr[0], hy = in_ptr[1], hz = in_ptr[2];
    float hyaw = in_ptr[3], hpitch = in_ptr[4], hroll = in_ptr[5];
    float tyaw = in_ptr[6];
    float tx_off = in_ptr[7], ty_off = in_ptr[8], tz_off = in_ptr[9];
    float gx_off = in_ptr[10], gy_off = in_ptr[11], gz_off = in_ptr[12];
    float sx = in_ptr[13], sy = in_ptr[14], sz = in_ptr[15];
    float v  = in_ptr[16] > 1.0f ? in_ptr[16] : 600.0f;
    float g  = fast_fabsf(in_ptr[17]);
    if (g < 0.1f) g = 9.81f;

    /* Hull basis vectors in BigWorld YPR convention */
    float sy_h = fast_sinf(hyaw),   cy_h = fast_cosf(hyaw);
    float sp_h = fast_sinf(hpitch), cp_h = fast_cosf(hpitch);
    float sr_h = fast_sinf(hroll),  cr_h = fast_cosf(hroll);

    float rx =  cy_h * cr_h + sy_h * sp_h * sr_h;
    float ry =  cp_h * sr_h;
    float rz = -sy_h * cr_h + cy_h * sp_h * sr_h;

    float ux = -cy_h * sr_h + sy_h * sp_h * cr_h;
    float uy =  cp_h * cr_h;
    float uz =  sy_h * sr_h + cy_h * sp_h * cr_h;

    float fx =  sy_h * cp_h;
    float fy = -sp_h;
    float fz =  cy_h * cp_h;

    /* Rotate gun_off by cur_turret_yaw in hull space, then add turret_off */
    float st = fast_sinf(tyaw), ct = fast_cosf(tyaw);
    float loc_gx = tx_off + gx_off * ct + gz_off * st;
    float loc_gy = ty_off + gy_off;
    float loc_gz = tz_off - gx_off * st + gz_off * ct;

    /* Transform to world space: last_true_gun_pos */
    float gun_wx = hx + rx * loc_gx + ux * loc_gy + fx * loc_gz;
    float gun_wy = hy + ry * loc_gx + uy * loc_gy + fy * loc_gz;
    float gun_wz = hz + rz * loc_gx + uz * loc_gy + fz * loc_gz;

    /* World vector from gun joint to target shot_point */
    float dx = sx - gun_wx;
    float dy = sy - gun_wy;
    float dz = sz - gun_wz;
    float d2 = dx * dx + dz * dz;
    float dist = fast_sqrtf(d2);

    /* Ballistic elevation compensation: adjust virtual aim height by gravity drop / parabola */
    float aim_dy = dy;
    if (dist > 0.5f) {
        float v2 = v * v;
        float v4 = v2 * v2;
        float disc = v4 - g * (g * d2 + 2.0f * dy * v2);
        if (disc > 0.0f) {
            float tan_theta = (v2 - fast_sqrtf(disc)) / (g * dist);
            aim_dy = tan_theta * dist;
        }
    }

    /* Project ballistic aim vector (dx, aim_dy, dz) into tilted hull frame */
    float local_tyaw = 0.0f, local_gpitch = 0.0f;
    compute_hull_local_aim(dx, aim_dy, dz, hyaw, hpitch, hroll, &local_tyaw, &local_gpitch);

    out_ptr[0] = 1.0f;
    out_ptr[1] = gun_wx;
    out_ptr[2] = gun_wy;
    out_ptr[3] = gun_wz;
    out_ptr[4] = wrap_pi(local_tyaw);
    out_ptr[5] = local_gpitch;
    out_ptr[6] = dist;
    return return_borrowed_arg(out_obj);
}

/* =========================================================================
 * py_native_step_bot_aim(in_20floats, out_8floats) -> int
 *
 * 60 Hz C-native bot aiming & turret/gun servo with difficulty separation:
 *   diff == 0 (easy):   0.72x slew speed, smooth weave offset (+-0.85m H, +-0.28m V),
 *                       0% ballistic drop compensation, wider lock tolerance (0.065/0.045 rad).
 *   diff == 1 (medium): 1.00x slew speed, subtle wander (+-0.22m H, +-0.08m V),
 *                       65% ballistic parabola compensation, standard lock tolerance (0.035/0.025 rad).
 *   diff == 2 (hard):   1.22x stabilized slew speed, 0.0m jitter (exact weakspot/lead),
 *                       100% exact ballistic parabola + 3D tilted-hull local projection,
 *                       razor-sharp lock tolerance (0.018/0.014 rad).
 * ========================================================================= */
static float g_bot_aim_phase_accum = 0.0f;

static PyObject * CDECL py_native_step_bot_aim(PyObject *self, PyObject *args) {
    PyObject *in_obj = NULL, *out_obj = NULL;
    (void)self;
    if (!p_PyArg_ParseTuple(args, "OO", &in_obj, &out_obj)) return NULL;

    int in_len = 0, out_len = 0;
    const float *in_f = (const float *)get_array_buffer(in_obj, &in_len);
    float *out_f      = (float *)get_array_buffer(out_obj, &out_len);
    if (!in_f || !out_f || in_len < 20 || out_len < 8) {
        if (out_f && out_len >= 8) out_f[7] = 0.0f;
        return return_borrowed_arg(out_obj);
    }

    float hx = in_f[0], hy = in_f[1], hz = in_f[2];
    float hyaw = in_f[3], hpitch = in_f[4], hroll = in_f[5];
    float cur_tyaw = in_f[6], cur_gpitch = in_f[7];
    float tx = in_f[8], ty = in_f[9], tz = in_f[10];
    float tspd = in_f[11], gspd = in_f[12];
    float min_yaw = in_f[13], max_yaw = in_f[14];
    float min_pitch = in_f[15], max_pitch = in_f[16];
    float proj_speed = in_f[17];
    float dt = in_f[18];
    int pflags = (int)(in_f[19] + 0.5f);

    int diff = pflags & 3;
    if (diff < 0) diff = 0;
    if (diff > 2) diff = 2;
    int has_lim = (pflags & 4) != 0;
    int has_combat = (pflags & 8) != 0;
    int is_arty = (pflags & 16) != 0;
    int bot_id = (pflags >> 5) & 2047;

    if (dt <= 0.0001f || dt > 0.25f) dt = 0.0166667f;
    g_bot_aim_phase_accum += dt * 0.022f;
    if (g_bot_aim_phase_accum > 628.3185f) g_bot_aim_phase_accum -= 628.3185f;
    float phase = g_bot_aim_phase_accum + (float)(bot_id & 63) * 0.73f;

    float slew_mult = 1.0f;
    float grav_comp_scale = 1.0f;
    float lock_tol_yaw = 0.035f;
    float lock_tol_pitch = 0.025f;

    float aim_tx = tx, aim_ty = ty, aim_tz = tz;
    float dx0 = aim_tx - hx;
    float dz0 = aim_tz - hz;
    float dist_xz = fast_sqrtf(dx0 * dx0 + dz0 * dz0);

    if (diff == 0) {
        slew_mult = 0.72f;
        grav_comp_scale = 0.0f;
        lock_tol_yaw = 0.065f;
        lock_tol_pitch = 0.045f;
        if (has_combat && dist_xz > 4.0f) {
            float scale_d = clampf(dist_xz * 0.012f, 0.4f, 1.8f);
            float perp_x = -dz0 / dist_xz;
            float perp_z =  dx0 / dist_xz;
            float w_h = fast_sinf(phase * 1.35f) * 0.85f * scale_d;
            float w_v = fast_cosf(phase * 0.95f) * 0.28f * scale_d;
            aim_tx += perp_x * w_h;
            aim_tz += perp_z * w_h;
            aim_ty += w_v;
        }
    } else if (diff == 1) {
        slew_mult = 1.00f;
        grav_comp_scale = 0.65f;
        lock_tol_yaw = 0.035f;
        lock_tol_pitch = 0.025f;
        if (has_combat && dist_xz > 4.0f) {
            float scale_d = clampf(dist_xz * 0.010f, 0.35f, 1.35f);
            float perp_x = -dz0 / dist_xz;
            float perp_z =  dx0 / dist_xz;
            float w_h = fast_sinf(phase * 1.15f) * 0.22f * scale_d;
            float w_v = fast_cosf(phase * 0.85f) * 0.08f * scale_d;
            aim_tx += perp_x * w_h;
            aim_tz += perp_z * w_h;
            aim_ty += w_v;
        }
    } else {
        slew_mult = 1.22f;
        grav_comp_scale = 1.00f;
        lock_tol_yaw = 0.018f;
        lock_tol_pitch = 0.014f;
    }

    float gun_ox = hx;
    float gun_oy = hy + 1.65f;
    float gun_oz = hz;
    float adx = aim_tx - gun_ox;
    float ady = aim_ty - gun_oy;
    float adz = aim_tz - gun_oz;
    float adist_xz = fast_sqrtf(adx * adx + adz * adz);

    if (has_combat && grav_comp_scale > 0.01f && adist_xz > 8.0f) {
        float v = (proj_speed > 50.0f && proj_speed < 3000.0f) ? proj_speed : 720.0f;
        float g = 9.81f;
        float v2 = v * v;
        float disc = v2 * v2 - g * (g * adist_xz * adist_xz + 2.0f * ady * v2);
        float drop_y = 0.5f * g * (adist_xz * adist_xz) / v2;
        if (disc > 0.0f && !is_arty) {
            float tan_theta = (v2 - fast_sqrtf(disc)) / (g * adist_xz);
            float ball_dy = tan_theta * adist_xz;
            if (ball_dy > ady) drop_y = ball_dy - ady;
        }
        ady += clampf(drop_y, 0.0f, 35.0f) * grav_comp_scale;
    }

    float want_tyaw = 0.0f;
    float want_gpitch = 0.0f;
    compute_hull_local_aim(adx, ady, adz, hyaw, hpitch, hroll, &want_tyaw, &want_gpitch);
    want_tyaw = wrap_pi(want_tyaw);

    int yaw_clamped = 0;
    if (has_lim) {
        if (want_tyaw < min_yaw) { want_tyaw = min_yaw; yaw_clamped = 1; }
        else if (want_tyaw > max_yaw) { want_tyaw = max_yaw; yaw_clamped = 1; }
    }
    int pitch_clamped = 0;
    if (min_pitch < max_pitch) {
        if (want_gpitch < min_pitch) { want_gpitch = min_pitch; pitch_clamped = 1; }
        else if (want_gpitch > max_pitch) { want_gpitch = max_pitch; pitch_clamped = 1; }
    }

    float eff_tspd = ((tspd > 0.05f && tspd < 12.0f) ? tspd : 0.78f) * slew_mult;
    float eff_gspd = ((gspd > 0.05f && gspd < 12.0f) ? gspd : (eff_tspd * 0.75f)) * slew_mult;

    float new_tyaw = cur_tyaw;
    float max_dyaw = eff_tspd * dt;
    if (has_lim) {
        float lin_dyaw = want_tyaw - cur_tyaw;
        float ady = fast_fabsf(lin_dyaw);
        if (ady < 0.0018f) new_tyaw = cur_tyaw;
        else if (ady < 0.045f) {
            float fine_factor = clampf(dt * 21.0f, 0.02f, 0.85f);
            float d_step = lin_dyaw * fine_factor;
            if (fast_fabsf(d_step) > max_dyaw) d_step = (lin_dyaw > 0.0f ? max_dyaw : -max_dyaw);
            new_tyaw = cur_tyaw + d_step;
        } else if (ady <= max_dyaw) new_tyaw = want_tyaw;
        else new_tyaw = cur_tyaw + (lin_dyaw > 0.0f ? max_dyaw : -max_dyaw);
        new_tyaw = clampf(new_tyaw, min_yaw, max_yaw);
    } else {
        float dyaw = wrap_pi(want_tyaw - cur_tyaw);
        float ady = fast_fabsf(dyaw);
        if (ady < 0.0018f) new_tyaw = cur_tyaw;
        else if (ady < 0.045f) {
            float fine_factor = clampf(dt * 21.0f, 0.02f, 0.85f);
            float d_step = dyaw * fine_factor;
            if (fast_fabsf(d_step) > max_dyaw) d_step = (dyaw > 0.0f ? max_dyaw : -max_dyaw);
            new_tyaw = wrap_pi(cur_tyaw + d_step);
        } else if (ady <= max_dyaw) new_tyaw = want_tyaw;
        else new_tyaw = wrap_pi(cur_tyaw + (dyaw > 0.0f ? max_dyaw : -max_dyaw));
    }

    float new_gpitch = cur_gpitch;
    float dpitch = want_gpitch - cur_gpitch;
    float adp = fast_fabsf(dpitch);
    float max_dpitch = eff_gspd * dt;
    if (adp < 0.0010f) new_gpitch = cur_gpitch;
    else if (adp <= max_dpitch) {
        float fine_factor = clampf(dt * 24.0f, 0.02f, 0.85f);
        new_gpitch = cur_gpitch + dpitch * fine_factor;
    } else {
        new_gpitch = cur_gpitch + (dpitch > 0.0f ? max_dpitch : -max_dpitch);
    }
    if (min_pitch < max_pitch) {
        new_gpitch = clampf(new_gpitch, min_pitch, max_pitch);
    }

    float rem_yaw = fast_fabsf(wrap_pi(want_tyaw - new_tyaw));
    float rem_pitch = fast_fabsf(want_gpitch - new_gpitch);
    int aim_status = 0;
    if (yaw_clamped || pitch_clamped) aim_status = 2;
    else if (rem_yaw <= lock_tol_yaw && rem_pitch <= lock_tol_pitch) aim_status = 1;

    out_f[0] = new_tyaw;
    out_f[1] = new_gpitch;
    out_f[2] = want_tyaw;
    out_f[3] = want_gpitch;
    out_f[4] = (float)aim_status;
    out_f[5] = rem_yaw;
    out_f[6] = rem_pitch;
    out_f[7] = 1.0f;

    return return_borrowed_arg(out_obj);
}

/* =========================================================================
 * Direct3D 9Ex Low-Latency Frame Queue Optimizer (v10.2):
 *   py_apply_d3d9_low_latency(out_array, max_latency=1) -> out_array
 *
 * Traverses Moo::RenderContext singleton (RVA 0x00EB07FC) -> device_ (+0x12C).
 * Queries IDirect3DDevice9Ex {B18B10DE-D5CD-4cca-870F-078343222402}.
 * Calls IDirect3DDevice9Ex::SetMaximumFrameLatency(max_latency) (vtable index 126).
 * Eliminates GPU draw backlog (default 3 queued frames) and removes frame stutter!
 * ========================================================================= */
static PyObject * CDECL py_apply_d3d9_low_latency(PyObject *self, PyObject *args) {
    PyObject *out_obj = NULL;
    int max_latency = 1;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "O|i", &out_obj, &max_latency)) {
        return NULL;
    }
    int out_len = 0;
    float *out_ptr = (float *)get_array_buffer(out_obj, &out_len);
    if (!out_ptr || out_len < 2) {
        return return_borrowed_arg(out_obj);
    }
    out_ptr[0] = 0.0f;
    out_ptr[1] = 0.0f;

    if (max_latency < 1) max_latency = 1;
    if (max_latency > 3) max_latency = 3;

    if (!g_wot_base) {
        return return_borrowed_arg(out_obj);
    }

    uint8_t *rc_ptr = *(uint8_t **)(g_wot_base + 0x00EB07FCu);
    if (!rc_ptr) {
        return return_borrowed_arg(out_obj);
    }

    void *dev = *(void **)(rc_ptr + 0x12Cu);
    if (!dev) {
        return return_borrowed_arg(out_obj);
    }

    void **vtbl = *(void ***)dev;
    if (!vtbl) {
        return return_borrowed_arg(out_obj);
    }

    typedef struct {
        uint32_t Data1;
        uint16_t Data2;
        uint16_t Data3;
        uint8_t  Data4[8];
    } Offh_GUID;
    static const Offh_GUID IID_IDirect3DDevice9Ex = {
        0xb18b10de, 0xd5cd, 0x4cca, {0x87, 0x0f, 0x07, 0x83, 0x43, 0x22, 0x24, 0x02}
    };

    void *dev_ex = NULL;
    typedef int32_t (WINAPI *fn_QI_t)(void *, const void *, void **);
    fn_QI_t pQI = (fn_QI_t)vtbl[0];
    if (pQI && pQI(dev, (const void *)&IID_IDirect3DDevice9Ex, &dev_ex) == 0 && dev_ex) {
        void **ex_vtbl = *(void ***)dev_ex;
        if (ex_vtbl) {
            typedef int32_t (WINAPI *fn_SetLatency_t)(void *, uint32_t);
            fn_SetLatency_t pSetLatency = (fn_SetLatency_t)ex_vtbl[126];
            if (pSetLatency) {
                pSetLatency(dev_ex, (uint32_t)max_latency);
                out_ptr[0] = 1.0f;
                out_ptr[1] = (float)max_latency;
            }
        }
        typedef uint32_t (WINAPI *fn_Release_t)(void *);
        fn_Release_t pRel = (fn_Release_t)ex_vtbl[2];
        if (pRel) pRel(dev_ex);
    }

    return return_borrowed_arg(out_obj);
}

/* =========================================================================
 * Fast C Vector Projection + Native Raycast Outline / AutoAim Scanner (v10.2):
 *   py_native_scan_outline_and_autoaim(in_array, out_array) -> out_array
 * ========================================================================= */
static PyObject * CDECL py_native_scan_outline_and_autoaim(PyObject *self, PyObject *args) {
    PyObject *in_obj = NULL, *out_obj = NULL;
    (void)self;

    if (!p_PyArg_ParseTuple(args, "OO", &in_obj, &out_obj)) {
        return NULL;
    }
    int in_len = 0, out_len = 0;
    const float *in_ptr = (const float *)get_array_buffer(in_obj, &in_len);
    float *out_ptr      = (float *)get_array_buffer(out_obj, &out_len);
    if (!in_ptr || !out_ptr || in_len < 17 || out_len < 8) {
        if (out_ptr && out_len >= 8) {
            out_ptr[0] = -1.0f;
            out_ptr[7] = 0.0f;
        }
        return return_borrowed_arg(out_obj);
    }

    float mode        = in_ptr[0];
    int   player_vid  = (int)(in_ptr[1] + 0.5f);
    int   player_team = (int)(in_ptr[2] + 0.5f);
    int   space_id    = (int)(in_ptr[3] + 0.5f);
    float gx          = in_ptr[4],  gy  = in_ptr[5],  gz  = in_ptr[6];
    float gdx         = in_ptr[7],  gdy = in_ptr[8],  gdz = in_ptr[9];
    float cx          = in_ptr[10], cy  = in_ptr[11], cz  = in_ptr[12];
    float cdx         = in_ptr[13], cdy = in_ptr[14], cdz = in_ptr[15];
    int   num_cand    = (int)(in_ptr[16] + 0.5f);

    if (num_cand <= 0 || (17 + num_cand * 7) > in_len) {
        out_ptr[0] = -1.0f;
        out_ptr[1] = 9999.0f;
        out_ptr[2] = 0.0f;
        out_ptr[3] = 0.0f;
        out_ptr[7] = 1.0f;
        return return_borrowed_arg(out_obj);
    }

    int best_vid = -1;
    float best_val = 9999.0f;
    int best_team = 0;
    int hit_ray_flag = 0;
    int is_autoaim = (mode > 0.5f);

    Py_BEGIN_ALLOW_THREADS

    if (!is_autoaim) {
        /* Mode 0: Outline ray scan */
        float min_dist = 9999.0f;
        for (int i = 0; i < num_cand; ++i) {
            const float *c = in_ptr + 17 + i * 7;
            int vid = (int)(c[0] + 0.5f);
            int team = (int)(c[1] + 0.5f);
            int alive = (int)(c[2] + 0.5f);
            int spotted = (int)(c[3] + 0.5f);
            if (vid == player_vid || !alive || !spotted) continue;

            float bx = c[4];
            float by = c[5] + 1.35f;
            float bz = c[6];

            float proj_len = 0.0f;
            int hit = 0;

            /* Check gun ray */
            float bvx = bx - gx, bvy = by - gy, bvz = bz - gz;
            float g_proj = bvx * gdx + bvy * gdy + bvz * gdz;
            if (g_proj > 0.0f) {
                float px = (gx + gdx * g_proj) - bx;
                float py = (gy + gdy * g_proj) - by;
                float pz = (gz + gdz * g_proj) - bz;
                if ((px * px + py * py + pz * pz) < (3.6f * 3.6f)) {
                    hit = 1;
                    proj_len = g_proj;
                }
            }

            /* Check camera ray */
            if (!hit) {
                float cvx = bx - cx, cvy = by - cy, cvz = bz - cz;
                float c_proj = cvx * cdx + cvy * cdy + cvz * cdz;
                if (c_proj > 2.0f) {
                    float px = (cx + cdx * c_proj) - bx;
                    float py = (cy + cdy * c_proj) - by;
                    float pz = (cz + cdz * c_proj) - bz;
                    if ((px * px + py * py + pz * pz) < (3.8f * 3.8f)) {
                        hit = 1;
                        proj_len = c_proj;
                    }
                }
            }

            if (hit && proj_len < min_dist) {
                min_dist = proj_len;
                best_vid = vid;
                best_team = team;
                hit_ray_flag = 1;
            }
        }
        best_val = min_dist;
    } else {
        /* Mode 1: Autoaim screen cone scan */
        float best_score = 9999.0f;
        for (int i = 0; i < num_cand; ++i) {
            const float *c = in_ptr + 17 + i * 7;
            int vid = (int)(c[0] + 0.5f);
            int team = (int)(c[1] + 0.5f);
            int alive = (int)(c[2] + 0.5f);
            int spotted = (int)(c[3] + 0.5f);
            if (vid == player_vid || team == player_team || !alive || !spotted) continue;

            float bx = c[4];
            float by = c[5] + 1.5f;
            float bz = c[6];

            float bvx = bx - cx, bvy = by - cy, bvz = bz - cz;
            float proj = bvx * cdx + bvy * cdy + bvz * cdz;
            if (proj <= 2.0f || proj > 600.0f) continue;

            float px = (cx + cdx * proj) - bx;
            float py = (cy + cdy * proj) - by;
            float pz = (cz + cdz * proj) - bz;
            float perp = fast_sqrtf(px * px + py * py + pz * pz);
            float max_perp = proj * 0.105f;
            if (max_perp < 2.5f) max_perp = 2.5f;
            if (perp > max_perp) continue;

            float score = perp / proj;
            if (score < best_score) {
                /* Obstacle LOS raycast via C++ ClientSpace::collide without GIL */
                if (g_collide_hook_ready && p_BW_SpaceCollide && space_id > 0) {
                    uint8_t cb_buf[0x60];
                    fast_memset_zero(cb_buf, sizeof(cb_buf));
                    *(void **)(cb_buf + 0x00)    = (void *)g_custom_collide_vtable;
                    *(uint16_t *)(cb_buf + 0x04) = 128u;
                    *(void **)(cb_buf + 0x08)    = NULL;
                    cb_buf[0x0C]                 = 0;
                    *(uint32_t *)(cb_buf + 0x10) = 0x7F7FFFFFu;
                    cb_buf[0x20]                 = 0;
                    cb_buf[0x21]                 = 0;
                    *(uint32_t *)(cb_buf + 0x24) = 0x7F7FFFFFu;
                    cb_buf[0x4C]                 = 1;
                    *(int *)(cb_buf + 0x50)      = 70;
                    float s_xyz[3] = {cx, cy, cz};
                    float e_xyz[3] = {bx, by, bz};
                    p_BW_SpaceCollide(space_id, s_xyz, e_xyz, (void *)cb_buf);
                    if (cb_buf[0x4C] != 0) {
                        float hx = *(float *)(cb_buf + 0x08);
                        float hy = *(float *)(cb_buf + 0x0C);
                        float hz = *(float *)(cb_buf + 0x10);
                        float h_dist = fast_sqrtf((hx - cx)*(hx - cx) + (hy - cy)*(hy - cy) + (hz - cz)*(hz - cz));
                        float t_dist = fast_sqrtf(bvx * bvx + bvy * bvy + bvz * bvz);
                        if (h_dist + 2.0f < t_dist) {
                            continue; /* Wall/mountain blocks line of sight */
                        }
                    }
                }
                best_vid = vid;
                best_score = score;
                best_team = team;
            }
        }
        best_val = best_score;
    }

    Py_END_ALLOW_THREADS

    out_ptr[0] = (float)best_vid;
    out_ptr[1] = best_val;
    out_ptr[2] = (best_vid >= 0) ? ((best_team == player_team) ? 2.0f : 1.0f) : 0.0f;
    out_ptr[3] = (float)hit_ray_flag;
    out_ptr[4] = 0.0f;
    out_ptr[5] = 0.0f;
    out_ptr[6] = 0.0f;
    out_ptr[7] = 1.0f;

    return return_borrowed_arg(out_obj);
}

PyMethodDef g_offh_mt_methods[] = {
    {"apply_d3d9_low_latency", py_apply_d3d9_low_latency, METH_VARARGS,
     "Query IDirect3DDevice9Ex and set SetMaximumFrameLatency(1) to eliminate GPU frame backlog and input lag."},
    {"native_scan_outline_and_autoaim", py_native_scan_outline_and_autoaim, METH_VARARGS,
     "High-performance C vector projection + C++ obstacle raycast for outline detection and autoaim cone targeting."},
    {"native_step_bot_aim", py_native_step_bot_aim, METH_VARARGS,
     "60Hz C-native bot aiming & turret/gun servo with difficulty separation (0=easy,1=medium,2=hard)."},
    {"native_spot_3d_los", py_native_spot_3d_los, METH_VARARGS,
     "GIL-free C++ 3D multi-point spotting contour + heightfield mountain LOS check."},
    {"native_solve_shot_angles", py_native_solve_shot_angles, METH_VARARGS,
     "C++ turret world matrix + ballistic parabola + tilted-hull local shot angle solver."},
    {"calibrate_array_offset", py_calibrate_array_offset, METH_VARARGS,
     "Calibrate array.array ob_item offset and return worker count + exe hook status."},
    {"upload_heightfield", py_upload_heightfield, METH_VARARGS,
     "Upload baked terrain heightfield (float32 array) to PhysicsSpace worker pool."},
    {"upload_foliage", py_upload_foliage, METH_VARARGS,
     "Upload prebaked 3D oriented foliage volumes (N*10 float32 array) to C pool."},
    {"foliage_bonus", py_foliage_bonus, METH_VARARGS,
     "Compute 3D foliage camouflage bonus for (observer, target, fired) in C."},
    {"lidar_scan", py_lidar_scan, METH_VARARGS,
     "16-sector 3D LiDAR lookahead terrain/water/cliff/contour scanner."},
    {"step_frame", py_step_frame, METH_VARARGS,
     "Legacy parallel terrain YPR, OBB push, and 32x32 LOS bitmask step."},
    {"get_last_results_nonblocking", py_get_last_results_nonblocking, METH_VARARGS,
     "Non-blocking harvest of previous step results from double-buffered C worker pool."},
    {"submit_step_async", py_submit_step_async, METH_VARARGS,
     "Non-blocking async submission of fresh vehicle inputs to double-buffered C worker pool."},
    {"step_physics_space", py_step_physics_space, METH_VARARGS,
     "60Hz PhysicsSpace step + resolve_inter_collisions + LERP alpha output."},
    {"get_interpolated_transform", py_get_interpolated_transform, METH_VARARGS,
     "Return LERP-interpolated (x,y,z,yaw,pitch,roll,turret_yaw,gun_pitch) for vehicle id at alpha."},
    {"fast_publish_root", py_fast_publish_root, METH_VARARGS,
     "Zero-syscall C publisher for WGVehiclePhysics2 -> WGVehicleFilter2 + pose/speed/rspeed/isFrozen extraction."},
    {"fast_seed_and_output", py_fast_seed_and_output, METH_VARARGS,
     "Zero-syscall LAN/Sub-Engine replica pose publisher for WGVehicleFilter2."},
    {"native_ballistic_impact", py_native_ballistic_impact, METH_VARARGS,
     "C++ parabolic trajectory raycaster (ClientSpace::collide 0x0092D4B0)."},
    {"native_eyes_scan", py_native_eyes_scan, METH_VARARGS,
     "Real 3D C++ corridor & terrain scanner (ClientSpace::collide 0x0092D4B0)."},
    {"native_collide_segment", py_native_collide_segment, METH_VARARGS,
     "Direct C++ ClientSpace::collide (0x0092D4B0) raycaster skipping leaves (matKind 72) in C++."},
    {"fix_window_mode", py_fix_window_mode, METH_VARARGS,
     "Restore bordered WS_OVERLAPPEDWINDOW, work-area placement, and cursor on fullscreen exit."},
    {"ensure_windowed_style", py_ensure_windowed_style, METH_VARARGS,
     "Check and restore WS_OVERLAPPEDWINDOW if DXVK stripped window caption/buttons."},
    {"shutdown_process", py_shutdown_process, METH_VARARGS,
     "Stop worker threads, restore display mode, and cleanly exit WorldOfTanks.exe."},
    {NULL, NULL, 0, NULL}
};

void CDECL _initoffh_mt(void) {
    if (!resolve_wot_python_api()) {
        return;
    }
    init_thread_pool();
    p_Py_InitModule4(
        "offh_mt",
        g_offh_mt_methods,
        "Native RVA-bound Win32 multithreaded PhysicsSpace sub-engine + EXE hooks for WoT 0.8.2",
        NULL,
        PYTHON_API_VERSION_26);
}

int WINAPI _DllMainCRTStartup(void *hinstDLL, uint32_t fdwReason, void *lpvReserved) {
    (void)lpvReserved;
    if (fdwReason == DLL_PROCESS_ATTACH) {
        g_self_base = (uint8_t *)hinstDLL;
    } else if (fdwReason == DLL_PROCESS_DETACH) {
        g_shutdown = 1;
        if (g_cs_inited && p_SetEvent) {
            for (int i = 0; i < g_num_threads; ++i) {
                if (g_start_events[i]) p_SetEvent(g_start_events[i]);
            }
        }
    }
    return 1;
}