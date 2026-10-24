/* Grant the current logon SID access to fresh default-DACL pipes.
 * No token changes, file ACL changes, custom-DACL changes, or host-wide hooks. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#include <accctrl.h>
#include <string.h>
#include <psapi.h>
#include <wchar.h>
#include "ipc_common.h"

typedef BOOL (WINAPI *process_a_fn)(LPCSTR,LPSTR,LPSECURITY_ATTRIBUTES,LPSECURITY_ATTRIBUTES,BOOL,DWORD,LPVOID,LPCSTR,LPSTARTUPINFOA,LPPROCESS_INFORMATION);
typedef BOOL (WINAPI *process_w_fn)(LPCWSTR,LPWSTR,LPSECURITY_ATTRIBUTES,LPSECURITY_ATTRIBUTES,BOOL,DWORD,LPVOID,LPCWSTR,LPSTARTUPINFOW,LPPROCESS_INFORMATION);
typedef HMODULE (WINAPI *load_a_fn)(LPCSTR);
typedef HMODULE (WINAPI *load_w_fn)(LPCWSTR);
typedef HMODULE (WINAPI *load_ex_a_fn)(LPCSTR,HANDLE,DWORD);
typedef HMODULE (WINAPI *load_ex_w_fn)(LPCWSTR,HANDLE,DWORD);
typedef FARPROC (WINAPI *get_proc_fn)(HMODULE,LPCSTR);
static process_a_fn original_process_a;
static process_w_fn original_process_w;
static load_a_fn original_load_a;
static load_w_fn original_load_w;
static load_ex_a_fn original_load_ex_a;
static load_ex_w_fn original_load_ex_w;
static get_proc_fn original_get_proc;
static HMODULE own_module;
static SRWLOCK patch_lock=SRWLOCK_INIT;
static volatile LONG initialized=0;
static BOOL patch_modules(void);

typedef HANDLE (WINAPI *pipe_a_fn)(LPCSTR,DWORD,DWORD,DWORD,DWORD,DWORD,DWORD,LPSECURITY_ATTRIBUTES);
typedef HANDLE (WINAPI *pipe_w_fn)(LPCWSTR,DWORD,DWORD,DWORD,DWORD,DWORD,DWORD,LPSECURITY_ATTRIBUTES);
static pipe_a_fn original_a;
static pipe_w_fn original_w;
static BYTE logon_sid[SECURITY_MAX_SID_SIZE];

static BOOL initialize_logon(void) {
    HANDLE token = NULL;
    DWORD size = 0;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return FALSE;
    GetTokenInformation(token, TokenLogonSid, NULL, 0, &size);
    TOKEN_GROUPS *groups = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
    BOOL ok = groups && GetTokenInformation(token, TokenLogonSid, groups, size, &size)
        && groups->GroupCount == 1
        && CopySid(sizeof(logon_sid), logon_sid, groups->Groups[0].Sid);
    if (groups) HeapFree(GetProcessHeap(), 0, groups);
    CloseHandle(token);
    return ok;
}

static BOOL grant_pipe(HANDLE pipe) {
    DWORD size = 0;
    GetKernelObjectSecurity(pipe, DACL_SECURITY_INFORMATION, NULL, 0, &size);
    PSECURITY_DESCRIPTOR old = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size);
    PACL acl = NULL, merged = NULL;
    BOOL present = FALSE, defaulted = FALSE;
    BOOL ok = old && GetKernelObjectSecurity(pipe, DACL_SECURITY_INFORMATION, old, size, &size)
        && GetSecurityDescriptorDacl(old, &present, &acl, &defaulted) && present && acl;
    if (ok) {
        EXPLICIT_ACCESS_W entry = {0};
        entry.grfAccessPermissions = FILE_GENERIC_READ | FILE_GENERIC_WRITE | WRITE_DAC;
        entry.grfAccessMode = GRANT_ACCESS;
        entry.Trustee.TrusteeForm = TRUSTEE_IS_SID;
        entry.Trustee.TrusteeType = TRUSTEE_IS_GROUP;
        entry.Trustee.ptstrName = (LPWSTR)logon_sid;
        SECURITY_DESCRIPTOR sd;
        ok = SetEntriesInAclW(1, &entry, acl, &merged) == ERROR_SUCCESS
            && InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION)
            && SetSecurityDescriptorDacl(&sd, TRUE, merged, FALSE)
            && SetKernelObjectSecurity(pipe, DACL_SECURITY_INFORMATION, &sd);
    }
    if (merged) LocalFree(merged);
    if (old) HeapFree(GetProcessHeap(), 0, old);
    return ok;
}

static BOOL needs_grant(DWORD mode, LPSECURITY_ATTRIBUTES sa) {
    return (mode & FILE_FLAG_FIRST_PIPE_INSTANCE) && (!sa || !sa->lpSecurityDescriptor);
}

static HANDLE WINAPI compatible_a(LPCSTR name,DWORD open,DWORD mode,DWORD max,DWORD out,DWORD in,DWORD timeout,LPSECURITY_ATTRIBUTES sa) {
    BOOL grant = needs_grant(open, sa);
    HANDLE pipe = original_a(name, open | (grant ? WRITE_DAC : 0),mode,max,out,in,timeout,sa);
    DWORD error = GetLastError();
    if (grant && pipe != INVALID_HANDLE_VALUE && !grant_pipe(pipe)) {
        CloseHandle(pipe); SetLastError(ERROR_ACCESS_DENIED); return INVALID_HANDLE_VALUE;
    }
    SetLastError(error); return pipe;
}

static HANDLE WINAPI compatible_w(LPCWSTR name,DWORD open,DWORD mode,DWORD max,DWORD out,DWORD in,DWORD timeout,LPSECURITY_ATTRIBUTES sa) {
    BOOL grant = needs_grant(open, sa);
    HANDLE pipe = original_w(name, open | (grant ? WRITE_DAC : 0),mode,max,out,in,timeout,sa);
    DWORD error = GetLastError();
    if (grant && pipe != INVALID_HANDLE_VALUE && !grant_pipe(pipe)) {
        CloseHandle(pipe); SetLastError(ERROR_ACCESS_DENIED); return INVALID_HANDLE_VALUE;
    }
    SetLastError(error); return pipe;
}

static BOOL finish_child(BOOL ok, DWORD flags, PROCESS_INFORMATION *child) {
    if (!ok) return FALSE;
    DWORD error=ipc_load_child(own_module,child,original_process_w);
    if (!error && !(flags & CREATE_SUSPENDED) && ResumeThread(child->hThread)==(DWORD)-1)
        error=GetLastError();
    if (!error) return TRUE;
    TerminateProcess(child->hProcess,127);
    CloseHandle(child->hThread);CloseHandle(child->hProcess);
    ZeroMemory(child,sizeof(*child));
    SetLastError(error);return FALSE;
}

static BOOL WINAPI compatible_process_a(LPCSTR app,LPSTR command,LPSECURITY_ATTRIBUTES pa,LPSECURITY_ATTRIBUTES ta,BOOL inherit,DWORD flags,LPVOID env,LPCSTR cwd,LPSTARTUPINFOA startup,LPPROCESS_INFORMATION child) {
    return finish_child(original_process_a(app,command,pa,ta,inherit,flags|CREATE_SUSPENDED,env,cwd,startup,child),flags,child);
}

static BOOL WINAPI compatible_process_w(LPCWSTR app,LPWSTR command,LPSECURITY_ATTRIBUTES pa,LPSECURITY_ATTRIBUTES ta,BOOL inherit,DWORD flags,LPVOID env,LPCWSTR cwd,LPSTARTUPINFOW startup,LPPROCESS_INFORMATION child) {
    return finish_child(original_process_w(app,command,pa,ta,inherit,flags|CREATE_SUSPENDED,env,cwd,startup,child),flags,child);
}

static HMODULE finish_load(HMODULE module) {
    DWORD error=GetLastError();
    if (module && !patch_modules()) {SetLastError(ERROR_DLL_INIT_FAILED);return NULL;}
    SetLastError(error);return module;
}
static HMODULE WINAPI compatible_load_a(LPCSTR name) {return finish_load(original_load_a(name));}
static HMODULE WINAPI compatible_load_w(LPCWSTR name) {return finish_load(original_load_w(name));}
static HMODULE WINAPI compatible_load_ex_a(LPCSTR name,HANDLE file,DWORD flags) {return finish_load(original_load_ex_a(name,file,flags));}
static HMODULE WINAPI compatible_load_ex_w(LPCWSTR name,HANDLE file,DWORD flags) {return finish_load(original_load_ex_w(name,file,flags));}

static FARPROC WINAPI compatible_get_proc(HMODULE module,LPCSTR name);
static void *replacement_for(const char *name, void **original) {
#define ENTRY(api,real,hook) if (!strcmp(name,api)) {*original=(void*)real;return (void*)hook;}
    ENTRY("CreateNamedPipeA",original_a,compatible_a)
    ENTRY("CreateNamedPipeW",original_w,compatible_w)
    ENTRY("CreateProcessA",original_process_a,compatible_process_a)
    ENTRY("CreateProcessW",original_process_w,compatible_process_w)
    ENTRY("LoadLibraryA",original_load_a,compatible_load_a)
    ENTRY("LoadLibraryW",original_load_w,compatible_load_w)
    ENTRY("LoadLibraryExA",original_load_ex_a,compatible_load_ex_a)
    ENTRY("LoadLibraryExW",original_load_ex_w,compatible_load_ex_w)
    ENTRY("GetProcAddress",original_get_proc,compatible_get_proc)
#undef ENTRY
    return NULL;
}

static FARPROC WINAPI compatible_get_proc(HMODULE module,LPCSTR name) {
    FARPROC result=original_get_proc(module,name);
    if ((ULONG_PTR)name<=0xffff || !result) return result;
    /* Only replace actual kernel exports, never an unrelated DLL's same-named API. */
    void *expected=NULL;
    void *replacement=replacement_for(name,&expected);
    return (void*)result==expected && replacement?(FARPROC)replacement:result;
}

