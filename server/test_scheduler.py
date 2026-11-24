"""调度器场景测试：不接设备、不调模型，只验证并发逻辑。

运行：
  python -m server.test_scheduler

覆盖 10 个场景：
  1. 两台设备并行跑两个不同游戏
  2. 两个游戏 + 三个任务 → 2 跑 1 排队
  3. 同一个游戏两个任务 → 串行（game_locked），另一台设备空着
  4. 排队中取消 → 立即移除，不占设备
  5. 运行中取消 → 任务中断并释放设备
  6. 设备掉线（运行中）→ 任务标记 interrupted
  7. 设备掉线（空闲）→ 不再接受新任务
  8. 队列满 → 抛 BusyError
  9. 单设备兼容 → 两个任务串行
 10. 等待原因与排队位置正确
"""
from __future__ import annotations

import sys
import time

# Windows 控制台默认 GBK，输出 ✓ 会直接抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from server.devices import fake_devices  # noqa: E402
from server.executor import FakeExecutor  # noqa: E402
from server.scheduler import BusyError, Scheduler  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str):
    def wrapper(func):
        global PASSED, FAILED
        try:
            func()
            PASSED += 1
            print(f"  ✓ {name}")
        except AssertionError as error:
            FAILED += 1
            print(f"  ✗ {name}\n      {error}")
        return func

    return wrapper


def make_scheduler(count: int = 2, duration: float = 0.4, max_parallel: int | None = None):
    devices = fake_devices(
        [{"serial": f"dev-{'abcdefgh'[index]}", "alias": f"设备{index + 1}"} for index in range(count)]
    )
    executor = FakeExecutor(duration=duration)
    scheduler = Scheduler(
        devices,
        executor,
        max_parallel=max_parallel if max_parallel is not None else count,
    )
    return scheduler, executor


@check("1. 两台设备并行跑两个不同游戏")
def case_parallel():
    scheduler, _ = make_scheduler()
    first = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    second = scheduler.start("smoke", ["b.yaml"], game_key="douyin/two")

    time.sleep(0.15)
    assert first.status in ("assigned", "running"), f"任务1 状态 {first.status}"
    assert second.status in ("assigned", "running"), f"任务2 状态 {second.status}"
    assert first.device != second.device, "两个任务应落在不同设备"

    assert scheduler.wait_idle(3), "任务未全部结束"
    assert first.status == "passed" and second.status == "passed"


@check("2. 三个游戏任务 → 两个并行、一个排队")
def case_queue():
    scheduler, _ = make_scheduler()
    jobs = [
        scheduler.start("smoke", [f"{name}.yaml"], game_key=f"douyin/{name}")
        for name in ("one", "two", "three")
    ]

    time.sleep(0.15)
    running = [job for job in jobs if job.status in ("assigned", "running")]
    queued = [job for job in jobs if job.status == "queued"]
    assert len(running) == 2, f"应有 2 个在跑，实际 {len(running)}"
    assert len(queued) == 1, f"应有 1 个排队，实际 {len(queued)}"
    assert queued[0].queue_position == 1, f"排队位置 {queued[0].queue_position}"

    assert scheduler.wait_idle(5), "任务未全部结束"
    assert all(job.status == "passed" for job in jobs)


@check("3. 同一游戏两个任务 → 串行，另一台设备空闲")
def case_same_game_serialized():
    scheduler, _ = make_scheduler()
    first = scheduler.start("smoke", ["a.yaml"], game_key="douyin/same")
    second = scheduler.start("smoke", ["b.yaml"], game_key="douyin/same")

    time.sleep(0.15)
    assert first.status == "running", f"第一个应运行中，实际 {first.status}"
    assert second.status == "queued", f"第二个应排队，实际 {second.status}"
    assert second.wait_reason == "game_locked", f"等待原因 {second.wait_reason}"
    assert second.blocking_game == "douyin/same", f"阻塞游戏 {second.blocking_game}"

    idle = [device for device in scheduler.snapshot()["devices"] if device["status"] == "idle"]
    assert len(idle) == 1, f"应有一台设备空闲，实际 {len(idle)}"

    assert scheduler.wait_idle(5), "任务未全部结束"
    assert second.status == "passed"


@check("4. 排队中取消 → 移除且不占设备")
def case_cancel_queued():
    scheduler, executor = make_scheduler(duration=0.6)
    first = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    second = scheduler.start("smoke", ["b.yaml"], game_key="douyin/two")
    third = scheduler.start("smoke", ["c.yaml"], game_key="douyin/three")

    time.sleep(0.1)
    assert third.status == "queued", f"第三个应排队，实际 {third.status}"

    scheduler.cancel(third.run_id)
    assert third.status == "cancelled", f"取消后状态 {third.status}"
    assert third.finished_at is not None
    assert scheduler.snapshot()["queue_depth"] == 0, "队列应已清空"

    assert scheduler.wait_idle(5)
    assert first.status == "passed" and second.status == "passed"
    assert len(executor.calls) == 2, f"被取消的任务不应执行，实际执行 {len(executor.calls)} 次"


