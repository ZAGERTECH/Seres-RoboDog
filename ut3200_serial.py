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

OPEN_CIRCUIT_VALUE = 100000
#自动查询设备,成功返回已打开的串口对象，失败时返回None
def HAL_UT3200_FindDevice():
    ports = serial.tools.list_ports.comports()
    tries = []
    if not ports:
       print("当前无COM口工作")
       return None
    for p in ports:
        tries.append(p.device)
        ser = None                #循环重置，不影响except块的ser.close()，避免未定义错误
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
               return ser
            else:
               print(f'串口{p.device}无应答，继续尝试')   
               ser.close()
        except Exception as e:
              print(f'串口{p.device}无法试探{e},请跳过')
              if ser and ser.is_open:
                  ser.close()
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
def HAL_SetUpInstruments(ser):
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
def HAL_InitDevice():
    ser = None
    try:
        ser = HAL_UT3200_FindDevice()           #自动查找，成功时返回已经打开的串口对象
        if ser is None:
            print("[失败] 未找到仪器，程序退出。")
            return None
        HAL_SetUpInstruments(ser)               #复用已打开的串口
        return ser
    except Exception as e:
        print(f"初始化仪器异常：{e}")
        if ser is not None and ser.is_open:
            ser.close()
        return None

#创建采集函数
def HAL_UT3200_CollectTempData_PeriodThread(ser,stop_event):
    print("开始自动采集")
    interval =float(CONFIG["interval"])
    next_t = time.monotonic()
    try:
         while not stop_event.is_set():
            next_t += interval
            values = HAL_UT3200_FetchTemperatures(ser)
            if values:
                print([f"{v:.2f}" for v in values])
            delay = next_t - time.monotonic()
            if delay > 0:
                 if stop_event.wait(delay):
                    break
            else:
                next_t = time.monotonic()
    except serial.SerialException as e:
         print(f"串口异常: {e}")
         stop_event.set()

#调用主函数
def main():
    ser = HAL_InitDevice()
    if ser == None:
        return
    stop_event = threading.Event()
    worker = threading.Thread(target=HAL_UT3200_CollectTempData_PeriodThread,
                              args=(ser, stop_event),
                              daemon=True)
    worker.start()
    try:
         input("按回车键停止采集\n")
    except KeyboardInterrupt:
         print("\n接收到中断信号,正在停止采集")
    finally:
         stop_event.set()
         worker.join()
         ser.close()
    print("采集已停止，串口已关闭。")
     
if __name__ == "__main__":
    main()


