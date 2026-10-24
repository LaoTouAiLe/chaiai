#include "ipc_common.h"
#include <wchar.h>
#include <stdlib.h>

int wmain(int argc, wchar_t **argv) {
    if (argc!=4 || wcscmp(argv[1],L"--pid")) return ERROR_INVALID_PARAMETER;
    wchar_t *end=NULL;
    unsigned long pid=wcstoul(argv[2],&end,10);
    if (!pid || !end || *end || pid==GetCurrentProcessId()) return ERROR_INVALID_PARAMETER;
    return (int)ipc_inject((DWORD)pid,argv[3]);
}
