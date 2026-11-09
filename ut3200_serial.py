import time
from datetime import datetime
import serial          
import serial.tools.list_ports   
import threading


CONFIG = {
"baudrate":9600,
"bytesize":8,
"parity":"N",    #校验位
"stopbits":1,    #停止位
"timeout":0.5,   #超时时间
"interval":0.5 , #自动发送间隔0.5s"
"type":"tc-k",    #k型热电偶
}

OPEN_CIRCUIT_VALUE = -100000
#自动查询设备,成功返回已打开的串口对象，失败时返回None
def HAL_UT3200_FindDevice():
    ports = serial.tools.list_ports.comports()
    tries = []
    if not ports:
       print("当前无COM口工作")
       return None
    for p in ports:
        tries.append(p.device)
        ser = None
        device_found = False
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
    data = (cmd + "\n").encode("ascii")    
    ser.write(data)                          #ser.write()方法发送字节数据
    print(f'已发送指令：{cmd}')
    pass

def HAL_UT3200_ReadResponse(ser):
    buffer = bytearray()
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
    ser.reset_input_buffer()
    HAL_UT3200_SndCmd(ser,cmd)
    reply = HAL_UT3200_ReadResponse(ser)
    if not reply:
        print(f'超时{ser.timeout}秒：设备未返回数据')
        return None
    return reply

   #fetch_temperature()函数用于获取温度数据，返回值为浮点数
def HAL_UT3200_FetchTemperatures(ser):
    reply = HAL_UT3200_Query(ser, "FETCH?")
    if not reply:
        print(f'超时{ser.timeout}秒')
        return None
    values = []
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
    MEAS:MODEL tc-k  -> 设置传感器为 K 型热电偶
    MEAS:RATE slow   -> 采样速率:slow 慢 / fast 快
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
def HAL_UT3200_InitDevice():
    ser = None
    try:
        ser = HAL_UT3200_FindDevice()           #自动查找，成功时返回已经打开的串口对象
        if ser is None:
            print("[Fail] 未找到仪器，程序退出。")
            return None
        HAL_UT3200_SetUpInstruments(ser)               #复用已打开的串口
        return ser
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
def HAL_UT3200_CollectTempData_Thread(ser, stop_event,ut3200_signals, data_write):
    try:
        interval = float(CONFIG["interval"])
        next_t = time.monotonic()
        while not stop_event.is_set():
            next_t += interval
            values = HAL_UT3200_FetchTemperatures(ser)
            if values:
                updates = []
                #在ut3200_updates 查找enable是否为Flase，若为否，则开始写数据
                #根据ut3200_signals的id，把数值写进对应的id里
                for ut3200_updates in ut3200_signals:
                    if ut3200_updates.enabled is False:
                        continue
                    # ch1对应 values[0]，ch2对应 values[1]。
                    index = ut3200_updates.ch - 1
                    if 0 <= index < len(values):
                        updates.append((ut3200_updates,values[index]))
                data_write(updates)
                print([f"{v:.2f}" for v in values])
            delay = next_t - time.monotonic()
            if delay > 0:
                if stop_event.wait(delay):
                    break
            else:
                next_t = time.monotonic()
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
    from datahub_v import BLL_JSON_InitDevices, BLL_DataWrite
    # 测试入口初始化一次 DataHub
    json_devices = BLL_JSON_InitDevices("signals_config.json")
    # 获取 UT3200 对应的原有信号对象
    ut3200_signals = None
    for device in json_devices:
        if device.api == "ut3200.fetch_all":
            ut3200_signals = device.signals
            break

    if ut3200_signals is None:
        raise ValueError("配置中未找到 ut3200.fetch_all")

    ser = None
    worker = None
    stop_event = threading.Event()
    try:
        # 打开串口也放入清理范围
        ser = HAL_UT3200_InitDevice()
        if ser is None:
            return
        worker = threading.Thread(
            target=HAL_UT3200_CollectTempData_Thread,
            args=(ser, stop_event, ut3200_signals, BLL_DataWrite),
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