static BOOL patch_imports(HMODULE module) {
    BYTE *base = (BYTE*)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER*)base;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    DWORD rva = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress;
    if (!rva) return TRUE;
    IMAGE_IMPORT_DESCRIPTOR *desc = (IMAGE_IMPORT_DESCRIPTOR*)(base+rva);
    for (; desc->Name; desc++) {
        IMAGE_THUNK_DATA *slot = (IMAGE_THUNK_DATA*)(base+desc->FirstThunk);
        IMAGE_THUNK_DATA *names = desc->OriginalFirstThunk ? (IMAGE_THUNK_DATA*)(base+desc->OriginalFirstThunk) : NULL;
        for (;slot->u1.Function;slot++, names = names ? names+1 : NULL) {
            void *replacement = NULL;
            if (names && !IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) {
                IMAGE_IMPORT_BY_NAME *import = (IMAGE_IMPORT_BY_NAME*)(base+names->u1.AddressOfData);
                const char *name=(char*)import->Name;
                void *original=NULL;
                /* Also recognize already-patched slots on subsequent module loads. */
                void *candidate=replacement_for(name,&original);
                if (candidate && ((void*)slot->u1.Function==original || (void*)slot->u1.Function==candidate)) replacement=candidate;
            }
            if (replacement) {
                DWORD protect;
                if ((void*)slot->u1.Function==replacement) continue;
                if (!VirtualProtect(&slot->u1.Function,sizeof(ULONG_PTR),PAGE_READWRITE,&protect)) return FALSE;
                InterlockedExchangePointer((PVOID volatile*)&slot->u1.Function,replacement);
                if (!VirtualProtect(&slot->u1.Function,sizeof(ULONG_PTR),protect,&protect)) return FALSE;
            }
        }
    }
    return TRUE;
}

