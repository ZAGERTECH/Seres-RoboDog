"""通过串口查询 UT3200 温度，将通道数据映射为 BLL 的批量写入列表。"""

import time
from datetime import datetime
import serial          
import serial.tools.list_ports   
import threading


CONFIG = {       # 串口通信参数与采集参数；所有时间值以秒为单位
"baudrate":9600, # 波特率
"bytesize":8,    # 每个字符的数据位数
"parity":"N",    # N 表示无奇偶校验
"stopbits":1,    # 停止位数
"timeout":0.5,   # 单次串口读取的超时时间
"interval":0.5 , # 采集循环的目标周期，不包含启动时的设备配置
"type":"tc-k",    # K 型热电偶
}

OPEN_CIRCUIT_VALUE = -100000  # 预留的开路标记值；当前采集流程未对它做过滤或转换
#自动查询设备,成功返回已打开的串口对象，失败时返回None
def HAL_UT3200_FindDevice():
    """扫描串口并识别回复含 UT3208+ 的仪器；返回打开的 Serial 句柄，未找到返回 None。

    匹配成功后由调用方负责关闭串口；试探失败的串口在本函数内关闭。
    """
    ports = serial.tools.list_ports.comports()  # 本机串口信息列表，元素含 device 路径与 description
    tries = []                                # 已试探的串口名称列表，如 ["COM3", "COM4"]
    if not ports:
       print("当前无COM口工作")
       return None
    for p in ports:
        tries.append(p.device)
        ser = None                            # 当前试探的 serial.Serial 句柄，打开前为 None
        device_found = False                  # 是否已匹配并将串口所有权交给调用方
        try:
            ser = serial.Serial(port=p.device,                  #创建一个串口对象，打开电脑上的串口
                            baudrate=CONFIG["baudrate"],
                            bytesize=CONFIG["bytesize"],
                            parity=CONFIG["parity"],
                            stopbits=CONFIG["stopbits"],
                            timeout=CONFIG["timeout"])
            print(f'已检测到串口：{p.device} {p.description}')
            ser.reset_input_buffer()                       # 清缓冲
            ser.write(b"*IDN?\n")                          # 发送查询指令,b"*IDN?\n"是SCPI指令，查询设备信息
            time.sleep(0.5)                                # 给仪器一点反应时间
            reply = ser.read(256).decode("ascii", errors="replace").strip()
            if  "UT3208+" in reply:
               print(f'已连接到仪器：{p.device}')
               device_found = True
               return ser
            else:
               print(f'串口{p.device}无应答，继续尝试')
        except Exception as error:
            print(f"串口 {p.device} 无法试探：{error}，跳过")
        finally:
        # 未成功交给调用者的串口，由本函数关闭
            if ser is not None and not device_found:
                try:
                    ser.close()
                except Exception as close_error:
                    print(f"[UT3200] 关闭试探串口失败：{close_error}")
    print(f'串口均已试探，无仪器应答。本次试探 {len(tries)} 个口: {", ".join(tries)}')
    return None

#发送SCPI指令
def HAL_UT3200_SndCmd(ser,cmd):
    """向已打开的串口句柄 ser 发送命令字符串 cmd；cmd 不含结尾换行，由本函数补齐。"""
    data = (cmd + "\n").encode("ascii")    # 串口发送的 ASCII 字节，换行用于结束一条命令
    ser.write(data)                          #ser.write()方法发送字节数据
    print(f'已发送指令：{cmd}')
    pass

def HAL_UT3200_ReadResponse(ser):
    """从已打开的串口 ser 读取一行 ASCII 文本；换行或读取超时结束，无内容返回空串。"""
    buffer = bytearray()                    # 累积本条回复字节，不包含行尾 CR/LF
    while True:
        byte = ser.read(1)
        if byte == b'':
            break
        if byte in (b'\r', b'\n'):
            if buffer:               
                break
            continue
        buffer.extend(byte)
    return buffer.decode("ascii", errors="replace").strip()

#发送一次指令，就回复数据，并处理超时情况。要把发送和接收放在这里，方便调用
def HAL_UT3200_Query(ser,cmd):
    """清空 ser 输入缓冲后发送 cmd 并读取回复；返回非空 str，无回复返回 None。"""
    ser.reset_input_buffer()
    HAL_UT3200_SndCmd(ser,cmd)
    reply = HAL_UT3200_ReadResponse(ser)
    if not reply:
        print(f'超时{ser.timeout}秒：设备未返回数据')
        return None
    return reply

