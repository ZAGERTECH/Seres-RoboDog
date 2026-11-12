"""接收机器狗的 UTF-8 逗号分隔报文，按配置位置映射到 BLL 信号。"""

import socket
import datetime
import threading

def HAL_Dog_Open(snapshot,host="0.0.0.0", port=8080, timeout=3.0):
    """按 BLL 快照构造路由并绑定 UDP，返回 (sock, routes)。

    snapshot：含 devices 及各设备 signals 列表的字典，取第一个 api=udp.parse_frame 的设备。
    host/port：本机监听地址/端口；0.0.0.0 表示监听所有本机 IPv4 网络接口。
    timeout：单次接收的超时秒数，超时后线程有机会再次检查停止事件。
    sock：已绑定的 socket.socket 句柄，由调用方在线程退出后关闭。
    routes：list[tuple[int, int, int]]，每项为
        (device_index, signal_index, source_index)：设备列表下标、设备内的信号下标、
        逗号分隔报文中的字段下标，均从 0 开始，前两项不是配置中的字符串 ID。
        source_index 等于原始 signal_index，要求发送端字段顺序与配置一致。
        仅 enabled=False 被跳过；停用字段仍须保留位置，不能压缩报文。
    """
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口必须是 1～65535 的整数")
    if timeout <= 0:
        raise ValueError("接收超时必须大于 0 秒")
    routes = None                  # None 表示没找到目标设备；[] 表示找到设备但没有待采集信号
    for device_index, device in enumerate(snapshot["devices"]):
        if device["api"] != "udp.parse_frame":
            continue
        routes = []

        for signal_index, signal in enumerate(device["signals"]):
            if signal["enabled"] is False:
                continue
            routes.append((device_index, signal_index, signal_index))
        break

    if routes is None:
        raise ValueError("配置中未找到 udp.parse_frame")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # IPv4 UDP 接收句柄
    try:
        sock.settimeout(timeout)
        sock.bind((host, port))
    except BaseException:
        try:
            sock.close()
        except Exception as close_error:
            print(f"[UDP] 打开失败后关闭 socket 异常：{close_error}")
        raise
    return sock,routes

def HAL_Dog_ReceiveData_Thread(sock, stop_event,dog_routes, data_write_callback):
    """循环收报文并提交三元组列表；退出时设置共享停止事件。

    sock：HAL_Dog_Open 返回的 UDP 句柄，本线程不负责关闭。
    stop_event：threading.Event，设置后停止采集，接收等待受 sock 的超时约束。
    dog_routes：[(设备下标, 设备内信号下标, 报文字段下标), ...]，三项均从 0 开始。
    data_write_callback：接收 updates 列表的可调用对象，通常为 BLL_DataWrite；
        每项是 (设备下标, 信号下标, 字段字符串)，回调返回值不使用。
        报文中缺失的字段不提交，对应信号保留旧值；字段不转换为 float。
    """
    try:
        while not stop_event.is_set():
            try:
                # data 为原始报文字节；address 为发送方 (IPv4 地址字符串, UDP 端口号)。
                data, address = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError as error:
                print(f'接受失败，停止接收：{error}')
                return
            if stop_event.is_set():
                break

            timestamp = datetime.datetime.now().strftime("%H:%M:%S")  # 日志接收时间，非 BLL 写入时间
            try:
                text = data.decode("utf-8")
            except ValueError as error:
                print(f"编码失败，丢弃当前报文：{error}")
                continue
            # 按逗号拆分，并去掉每个字段两端的空白
            values_udp = [field.strip() for field in text.split(",")]  # list[str]，下标对应报文字段位置
            # 如果末尾逗号产生了空字段，就删除最后一个空项
            if values_udp and values_udp[-1] == "":
                values_udp.pop()
            # updates 为 list[tuple[int, int, str]]，每项依次是（设备下标、信号下标、实际值）。
            # 路由的第三项 source_index 用来取值；回调的第三项必须换成该位置的字段字符串。
            updates = []
            for device_index,signal_index,source_index in dog_routes:
                if source_index < len(values_udp):
                    updates.append(
                        (device_index,signal_index,values_udp[source_index])
                                   )
            data_write_callback(updates)
            print(f'{timestamp}来自{address}:{values_udp}')
    finally:
        stop_event.set()
        print("接收线程已结束")

def HAL_Dog_RunTest():
    """单独测试 Dog 的采集和 DataHub 写入。"""
    from BLL_datahub_v import BLL_JSON_InitDevices, BLL_GetDevicesSnapshot,BLL_DataWrite
    # 测试入口初始化一次 DataHub
    BLL_JSON_InitDevices("signals_config.json")
    snapshot = BLL_GetDevicesSnapshot()  # 独立设备/信号字典快照，用于生成接收路由

    sock = None                         # UDP 句柄，测试结束后由本函数关闭
    receiver_thread = None               # 接收线程句柄，创建失败时仍为 None
    stop_event = threading.Event()      # 测试主线程与接收线程共用的停止通知
    try:
        # dog_routes：[(设备下标, 信号下标, 报文字段下标), ...]，各下标从 0 开始。
        sock ,dog_routes= HAL_Dog_Open(snapshot,host="0.0.0.0", port=8080, timeout=3.0)
        host, port = sock.getsockname()
        print(f"UDP 已打开，绑定 {host}:{port}")
        receiver_thread = threading.Thread(
            target=HAL_Dog_ReceiveData_Thread,
            args=(sock, stop_event,dog_routes,BLL_DataWrite),
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
