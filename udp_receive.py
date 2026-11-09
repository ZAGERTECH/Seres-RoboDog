import socket
import datetime
import threading

def HAL_Dog_Open(host="0.0.0.0", port=8080, timeout=3.0):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口必须是 1～65535 的整数")
    if timeout <= 0:
        raise ValueError("接收超时必须大于 0 秒")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.bind((host, port))
    except BaseException:
        try:
            sock.close()
        except Exception as close_error:
            print(f"[UDP] 打开失败后关闭 socket 异常：{close_error}")
        raise
    return sock

def HAL_Dog_ReceiveData_Thread(sock, stop_event,dog_signals, data_write):
    try:
        while not stop_event.is_set():
            try:
                data, address = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError as error:
                print(f'接受失败，停止接收：{error}')
                return
            if stop_event.is_set():
                break

            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            try:
                text = data.decode("utf-8")
            except ValueError as error:
                print(f"编码失败，丢弃当前报文：{error}")
                continue
            # 按逗号拆分，并去掉每个字段两端的空白
            values_udp = [field.strip() for field in text.split(",")]
            # 如果末尾逗号产生了空字段，就删除最后一个空项
            if values_udp and values_udp[-1] == "":
                values_udp.pop()
            updates = []
            udp_index = 0
            for dog_signal in dog_signals:    #dog_signals包含多个对象，每次取出一个以dog_signal引用
                if dog_signal.enabled is not False:
                    if udp_index < len(values_udp):
                        updates.append  (
                            (dog_signal,values_udp[udp_index])
                        )
                udp_index = udp_index + 1
            data_write(updates)
            print(f'{timestamp}来自{address}:{values_udp}')
    finally:
        stop_event.set()
        print("接收线程已结束")

def HAL_Dog_RunTest():
    """单独测试 Dog 的采集和 DataHub 写入。"""
    from datahub_v import BLL_JSON_InitDevices, BLL_DataWrite
    # 测试入口初始化一次 DataHub
    json_devices = BLL_JSON_InitDevices("signals_config.json")
    dog_signals = None
    for device in json_devices:
        if device.api == "udp.parse_frame":
            dog_signals = device.signals
            break

    if dog_signals is  None:
        raise ValueError("配置中未找到 udp.parse_frame")

    sock = None
    receiver_thread = None
    stop_event = threading.Event()
    try:
        sock = HAL_Dog_Open(host="0.0.0.0", port=8080, timeout=3.0)
        host, port = sock.getsockname()
        print(f"UDP 已打开，绑定 {host}:{port}")
        receiver_thread = threading.Thread(
            target=HAL_Dog_ReceiveData_Thread,
            args=(sock, stop_event,dog_signals,BLL_DataWrite),
            daemon=False
        )
        receiver_thread.start()
        print("线程已启动")
        while not stop_event.wait(0.5):
            pass
    except (OSError, ValueError) as error:
        print(f"UDP 初始化或打开失败：{error}")
    except KeyboardInterrupt:
        print("\n收到退出请求")

    finally:
        stop_event.set()
        if receiver_thread is not None and receiver_thread.is_alive():
            receiver_thread.join()  # 先等待接收线程退出
        if sock is not None:
            sock.close()
            print("UDP 已关闭")

def main():
    HAL_Dog_RunTest()

if __name__ == "__main__":
    main()
