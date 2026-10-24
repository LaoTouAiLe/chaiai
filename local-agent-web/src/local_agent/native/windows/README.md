# Windows IPC compatibility component

The write restricted token remains active. This component adds the current logon SID
to the DACL of a newly created, default-security, first-instance named pipe. It does
not add the user SID to the token's restricting SID list or grant external file writes.
Explicit pipe security descriptors are preserved.

The trusted Python runner creates a suspended command, assigns its Job Object,
loads the matching x64/x86 component, calls `CompatInitialize`, then resumes the main
thread. The component hooks application imports and dynamic resolution for named
pipes, process creation and library loading. Children inherit the same initialization
path. System DLL imports are untouched. `DllMain` does not initialize hooks.

Only newly created commands owned by this runtime are initialized. The internal
loader is not a model tool. Initialization failure stops the suspended command;
there is no retry without isolation. Applications bypassing these Win32 imports,
protected processes and other architectures are outside the verified scope.

## Rebuild

Run in `local-agent-web`, with an explicitly supplied Zig compiler. The verified
compiler was Zig 0.17.0. Building never downloads a compiler or changes system setup.

```powershell
.venv/Scripts/python.exe scripts/build_windows_ipc.py --zig <absolute-path-to-zig.exe>
if ($LASTEXITCODE -ne 0) { throw 'native build failed' }
.venv/Scripts/python.exe scripts/build_windows_ipc.py --check
```

Commit the four runtime binaries and `bin/manifest.json` together with their sources.
The manifest records source and binary SHA256 values and compiler version. PDB/import
libraries are local compiler intermediates; they are excluded from wheel and EXE assets.
Windows package builds verify sources before building and packaged assets afterward.
End users need neither Zig nor an online compiler.

## Runtime cache

Before creating commands the trusted host verifies filenames, PE architecture and
SHA256, copies bytes into a unique temporary directory, denies writes using an
internal restricting SID and retains noninheritable file/directory read handles to
prevent replacement. The runner verifies the copy again. The guard SID never grants
access. The host retains cache handles until exit; cache cleanup after a crash is not
implemented in this change. SHA256 detects corruption, not replacement by a trusted
host able to replace both the distribution manifest and all binaries.

Owned ancestor directories may receive noninheritable read-attributes/traverse metadata
access, needed for Node absolute-path canonicalization through owner-only directories.
This uses object-only `SetFileSecurityW`, so existing inherited ACEs are not recursively
reapplied. It grants no directory enumeration, file body reads or writes.

## Verification

The sealed cache also contains a generated `taskkill.cmd` for unqualified PID-only
cleanup commands. Its stdlib helper accepts `/PID <pid> /F [/T]`, checks membership
in the caller's current Job, and refuses helper ancestors and processes in other
Jobs. It does not grant process or file permissions. Explicit system taskkill paths
and remote/image selectors are outside this compatibility entry.

Source mode invokes the base Python interpreter directly: the venv redirector
would introduce a nested Job and hide the command's Job from the helper. Frozen
mode dispatches an internal startup flag before API initialization. The source-mode
Playwright cleanup and scope boundaries have native tests; an updated EXE still
requires its own packaging acceptance.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_windows_ipc.py -p no:cacheprovider --basetemp=<new-directory-outside-git> -q
```

Run native tests from a normal host token, not inside another write restricted token.
Tests cover pipes, UTF-8, child status, fork messages, descendants, x64/x86 chains,
custom DACL preservation, persistent input, cancellation, timeout, binary tampering,
guarded cache writes and allowed/denied external file writes.

Raw WMI access and curl Schannel credentials remain separate Windows service limits.
Full-screen PTY, restart resource recovery and memory policy are not part of this component.
