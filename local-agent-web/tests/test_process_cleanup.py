"""Real process cleanup and refusal to cross the command's sandbox Job."""
import json
import os
import shutil
import time

import pytest

from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_cleanup import _WindowsProcesses, parse_arguments, shim_script
from local_agent.security.process_guard import ProcessRegistry


@pytest.mark.parametrize("args,expected", [
    (["/pid", "123", "/T", "/F"], (123, True)),
    (["/F", "/PID", "42"], (42, False)),
])
def test_supported_pid_arguments(args, expected):
    assert parse_arguments(args) == expected


@pytest.mark.parametrize("args", [
    [], ["/pid", "1"], ["/pid", "0", "/f"], ["/pid", "-1", "/f"],
    ["/pid", "4294967296", "/f"], ["/pid", "１", "/f"],
    ["/pid", "1&whoami", "/f"], ["/im", "node.exe", "/f"],
    ["/s", "other", "/pid", "1", "/f"], ["/pid", "1", "/pid", "2", "/f"],
])
def test_rejects_unbounded_or_ambiguous_selectors(args):
    with pytest.raises(ValueError):
        parse_arguments(args)


def test_shim_handles_source_and_frozen_entries(monkeypatch):
    assert b'"%CHAIAI_CLEANUP_MODULE%"' in shim_script()
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert b"--internal-sandbox-taskkill" in shim_script()


WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Windows sandbox process cleanup")


def _policy(root, app_config, mode):
    return SecurityPolicy.from_config(app_config, workspace_root=root,
                                     external_policy=ExternalPathPolicy(mode=mode))


@WINDOWS
@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_taskkill_terminates_owned_child_and_grandchild(tmp_path, app_config, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    script = tmp_path / "cleanup.cjs"
    script.write_text(r'''
const cp=require('child_process');
const server=`const cp=require('child_process');const child=cp.spawn(process.execPath,['-e','setTimeout(()=>{},10000)'],{stdio:'ignore'});console.log(JSON.stringify({parent:process.pid,child:child.pid}));setTimeout(()=>{},10000);`;
const processHandle=cp.spawn(process.execPath,['-e',server],{stdio:['ignore','pipe','pipe']});
processHandle.stdout.once('data',data=>{
  const ids=JSON.parse(data.toString());
  const result=cp.spawnSync('taskkill /pid '+ids.parent+' /T /F',{shell:true,encoding:'utf8',windowsHide:true});
  console.log(JSON.stringify({...ids,status:result.status,stdout:result.stdout,stderr:result.stderr}));
  process.exitCode=result.status;
});
''', encoding="utf-8")
    policy = _policy(tmp_path, app_config, mode)
    started = time.monotonic()
    result = policy.processes.run([node, str(script)], cwd=tmp_path, timeout_seconds=6,
                                  sandbox=policy.shell_sandbox_request())
    assert not result.timed_out and result.exit_code == 0, (result.stdout, result.stderr)
    assert time.monotonic() - started < 6
    ids = json.loads(result.stdout)
    assert ids["status"] == 0 and "Terminated sandbox process" in ids["stdout"]
    kernel = _WindowsProcesses().kernel
    for pid in (ids["parent"], ids["child"]):
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 0) == 0
            finally:
                kernel.CloseHandle(handle)


@WINDOWS
def test_taskkill_refuses_process_outside_its_job(tmp_path, app_config):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    outside = ProcessRegistry()
    target = outside.spawn([node, "-e", "setTimeout(()=>{},10000)"], cwd=tmp_path)
    try:
        policy = _policy(tmp_path, app_config, "workspace-write")
        command = f"taskkill /pid {target.handle.pid} /T /F"
        result = policy.processes.run([os.environ["COMSPEC"], "/d", "/c", command],
            cwd=tmp_path, timeout_seconds=6, sandbox=policy.shell_sandbox_request())
        assert result.exit_code != 0 and not result.timed_out
        assert b"outside the caller's sandbox Job" in result.stderr
        assert target.handle.poll() is None
    finally:
        outside.terminate(target, reason="cleanup test-owned process")


@WINDOWS
def test_taskkill_refuses_helper_ancestors(tmp_path, app_config):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    script = "const r=require('child_process').spawnSync('taskkill /pid '+process.pid+' /T /F',{shell:true,encoding:'utf8',windowsHide:true});console.log(JSON.stringify({status:r.status,stderr:r.stderr}));"
    policy = _policy(tmp_path, app_config, "workspace-write")
    result = policy.processes.run([node, "-e", script], cwd=tmp_path, timeout_seconds=6,
                                  sandbox=policy.shell_sandbox_request())
    assert not result.timed_out and result.exit_code == 0
    response = json.loads(result.stdout)
    assert response["status"] == 1 and "ancestor" in response["stderr"]
