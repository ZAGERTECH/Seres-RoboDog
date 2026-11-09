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
import json
from dataclasses import dataclass, field, fields,asdict
from readerwriterlock import rwlock
import hal_startup_shutdown

from datetime import datetime

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

def BLL_JSON_LoadDevices(json_path):
    # 只认设备分组写法 {"devices": [ {..., "signals": [ {...} ]} ]}
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

# ---------- 读写锁（读读共享 / 读写互斥 / 写写互斥）----------
#   写：HAL 驱动线程每 0.5s 写一批 value/ts，操作极短
#   读：将来 UI 刷新、CSV 落盘、日志，可能高频、多个来源同时读
# 选 RWLockWrite（写优先）：一旦有写者在排队，新来的读者先等一等，
#   保证温度数据"及时更新"，读者最多等一次微秒级的写。
#   另外两个选项：RWLockRead(读优先)——读者多时写者可能永远等不到，
#   本项目绝对不能用；RWLockFair(公平)——先来后到，可备选。
_RWLOCK = rwlock.RWLockWrite()
_RLOCK = _RWLOCK.gen_rlock()                    # 读锁句柄（可重复使用）
_WLOCK = _RWLOCK.gen_wlock()                    # 写锁句柄（可重复使用）
#
# 两条铁律：
#   1. 锁内只放内存操作（赋值/浅拷贝），快进快出
#   2. 锁内绝不做 I/O（文件、串口、网络）——I/O 先做完，再进锁写结果
# -------------------------------------------------------------


def BLL_JSON_InitDevices(json_path):
    # 启动时调一次：读 JSON → list[Device] 存进全局，不启动任何线程
    # BLL_JSON_LoadDevices 里有文件 I/O，必须在锁外做完；锁内只"换引用"（一条赋值）
    # 采集尚未关闭时，禁止替换线程正在使用的数据对象
    if hal_startup_shutdown.HAL_HasResources():
        raise RuntimeError("HAL 尚未关闭，不能重新初始化 DataHub")
    
    devices = BLL_JSON_LoadDevices(json_path)
    global _DEVICES
    with _WLOCK:
        _DEVICES = devices
    return devices


def BLL_JSON_GetDevices():
    # 取全局的 Device 列表（拿到的是本体，改它 = 改全局变量）
    with  _RWLOCK.gen_rlock(): #把原本_RLOCK全局共享的句柄写成其自己用的读锁
        if not _DEVICES:
            raise RuntimeError("未初始化，请先调用 BLL_JSON_InitDevices(配置文件路径)")
        return _DEVICES

#写入接口函数，HAL层数据由此传入
def BLL_DataWrite(updates):
    if not updates:
        print("无数据")
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with _RWLOCK.gen_wlock():
        for signal, value in updates:
            signal.value = value
            signal.ts = timestamp
        # 从 DataHub 全局数据结构复制快照,此处验证数值是否正确传输
        snapshot = {
            "devices": [asdict(device) for device in _DEVICES]
        }
    # 离开 with 的缩进范围时，写锁已经自动释放
    # 打印放在锁外
    print("[DataHub] 写入后的数据：")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))

# ============================================================
# 四、BLL 层线程编排（初始化 / 创建 / 停止）
# ============================================================
# BLL 负责初始化 DataHub，并调用 HAL 的启动、关闭接口。
# 硬件句柄、线程列表和停止标志由 HAL 管理。
def BLL_All_InitSystem(json_path):
    # 统一初始化：JSON + UT3200 串口 + UDP socket
    # ---- 1. JSON ----
    try:
        devices = BLL_JSON_InitDevices(json_path)
        print("[BLL] JSON 初始化成功")
    # ---- 2. UT3200 串口 ----
    # 传入json里的对象和写入函数，再启动 HAL
        hal_startup_shutdown.HAL_Start(devices,BLL_DataWrite)
        print("[BLL] HAL 启动成功")
        return devices
    except Exception as error:
        print("[BLL] 系统初始化失败：%s" % error)
        return None

def BLL_Thread_StopAll():
    # 统一停止：通知所有线程退出 → 等待 → 关硬件
    hal_startup_shutdown.HAL_Shutdown()
    print("[BLL] 所有线程已停止，硬件已关闭")

def BLL_RoboDogDetect(json_path):
    """初始化系统，等待停止通知，最后关闭资源。"""
    if hal_startup_shutdown.HAL_HasResources():
        print("[BLL] 系统已启动或上次关闭未完成，拒绝重复启动")
        return
    try:
        devices = BLL_All_InitSystem(json_path)
        if devices is None:
            print("[BLL] 初始化失败，程序退出")
            return
        print("\n程序运行中，按 Ctrl+C 停止...")
        # 等待 HAL 的停止通知
        while not hal_startup_shutdown.HAL_WaitForStop(0.2):
            pass
        print("[BLL] 收到 HAL 停止通知，正在关闭系统...")
    except KeyboardInterrupt:
        print("\n[BLL] 收到用户停止请求，正在关闭系统...")
    finally:
        try:
            BLL_Thread_StopAll()
        except Exception as error:
            print("[BLL] HAL 关闭未完成：%s" % error)

SIGNALS_CONFIG_PATH = "signals_config.json"

def main():
    BLL_RoboDogDetect(SIGNALS_CONFIG_PATH)

if __name__ == "__main__":
    main()
