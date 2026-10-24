#ifndef CHAIAI_IPC_COMMON_H
#define CHAIAI_IPC_COMMON_H
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

/* Internal launcher ABI, not a model-callable API. */
DWORD ipc_machine(HANDLE process, WORD *machine);
DWORD ipc_inject(DWORD pid, const wchar_t *dll_path);
DWORD ipc_load_child(HMODULE self, PROCESS_INFORMATION *child,
                     BOOL (WINAPI *create)(LPCWSTR,LPWSTR,LPSECURITY_ATTRIBUTES,
                     LPSECURITY_ATTRIBUTES,BOOL,DWORD,LPVOID,LPCWSTR,
                     LPSTARTUPINFOW,LPPROCESS_INFORMATION));
#endif
