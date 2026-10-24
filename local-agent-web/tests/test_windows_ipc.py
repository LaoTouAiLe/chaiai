"""Native Node IPC, binary integrity, propagation and unchanged disk boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from local_agent.cancellation import CancellationToken
from local_agent.security import win_ipc, win_sandbox
from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessRegistry, SandboxRequest
from local_agent.tools.terminal_tools import TerminalSession

WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Windows native IPC")


def test_shipped_bundle_matches_sources_and_both_architectures() -> None:
    blobs = win_ipc.verify_bundle(check_sources=True)
    assert set(blobs) == {*win_ipc.FILES, "manifest.json"}


@pytest.mark.parametrize("mutation", ["missing", "tampered", "extra_path", "wrong_machine"])
def test_invalid_bundle_is_rejected(tmp_path: Path, mutation: str) -> None:
    for name, blob in win_ipc.verify_bundle().items():
        (tmp_path / name).write_bytes(blob)
    name = "ipc-compat-x64.dll"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    if mutation == "missing":
        (tmp_path / name).unlink()
    elif mutation == "extra_path":
        manifest["files"]["../outside.exe"] = "0" * 64
    else:
        data = bytearray((tmp_path / name).read_bytes())
        if mutation == "tampered":
            data[-1] ^= 1
        else:
            offset = int.from_bytes(data[60:64], "little")
            data[offset + 4:offset + 6] = b"\x4c\x01"
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        (tmp_path / name).write_bytes(data)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(win_sandbox.SandboxUnavailable, match="组件缺失或校验失败"):
        win_ipc.verify_bundle(tmp_path)


@WINDOWS
def test_invalid_bundle_prevents_process_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def invalid(*args, **kwargs):
        raise win_sandbox.SandboxUnavailable("test corrupt component")

    monkeypatch.setattr(win_ipc, "verify_bundle", invalid)
    monkeypatch.setattr("local_agent.security.process_guard.subprocess.Popen", lambda *a, **kw: calls.append(a))
    with pytest.raises(win_sandbox.SandboxUnavailable, match="corrupt component"):
        ProcessRegistry().spawn(["node", "-e", "console.log(42)"], sandbox=SandboxRequest())
    assert not calls


@pytest.fixture
def node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js not installed")
    return executable


def _policy(root: Path, app_config, mode="workspace-write", grants=None) -> SecurityPolicy:
    return SecurityPolicy.from_config(
        app_config, workspace_root=root,
        external_policy=ExternalPathPolicy(mode=mode, grants=grants or {}),
    )


@WINDOWS
@pytest.mark.parametrize("mode", ["workspace-write", "read-only", "danger-full-access"])
def test_sync_pipe_input_output_and_real_child_exit(tmp_path: Path, app_config, node: str, mode: str) -> None:
    script = tmp_path / "sync.cjs"
    script.write_text("""
const cp=require('child_process');
const child="let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>{process.stdout.write(s);process.stderr.write('stderr-ok');process.exit(7)})";
const r=cp.spawnSync(process.execPath,['-e',child],{input:'中文输入',encoding:'utf8'});
console.log(JSON.stringify({status:r.status,stdout:r.stdout,stderr:r.stderr,error:r.error?.code}));
process.exit(r.status===7?0:1);
""", encoding="utf-8")
    policy = _policy(tmp_path, app_config, mode)
    result = policy.processes.run([node, str(script)], cwd=tmp_path, timeout_seconds=30,
                                   sandbox=policy.shell_sandbox_request())
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"status": 7, "stdout": "中文输入", "stderr": "stderr-ok"}


@WINDOWS
@pytest.mark.parametrize("granted", [False, True])
def test_fork_ipc_and_grandchild_keep_external_write_boundary(tmp_path: Path, app_config, node: str, granted: bool) -> None:
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir(); outside.mkdir()
    # Materialize an old grant first: denial must survive an existing external ACE.
    win_sandbox.grant_write(str(outside), win_sandbox.workspace_sid_text(str(outside)))
    target = outside / "written.txt"
    child = work / "child.cjs"
    child.write_text("""
const cp=require('child_process'),fs=require('fs');
const grandchild="const fs=require('fs');let error=null;try{fs.writeFileSync(process.argv[1],'written')}catch(e){error=e.code};console.log(JSON.stringify({answer:42,error}))";
const r=cp.spawnSync(process.execPath,['-e',grandchild,process.argv[2]],{encoding:'utf8'});
if(r.error||r.status!==0)throw r.error||new Error(r.stderr);
process.stdout.write('child-output');process.stderr.write('child-error');
process.send(JSON.parse(r.stdout),()=>process.disconnect());
""", encoding="utf-8")
    parent = work / "parent.cjs"
    parent.write_text("""
