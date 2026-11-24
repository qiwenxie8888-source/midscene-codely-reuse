"""设备注册表：把 adb 看到的设备整理成调度器能用的状态。

只做三件事：
  - 解析 `adb devices` 的输出（serial + 状态）
  - 维护逻辑设备（别名、绑定的测试号、当前占用）
  - 提供假设备注入，让调度器在没有真机时也能被测试

WiFi 设备（192.168.x.x:5555）和 USB 设备在这里一视同仁，
区别只体现在"掉线概率"和"重连方式"上，由上层处理。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Iterable

# adb devices 里会出现、但不可用的状态
UNAVAILABLE_STATES = {"offline", "unauthorized", "no permissions", "unknown"}


@dataclass
class Device:
    """一台可被调度的设备。serial 是连接标识，alias 是给人看的名字。"""

    serial: str
    alias: str = ""
    account: str | None = None  # 绑定的测试号，用于账号互斥
    status: str = "idle"  # idle | busy | offline
    current_run: str | None = None
    game_key: str | None = None
    last_seen: float = field(default=0.0)

    def label(self) -> str:
        return self.alias or self.serial

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "alias": self.label(),
            "account": self.account,
            "status": self.status,
            "current_run": self.current_run,
            "game_key": self.game_key,
        }


def parse_adb_devices(output: str) -> list[tuple[str, str]]:
    """解析 `adb devices` 的文本，返回 [(serial, state)]。

    第一行是 "List of devices attached"，后面每行 "serial\\tstate"。
    空行和 "* daemon started" 之类的噪声直接跳过。
    """
    devices: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def list_adb_devices(timeout: float = 10.0) -> list[tuple[str, str]]:
    """调用 adb 列出当前连接的设备；adb 不可用时返回空列表，不抛异常。"""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_adb_devices(result.stdout or "")


def discover_devices(
    aliases: dict[str, dict] | None = None,
    previous: Iterable[Device] | None = None,
) -> list[Device]:
    """扫描设备并合并已有状态。

    aliases 里按 serial 配置别名和测试号，例如
        {"15430196920086O": {"alias": "设备A", "account": "test01"}}
    previous 用于保留"正在跑任务"的占用信息：设备临时消失再出现时，
    不能让状态回到 idle，否则会重复派活。
    """
    aliases = aliases or {}
    keep = {device.serial: device for device in (previous or [])}
    devices: list[Device] = []
    for serial, state in list_adb_devices():
        config = aliases.get(serial, {})
        old = keep.get(serial)
        device = Device(
            serial=serial,
            alias=config.get("alias") or (old.alias if old else serial),
            account=config.get("account") or (old.account if old else None),
        )
        if state in UNAVAILABLE_STATES:
            device.status = "offline"
            # 掉线时把占用信息交给上层处理，这里保留 current_run 便于定位
            device.current_run = old.current_run if old else None
            device.game_key = old.game_key if old else None
        elif old:
            # 在线设备沿用原有占用状态，避免把正在跑的设备重置成空闲
            device.status = old.status
            device.current_run = old.current_run
            device.game_key = old.game_key
        devices.append(device)
    return devices


def fake_devices(specs: list[dict]) -> list[Device]:
    """测试专用：直接构造设备列表，不碰 adb。

    specs 形如 [{"serial": "fake-1", "alias": "设备A", "status": "idle"}]
    """
    return [
        Device(
            serial=item["serial"],
            alias=item.get("alias", item["serial"]),
            account=item.get("account"),
            status=item.get("status", "idle"),
        )
        for item in specs
    ]
