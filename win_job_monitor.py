"""
Windows Job Object 子进程创建监控模块

通过 Win32 Job Object + I/O Completion Port (IOCP) 实现内核级别的子进程创建事件推送。
当被调试进程创建子进程时，IOCP 会收到 JOB_OBJECT_MSG_NEW_PROCESS 通知，
从而实现零延迟、不漏进程的子进程捕获。

仅在 Windows 8+ 上可用（需要嵌套 Job Object 支持）。
Win7 及以下版本不支持此功能。

使用方法：
    monitor = JobMonitor()
    monitor.start(target_pid=12345, process_name="child.exe", callback=on_found)
    # ... 当子进程创建时，callback 会被调用，参数为子进程 PID
    monitor.stop()
"""

import sys
import platform
import threading
import ctypes
import ctypes.wintypes
import os
from typing import Optional, Callable
from logger import get_logger

_logger = get_logger("win_job_monitor")

# =====================================================================
# 平台检测
# =====================================================================

def is_supported() -> bool:
    """检测当前平台是否支持 Job Object 子进程监控（需要 Windows 8+）"""
    if platform.system() != "Windows":
        return False
    ver = sys.getwindowsversion()
    # Windows 8 = 6.2, Windows 8.1 = 6.3, Windows 10 = 10.0
    if ver.major > 6:
        return True
    if ver.major == 6 and ver.minor >= 2:
        return True
    return False


# =====================================================================
# Win32 API 常量和结构体定义
# =====================================================================

# Job Object 信息类型
_JobObjectAssociateCompletionPortInformation = 7

# Job Object 通知消息类型
JOB_OBJECT_MSG_NEW_PROCESS = 6
JOB_OBJECT_MSG_EXIT_PROCESS = 7
JOB_OBJECT_MSG_ABNORMAL_EXIT_PROCESS = 8
JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 4

# 进程访问权限
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400

# 进程快照
TH32CS_SNAPPROCESS = 0x00000002

# 错误码常量
ERROR_INVALID_PARAMETER = 87
ERROR_ACCESS_DENIED = 5

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
INFINITE = 0xFFFFFFFF
MAX_PATH = 260


class JOBOBJECT_ASSOCIATE_COMPLETION_PORT(ctypes.Structure):
    """Job Object 关联 IOCP 的信息结构体"""
    _fields_ = [
        ("CompletionKey", ctypes.c_void_p),
        ("CompletionPort", ctypes.wintypes.HANDLE),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    """进程快照条目结构体（用于查询进程名）"""
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


# =====================================================================
# Win32 API 函数声明
# =====================================================================

kernel32 = ctypes.windll.kernel32

# Job Object
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
kernel32.CreateJobObjectW.restype = ctypes.wintypes.HANDLE

kernel32.SetInformationJobObject.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.wintypes.DWORD
]
kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL

kernel32.AssignProcessToJobObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL

kernel32.IsProcessInJob.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.BOOL)]
kernel32.IsProcessInJob.restype = ctypes.wintypes.BOOL

# IOCP
kernel32.CreateIoCompletionPort.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulong), ctypes.wintypes.DWORD
]
kernel32.CreateIoCompletionPort.restype = ctypes.wintypes.HANDLE

kernel32.GetQueuedCompletionStatus.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.POINTER(ctypes.wintypes.DWORD),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
    ctypes.wintypes.DWORD,
]
kernel32.GetQueuedCompletionStatus.restype = ctypes.wintypes.BOOL

kernel32.PostQueuedCompletionStatus.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong)
]
kernel32.PostQueuedCompletionStatus.restype = ctypes.wintypes.BOOL

# 进程
kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE

kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

# 进程快照（用于查进程名）
kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE

kernel32.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL

kernel32.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = ctypes.wintypes.BOOL

kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = ctypes.wintypes.DWORD


# =====================================================================
# 辅助函数
# =====================================================================