const cp=require('child_process');const p=cp.fork(process.argv[2],[process.argv[3]],{silent:true});
let stdout='',stderr='',message=null;p.stdout.on('data',d=>stdout+=d);p.stderr.on('data',d=>stderr+=d);
p.on('message',m=>message=m);p.on('error',e=>{console.error(e);process.exit(1)});
p.on('close',code=>{console.log(JSON.stringify({code,stdout,stderr,message}));process.exit(code||0)});
""", encoding="utf-8")
    grants = {normalize_grant_path(outside): "read_write"} if granted else {}
    policy = _policy(work, app_config, grants=grants)
    result = policy.processes.run([node, str(parent), str(child), str(target)], cwd=work,
                                   timeout_seconds=30, sandbox=policy.shell_sandbox_request())
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["code"] == 0 and body["stdout"] == "child-output" and body["stderr"] == "child-error"
    assert body["message"]["answer"] == 42
    if granted:
        assert body["message"]["error"] is None and target.read_text() == "written"
    else:
        assert body["message"]["error"] in {"EPERM", "EACCES"} and not target.exists()


@WINDOWS
def test_x64_x86_x64_process_chain(tmp_path: Path, app_config, node: str) -> None:
    cmd32 = Path(os.environ["SystemRoot"]) / "SysWOW64/cmd.exe"
    if not cmd32.is_file():
        pytest.skip("x86 subsystem unavailable")
    child = tmp_path / "child.cjs"
    child.write_text("const r=require('child_process').spawnSync(process.execPath,['-e','console.log(42)'],{encoding:'utf8'});if(r.error)throw r.error;process.stdout.write(r.stdout);process.exit(r.status)")
    batch = tmp_path / "bridge.cmd"
    batch.write_text(f'@"{node}" "{child}"\r\n', encoding="utf-8")
    script = tmp_path / "parent.cjs"
    script.write_text("const r=require('child_process').spawnSync(process.argv[2],['/d','/c',process.argv[3]],{encoding:'utf8'});if(r.error)throw r.error;process.stdout.write(r.stdout);process.stderr.write(r.stderr);process.exit(r.status)")
    policy = _policy(tmp_path, app_config)
    result = policy.processes.run([node, str(script), str(cmd32), str(batch)], cwd=tmp_path,
                                   timeout_seconds=30, sandbox=policy.shell_sandbox_request())
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == b"42"


@WINDOWS
def test_cache_cannot_be_written_even_under_an_authorized_parent(tmp_path: Path, app_config, monkeypatch, node: str) -> None:
    work, allowed = tmp_path / "work", tmp_path / "allowed"
    work.mkdir(); allowed.mkdir()
    cache = allowed / "components"
    monkeypatch.setattr(win_ipc, "_BUNDLES", {})

    def create_cache(*args, **kwargs):
        cache.mkdir()
        return str(cache)

    monkeypatch.setattr(win_ipc.tempfile, "mkdtemp", create_cache)
    try:
        assert win_ipc.prepare_bundle() == cache
        before = (cache / "manifest.json").read_bytes()
        policy = _policy(work, app_config, grants={normalize_grant_path(allowed): "read_write"})
        script = work / "protect.cjs"
        script.write_text("""
const fs=require('fs'),path=require('path');const root=process.argv[2];let denied=0;
for(const file of ['manifest.json','new.dll','taskkill.cmd']){try{fs.writeFileSync(path.join(root,file),'modified')}catch(e){if(!['EPERM','EACCES','EBUSY'].includes(e.code))throw e;denied++}}
try{fs.renameSync(root,root+'-moved')}catch(e){if(!['EPERM','EACCES','EBUSY'].includes(e.code))throw e;denied++}
console.log(denied);process.exit(denied===4?0:1);
""")
        result = policy.processes.run([node, str(script), str(cache)], cwd=work, timeout_seconds=30,
                                       sandbox=policy.shell_sandbox_request())
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == b"4"
        assert (cache / "manifest.json").read_bytes() == before
        assert not (cache / "new.dll").exists()
    finally:
        for bundle in win_ipc._BUNDLES.values():
            for handle in bundle.handles:
                win_sandbox._kernel32.CloseHandle(handle)


@WINDOWS
def test_explicit_pipe_security_descriptor_is_unchanged(tmp_path: Path, app_config) -> None:
    script = tmp_path / "custom_pipe.py"
    script.write_text(r'''
import ctypes, json, os
from ctypes import wintypes
advapi = ctypes.WinDLL('advapi32', use_last_error=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
kernel.CreateNamedPipeW.restype = wintypes.HANDLE
advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.LocalFree.argtypes = [ctypes.c_void_p]
class SA(ctypes.Structure):
    _fields_ = [('size',wintypes.DWORD), ('descriptor',ctypes.c_void_p), ('inherit',wintypes.BOOL)]
sd = ctypes.c_void_p()
assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:(A;;FA;;;OW)',1,ctypes.byref(sd),None)
sa = SA(ctypes.sizeof(SA),sd,False)
pipe = kernel.CreateNamedPipeW(r'\\.\pipe\chaiai-custom-'+str(os.getpid()),3|0x80000,0,1,4096,4096,0,ctypes.byref(sa))
assert pipe != ctypes.c_void_p(-1).value, ctypes.get_last_error()
actual = ctypes.c_void_p()
assert advapi.GetSecurityInfo(pipe,6,4,None,None,None,None,ctypes.byref(actual)) == 0
text = wintypes.LPWSTR()
assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(actual,1,4,ctypes.byref(text),None)
print(json.dumps(text.value))
kernel.LocalFree(text);kernel.LocalFree(actual);kernel.LocalFree(sd);kernel.CloseHandle(pipe)
''', encoding="utf-8")
    policy = _policy(tmp_path, app_config)
    result = policy.processes.run([sys.executable, str(script)], cwd=tmp_path, timeout_seconds=30,
                                   sandbox=policy.shell_sandbox_request())
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == "D:(A;;FA;;;OW)"


@WINDOWS
def test_persistent_node_stdin_can_spawn_pipe_children(tmp_path: Path, app_config, node: str) -> None:
    script = tmp_path / "interactive.cjs"
    script.write_text("""
