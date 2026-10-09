"""三档固定任务集的自洽性（v0.5 T56 的验收判据之一）。

这些用例**不调用模型、不联网**：它们守的是任务集能不能成立——
固定任务集如果要求了该档位没有的能力，那它就是"注定做不到的任务"，跑出来的失败会被误读成
"这个档位有问题"。所以自洽性必须机械地守住（架构 §16.1 的能力边界、§16.6 的对外解释口径）。
"""

from __future__ import annotations

from local_agent.capabilities import CAPABILITY_TOOLS, PROFILES
from local_agent.profile_tasks import PROFILE_TASK_SETS, select_tasks


def test_every_builtin_profile_has_a_task_set() -> None:
    """三档内置 Profile 各一套；新增档位必须同时登记（否则"每档可跑通"就无从谈起）。"""
    assert set(PROFILE_TASK_SETS) == set(PROFILES)


def test_tasks_only_require_capabilities_their_profile_declares() -> None:
    """任务需要的能力必须落在**本档位声明的能力**与能力闭集之内。"""
    for profile, task_set in PROFILE_TASK_SETS.items():
        declared = set(PROFILES[profile])
        for task in task_set.tasks:
            assert set(task.needs) <= declared, f"{profile} 的任务要求了本档没有的能力：{task.text}"
            assert set(task.needs) <= set(CAPABILITY_TOOLS), f"能力名必须在闭集内：{task.needs}"


def test_each_set_keeps_the_point_of_its_profile() -> None:
    """三档各自的"那一件事"必须留在集里——这是任务集存在的理由。"""
    coder = PROFILE_TASK_SETS["coder"]
    assert any(task.expects_write for task in coder.tasks), "默认档要有真正写文件的任务"
    # 会产生文件的那条任务，它的产物必须在核对清单里（否则那一轮会报"工作区没变"——不实）
    assert "notes_copy.md" in coder.watched_files

    researcher = PROFILE_TASK_SETS["researcher"]
    assert any(task.needs == ("web",) for task in researcher.tasks), "研究档要有联网任务"
    assert "reference.md" in researcher.seed_files, "检索任务要有可检索的种子文件"

    reviewer = PROFILE_TASK_SETS["reviewer"]
    # 只读档不得要求任何写类能力，并且要留一条"它做不到的事"：验收点是**如实说做不到**。
    assert all("files_write" not in task.needs for task in reviewer.tasks)
    assert any("改掉" in task.text for task in reviewer.tasks), "只读档要留一条越界请求"
    assert all(not task.expects_write for task in reviewer.tasks), "只读档任何一轮都不该改工作区"
    assert "notes_copy.md" in reviewer.watched_files, "只读档若真的写了文件，必须能被抓到"


def test_watched_files_cover_every_file_the_set_touches() -> None:
    """核对清单必须覆盖**种子文件与本档会写出的文件**。

    漏掉的后果不是报错而是"报了个假的 False"：跑器会说这一轮没改工作区，而实际上改了。
    种子文件是已知集合，这里机械守住；会写出的文件由上面那两条显式断言守住。
    """
    for profile, task_set in PROFILE_TASK_SETS.items():
        watched = set(task_set.watched_files)
        assert "notes.md" in watched, profile
        assert set(task_set.seed_files) <= watched, f"{profile} 的种子文件没全进核对清单"


def test_skip_reason_names_the_missing_capability() -> None:
    """缺能力时跳过，且必须说清缺的是**哪一个**（不是笼统一句"跳过"）。"""
    selected = select_tasks(
        PROFILE_TASK_SETS["researcher"], ("plan", "files_read", "code_search")
    )

    web_task = next(item for item in selected if item.task.needs == ("web",))
    assert web_task.runs is False
    assert "web" in (web_task.skip_reason or "")

    # 不要求任何能力的收尾自述一定会跑：跳过不该把整份任务集清空。
    assert any(item.runs and item.task.needs == () for item in selected)


def test_everything_runs_when_the_profile_has_all_it_needs() -> None:
    """能力齐备时不该跳过任何一轮（默认关闭的开关是在跑器里按有效能力集判定的）。"""
    for profile, task_set in PROFILE_TASK_SETS.items():
        selected = select_tasks(task_set, PROFILES[profile])
        assert all(item.runs for item in selected), profile


def test_task_sets_are_frozen_at_this_shape() -> None:
    """形状与条数一起钉住：改了任务文本就等于换了基线，历史结果不再可比（跑器已默认跑 baseline）。"""
    assert {profile: len(task_set.tasks) for profile, task_set in PROFILE_TASK_SETS.items()} == {
        "coder": 4,
        "researcher": 4,
        "reviewer": 4,
    }