# 一次 FETCH? 读取多通道回复，返回温度列表。
def HAL_UT3200_FetchTemperatures(ser):
    """通过已打开的串口 ser 查询温度，返回 list[float]；无回复返回 None。

    按回复字段顺序转换为浮点数（项目配置的温度单位为 °C），空字段或无法解析的字段
    会被跳过，因此有回复但没有有效数值时返回 []。路由按 ch - 1 取值，依赖仪器按
    通道顺序返回完整数值；当前解析若跳过中间字段，后续元素会前移，不能视为保留通道占位。
    """
    reply = HAL_UT3200_Query(ser, "FETCH?")
    if not reply:
        print(f'超时{ser.timeout}秒')
        return None
    values = []                             # list[float]，保存成功解析的温度值
    for item in (reply.split(",")):
        item = item.strip().strip("<>")   # 去掉可能的 <> 包裹和空格
        if not item:
            continue
        try:
            values.append(float(item))    
        except ValueError:
            # 有时会有无法解析的字段，跳过并提示
            print(f"  [提示] 无法解析字段: {item!r}，已跳过")
    return values

#设置操作指令，然后一问一答,只在串口成功打开后执行一次
def HAL_UT3200_SetUpInstruments(ser):
    """
    ser 为已打开的 serial.Serial 句柄，配置与采集复用同一串口。
    MEAS:MODEL tc-k  -> 设置传感器为 K 型热电偶
    MEAS:RATE fast   -> 采样速率:slow 慢 / fast 快；当前设置 fast
    MEAS:START on    -> 启动采样
    """
    print("-" * 50)
    HAL_UT3200_Query(ser, f"MEAS:MODEL {CONFIG['type']}")   # 设置传感器类型
    HAL_UT3200_Query(ser, "MEAS:MODEL?")                    # 读回来确认一下
    HAL_UT3200_Query(ser, "MEAS:RATE fast")                 # 快速模式
    HAL_UT3200_Query(ser, "MEAS:RATE?")                     # 确认设置成功
    HAL_UT3200_Query(ser, "MEAS:START on")                  # 启动采样
    print("[OK] 仪器配置完成，开始采样。\n")

#创建初始化设备函数
def HAL_UT3200_InitDevice(snapshot):
    """按 BLL 字典快照 snapshot 初始化仪器，返回 (ser, routes)。

    snapshot 含 devices 列表及设备内 signals 列表，取首个 api=ut3200.fetch_all 的设备。
    ser 是已打开且完成配置的 serial.Serial 句柄，调用方负责在线程退出后关闭。
    routes 是 list[tuple[int, int, int]]，每项为 (device_index, signal_index, source_index)：
        第 1 项：snapshot["devices"] 中的设备下标，从 0 开始，不是设备 ID。
        第 2 项：该设备 signals 中的信号下标，从 0 开始，不是信号 ID 或通道号。
        第 3 项：温度返回列表中的下标，从 0 开始，由配置的仪器通道号 ch - 1 得到。
    仅 enabled=False 不生成路由；ch 必须是从 1 开始的整数。配置下标必须与 BLL 保持一致。
    """
    routes = None                           # None 表示未匹配设备；[] 表示设备存在但无待采集信号
    # 初始化时找到本设备，记录启用信号的位置。
    for device_index, device in enumerate(snapshot["devices"]):
        if device["api"] != "ut3200.fetch_all":
            continue
        routes = []
        for signal_index, signal in enumerate(device["signals"]):
            if signal["enabled"] is False:
                continue
            ch = signal["ch"]               # 仪器物理通道号，从 1 开始；与 signals 列表下标无关
            if type(ch) is not int or ch < 1:
                raise ValueError(
                    f"UT3200 信号 {signal['id']} 的通道配置无效：{ch!r}"
                )
            routes.append((device_index, signal_index, ch - 1))
        break

    if routes is None:
        raise ValueError("配置中未找到 ut3200.fetch_all")

    ser = None                              # 仪器串口句柄，初始化失败时由本函数清理
    try:
        ser = HAL_UT3200_FindDevice()           #自动查找，成功时返回已经打开的串口对象
        if ser is None:
            raise RuntimeError("UT3200 未找到仪器")

        HAL_UT3200_SetUpInstruments(ser)               #复用已打开的串口
        return ser ,routes
    except BaseException:
        # 配置失败或被 Ctrl+C时候异常处理
        if ser is not None:
            try:
                ser.close()
            except Exception as close_error:
                print(f"[UT3200] 初始化失败后关闭串口异常：{close_error}")
        # 清理后继续向外传递原来的异常或中断
        raise