def is_process_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否仍然存活"""
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def get_process_name_by_pid(pid: int) -> Optional[str]:
    """通过进程快照查询指定 PID 的进程可执行文件名（不含路径）"""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32ProcessID == pid:
                    return entry.szExeFile
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def _match_process_name(actual_name: str, target_name: str) -> bool:
    """匹配进程名，忽略大小写和 .exe 后缀差异"""
    actual = actual_name.lower()
    target = target_name.lower()
    # 直接匹配
    if actual == target:
        return True
    # 加上 .exe 后缀匹配
    if not target.endswith(".exe"):
        if actual == target + ".exe":
            return True
    # 去掉 .exe 后缀匹配
    if actual.endswith(".exe") and actual[:-4] == target:
        return True
    return False


# =====================================================================
# JobMonitor 核心类
# =====================================================================

class JobMonitor:
    """
    Windows Job Object 子进程创建监控器。

    通过将目标进程加入 Job Object，并关联 IOCP，监听内核级别的
    JOB_OBJECT_MSG_NEW_PROCESS 事件，实现零延迟子进程创建捕获。

    用法：
        def on_child_found(pid: int, name: str):
            print(f"子进程已创建: {name} (PID: {pid})")

        monitor = JobMonitor()
        error = monitor.start(target_pid=12345, process_name="child.exe", callback=on_child_found)
        if error:
            print(f"启动失败: {error}")
        # ... 等待 ...
        monitor.stop()
    """

    def __init__(self):
        self._job_handle: Optional[ctypes.wintypes.HANDLE] = None
        self._iocp_handle: Optional[ctypes.wintypes.HANDLE] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._target_process_name: str = ""
        self._found_pid: Optional[int] = None
        self._error: Optional[str] = None

    @property
    def found_pid(self) -> Optional[int]:
        """已捕获的子进程 PID，未捕获时为 None"""
        return self._found_pid

    @property
    def is_running(self) -> bool:
        """监控是否正在运行"""
        return self._monitor_thread is not None and self._monitor_thread.is_alive()

    @property
    def error(self) -> Optional[str]:
        """如果监控出错，返回错误信息"""
        return self._error

    def start(
        self,
        target_pid: int,
        process_name: str,
        callback: Optional[Callable[[int, str], None]] = None,
    ) -> Optional[str]:
        """
        启动子进程创建监控。

        Args:
            target_pid: 被监控的父进程 PID（通常是被调试进程的 PID）
            process_name: 要等待的子进程可执行文件名（不含路径）
            callback: 子进程创建时的回调函数，参数为 (pid, process_name)。
                      回调在监控线程中执行，注意线程安全。

        Returns:
            成功返回 None，失败返回错误信息字符串
        """
        if self.is_running:
            return "监控已在运行中"

        self._target_process_name = process_name
        self._found_pid = None
        self._error = None
        self._stop_event.clear()

        # 1. 创建 Job Object
        self._job_handle = kernel32.CreateJobObjectW(None, None)
        if not self._job_handle:
            err = kernel32.GetLastError()
            return f"CreateJobObjectW 失败 (错误码: {err})"

        # 2. 创建 IOCP
        self._iocp_handle = kernel32.CreateIoCompletionPort(
            ctypes.wintypes.HANDLE(INVALID_HANDLE_VALUE), None, None, 1
        )
        if not self._iocp_handle:
            err = kernel32.GetLastError()
            self._cleanup_handles()
            return f"CreateIoCompletionPort 失败 (错误码: {err})"

        # 3. 关联 Job Object 和 IOCP
        port_info = JOBOBJECT_ASSOCIATE_COMPLETION_PORT()
        port_info.CompletionKey = self._job_handle
        port_info.CompletionPort = self._iocp_handle
        ret = kernel32.SetInformationJobObject(
            self._job_handle,
            _JobObjectAssociateCompletionPortInformation,
            ctypes.byref(port_info),
            ctypes.sizeof(port_info),
        )
        if not ret:
            err = kernel32.GetLastError()
            self._cleanup_handles()
            return f"SetInformationJobObject 失败 (错误码: {err})"

        # 4. 打开目标进程并加入 Job Object
        _logger.info("打开目标进程 PID=%d 并加入 Job Object", target_pid)
        h_process = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, target_pid
        )
        if not h_process:
            err = kernel32.GetLastError()
            self._cleanup_handles()
            if err == ERROR_INVALID_PARAMETER:
                _logger.error("OpenProcess(%d) 失败：目标进程已退出或 PID 无效", target_pid)
                return (f"OpenProcess({target_pid}) 失败：目标进程已退出或 PID 无效。\n"
                        f"请确保被调试进程仍在运行（处于断点暂停状态），然后再创建 waitfor 实例。")
            elif err == ERROR_ACCESS_DENIED:
                _logger.error("OpenProcess(%d) 失败：权限不足 (错误码: %d)", target_pid, err)
                return f"OpenProcess({target_pid}) 失败：权限不足（错误码: {err}），可能需要管理员权限"
            else:
                return f"OpenProcess({target_pid}) 失败 (错误码: {err})"

        ret = kernel32.AssignProcessToJobObject(self._job_handle, h_process)
        kernel32.CloseHandle(h_process)
        if not ret:
            err = kernel32.GetLastError()
            _logger.error("AssignProcessToJobObject 失败 (错误码: %d)", err)
            self._cleanup_handles()
            return f"AssignProcessToJobObject 失败 (错误码: {err})，进程可能已在不兼容的 Job 中"

        # 5. 启动 IOCP 监控线程
        self._monitor_thread = threading.Thread(
            target=self._iocp_monitor_loop,
            args=(callback,),
            daemon=True,
        )
        self._monitor_thread.start()
        _logger.info("JobMonitor 已启动，监控 PID=%d，等待进程=%s", target_pid, process_name)
        return None  # 成功

    def stop(self):
        """停止监控并清理资源"""
        _logger.info("JobMonitor.stop 调用")
        self._stop_event.set()
        # 向 IOCP 投递一个退出信号，唤醒 GetQueuedCompletionStatus
        if self._iocp_handle:
            kernel32.PostQueuedCompletionStatus(self._iocp_handle, 0, None, None)
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3.0)
        self._cleanup_handles()
        self._monitor_thread = None

    def _iocp_monitor_loop(self, callback: Optional[Callable[[int, str], None]]):
        """IOCP 监控循环：等待子进程创建事件"""
        bytes_transferred = ctypes.wintypes.DWORD(0)
        completion_key = ctypes.POINTER(ctypes.c_ulong)()
        overlapped = ctypes.POINTER(ctypes.c_ulong)()

        while not self._stop_event.is_set():
            # 等待 IOCP 事件，超时 500ms 以便检查 stop_event
            ret = kernel32.GetQueuedCompletionStatus(
                self._iocp_handle,
                ctypes.byref(bytes_transferred),
                ctypes.byref(completion_key),
                ctypes.byref(overlapped),
                500,  # 超时毫秒
            )

            if self._stop_event.is_set():
                break

            if not ret:
                # 超时（ERROR_TIMEOUT = 258）或其他错误，继续循环
                continue

            msg_type = bytes_transferred.value
            # overlapped 实际上包含了进程 PID（被重用为消息参数）
            new_pid = ctypes.cast(overlapped, ctypes.c_void_p).value or 0

            if msg_type == JOB_OBJECT_MSG_NEW_PROCESS and new_pid > 0:
                # 有新进程创建，查询进程名
                proc_name = get_process_name_by_pid(new_pid)
                _logger.debug("IOCP 收到新进程通知: PID=%d, name=%s", new_pid, proc_name)
                if proc_name and _match_process_name(proc_name, self._target_process_name):
                    _logger.info("目标子进程已捕获: PID=%d, name=%s", new_pid, proc_name)
                    self._found_pid = new_pid
                    if callback:
                        try:
                            callback(new_pid, proc_name)
                        except Exception:
                            pass
                    break  # 找到目标进程，退出监控循环

            elif msg_type == JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO:
                # Job 中所有进程都已退出
                _logger.warning("被监控进程及所有子进程已全部退出，未检测到目标子进程")
                self._error = "被监控进程及所有子进程已全部退出，未检测到目标子进程"
                break

    def _cleanup_handles(self):
        """清理 Win32 句柄"""
        if self._iocp_handle:
            kernel32.CloseHandle(self._iocp_handle)
            self._iocp_handle = None
        if self._job_handle:
            kernel32.CloseHandle(self._job_handle)
            self._job_handle = None
