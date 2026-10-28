# DataHub_v2.0版（dataclass 显式字段版，2026-10-28）
#
# 只做一件事：把 JSON 配置变成内存里的数据结构。
#
# 与 v1.1 的区别：存储从"列式字典"换成 dataclass 对象——
#   Signal 类里把每个字段都显式写出来，能看到"一条信号到底有哪些数据"
#
# 程序与 JSON 之间只有两条约定：
#   1. 每条信号必须有 "id"，且全局唯一 —— 读写时用它查找
#   2. 必须有一个 devices 数组 —— 设备层的字段（device_name / api）
#      下放到该设备的每一条信号上，信号层可以写同名字段覆盖
#
# 取舍说明（重要）：
#   字段在 Signal 类里显式定义 = "看得见的字段清单"；
#   代价是 JSON 新增字段时必须在 Signal 类里同步加一行（下面"新增字段怎么改"有清单）。
#   加载时遇到类里没有的字段会直接报错，不静默丢弃
#   不会出现"JSON 里明明写了，界面上却没有"这种查不出来的问题。
#   enabled 是普通数据（null=待定 / true=启用 / false=停用），
#   加载不跳过任何信号 —— 要不要采集由驱动自己读这一列决定。

import json
import os
import threading
import time
from dataclasses import dataclass, field, fields, asdict


# ============================================================
# 一、数据结构定义（字段显式声明）
# ============================================================
#
# ---------- 新增一个 JSON 字段，要改哪些代码 ----------
#   例：规约里多了一个"安装位置"，想让它在界面上显示出来
#
#   1. signals_config.json：每条信号加上  "position": "左前腿髋关节"
#   2. 下面 Signal 类里加一行  position: str = ""
#      （只有这一处是必须的；加在哪个位置，列的显示顺序就是那个位置）
#   3. 本文件其余地方不用动：列名、行字典、快照都是从 Signal 类自动取的
#   4. 界面要显示 → UI 层的表格加一列；导出 CSV → 导出表头加一项
#      驱动要用它 → 直接 s.position 点出来
#
#   注意：第 1 步做了、第 2 步忘了，加载时会报错提醒。
# ------------------------------------------------------

@dataclass
class Signal:
    # —— 一条信号的全部数据，字段清单在这里 ——
    id: str                                   # 必填，全局唯一，读写查找键
    enabled: bool | None = None               # null=待定 / true=启用 / false=停用
    ch: int | None = None                     # UT3200 通道号（其他设备为 None）
    name: str = ""                            # 展示名
    unit: str = ""                            # 单位
    min: float | None = None                  # 量程下限
    max: float | None = None                  # 量程上限
    fmt: str = ""                             # 显示格式，如 "{:.1f}"
    device_name: str = ""                     # 所属设备（加载时从设备层下放）
    api: str = ""                             # 所属设备的采集函数名（同样下放）
    value: float | str | None = None          # 最新值（运行时由驱动写入）
    ts: str = ""                              # 最新值的时间戳（年-月-日 时:分:秒.毫秒）


@dataclass
class Device:
    # —— 一个设备 = 设备级公共字段 + 自己的信号列表 ——
    device_name: str
    api: str = ""
    signals: list[Signal] = field(default_factory=list)


_SIGNAL_FIELDS = {f.name for f in fields(Signal)}   # 显式字段名集合（只算一次）：集合查某个名字在不在里面极快，而且自动去重


# ============================================================
# 二、读 JSON -> Device / Signal 对象（树形结构）
# ============================================================