#创建采集函数（线程 target：循环采集温度，直到 stop_event 被设置）
def HAL_UT3200_CollectTempData_Thread(ser, stop_event,ut3200_routes, data_write_callback):
    """定期查询温度并提交一批更新；退出时设置共享停止事件。

    ser：已配置的串口句柄，本线程不负责关闭。
    stop_event：threading.Event，由主线程或任一采集线程设置以通知停止。
    ut3200_routes：[(设备下标, 设备内信号下标, 温度数组下标), ...]，三项均从 0 开始。
    data_write_callback：接收 updates 列表的函数对象，通常为 BLL_DataWrite；
        每项为 (设备下标, 信号下标, float 温度值)，返回值不使用。
        温度列表中不存在的源下标不提交，对应信号保留旧值。
    """
    try:
        interval = float(CONFIG["interval"])  # 目标采集周期，单位秒
        next_t = time.monotonic()             # 单调时钟上的下次周期截止时间，不受系统校时影响
        while not stop_event.is_set():
            next_t += interval
            values = HAL_UT3200_FetchTemperatures(ser)  # list[float] 或 None，供路由 source_index 索引
            if values:
                # list[tuple[int, int, float]]；第三项是取出的温度值，不再是路由中的源下标。
                updates = []

                for device_index, signal_index, source_index in ut3200_routes:
                    if source_index < len(values):
                        updates.append(
                            (device_index, signal_index, values[source_index])
                        )
                data_write_callback(updates)
                print([f"{v:.2f}" for v in values])

            delay = next_t - time.monotonic()  # 本周期剩余等待秒数，扣除查询与回调耗时
            if delay > 0:
                if stop_event.wait(delay):
                    break
            else:
                next_t = time.monotonic()     # 本周期已超时，从当前时间重新计时，避免连续追赶采样
    except Exception as error:
        print(
            f"[UT3200] 采集线程异常："
            f"{type(error).__name__}: {error}"
        )
    finally:
        stop_event.set()
        print("[UT3200] 采集线程已结束")


def HAL_UT3200_RunTest():
    """单独测试 UT3200 的采集和 DataHub 写入。"""
    from BLL_datahub_v import BLL_JSON_InitDevices, BLL_GetDevicesSnapshot,BLL_DataWrite
    # 测试入口初始化一次 DataHub
    BLL_JSON_InitDevices("signals_config.json")
    snapshot = BLL_GetDevicesSnapshot()  # 独立设备/信号字典快照，用于生成通道路由

    ser = None                          # 测试持有的串口句柄，结束时关闭
    worker = None                       # 温度采集线程句柄，创建失败时仍为 None
    stop_event = threading.Event()      # 测试主线程与采集线程共用的停止通知
    try:
        # ut3200_routes 为（设备下标、信号下标、温度数组下标）的三元组列表，各下标从 0 开始。
        # 打开串口也放入清理范围。
        ser, ut3200_routes = HAL_UT3200_InitDevice(snapshot)
        if ser is None:
            return
        worker = threading.Thread(
            target=HAL_UT3200_CollectTempData_Thread,
            args=(ser, stop_event, ut3200_routes, BLL_DataWrite),
            daemon=True,
        )
        worker.start()
        input("按回车键停止采集\n")
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止采集")
    except Exception as error:
        print(f"[UT3200] 测试失败：{error}")
    finally:
        stop_event.set()
        # 线程可能还没创建，或者启动失败
        if worker is not None and worker.is_alive():
            worker.join()

        if ser is not None:
            try:
                ser.close()
                print("[UT3200] 串口已关闭")
            except Exception as error:
                print(f"[UT3200] 关闭串口失败：{error}")

def main():
    HAL_UT3200_RunTest()

if __name__ == "__main__":
    main()


