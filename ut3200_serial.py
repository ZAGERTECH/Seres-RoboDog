import time
from datetime import datetime
import serial          
import serial.tools.list_ports   
import threading

CONFIG = {
"port":"auto",
"baudrate":9600,
"bytesize":8,
"parity":"N",    #校验位
"stopbits":1,    #停止位
"timeout":2.0,   #超时时间
"interval":0.5 , #自动发送间隔0.5s"
"type":"tc-k",    #k型热电偶
}

CONFIG_FIND = {
"baudrate":9600,
"bytesize":8,
"parity":"N",    
"stopbits":1,    
"timeout":1.0,   
}

OPEN_CIRCUIT_VALUE = 100000
#自动查询设备
def find_device():
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
                            baudrate=CONFIG_FIND["baudrate"],
                            bytesize=CONFIG_FIND["bytesize"],
                            parity=CONFIG_FIND["parity"],
                            stopbits=CONFIG_FIND["stopbits"],
                            timeout=CONFIG_FIND["timeout"])
            print(f'已检测到串口：{p.device} {p.description}')
            ser.reset_input_buffer()                       # 清缓冲
            ser.write(b"*IDN?\n")                          # 发送查询指令,b"*IDN?\n"是SCPI指令，查询设备信息
            time.sleep(0.5)                                # 给仪器一点反应时间
            reply = ser.read(256).decode("ascii", errors="replace").strip()
            if  "UT3208+" in reply:
               print(f'已连接到仪器：{p.device}')
               ser.close()
               return p.device
            else:
               print(f'串口{p.device}无应答，继续尝试')   
               ser.close()
        except Exception as e:
              print(f'串口{p.device}无法试探{e},请跳过')
              if ser and ser.is_open:
                  ser.close()
    print(f'串口均已试探，无仪器应答。本次试探 {len(tries)} 个口: {", ".join(tries)}')
    return None

#打开串口
def open_port():
    try:
        ser = serial.Serial(port=CONFIG["port"],                 #创建一个串口对象，打开电脑上的串口
                            baudrate=CONFIG["baudrate"],
                            bytesize=CONFIG["bytesize"],
                            parity=CONFIG["parity"],
                            stopbits=CONFIG["stopbits"],
                            timeout=CONFIG["timeout"])
        print(f'串口{CONFIG["port"]}已打开')
        return ser
    except Exception as e:
        print(f'打开串口失败，错误信息：{e}')
        return None

#发送SCPI指令
def send_cmd(ser,cmd):
    data = (cmd + "\n").encode("ascii")    
    ser.write(data)                          #ser.write()方法发送字节数据
    print(f'已发送指令：{cmd}')
    pass

def read_response(ser):
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
def query(ser,cmd):
    ser.reset_input_buffer()
    send_cmd(ser,cmd)
    reply = read_response(ser)
    if not reply:
        print(f'超时{ser.timeout}秒：设备未返回数据')
        return None
    return reply

   #fetch_temperature()函数用于获取温度数据，返回值为浮点数
def fetch_temperatures(ser):
    reply = query(ser, "FETCH?")
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
def setup_instruments(ser):
    """
    MEAS:MODEL tc-k  -> 设置传感器为 K 型热电偶
    MEAS:RATE slow   -> 采样速率:slow 慢 / fast 快
    MEAS:START on    -> 启动采样
    """
    print("-" * 50)
    print("配置仪器(K 型热电偶 + 采样速率）...")
    query(ser, f"MEAS:MODEL {CONFIG['type']}")   # 设置传感器类型
    query(ser, "MEAS:MODEL?")                    # 读回来确认一下
    query(ser, "MEAS:RATE fast")                 # 快速模式
    query(ser, "MEAS:RATE?")                     # 确认设置成功
    query(ser, "MEAS:START on")                  # 启动采样
    print("[OK] 仪器配置完成，开始采样。\n")

#创建初始化设备函数
def init_device():
    if CONFIG["port"] == "auto":
        found = find_device()
        if found is None:
            print("[失败] 未找到仪器，程序退出。")   
            return None
        CONFIG["port"] = found    
    ser = open_port()
    if ser is None:                 
        return None
    if not query(ser, "*IDN?"):      
        print("[失败] 无法和仪器通讯，程序退出。")
        ser.close()
        return None
    setup_instruments(ser)          
    return ser

#创建采集函数
def collect_work(ser,stop_event):
        print("开始自动采集")
        try:
            while not stop_event.is_set():
                if stop_event.wait(CONFIG["interval"]):
                   break
                values = fetch_temperatures(ser)
                if values:
                  print([f"{v:.2f}" for v in values])
        except serial.SerialException as e:
            print(f"串口异常: {e}")
            stop_event.set()

#调用主函数
def main():
    ser = init_device()
    if ser == None:
        return
    stop_event = threading.Event()
    worker = threading.Thread(target=collect_work, args=(ser, stop_event),daemon=True)
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

   