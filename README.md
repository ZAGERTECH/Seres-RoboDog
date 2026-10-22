# Seres RoboDog

UT3200 系列仪器串口温度采集脚本。

## 环境配置

项目使用 Python 3.13 和 `pyserial`。在 Windows PowerShell 中执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 运行

```powershell
.\.venv\Scripts\python.exe .\ut3200_serial.py
```

脚本默认自动扫描串口，并通过 SCPI 指令查找 UT3208+ 仪器。请先连接仪器并确认 Windows 已安装对应的串口驱动。
