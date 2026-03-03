"""
日志配置模块

日志文件存放在脚本所在目录（工作目录）下，
文件名格式：cpp-debugger-mcp{YYYY-MM-DD}.log
例如：cpp-debugger-mcp{2025-11-11}.log
"""

import os
import logging
import logging.handlers
from datetime import datetime


# 脚本所在目录（工作目录）
_script_dir = os.path.dirname(os.path.abspath(__file__))

# 日志级别（可通过环境变量 CPP_DEBUGGER_LOG_LEVEL 覆盖，默认 INFO）
_log_level_str = os.environ.get("CPP_DEBUGGER_LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_str, logging.INFO)

# 日志格式
_log_format = "[%(asctime)s][%(levelname)s][%(name)s] %(message)s"
_date_format = "%Y-%m-%d %H:%M:%S"


def _get_log_filename() -> str:
    """生成日志文件名：cpp-debugger-mcp{YYYY-MM-DD}.log"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    return f"cpp-debugger-mcp{{{date_str}}}.log"


def _get_log_filepath() -> str:
    """获取日志文件完整路径"""
    return os.path.join(_script_dir, _get_log_filename())


class _DailyFileHandler(logging.FileHandler):
    """
    自定义日志处理器：按日期切换日志文件。

    每次写日志时检查日期是否变化，如果变化则切换到新的日志文件。
    日志文件名格式：cpp-debugger-mcp{YYYY-MM-DD}.log
    单个日志文件不限制大小（按天切割已足够）。
    """

    def __init__(self):
        self._current_date = datetime.now().date()
        filepath = _get_log_filepath()
        super().__init__(filepath, mode="a", encoding="utf-8")

    def emit(self, record):
        """写日志前检查是否需要切换文件"""
        now_date = datetime.now().date()
        if now_date != self._current_date:
            # 日期变化，切换日志文件
            self._current_date = now_date
            # 关闭当前文件
            if self.stream:
                self.stream.close()
                self.stream = None  # type: ignore
            # 更新文件路径
            self.baseFilename = _get_log_filepath()
            # 重新打开文件
            self.stream = self._open()
        super().emit(record)


# 全局文件处理器（延迟初始化，避免导入时就创建文件）
_file_handler: logging.Handler | None = None
_handler_lock = __import__("threading").Lock()


def _ensure_handler() -> logging.Handler:
    """确保文件处理器已初始化（线程安全）"""
    global _file_handler
    if _file_handler is not None:
        return _file_handler
    with _handler_lock:
        if _file_handler is not None:
            return _file_handler
        handler = _DailyFileHandler()
        handler.setLevel(_log_level)
        handler.setFormatter(logging.Formatter(_log_format, datefmt=_date_format))
        _file_handler = handler
        return _file_handler


def get_logger(name: str) -> logging.Logger:
    """
    获取一个已配置好的 logger 实例。

    Args:
        name: logger 名称，通常传模块名，如 "server"、"debugger"、"win_job_monitor"

    Returns:
        配置好文件输出的 logging.Logger 实例
    """
    logger = logging.getLogger(f"cpp-debugger.{name}")
    logger.setLevel(_log_level)
    # 避免重复添加 handler
    if not logger.handlers:
        logger.addHandler(_ensure_handler())
        # 不传播到 root logger，避免 MCP 框架的日志处理干扰
        logger.propagate = False
    return logger


def set_log_level(level_str: str) -> None:
    """
    运行时动态设置日志级别（供命令行参数 --log-level 调用）。

    会同时更新全局级别、已创建的所有 logger 和 file handler。

    Args:
        level_str: 日志级别字符串，如 "DEBUG"、"INFO"、"WARNING"、"ERROR"
    """
    global _log_level
    level_str = level_str.upper()
    new_level = getattr(logging, level_str, None)
    if new_level is None:
        return  # 无效级别，忽略
    _log_level = new_level
    # 更新已有的 file handler
    if _file_handler is not None:
        _file_handler.setLevel(new_level)
    # 更新所有已创建的 cpp-debugger.* logger
    for logger_name, logger_obj in logging.Logger.manager.loggerDict.items():
        if isinstance(logger_obj, logging.Logger) and logger_name.startswith("cpp-debugger."):
            logger_obj.setLevel(new_level)
