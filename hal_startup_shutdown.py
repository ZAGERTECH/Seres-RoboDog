import threading
import ut3200_serial
import udp_receive

threads= []
stop_event = threading.Event()
ser = None
sock = None

def find_signals(devices,api):
    """按 api 查找并返回 DataHub 原有的信号列表。"""
    for device in devices:
        if device.api == api:
            return  device.signals
    raise ValueError("配置中未找到设备 api：%s" % api)

def HAL_WaitForStop(timeout=0.2):
    """收到停止通知返回 True，等待超时返回 False。"""
    return stop_event.wait(timeout)

def HAL_HasResources():
    """是否仍持有设备句柄或线程记录。"""
    return ser is not None or sock is not None or bool(threads)

def HAL_Start(devices,data_write):
    """
    初始化硬件并启动采集线程。
    devices：BLL 已初始化的设备列表。
    data_write：BLL 传入的写入函数，不是函数调用结果。
    正常返回表示启动流程完成，失败则抛出异常。
    """
    global ser,sock
    if  HAL_HasResources():
        raise RuntimeError("硬件设备尚未关闭，不能重复启动")
    ut3200_signals = find_signals(devices,"ut3200.fetch_all")
    dog_signals = find_signals(devices,"udp.parse_frame")
    stop_event.clear()

    try:
        #初始化HAL层的设备：ut3200和dog
        ser = ut3200_serial.HAL_UT3200_InitDevice()
        if ser is None:
            raise RuntimeError("UT3200 未找到仪器")
        print("[HAL] UT3200 初始化成功")

        sock = udp_receive.HAL_Dog_Open(host="0.0.0.0",port = 8080,timeout=0.5)
        print("[HAL] UDP 初始化成功，绑定 %s:%d"% sock.getsockname())
        #创建HAL的线程：
        thread_ut3200 = threading.Thread(
            target = ut3200_serial.HAL_UT3200_CollectTempData_Thread,
            args = (ser,stop_event,ut3200_signals,data_write),
            name = "ut3200_collect",
            daemon = True,)
        thread_dog =threading.Thread(target = udp_receive.HAL_Dog_ReceiveData_Thread,
                                     args = (sock,stop_event,dog_signals,data_write),
                                     name = "dog_collect",
                                     daemon = True,)
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
    alive_thread = []
    errors = []
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