def load_signals(json_path):
    # 只认设备分组写法 {"devices": [ {..., "signals": [ {...} ]} ]}
    if not os.path.exists(json_path):
        raise FileNotFoundError("配置文件不存在: " + json_path)

    with open(json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if "devices" not in cfg:
        raise ValueError("配置缺少 devices 数组（只支持设备分组写法）: " + json_path)

    known = _SIGNAL_FIELDS                      # Signal 里显式定义过的字段名

    devices = []
    seen = set()                                # id 必填且唯一
    for dev in cfg["devices"]:
        d = Device(device_name=dev.get("device_name", ""),
                   api=dev.get("api", ""))
        for sig in dev.get("signals", []):
            sid = sig.get("id")
            if not sid:
                raise ValueError("%s 下有一条信号缺少 id 字段" % d.device_name)
            if sid in seen:
                raise ValueError("信号 id 重复: " + repr(sid))
            seen.add(sid)

            s = Signal(id=sid)                  # 先给必填的 id，其余逐个填
            for k, v in sig.items():
                if k == "id":
                    continue
                if k not in known:
                    raise ValueError("信号 %r 的字段 %r 在 Signal 类里没有定义，"
                                     "请在 Signal 里加一行（例如 %s: str = \"\"）"
                                     % (sid, k, k))
                setattr(s, k, v)                # 显式字段：直接赋给对象属性——真正使用的是 s 这个对象，字典只是中间桥梁
            # 设备层字段下放（信号层若写了同名字段，上面已覆盖设备层）
            if not s.device_name:
                s.device_name = d.device_name
            if not s.api:
                s.api = d.api
            d.signals.append(s)
        devices.append(d)

    return devices


# ============================================================
# 三、DataHub —— 全局变量存储 + 读写 API
# ============================================================

class DataHub:
    # 内部把 Device 树摊平成一行行 Signal；对外提供按 id 的读写

    def __init__(self, devices):
        rows = []
        for dev in devices:
            for sig in dev.signals:
                rows.append(sig)                # 树 -> 平铺（device_name/api 已在信号上）
        if not rows:
            raise ValueError("信号列表为空：请检查 JSON 配置")
        self._rows = rows                       # 全局变量本体：list[Signal]
        self._n = len(rows)
        self._id_to_idx = {s.id: i for i, s in enumerate(rows)}  # id -> 下标
        self._lock = threading.Lock()

    # ---------------- 写（驱动层调用）--根据实际使用后续修改----------------

    def write(self, idx_or_id, value):
        idx = self._resolve_idx(idx_or_id)
        with self._lock:
            self._rows[idx].value = value
            self._rows[idx].ts = self._now()

    def write_batch(self, updates):
        # updates: {信号id: 值}，整批共用一个时间戳
        ts = self._now()
        with self._lock:
            for key, val in updates.items():
                idx = self._id_to_idx.get(key)
                if idx is not None:
                    self._rows[idx].value = val
                    self._rows[idx].ts = ts

    # ---------------- 读（UI / BLL 调用）--根据实际使用后续修改----------------

    def signal(self, idx_or_id):
        # 直接拿 Signal 对象本体：hub.signal("ut3200_ch1").value
        # 注意：拿到的是全局变量本身，改它 = 改全局变量
        idx = self._resolve_idx(idx_or_id)
        return self._rows[idx]

    def column_names(self):
        # 列名 = Signal 类里定义的字段，按类里的定义顺序
        return [f.name for f in fields(Signal)]

    def col(self, name):
        with self._lock:
            return [self._get_cell(s, name) for s in self._rows]

    def get(self, idx_or_id, column):
        idx = self._resolve_idx(idx_or_id)
        with self._lock:
            return self._get_cell(self._rows[idx], column)

    def row(self, idx_or_id):
        # 整行读成 dict，如 hub.row("ut3200_ch1")["unit"]
        idx = self._resolve_idx(idx_or_id)
        with self._lock:
            return self._row_dict(self._rows[idx])

    def snapshot(self):
        # 按列：{列名: [N 个值]}
        with self._lock:
            return {name: [self._get_cell(s, name) for s in self._rows]
                    for name in self.column_names()}

    def snapshot_rows(self):
        # 按行：[{...}, {...}]
        with self._lock:
            return [self._row_dict(s) for s in self._rows]

    def iter_rows(self):
        with self._lock:
            for s in self._rows:
                yield self._row_dict(s)

    def get_ids(self):
        return [s.id for s in self._rows]

    @property
    def signal_count(self):
        return self._n

    # ---------------- 内部 ----------------

    def _resolve_idx(self, idx_or_id):
        # 既接受序号 int，也接受信号 id 字符串，都换算成内部下标
        if isinstance(idx_or_id, int):
            return idx_or_id
        if isinstance(idx_or_id, str):
            idx = self._id_to_idx.get(idx_or_id)
            if idx is None:
                raise KeyError("未知信号 id: " + repr(idx_or_id))
            return idx
        raise TypeError("应为 int 或 str，收到 " + str(type(idx_or_id)))

    @staticmethod
    def _get_cell(s, column):
        # 取一个格子；字段不存在就报 KeyError（列清单 = Signal 类的字段）
        if column in _SIGNAL_FIELDS:
            return getattr(s, column)
        raise KeyError("不存在的列: " + repr(column))

    @staticmethod
    def _row_dict(s):
        return asdict(s)

    @staticmethod
    def _now():
        # 时间戳字符串：年-月-日 时:分:秒.毫秒，UI 直显、CSV 直出
        t = time.time()
        return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
                + ".%03d" % int((t - int(t)) * 1000))


# ------------------------------------------------------------
# 全局单例
# ------------------------------------------------------------

_hub = None


def init_datahub(config_path):
    # 启动时调一次：读 JSON 建仓，不启动任何线程
    global _hub
    _hub = DataHub(load_signals(config_path))
    return _hub


def get_hub():
    if _hub is None:
        raise RuntimeError("DataHub 未初始化，请先调用 init_datahub()")
    return _hub


# ===================== 展示 =====================

SIGNALS_CONFIG_PATH = "signals_config.json"

if __name__ == "__main__":
    hub = init_datahub(SIGNALS_CONFIG_PATH)

    # —— 打印数据结构 ——
    print("信号总数:", hub.signal_count)
    print("Signal 字段清单:", [f.name for f in fields(Signal)])
    print("信号 id 列表:", hub.get_ids())
    print("列名顺序:", hub.column_names())

    print("\n=== 对象式访问（dataclass 的好处）===")
    s = hub.signal("ut3200_ch1")
    print("hub.signal('ut3200_ch1') ->", s)
    print("  s.name =", s.name, "| s.ch =", s.ch, "| s.value =", s.value)

    print("\n=== 全局变量表（按列）===")
    for name, col in hub.snapshot().items():
        # 每列只打前 3 个值，避免刷屏
        print("%-12s: %s" % (name, col[:3] + ["..."] if len(col) > 3 else col))

    print("\n=== 全局变量表（按行，共 %d 行）===" % hub.signal_count)
    for row in hub.snapshot_rows():
        print(row)
