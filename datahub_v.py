# DataHub_v3.0（dataclass 列表版，2026-10-29）
#
# 只做一件事：把 JSON 配置读成内存里的 list[Device]。
#
# 与 v2.0 的区别：不再有 DataHub 类。
#   初始化完成后留在全局空间的，就是 Device 列表本身（Device.signals 里是一堆
#   Signal 对象）。读写都是直接操作这些 dataclass 对象
#
# 程序与 JSON 之间只有三条约定：
#   1. 每个设备必须有 "id"，且设备之间唯一
#   2. 每条信号必须有 "id"，且全项目唯一 —— 按 id 查找时用
#   3. 必须有一个 devices 数组
#
# 取舍说明（重要）：
#   字段在类里显式定义 = "看得见的字段清单"；
#   代价是 JSON 新增字段时必须在类里同步加一行（下面"新增字段怎么改"有清单）。
#   加载时遇到类里没有的字段会直接报错，不静默丢弃——
#   不会出现"JSON 里明明写了，界面上却没有"这种查不出来的问题。
#   enabled 是普通数据（null=待定 / true=启用 / false=停用），
#   加载不跳过任何信号 —— 要不要采集由驱动自己读这一列决定。
#
#   ai提示词：不再使用datahub这个类。读json初始化完成后，留在全局空间的应该是一个dataclass Device类的列表。
#   ai模型：Hy4 preview——20261029-14:25
import json
import os
import threading
from dataclasses import dataclass, field, fields


# ============================================================
# 一、数据结构定义（字段显式声明）
# ============================================================
#
# ---------- 新增一个 JSON 字段，要改哪些代码 ----------
#   信号级字段（例：规约里多了一个"安装位置"）：
#     1. signals_config.json：每条信号加上  "position": "左前腿髋关节"
#     2. 下面 Signal 类里加一行  position: str = ""
#        （只有这一处是必须的；加在哪个位置，列的显示顺序就是那个位置）
#     3. 本文件其余地方不用动：列名、行字典、快照都是从类自动取的
#     4. 界面要显示 → UI 加一列；驱动要用 → s.position 点出来
#
#   设备级字段（例：每个设备多一个"安装位置"）：
#     1. signals_config.json：设备层加上这个字段
#     2. Device 类里加一行；它会自动摊进 row()/snapshot() 的每一行
#
#   注意：JSON 加了、类忘了加，加载时会报错提醒。
# ------------------------------------------------------

@dataclass
class Device:
    # —— 一个设备 = 设备级字段 + 自己的信号列表 ——
    id: str                                   # 必填，设备之间唯一，设备查找键
    name: str = ""                            # 展示名（界面/日志显示用）
    api: str = ""                             # 本设备信号的采集函数名
    signals: list["Signal"] = field(default_factory=list)


@dataclass
class Signal:
    # —— 一条信号自己的数据；设备级的 device_name / api 不在这里，
    #    用 device_of(devices, 信号id) 找到所属设备后再取 ——
    id: str                                   # 必填，全项目唯一，读写查找键
    enabled: bool | None = None               # null=待定 / true=启用 / false=停用
    ch: int | None = None                     # UT3200 通道号（其他设备为 None）
    name: str = ""                            # 展示名
    unit: str = ""                            # 单位
    min: float | None = None                  # 量程下限
    max: float | None = None                  # 量程上限
    fmt: str = ""                             # 显示格式，如 "{:.1f}"
    value: float | str | None = None          # 最新值（运行时由驱动写入）
    ts: str = ""                              # 最新值的时间戳（年-月-日 时:分:秒.毫秒）


_SIGNAL_FIELDS = {f.name for f in fields(Signal)}   # 信号字段名集合（只算一次）
_DEVICE_FIELDS = {f.name for f in fields(Device)}   # 设备字段名集合（只算一次）


# ============================================================
# 二、读 JSON -> list[Device]（初始化时跑一次）
# ============================================================

def load_devices(json_path):
    # 只认设备分组写法 {"devices": [ {..., "signals": [ {...} ]} ]}
    if not os.path.exists(json_path):
        raise FileNotFoundError("配置文件不存在: " + json_path)

    with open(json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if "devices" not in cfg:
        raise ValueError("配置缺少 devices 数组（只支持设备分组写法）: " + json_path)

    devices = []
    seen_dev = set()                            # 设备 id 必填且唯一
    seen_sig = set()                            # 信号 id 全项目唯一
    for dev in cfg["devices"]:
        did = dev.get("id")
        if not did:
            raise ValueError("有一个设备缺少 id 字段")
        if did in seen_dev:
            raise ValueError("设备 id 重复: " + repr(did))
        seen_dev.add(did)
        for k in dev:
            if k not in _DEVICE_FIELDS and k != "signals":
                raise ValueError("设备 %r 的字段 %r 在 Device 类里没有定义，"
                                 "请在 Device 里加一行（例如 %s: str = \"\"）"
                                 % (did, k, k))
        d = Device(id=did, name=dev.get("name", ""), api=dev.get("api", ""))

        for sig in dev.get("signals", []):
            sid = sig.get("id")
            if not sid:
                raise ValueError("设备 %s 下有一条信号缺少 id 字段" % d.id)
            if sid in seen_sig:
                raise ValueError("信号 id 重复: " + repr(sid))
            seen_sig.add(sid)

            s = Signal(id=sid)                  # 先给必填的 id，其余逐个填
            for k, v in sig.items():
                if k == "id":
                    continue
                if k not in _SIGNAL_FIELDS:
                    raise ValueError("信号 %r 的字段 %r 在 Signal 类里没有定义，"
                                     "请在 Signal 里加一行（例如 %s: str = \"\"）"
                                     % (sid, k, k))
                setattr(s, k, v)                # 显式字段：直接赋给对象属性
            d.signals.append(s)
        devices.append(d)

    return devices


# ============================================================
# 三、全局变量：初始化完成后留在内存里的就是这一个列表
# ============================================================

_DEVICES = []                                   # list[Device]
_LOCK = threading.Lock()                        # 驱动线程写、界面线程读，共用一把锁


def init_devices(json_path):
    # 启动时调一次：读 JSON → list[Device] 存进全局，不启动任何线程
    global _DEVICES
    with _LOCK:
        _DEVICES = load_devices(json_path)
    return _DEVICES


def get_devices():
    # 取全局的 Device 列表（拿到的是本体，改它 = 改全局变量）
    if not _DEVICES:
        raise RuntimeError("未初始化，请先调用 init_devices(配置文件路径)")
    return _DEVICES

# ---------------- 读（UI / BLL / CSV 调用）----------------

def signal_count(devices):
    return sum(len(d.signals) for d in devices)

# ===================== 展示 =====================

SIGNALS_CONFIG_PATH = "signals_config.json"

if __name__ == "__main__":
    # 初始化：读 JSON → 全局变量就是一个 Device 列表
    devices = init_devices(SIGNALS_CONFIG_PATH)

    print("=== 初始化后留在内存里的：list[Device] ===")
    print("共 %d 个设备、%d 条信号\n" % (len(devices), signal_count(devices)))
    for d in devices:
        print("  [%s]  id=%-8s api=%-20s 共 %d 个信号"
              % (d.name, d.id, d.api, len(d.signals)))
