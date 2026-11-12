"""集中持有采集资源；先通知线程停止并等待退出，再关闭硬件句柄。"""

import threading
import HAL_ut3200_serial
import HAL_udp_receive

threads= []                       # list[threading.Thread]，记录本轮启动的采集线程，供关闭时等待
stop_event = threading.Event()    # 两个采集线程共用的停止事件；任一线程退出都会设置它
ser = None                       # UT3200 的 serial.Serial 串口句柄；None 表示当前未持有
sock = None                      # 机器狗的 IPv4 UDP socket 句柄；None 表示当前未持有


def HAL_WaitForStop(timeout=0.2):
    """最多等待 timeout 秒；收到停止通知返回 True，等待超时返回 False。"""
    return stop_event.wait(timeout)

def HAL_HasResources():
    """是否仍持有设备句柄或线程记录。"""
    return ser is not None or sock is not None or bool(threads)

def HAL_Start(snapshot,data_write_callback):
    """
    初始化硬件并启动采集线程。
    snapshot：BLL_GetDevicesSnapshot 返回的字典，含 devices 列表及各设备的 signals 列表。
    data_write_callback：BLL 传入的写入函数对象，例如 BLL_DataWrite，而非调用结果。
        调用形式为 data_write_callback(updates)，返回值不使用。
        updates 是三元组列表 [(设备下标, 该设备内的信号下标, 实际值), ...]；
        两个下标均从 0 开始，值由串口传 float、UDP 传 str。
        两个采集线程可同时调用该函数，因此回调需要保证线程安全。
    正常返回表示启动流程完成，失败则抛出异常。
    """
    global ser,sock
    if  HAL_HasResources():
        raise RuntimeError("硬件设备尚未关闭，不能重复启动")
    stop_event.clear()

    try:
        # ser 为已打开的串口句柄；ut3200_routes 为 list[tuple[int, int, int]]。
        # 每项依次为（设备下标、该设备内的信号下标、温度数组下标），均从 0 开始；
        # 温度数组下标由配置中的仪器通道号 ch - 1 得到。
        ser,ut3200_routes = HAL_ut3200_serial.HAL_UT3200_InitDevice(snapshot)
        print("[HAL] UT3200 初始化成功")

        # sock 为已绑定的 UDP 句柄；dog_routes 的三项为（设备下标、信号下标、报文字段下标）。
        # 三项均从 0 开始；停用信号仍占据报文字段位置，不会使后续字段前移。
        sock,dog_routes = HAL_udp_receive.HAL_Dog_Open(snapshot,host="0.0.0.0", port = 8080, timeout=0.5)
        print("[HAL] UDP 初始化成功，绑定 %s:%d"% sock.getsockname())
        #创建HAL的线程：
        thread_ut3200 = threading.Thread(
            target = HAL_ut3200_serial.HAL_UT3200_CollectTempData_Thread,
            args = (ser,stop_event,ut3200_routes,data_write_callback),
            name = "ut3200_collect",
            daemon = True,)
        thread_dog =threading.Thread(
            target = HAL_udp_receive.HAL_Dog_ReceiveData_Thread,
            args = (sock,stop_event,dog_routes,data_write_callback),
            name = "dog_collect",
            daemon = True, )
        for t in (thread_ut3200,thread_dog):
            threads.append(t)
            t.start()
            print("[HAL] 线程 %s 已启动" % t.name)
    except BaseException:
        try:
            HAL_Shutdown()
        except Exception as cleanup_error:
            print("[HAL] 启动失败后未完成清理：%s" % cleanup_error)
        # 保留并上报启动异常
        raise

def HAL_Shutdown():
    """
    通知线程退出，等待结束，再关闭设备。
    允许重复调用；关闭未完成时抛出异常。
    """
    global ser, sock
    alive_thread = []             # list[str]，等待后仍存活的线程名；有残留则保留硬件句柄
    errors = []                   # list[str]，分别收集硬件关闭错误，最后统一上报
    stop_event.set()

    for t in threads:
        if t.is_alive():
            t.join(timeout = 3.0)

    for t in threads:
        if t.is_alive():
            alive_thread.append(t.name)
    if alive_thread :
    # 保留记录，避免仍有线程运行时再次启动 HAL
    # ", ".join( alive_thread)：把列表里的字符串，用 ", " 连接起来
        raise RuntimeError("线程尚未退出，关闭未完成：" + ", ".join( alive_thread))
    threads.clear()

    #两个设备分别关闭，一个失败也继续处理另一个
    if ser is not None:
        try:
            ser.close()
        except Exception as error:
            errors.append("关闭串口失败：%s" % error)
        else:
            ser = None
            print("[HAL] UT3200 串口已关闭")

    if sock is not None:
        try:
            sock.close()
        except Exception as error:
            errors.append("关闭 UDP socket 失败：%s" % error)
        else:
            sock = None
            print("[HAL] UDP socket 已关闭")

    if errors:
        raise RuntimeError("；".join(errors))
    print("[HAL] 线程已停止，硬件已关闭")






