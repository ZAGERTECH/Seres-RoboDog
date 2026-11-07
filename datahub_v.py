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
import threading
from dataclasses import dataclass, field, fields

from readerwriterlock import rwlock

import ut3200_serial
import udp_receive


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
    devices = BLL_JSON_LoadDevices(json_path)
    global _DEVICES
    with _WLOCK:
        _DEVICES = devices
    return devices


def BLL_JSON_GetDevices():
    # 取全局的 Device 列表（拿到的是本体，改它 = 改全局变量）
    with _RLOCK:
        if not _DEVICES:
            raise RuntimeError("未初始化，请先调用 BLL_JSON_InitDevices(配置文件路径)")
        return _DEVICES


# ============================================================
# 四、BLL 层线程编排（初始化 / 创建 / 停止）
# ============================================================
#
# 分工说明：
#   - HAL 层（ut3200_serial.py / udp_receive.py）提供线程"工作函数"
#     （线程 target 指向的循环函数）
#   - BLL 层（本文件）提供"初始化 / 启动 / 停止"三个封装：
#       BLL_All_InitSystem()        —— 统一初始化 JSON + 两个硬件
#       BLL_All_StartThreads_Thread —— 一次性创建并启动所有采集线程
#       BLL_Thread_StopAll()        —— 统一停止所有线程 + 关硬件
#   - main（__main__ 段）只负责按顺序调用上面三个封装 + 等退出信号
#
# 停止机制：
#   全局用一个 threading.Event —— BLL_ThreadStopEvent_Flag
#   所有线程的循环都检查这个 Event：is_set() 为真就退出
#   main 退出时调 BLL_Thread_StopAll()：set() 通知 + join() 等待 + 关硬件
# -----------------------------------------------------------

# 全局线程停止标志（threading.Event，不是普通 bool）
#   为什么用 Event 而不是 bool 变量：
#     1. Event 内部有锁，多线程 set/is_set 不会竞争出错
#     2. Event.wait(timeout) 能同时实现"定时等待"和"检查退出"
#        （现有 HAL 工作函数已经在用 stop_event.wait(delay) 做定时+退出）
#   所有线程检查的都是这同一个 Event。
BLL_ThreadStopEvent_Flag = threading.Event()


def BLL_All_InitSystem(json_path):
    # 统一初始化：JSON + UT3200 串口 + UDP socket
    #   全部成功 → 返回 (devices, ser, sock)
    #   任一失败 → 清理已初始化的资源，返回 (None, None, None)
    # 原则：初始化失败则线程一律不启动（由 main 判断返回值决定）
    #       不会出现"UT3200 开了、UDP 没开、线程只跑一半"的半启动状态

    # ---- 1. JSON ----
    try:
        devices = BLL_JSON_InitDevices(json_path)
    except Exception as e:
        print("[BLL] JSON 初始化失败：%s" % e)
        return None, None, None
    print("[BLL] JSON 初始化成功")
    # ---- 2. UT3200 串口 ----
    try:
        ser = ut3200_serial.HAL_InitDevice()
    except Exception as e:
        print("[BLL] UT3200 初始化异常：%s" % e)
        return None, None, None
    if ser is None:
        print("[BLL] UT3200 未找到仪器")
        return None, None, None
    print("[BLL] UT3200 初始化成功")
    # ---- 3. UDP socket ----
    try:
        sock = udp_receive.HAL_Dog_Open(host="0.0.0.0", port=8080, timeout=3.0)
    except Exception as e:
        print("[BLL] UDP 初始化异常：%s" % e)
        ser.close()                                  # 回滚：关掉已开的串口
        print("[BLL] 已回滚关闭 UT3200 串口")
        return None, None, None
    print("[BLL] UDP 初始化成功，绑定 %s:%d"
          % (sock.getsockname()[0], sock.getsockname()[1]))

    return devices, ser, sock

def BLL_All_StartThreads_Thread(ser, sock):
    # 一次性创建并启动所有采集线程
    #   ser / sock：已初始化的硬件句柄（由 BLL_All_InitSystem 返回）
    #   返回：list[Thread]，供 BLL_Thread_StopAll 使用
    #
    # 调用前提：BLL_All_InitSystem 已全部成功，ser / sock 都不是 None
    #   target 都指向 HAL 层的工作函数，BLL 只管"建线程 + start"
    threads = []

    t_ut = threading.Thread(
        target=ut3200_serial.HAL_UT3200_CollectTempData_Thread,
        args=(ser, BLL_ThreadStopEvent_Flag),
        name="UT3200_Collect",
        daemon=True,
    )
    t_ut.start()
    threads.append(t_ut)
    print("[BLL] UT3200 采集线程已启动")

    t_dog = threading.Thread(
        target=udp_receive.HAL_Dog_ReceiveData_Thread,
        args=(sock, BLL_ThreadStopEvent_Flag),
        name="Dog_Receive",
        daemon=True,
    )
    t_dog.start()
    threads.append(t_dog)
    print("[BLL] UDP 接收线程已启动")

    return threads


def BLL_Thread_StopAll(threads, ser=None, sock=None):
    # 统一停止：通知所有线程退出 → 等待 → 关硬件
    #   threads：需要 join 的 Thread 列表
    #   ser / sock：硬件句柄，None 表示没有该设备
    # 调用时机：main 收到退出信号（input/Ctrl+C）后调
    print("\n[BLL] 正在停止所有线程...")
    BLL_ThreadStopEvent_Flag.set()              # 1. 通知所有线程退出

    for t in threads:                           # 2. 等待每个线程结束
        if t is not None and t.is_alive():
            t.join(timeout=3.0)                 # 最多等 3s，防止卡死
            if t.is_alive():
                print("[BLL] 警告：线程 %s 3s 内未退出" % t.name)

    if ser is not None:                         # 3. 关闭串口
        try:
            ser.close()
            print("[BLL] UT3200 串口已关闭")
        except Exception as e:
            print("[BLL] 关串口异常：%s" % e)

    if sock is not None:                        # 4. 关闭 socket
        try:
            sock.close()
            print("[BLL] UDP socket 已关闭")
        except Exception as e:
            print("[BLL] 关 socket 异常：%s" % e)

    print("[BLL] 所有线程已停止，硬件已关闭")


# ============================================================
# 五、应用入口（暂放 __main__，后续可以单独放一个main.py）
# ============================================================
#
# main 的职责（编排层逻辑）：
#   1. BLL_All_InitSystem        —— 统一初始化（JSON + UT3200 + UDP）
#      失败直接退出，不开任何线程
#   2. BLL_All_StartThreads_Thread —— 启动所有采集线程
#   3. 等待退出信号（回车 / Ctrl+C）
#   4. BLL_Thread_StopAll        —— 统一停止所有线程 + 关硬件
#
# 这段也可以拆到独立的 main.py
# -----------------------------------------------------------

SIGNALS_CONFIG_PATH = "signals_config.json"


def main():
    # ---- 1. 统一初始化（JSON + UT3200 + UDP，全部初始化成功才继续）----
    devices, ser, sock = BLL_All_InitSystem(SIGNALS_CONFIG_PATH)
    if devices is None:
        print("\n[main] 初始化失败，程序退出")
        return
    # ---- 2. 启动所有采集线程 ----
    threads = BLL_All_StartThreads_Thread(ser, sock)

    # ---- 3. 等待退出信号 ----
    print("\n程序运行中，按回车键停止...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n[main] 收到中断信号，正在停止采集...")
    finally:
        BLL_Thread_StopAll(threads, ser=ser, sock=sock)

if __name__ == "__main__":
    main()
