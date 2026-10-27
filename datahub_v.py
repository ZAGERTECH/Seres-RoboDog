# DataHub_v1.1版（2026-10-27）
#
# 只做一件事：把 JSON 配置直接变成内存里的数据结构。
#
# 程序与 JSON 之间只有两条约定：
#   1. 每条信号必须有 "id"，且全局唯一 —— 读写时用它查找
#   2. 必须有一个 devices 数组 —— 设备层的字段（device_name / api ）
#      自动下放到该设备的每一条信号上，信号层可以写同名字段覆盖
#
# 除此之外不预设任何字段：JSON 里有什么字段，就自动生成什么列。
# "enabled" 现在是普通的数据列（null=待定 / true=启用 / false=停用），
# 加载时不再跳过任何信号 —— 要不要采集由驱动自己读这一列决定。
# 运行时再附加两个保留列 "value"（最新值）和 "ts"（时间戳）。
# 存储是列式的：_columns = {列名: [N 个值]}，一行 = 一条信号，只存最新一帧。

import json
import os
import threading
import time


# ------------------------------------------------------------
# 读 JSON -> 信号字典列表
# ------------------------------------------------------------

def load_signals(json_path):
    # 只认设备分组写法 {"devices": [ {..., "signals": [ {...} ]} ]}
    if not os.path.exists(json_path):
        raise FileNotFoundError("配置文件不存在: " + json_path)

    with open(json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if "devices" not in cfg:
        raise ValueError("配置缺少 devices 数组（只支持设备分组写法）: " + json_path)

    signals = []
    for dev in cfg["devices"]:
        # 设备层除 signals 之外的标量字段（device_name / api ...）下放给本设备每条信号
        common = {k: v for k, v in dev.items()
                  if k != "signals" and (isinstance(v, (str, int, float, bool)) or v is None)}
        for sig in dev.get("signals", []):
            row = dict(common)                  # 先放设备层的字段
            for k, v in sig.items():            # 再放信号层的字段，同名覆盖设备层
                if isinstance(v, (str, int, float, bool)) or v is None:
                    row[k] = v                  # dict/list 等嵌套值丢弃
            signals.append(row)

    seen = set()                            # id 必填且唯一
    for i, sig in enumerate(signals):
        sid = sig.get("id")
        if not sid:
            raise ValueError("第 %d 条信号缺少 id 字段" % i)
        if sid in seen:
            raise ValueError("信号 id 重复: " + repr(sid))
        seen.add(sid)

    return signals


# ------------------------------------------------------------
# 全局变量存储 + 读写 API
# ------------------------------------------------------------

DYNAMIC_COLS = ("value", "ts")   # 保留列名，JSON 里不要占用


class DataHub:
    # 行 = 信号，列 = JSON 字段 + value/ts

    def __init__(self, signals):
        if not signals:
            raise ValueError("信号列表为空：请检查 JSON 配置")
        self._n = len(signals)

        # 列名按 JSON 字段首次出现的顺序自动发现，再附加动态列
        col_names = []
        for sig in signals:
            for k in sig:
                if k not in col_names:
                    col_names.append(k)
        for k in DYNAMIC_COLS:
            if k not in col_names:
                col_names.append(k)

        # 建列：静态列取 JSON 值（缺字段填 None），动态列先空着
        self._columns = {c: [sig.get(c) for sig in signals] for c in col_names}
        self._columns["value"] = [None] * self._n
        self._columns["ts"] = [""] * self._n

        self._ids = [sig["id"] for sig in signals]
        self._id_to_idx = {sid: i for i, sid in enumerate(self._ids)}  # 每个 id 映射成它的下标
        self._lock = threading.Lock()

    # ---------------- 写（驱动层调用）----------------

    def write(self, idx_or_id, value):
        idx = self._resolve_idx(idx_or_id)
        with self._lock:
            self._columns["value"][idx] = value
            self._columns["ts"][idx] = self._now()

    def write_batch(self, updates):
        # updates: {信号id: 值}，整批共用一个时间戳
        ts = self._now()
        with self._lock:
            for key, val in updates.items():
                idx = self._id_to_idx.get(key)
                if idx is not None:
                    self._columns["value"][idx] = val
                    self._columns["ts"][idx] = ts

    # ---------------- 读（UI / BLL 调用）----------------

    def column_names(self):
        return list(self._columns.keys())

    def col(self, name):
        with self._lock:
            if name not in self._columns:
                raise KeyError("不存在的列: " + repr(name))
            return list(self._columns[name])

    def get(self, idx_or_id, column):
        idx = self._resolve_idx(idx_or_id)
        with self._lock:
            if column not in self._columns:
                raise KeyError("不存在的列: " + repr(column))
            return self._columns[column][idx]

    def row(self, idx_or_id):
        idx = self._resolve_idx(idx_or_id)
        with self._lock:
            return {c: vals[idx] for c, vals in self._columns.items()}

    def snapshot(self):
        # 按列：{列名: [N 个值]}
        with self._lock:
            return {c: list(vals) for c, vals in self._columns.items()}

    def snapshot_rows(self):
        # 按行：[{...}, {...}]
        with self._lock:
            return [{c: vals[i] for c, vals in self._columns.items()}
                    for i in range(self._n)]

    def iter_rows(self):
        with self._lock:
            for i in range(self._n):
                yield {c: vals[i] for c, vals in self._columns.items()}

    def get_ids(self):
        return list(self._ids)

    @property
    def signal_count(self):
        return self._n

    # ---------------- 内部 ----------------

    def _resolve_idx(self, idx_or_id):
        # 既接受序号 int，也接受信号 id 字符串(不管用"数字序号"还是"字符串 id"来找信号，都换算成内部用的数字序号（index）)
        if isinstance(idx_or_id, int):
            return idx_or_id
        if isinstance(idx_or_id, str):
            idx = self._id_to_idx.get(idx_or_id)
            if idx is None:
                raise KeyError("未知信号 id: " + repr(idx_or_id))
            return idx
        raise TypeError("应为 int 或 str，收到 " + str(type(idx_or_id)))

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

    # —— 打印代码 ——
    print("信号总数:", hub.signal_count)
    print("信号 id 列表:", hub.get_ids())
    print("列名顺序:", hub.column_names())

    print("\n=== 全局变量表（按列）===")
    for name, col in hub.snapshot().items():
        # 每列只打前 3 个值，避免刷屏
        print("%-12s: %s" % (name, col[:3] + ["..."] if len(col) > 3 else col))

    print("\n=== 全局变量表（按行，共 %d 行）===" % hub.signal_count)
    for row in hub.snapshot_rows():
        print(row)