require('readline').createInterface({input:process.stdin}).on('line',line=>{
const r=require('child_process').spawnSync(process.execPath,['-e','process.stdout.write(process.argv[1])',line],{encoding:'utf8'});
if(r.error)throw r.error;console.log('child:'+r.stdout);
});
""")
    session = TerminalSession(_policy(tmp_path, app_config), f'"{node}" "{script}"', ".")
    try:
        session.write("first\n", False)
        first = session.read(30000, 10)
        assert "child:first" in first["stdout"] and first["running"]
        session.write("second\n", True)
        output = ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            last = session.read(30000, .2)
            output += last["stdout"]
            if not last["running"]:
                break
        assert "child:second" in output and "child:first" not in output
        assert last["exit_code"] == 0
    finally:
        session.close()


@WINDOWS
def test_ancestor_metadata_updates_only_the_object(tmp_path: Path, monkeypatch) -> None:
    ancestor = tmp_path / "ancestor"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    sibling = ancestor / "private.txt"
    sibling.write_text("unchanged")
    before = {item: win_sandbox._dacl_sddl(str(item)) for item in (workspace, sibling)}
    assert all(before.values())

    def forbidden(*args):
        pytest.fail("ancestor metadata must not trigger recursive ACL propagation")

    monkeypatch.setattr(win_sandbox._advapi32, "SetNamedSecurityInfoW", forbidden)
    user = win_sandbox._current_user_sid_text()
    win_sandbox._grant_ancestor_metadata(workspace, user)
    assert win_sandbox._dacl_has_access(str(ancestor), user, ace_type="A", permissions=0x001200A0,
                                       require_inheritance=False)
    assert {item: win_sandbox._dacl_sddl(str(item)) for item in before} == before


@WINDOWS
def test_initialization_failure_does_not_release_the_command(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "must-not-run.txt"

    def fail(*args, **kwargs):
        raise win_sandbox.SandboxUnavailable("injected initialization failure")

    monkeypatch.setattr(win_ipc, "attach", fail)
    with pytest.raises(win_sandbox.SandboxUnavailable, match="initialization failure"):
        win_sandbox.run_sandboxed(
            [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('wrong')", str(marker)],
            writable_sids=(),
        )
    assert not marker.exists()


@WINDOWS
@pytest.mark.parametrize("cancel", [False, True])
def test_node_descendants_stop_on_timeout_or_cancel(tmp_path: Path, app_config, node: str, cancel: bool) -> None:
    marker = tmp_path / "heartbeat.txt"
    child = tmp_path / "child.cjs"
    child.write_text("const fs=require('fs');setInterval(()=>fs.appendFileSync(process.argv[2],'x'),50)")
    parent = tmp_path / "parent.cjs"
    parent.write_text("require('child_process').fork(process.argv[2],[process.argv[3]],{silent:true});setInterval(()=>{},1000)")
    token = CancellationToken()
    registry = ProcessRegistry(token)
    policy = _policy(tmp_path, app_config)
    results = []
    thread = threading.Thread(target=lambda: results.append(registry.run(
        [node, str(parent), str(child), str(marker)], cwd=tmp_path, timeout_seconds=3 if not cancel else 20,
        sandbox=policy.shell_sandbox_request(),
    )))
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(.05)
    assert marker.exists(), "descendant did not start"
    if cancel:
        token.cancel("native IPC cancellation test")
    thread.join(timeout=20)
    assert not thread.is_alive() and results
    assert results[0].cancelled if cancel else results[0].timed_out
    size = marker.stat().st_size
    time.sleep(.4)
    assert marker.stat().st_size == size