@check("5. 运行中取消 → 中断并释放设备")
def case_cancel_running():
    scheduler, _ = make_scheduler(count=1, duration=2.0)
    job = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    time.sleep(0.2)
    assert job.status == "running", f"应为运行中，实际 {job.status}"

    scheduler.cancel(job.run_id)
    assert scheduler.wait_idle(5), "取消后未结束"
    assert job.status == "cancelled", f"取消后状态 {job.status}"
    assert job.finished_at is not None

    device = scheduler.snapshot()["devices"][0]
    assert device["status"] == "idle", f"设备应释放为空闲，实际 {device['status']}"


@check("6. 设备运行中掉线 → 任务 interrupted 并释放")
def case_device_offline_running():
    scheduler, _ = make_scheduler(count=1, duration=2.0)
    job = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    time.sleep(0.2)
    assert job.status == "running", f"应为运行中，实际 {job.status}"

    scheduler.set_device_status(job.device, "offline")
    assert scheduler.wait_idle(5), "掉线后任务未结束"
    assert job.status == "interrupted", f"应标记 interrupted，实际 {job.status}"
    assert job.error and "掉线" in job.error, f"错误信息 {job.error}"


@check("7. 设备离线后不再接受新任务")
def case_device_offline_idle():
    scheduler, _ = make_scheduler()
    snapshot = scheduler.snapshot()["devices"]
    offline_serial = snapshot[0]["serial"]
    scheduler.set_device_status(offline_serial, "offline")

    job = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    time.sleep(0.15)
    assert job.device != offline_serial, "不应派给离线设备"
    assert job.device == snapshot[1]["serial"], f"应派给在线设备，实际 {job.device}"
    assert scheduler.wait_idle(3)


@check("8. 队列满 → 抛 BusyError")
def case_queue_full():
    scheduler, _ = make_scheduler(count=1, duration=1.0, max_parallel=1)
    scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    scheduler.start("smoke", ["b.yaml"], game_key="douyin/two")
    scheduler.start("smoke", ["c.yaml"], game_key="douyin/three")
    try:
        scheduler.start("smoke", ["d.yaml"], game_key="douyin/four")
    except BusyError:
        pass
    else:
        raise AssertionError("第四个任务应触发队列已满")
    assert scheduler.wait_idle(6)


@check("9. 单设备兼容 → 两个任务串行")
def case_single_device():
    scheduler, executor = make_scheduler(count=1, duration=0.2, max_parallel=1)
    first = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    second = scheduler.start("smoke", ["b.yaml"], game_key="douyin/two")

    time.sleep(0.1)
    running = [job for job in (first, second) if job.status in ("assigned", "running")]
    assert len(running) <= 1, "单设备不应并行"

    assert scheduler.wait_idle(5)
    assert first.started_at and second.started_at
    assert second.started_at >= first.finished_at - 0.05, "任务应串行执行"


@check("10. 等待原因与排队位置")
def case_wait_reason():
    scheduler, _ = make_scheduler(duration=0.6)
    first = scheduler.start("smoke", ["a.yaml"], game_key="douyin/one")
    second = scheduler.start("smoke", ["b.yaml"], game_key="douyin/two")
    third = scheduler.start("smoke", ["c.yaml"], game_key="douyin/one")

    time.sleep(0.1)
    # third 与 first 同游戏，应显示 game_locked
    assert third.status == "queued", f"第三个应排队，实际 {third.status}"
    assert third.wait_reason == "game_locked", f"等待原因 {third.wait_reason}"
    assert third.queue_position == 1, f"排队位置 {third.queue_position}"

    # second 在另一台设备上跑，与 first 不冲突
    assert second.status in ("assigned", "running"), f"第二个应运行，实际 {second.status}"

    assert scheduler.wait_idle(6)


def main() -> int:
    print("调度器场景测试（假执行器，不接设备）\n")
    for case in (
        case_parallel,
        case_queue,
        case_same_game_serialized,
        case_cancel_queued,
        case_cancel_running,
        case_device_offline_running,
        case_device_offline_idle,
        case_queue_full,
        case_single_device,
        case_wait_reason,
    ):
        case()

    print(f"\n通过 {PASSED} 个，失败 {FAILED} 个")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