static BOOL patch_modules(void) {
    HMODULE modules[2048];DWORD needed=0;
    if (!K32EnumProcessModules(GetCurrentProcess(),modules,sizeof(modules),&needed) || needed>sizeof(modules)) return FALSE;
    wchar_t windows[MAX_PATH],path[32768];
    UINT length=GetWindowsDirectoryW(windows,MAX_PATH);
    if (!length||length>=MAX_PATH) return FALSE;
    BOOL ok=TRUE;HMODULE main=GetModuleHandleW(NULL);
    for (DWORD i=0;ok && i<needed/sizeof(HMODULE);i++) {
        HMODULE module=modules[i];
        if (module==own_module) continue;
        HMODULE reference=NULL;
        /* Pin only while touching the IAT, so a concurrent unload cannot free it.
         * Loader operations stay outside patch_lock to avoid lock inversion. */
        if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,(LPCWSTR)module,&reference)) continue;
        DWORD count=GetModuleFileNameW(module,path,32768);
        if (!count||count>=32768) {FreeLibrary(reference);ok=FALSE;break;}
        /* OS implementation modules are not modified; a system EXE such as cmd is. */
        if (module!=main && !_wcsnicmp(path,windows,length) && path[length]==L'\\') {FreeLibrary(reference);continue;}
        AcquireSRWLockExclusive(&patch_lock);
        ok=patch_imports(module);
        ReleaseSRWLockExclusive(&patch_lock);
        FreeLibrary(reference);
    }
    return ok;
}

__declspec(dllexport) DWORD WINAPI CompatInitialize(LPVOID unused) {
    (void)unused;
    if (InterlockedCompareExchange(&initialized,1,0)!=0) return ERROR_ALREADY_INITIALIZED;
    HMODULE kernel=GetModuleHandleW(L"kernel32.dll");
    original_get_proc=(get_proc_fn)GetProcAddress(kernel,"GetProcAddress");
    original_a=(pipe_a_fn)original_get_proc(kernel,"CreateNamedPipeA");
    original_w=(pipe_w_fn)original_get_proc(kernel,"CreateNamedPipeW");
    original_process_a=(process_a_fn)original_get_proc(kernel,"CreateProcessA");
    original_process_w=(process_w_fn)original_get_proc(kernel,"CreateProcessW");
    original_load_a=(load_a_fn)original_get_proc(kernel,"LoadLibraryA");
    original_load_w=(load_w_fn)original_get_proc(kernel,"LoadLibraryW");
    original_load_ex_a=(load_ex_a_fn)original_get_proc(kernel,"LoadLibraryExA");
    original_load_ex_w=(load_ex_w_fn)original_get_proc(kernel,"LoadLibraryExW");
    if (!original_a||!original_w||!original_process_a||!original_process_w||!original_load_a||!original_load_w||!original_load_ex_a||!original_load_ex_w) return ERROR_PROC_NOT_FOUND;
    if (!initialize_logon()) return GetLastError()?GetLastError():ERROR_DLL_INIT_FAILED;
    return patch_modules()?ERROR_SUCCESS:ERROR_DLL_INIT_FAILED;
}

BOOL WINAPI DllMain(HINSTANCE self,DWORD reason,LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        own_module=self;
        DisableThreadLibraryCalls(self);
    }
    return TRUE;
}
