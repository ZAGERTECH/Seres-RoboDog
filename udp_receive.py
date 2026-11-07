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
    except Exception:
        sock.close()
        raise
    return sock

def HAL_Dog_ReceiveData_Thread(sock, stop_event):
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
            print(f'{timestamp}来自{address}:{values_udp}')
    finally:
        stop_event.set()
        print("接收线程已结束")

def main():
    sock = None
    receiver_thread = None
    stop_event = threading.Event()
    try:
        sock = HAL_Dog_Open(host="0.0.0.0", port=8080,timeout=3.0)
        host, port = sock.getsockname()
        print(f"UDP 已打开，绑定 {host}:{port}")
        receiver_thread = threading.Thread(
            target = HAL_Dog_ReceiveData_Thread,
            args = (sock,stop_event),
            daemon = False
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

if __name__ == "__main__":
    main()
