#include "ipc_common.h"
#include <tlhelp32.h>
#include <wchar.h>
#include <stdint.h>

#define IPC_WAIT_MS 15000

DWORD ipc_machine(HANDLE process, WORD *machine) {
    typedef BOOL (WINAPI *wow2_fn)(HANDLE,USHORT*,USHORT*);
    wow2_fn wow2 = (wow2_fn)GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"IsWow64Process2");
    if (!wow2) return ERROR_NOT_SUPPORTED;
    USHORT target = 0, native = 0;
    if (!wow2(process,&target,&native)) return GetLastError();
    *machine = target == IMAGE_FILE_MACHINE_UNKNOWN ? native : target;
    return (*machine == IMAGE_FILE_MACHINE_AMD64 || *machine == IMAGE_FILE_MACHINE_I386)
        ? ERROR_SUCCESS : ERROR_NOT_SUPPORTED;
}

static DWORD remote_call(HANDLE process, void *function, void *argument, DWORD *result) {
    HANDLE thread = CreateRemoteThread(process,NULL,0,(LPTHREAD_START_ROUTINE)function,argument,0,NULL);
    if (!thread) return GetLastError();
    DWORD error = ERROR_SUCCESS;
    DWORD wait = WaitForSingleObject(thread,IPC_WAIT_MS);
    if (wait != WAIT_OBJECT_0) error = wait == WAIT_TIMEOUT ? ERROR_TIMEOUT : GetLastError();
    else if (!GetExitCodeThread(thread,result)) error = GetLastError();
    CloseHandle(thread);
    return error;
}

static uintptr_t remote_module(DWORD pid, const wchar_t *path) {
    HANDLE snapshot = INVALID_HANDLE_VALUE;
    for (int attempt=0;attempt<8;attempt++) {
        snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPMODULE,pid);
        if (snapshot != INVALID_HANDLE_VALUE || GetLastError()!=ERROR_BAD_LENGTH) break;
    }
    if (snapshot==INVALID_HANDLE_VALUE) return 0;
    MODULEENTRY32W item = {0}; item.dwSize=sizeof(item);
    uintptr_t base=0;
    if (Module32FirstW(snapshot,&item)) do {
        if (!_wcsicmp(item.szExePath,path)) {base=(uintptr_t)item.modBaseAddr;break;}
    } while (Module32NextW(snapshot,&item));
    CloseHandle(snapshot);
    return base;
}

DWORD ipc_inject(DWORD pid, const wchar_t *dll_path) {
    HANDLE process=OpenProcess(PROCESS_CREATE_THREAD|PROCESS_QUERY_INFORMATION|
        PROCESS_VM_OPERATION|PROCESS_VM_WRITE|PROCESS_VM_READ|SYNCHRONIZE,FALSE,pid);
    if (!process) return GetLastError();
    DWORD error=ERROR_SUCCESS, result=0;
    WORD mine=0,target=0;
    error=ipc_machine(GetCurrentProcess(),&mine);
    if (!error) error=ipc_machine(process,&target);
    if (!error && mine!=target) error=ERROR_BAD_EXE_FORMAT;
    HMODULE local=NULL;
    void *remote=NULL;
    BOOL remote_finished=TRUE;
    if (!error) {
        /* DllMain only stores its module handle; it does not initialize hooks. */
        local=LoadLibraryExW(dll_path,NULL,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!local) error=GetLastError();
    }
    FARPROC initialize=local ? GetProcAddress(local,"CompatInitialize") : NULL;
    if (!initialize && local) initialize=GetProcAddress(local,"CompatInitialize@4");
    if (!initialize && local) initialize=GetProcAddress(local,"_CompatInitialize@4");
    if (!error && !initialize) error=ERROR_PROC_NOT_FOUND;
    SIZE_T size=(wcslen(dll_path)+1)*sizeof(wchar_t),written=0;
    if (!error) {
        remote=VirtualAllocEx(process,NULL,size,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
        if (!remote) error=GetLastError();
    }
    if (!error && (!WriteProcessMemory(process,remote,dll_path,size,&written)||written!=size))
        error=GetLastError() ? GetLastError() : ERROR_WRITE_FAULT;
    if (!error) {
        void *loader=(void*)GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"LoadLibraryW");
        error=remote_call(process,loader,remote,&result);
        remote_finished=error!=ERROR_TIMEOUT;
        if (!error && !result) error=ERROR_DLL_INIT_FAILED;
    }
    if (!error) {
        uintptr_t base=remote_module(pid,dll_path);
        if (!base) error=ERROR_MOD_NOT_FOUND;
        else {
            void *entry=(void*)(base+(uintptr_t)initialize-(uintptr_t)local);
            error=remote_call(process,entry,NULL,&result);
            if (!error) error=result;
        }
    }
    if (error) TerminateProcess(process,127); /* target is still suspended */
    if (remote && remote_finished) VirtualFreeEx(process,remote,0,MEM_RELEASE);
    if (local) FreeLibrary(local);
    CloseHandle(process);
    return error;
}

DWORD ipc_load_child(HMODULE self, PROCESS_INFORMATION *child,
                     BOOL (WINAPI *create)(LPCWSTR,LPWSTR,LPSECURITY_ATTRIBUTES,
                     LPSECURITY_ATTRIBUTES,BOOL,DWORD,LPVOID,LPCWSTR,
                     LPSTARTUPINFOW,LPPROCESS_INFORMATION)) {
    WORD machine=0;
    DWORD error=ipc_machine(child->hProcess,&machine);
    if (error) return error;
    wchar_t directory[32768],loader[32768],dll[32768],command[65536];
    DWORD length=GetModuleFileNameW(self,directory,32768);
    if (!length||length>=32768) return ERROR_INSUFFICIENT_BUFFER;
    wchar_t *last=wcsrchr(directory,L'\\');
    if (!last) return ERROR_BAD_PATHNAME;
    *last=0;
    const wchar_t *arch=machine==IMAGE_FILE_MACHINE_AMD64?L"x64":L"x86";
    if (_snwprintf(loader,32768,L"%ls\\ipc-loader-%ls.exe",directory,arch)<0 ||
        _snwprintf(dll,32768,L"%ls\\ipc-compat-%ls.dll",directory,arch)<0 ||
        _snwprintf(command,65536,L"\"%ls\" --pid %lu \"%ls\"",loader,child->dwProcessId,dll)<0)
        return ERROR_INSUFFICIENT_BUFFER;
    STARTUPINFOW startup={0};startup.cb=sizeof(startup);
    PROCESS_INFORMATION helper={0};
    /* Explicit executable and no shell. Do not recursively initialize the helper. */
    if (!create(loader,command,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,NULL,&startup,&helper))
        return GetLastError();
    DWORD wait=WaitForSingleObject(helper.hProcess,IPC_WAIT_MS);
    if (wait!=WAIT_OBJECT_0) {
        error=wait==WAIT_TIMEOUT?ERROR_TIMEOUT:GetLastError();
        TerminateProcess(helper.hProcess,127);
    } else if (!GetExitCodeProcess(helper.hProcess,&error)) error=GetLastError();
    CloseHandle(helper.hThread);CloseHandle(helper.hProcess);
    return error;
}
