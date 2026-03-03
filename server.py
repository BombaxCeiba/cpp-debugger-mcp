"""
C++ Debugger MCP 服务
通过 MCP 协议暴露 C++ 调试功能，允许 AI 助手调试 C++ 程序。
自动检测并使用 lldb（优先）或 gdb 作为调试后端。
"""

import os
import sys
import json
import shutil
import argparse
import platform

# 将脚本所在目录加入模块搜索路径，确保从任意位置用绝对路径启动时都能正确导入
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from mcp.server.fastmcp import FastMCP
from debugger import CppDebugger
from logger import get_logger, set_log_level

_logger = get_logger("server")


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="C++ Debugger MCP 服务",
    )
    parser.add_argument(
        "--llvm-path",
        type=str,
        default=None,
        help="LLVM 工具链的路径，将被添加到 PATH 最前面以确保优先使用该路径下的工具（如 lldb、clang++ 等）",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=30.0,
        help="程序运行类命令（run/continue/step_out）的最大等待超时时间（秒），默认 30 秒。"
        "超时后不会丢失输出，而是缓存起来，AI 可通过 debug_get_pending_output 获取",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help="日志级别，可选值：DEBUG, INFO, WARNING, ERROR。默认 INFO。"
        "也可通过环境变量 CPP_DEBUGGER_LOG_LEVEL 设置",
    )
    # 使用 parse_known_args 避免与 MCP 框架自身的参数冲突
    args, _ = parser.parse_known_args()
    return args


def setup_llvm_path(llvm_path: str):
    """将用户指定的 LLVM 路径插入到 PATH 环境变量的最前面"""
    llvm_path = os.path.abspath(llvm_path)
    if not os.path.isdir(llvm_path):
        _logger.error("指定的 LLVM 路径不存在：%s", llvm_path)
        print(f"警告：指定的 LLVM 路径不存在：{llvm_path}", file=sys.stderr)
        sys.exit(1)
    # 将 llvm_path 插到 PATH 最前面，确保优先使用
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = llvm_path + os.pathsep + current_path
    _logger.info("已将 LLVM 路径添加到 PATH 最前面：%s", llvm_path)
    print(f"已将 LLVM 路径添加到 PATH 最前面：{llvm_path}", file=sys.stderr)


# 解析命令行参数并设置 PATH
_args = parse_args()
if _args.log_level:
    set_log_level(_args.log_level)
if _args.llvm_path:
    setup_llvm_path(_args.llvm_path)

# 运行超时配置
_run_timeout: float = _args.run_timeout

_logger.info("cpp-debugger-mcp 服务启动，run_timeout=%.1f", _run_timeout)

# 检测 lldb 是否可用（在 setup_llvm_path 之后检测，确保 PATH 已更新）
_has_lldb = bool(shutil.which("lldb"))
_logger.info("lldb 可用: %s", _has_lldb)

# 检测是否支持 waitfor 模式（子进程自动捕获）
# - Windows 8+：通过 Job Object + IOCP 内核级监控，不依赖 lldb
# - 非 Windows：通过 lldb 的 process attach --waitfor 命令
if platform.system() == "Windows":
    _win_ver = sys.getwindowsversion()
    _can_waitfor = (_win_ver.major > 6) or (_win_ver.major == 6 and _win_ver.minor >= 2)
else:
    _can_waitfor = _has_lldb


# waitfor 模式的 instructions 内容（waitfor 可用时拼接）
_waitfor_instructions = """

### waitfor 模式（子进程捕获）
当子进程生命周期短、无法通过常规 attach 及时捕获时，使用 waitfor 模式：
- debug_create_instance(process_name="xxx")：创建 waitfor 实例，等待目标进程启动后自动附加
- waitfor 等待中的实例不能执行 debug_run/debug_load/debug_continue 等操作
- 子进程被捕获后，会通过跨实例通知（⚡ [通知][事件:xxx] 标记）附带在其他工具的返回值中
- 取消等待：debug_detach(debugger_id=N)

### 跨实例状态变化通知
工具返回值末尾可能包含其他调试会话的状态变化通知（以 ⚡ [通知] 标记），格式为：
  ⚡ [通知][调试器 #N][事件:事件类型] 描述文本
事件类型包括：
- [事件:waitfor_triggered]：waitfor 已触发，子进程已被捕获并暂停
- [事件:waitfor_failed]：waitfor 失败（调试器退出、attach 失败等）
注意查看这些通知，优先处理状态变化的实例。""" if _can_waitfor else ""

_waitfor_ipc_flow = """

### waitfor IPC 调试流程（推荐，子进程启动快时使用）
1. debug_start → debug_load("./parent") → debug_run(stop_at_entry=True)
2. debug_create_instance(process_name="child_app") → 创建 waitfor 实例 #N
3. debug_continue(debugger_id=0) → 主进程继续运行，触发子进程启动
4. 返回值中收到 ⚡ [通知][事件:waitfor_triggered]：waitfor 已触发
5. debug_set_breakpoint("handler", debugger_id=N) → 子进程设断点
6. debug_continue(debugger_id=N) → 子进程继续执行
7. debug_get_variables(debugger_id=N) → 查看子进程变量
8. debug_detach(debugger_id=N) → 脱离子进程
9. debug_stop() → 结束所有调试""" if _can_waitfor else ""

_waitfor_error_rows = """
| waitfor 模式不可用 | "不支持此功能" | 当前平台或系统版本不支持 waitfor |
| waitfor 等待中操作 | "正在 waitfor 等待中" | 等待子进程捕获后再操作，或 debug_detach 取消 |""" if _can_waitfor else ""

# 创建 MCP 服务实例
mcp = FastMCP(
    "C++ Debugger MCP",
    instructions=f"""C++ 调试 MCP 服务，支持 lldb/gdb 双后端，仅调试已编译的 -g 可执行文件，不负责编译。

# ═══════════════════════════════════════════════
# 第一层：执行宪法（不可违反）
# ═══════════════════════════════════════════════

█ 规则1（先验后动）：长时间状态变更工具（debug_run/debug_continue/debug_step_out）返回后，先读 [状态:xxx] 标记和 next=[...] 白名单，再决定下一步。优先从 next 白名单选择工具；无白名单则查状态可用工具表。当 next=[...] 与状态可用工具表冲突时，以 next 为准（运行时动态推荐 > 静态表格）。短暂状态变更工具（debug_step_over/debug_step_into）通常瞬时完成（暂停→暂停），返回值包含 [状态:xxx] 和 next 白名单，按其指引操作即可。
█ 规则2（状态门控）：debug_get_variables、debug_evaluate、debug_source_context、debug_backtrace、debug_select_frame 仅在 [状态:已暂停] 时调用，其他状态调用无效。
█ 规则3（超时恢复）：收到 [超时] → 第一步固定调用 debug_get_pending_output 获取调试器状态变化（判断程序是否已暂停/结束）；第二步（可选）调用 debug_get_program_output(mode="tail") 查看程序自身的 stdout/stderr 打印输出。顺序不可颠倒：必须先确认状态，再看输出。
█ 规则4（结束确认）：[状态:已结束] → 禁止 debug_continue/debug_step_*/debug_get_variables/debug_evaluate → 仅允许 debug_get_program_output 查看输出、debug_run 重启、debug_stop 结束。
█ 规则5（防空转）：同一状态变更工具连续调用 3 次且 [状态:xxx] 未变化即为无进展 → 必须切换策略（debug_list_breakpoints 检查断点 / debug_get_program_output(mode="tail") 查看输出 / 检查编译选项）并向用户说明。查询类工具（debug_get_pending_output/debug_get_program_output）不重置计数。
█ 规则6（输出获取）：程序 printf/cout 内容用 debug_get_program_output 获取，不从调试返回值拼凑。输出量大时优先 mode="tail"。
█ 规则7（兜底校验）：若工具返回不含 [状态:xxx] 标记，或标记与预期冲突 → 先调 debug_get_pending_output + debug_list_debuggers 做一致性校验 → 再决定下一步。
█ 规则8（raw_command 护栏）：debug_raw_command 仅在标准工具无法覆盖时才调用；调用前须在回复中自说明原因与预期影响，调用后必须立即做状态校验（读取 [状态:xxx] + next 确认）。

优先级：执行宪法 > 决策参考表 > 示例流程

# ═══════════════════════════════════════════════
# 第二层：决策参考（结构化表格）
# ═══════════════════════════════════════════════

## 调试器状态机

  [状态:未启动] --debug_start--> [状态:已启动] --debug_load--> [状态:已加载]
  [状态:已加载] --debug_run--> [状态:运行中]/[状态:已暂停]
  [状态:已暂停] --debug_continue/step_*--> [状态:运行中]/[状态:已暂停]
  [任意状态] --debug_stop--> [状态:未启动]

### 各状态可用工具
- 未启动 → 仅 debug_start
- 已启动 → debug_load, debug_stop
- 已加载 → debug_set_breakpoint, debug_run, debug_stop（建议先设断点再 run）
- 已暂停 → 所有检查和控制工具（包括 debug_set_breakpoint、debug_step_over、debug_step_into、debug_continue 等）
- 运行中 → debug_get_pending_output, debug_get_program_output, debug_stop

注意：debug_get_program_output 可在程序运行过（至少调用过 debug_run）的任何状态下使用。

## 返回值状态标记

- [状态:未启动] → 调试器未启动或已关闭 → 仅 debug_start
- [状态:已启动] → 调试会话已启动，未加载程序 → debug_load, debug_stop
- [状态:已加载] → 可执行文件已加载 → debug_set_breakpoint, debug_run, debug_stop
- [状态:已暂停] → 程序在断点/单步处停下 → 所有工具均可用（包括 debug_set_breakpoint）
- [状态:运行中] → 程序正在执行中 → debug_get_pending_output, debug_get_program_output, debug_stop
- [状态:已结束] → 程序已退出 → debug_get_program_output, debug_run 重启, debug_stop 结束（规则4）
- [超时] → 等待超时，程序可能仍在运行 → 按 next 白名单选择（规则3）
- [错误] → 操作失败 → 按错误返回中的 next 白名单恢复，无白名单则按失败分型表恢复

所有状态变更工具的返回值都包含 **next=[...]** 白名单：
- 长时间状态变更工具（debug_run、debug_continue、debug_step_out）：可能超时，返回值带状态标记和 next 白名单
- 短暂状态变更工具（debug_step_over、debug_step_into）：通常瞬时完成，返回值带状态标记和 next 白名单

## 失败分型与恢复策略

- **进程未启动**（"进程未启动"、RuntimeError）→ 仅 debug_start，禁止一切其他工具
- **目标未加载**（加载相关错误）→ debug_load 加载可执行文件，禁止 debug_run/debug_set_breakpoint
- **程序未暂停**（检查类工具返回空/无效）→ 按当前 [状态:xxx] 分支：运行中→debug_get_pending_output；未启动→debug_start；已启动→debug_load→debug_run；已加载→debug_run；已结束→debug_get_program_output 或 debug_run 重启。禁止 debug_get_variables/debug_evaluate/debug_source_context/debug_backtrace/debug_step_*/debug_select_frame（规则2）
- **程序已结束**（"exited"、"process exited"）→ debug_get_program_output 查看输出 / debug_run 重启 / debug_stop 结束。禁止 debug_continue/debug_step_*/debug_get_variables/debug_evaluate（规则4）
- **断点未命中**（程序运行结束但未暂停）→ debug_list_breakpoints 检查断点 → 确认文件名/行号 → 确认 -g 编译。禁止反复 debug_run 而不检查断点
- **符号未找到**（"symbol not found"）→ 检查函数名拼写；确认 -g 编译且未被 strip
- **变量不可用**（"variable not available"）→ 可能被优化掉，建议 -O0 编译
- **超时**（"[超时]"）→ 第一步 debug_get_pending_output 确认状态，第二步（可选）debug_get_program_output(mode="tail") 查看输出（规则3），禁止检查类工具（规则2）
- **文件不存在**（"文件不存在"）→ 检查可执行文件路径是否正确

## 超时处理流程

debug_run、debug_continue、debug_step_out 可能因程序长时间运行而超时（默认 30 秒，可通过 --run-timeout 配置）。

1. 收到 **[超时]** → 程序仍在运行，输出已缓存
2. **第一步（必须）**：debug_get_pending_output → 获取调试器缓存的控制输出（断点命中、程序退出等状态变化），判断程序状态（[状态:已暂停]/[状态:运行中]/[状态:已结束]）。如果调试器无新输出，会自动附带程序最后 10 行输出供参考。
3. **第二步（可选）**：debug_get_program_output(mode="tail") → 查看程序自身的 stdout/stderr 打印输出（如 printf/cout），与第一步获取的调试器控制输出不同
4. 根据返回的 [状态:xxx] 决定下一步：
   - [状态:已暂停]：断点已命中，继续检查
   - [状态:运行中]：程序还在执行，可 debug_get_pending_output 轮询或 debug_continue 继续等待
   - [状态:已结束]：程序已退出，查看输出或重新运行
5. 建议客户端超时设置 60-120 秒；超时不代表程序卡死

## 多进程调试（IPC 场景）

通过多调试器实例同时调试多个进程，每个实例独立控制一个进程。

### 核心概念
- **调试器 #0**：主调试器（launch 模式），debug_start → debug_load → debug_run
- **调试器 #1, #2, ...**：子调试器（attach 模式），通过 debug_attach_child(pid) 创建
- **debugger_id**：所有工具的可选参数（默认 0），指定操作目标
- 优先处理最近产生状态变化的 debugger_id，避免无意义轮询

### 多进程工具
- debug_list_children(debugger_id=0)：列出子进程（PID + 进程名）
- debug_attach_child(pid)：创建新实例并附加到子进程
- debug_detach(debugger_id)：脱离子进程并关闭实例（不能 detach #0）
- debug_list_debuggers()：列出所有活跃实例及状态
{_waitfor_instructions}

### 返回值标注
所有返回值包含 **[调试器 #N][进程: xxx (PID: xxx)]** 前缀标签。

### attach 模式限制
- attach 模式（#1+）不能调用 debug_run/debug_load，用 debug_continue 代替
- 最多同时 5 个调试器实例

## 多进程失败分型

- **attach 失败**（"无法附加到进程"）→ 检查 PID 是否正确、进程是否存在、是否需要管理员权限
- **实例不存在**（"调试器 #N 不存在"）→ debug_list_debuggers 查看可用实例
- **实例数量上限**（"实例数量已达上限"）→ 先 debug_detach 不需要的实例
- **无法 detach 主调试器**（"无法脱离主调试器"）→ 使用 debug_stop 结束整个会话
- **attach 模式下调用 run/load**（"附加模式下无法..."）→ 使用 debug_continue 代替 debug_run{_waitfor_error_rows}

# ═══════════════════════════════════════════════
# 第三层：示例与背景（按需阅读）
# ═══════════════════════════════════════════════

## 典型调试流程

debug_start → debug_load("./程序") → debug_set_breakpoint("main") → debug_run()
→ [状态:已暂停] → debug_source_context() → debug_get_variables()
→ debug_step_over()/debug_continue() → ... → debug_stop()

随时可调用 debug_get_program_output() 查看程序打印输出。输出量大时用 mode="tail"。

### 小程序快速调试（推荐）
debug_start → debug_load("./程序") → debug_run(stop_at_entry=True)
→ [状态:已暂停 - main 入口] → debug_source_context() → debug_set_breakpoint(...)
→ debug_continue() → ... → debug_stop()

小型程序建议 stop_at_entry=True，程序会在 main 入口自动暂停，有充足时间查看代码和设置断点。

### 超时场景调试（注意：超时后第一步固定调 debug_get_pending_output）
debug_run() → [超时] → debug_get_pending_output() → [状态:已暂停] → 正常调试
debug_run() → [超时] → debug_get_pending_output() → [状态:运行中] → debug_get_program_output(mode="tail") 查看输出 → debug_get_pending_output() → ...
debug_run() → [超时] → debug_get_pending_output() → [状态:已结束] → debug_get_program_output() 查看完整输出

### IPC 调试流程
1. debug_start → debug_load("./parent") → debug_run(stop_at_entry=True)
2. debug_list_children() → 找到子进程 PID
3. debug_attach_child(child_pid) → 获得调试器 #1
4. debug_set_breakpoint("on_message", debugger_id=1)
5. debug_continue(debugger_id=0) → 父进程继续
6. debug_get_pending_output(debugger_id=1) → 检查子进程断点
7. debug_get_variables(debugger_id=1) → 查看子进程变量
8. debug_detach(debugger_id=1) → 脱离子进程
9. debug_stop() → 结束所有调试
{_waitfor_ipc_flow}

## 环境变量

debug_start 支持 env_vars 参数（JSON 字符串），环境变量会透传给调试器和被调试程序。
示例：debug_start(env_vars='{{"MY_VAR": "hello", "LD_LIBRARY_PATH": "/opt/lib"}}')

## 补充说明
- 调试多线程：先 debug_thread_list 查看线程，再 debug_select_thread 切换
- 调用栈 frame 0 是最内层，用 debug_select_frame 切换帧后再查看变量
- 程序结束后可 debug_run 重新启动，无需再 debug_load
""",
)

# 全局调试器实例
_debugger = CppDebugger()


def _apply_run_timeout(debugger_id: int = 0):
    """将命令行配置的超时时间应用到调试器后端"""
    try:
        backend = _debugger._get_instance(debugger_id)
        backend.run_timeout = _run_timeout
    except RuntimeError:
        pass


def _log_call(func_name: str, **kwargs):
    """记录 MCP tool 调用入参"""
    # 过滤掉值为默认值的参数，让日志更简洁
    parts = [f"{k}={v!r}" for k, v in kwargs.items()]
    _logger.info("[CALL] %s(%s)", func_name, ", ".join(parts))


def _log_return(func_name: str, result: str) -> str:
    """记录 MCP tool 返回值，并原样返回 result"""
    _logger.info("[RETURN] %s\n%s", func_name, result)
    return result


def _add_step_state_tag(backend, result: str) -> str:
    """为短暂状态变更命令（step_over/step_into）的返回值追加状态标记和 next 白名单"""
    state_tag = backend._detect_program_state(result)
    if state_tag == "[状态:已结束]":
        return (f"{state_tag} {result}\n"
                f"next=[debug_get_program_output, debug_run, debug_stop]")
    elif state_tag == "[状态:已暂停]":
        return (f"{state_tag} {result}\n"
                f"next=[debug_source_context, debug_get_variables, debug_backtrace, debug_step_over, debug_step_into, debug_continue]")
    elif state_tag:
        return f"{state_tag} {result}"
    # 兜底：无法识别状态时默认标记为已暂停（step_over/step_into 通常瞬时完成，暂停→暂停）
    return (f"[状态:已暂停] {result}\n"
            f"next=[debug_source_context, debug_get_variables, debug_backtrace, debug_step_over, debug_step_into, debug_continue]")


# ========== 调试会话管理 ==========


@mcp.tool()
def debug_start(env_vars: str = "") -> str:
    """
    启动调试会话（所有调试操作的第一步）。

    自动检测系统中可用的调试器，优先使用 lldb，不可用时回退到 gdb。

    前置条件：无（可在任何时候调用）。
    后续步骤：调用 debug_load 加载要调试的可执行文件。

    Args:
            env_vars: （可选）环境变量 JSON 字符串，格式为 {"KEY": "VALUE", ...}。
            这些环境变量会透传给调试器进程和被调试的程序。
            示例：'{"LD_LIBRARY_PATH": "/opt/lib", "MY_DEBUG": "1"}'
            不传或传空字符串则不设置额外环境变量。

    Returns:
        启动结果，包含使用的调试器类型（lldb 或 gdb）。如果返回错误，通常是 lldb/gdb 均未安装。
    """
    _log_call("debug_start", env_vars=env_vars)
    parsed_env = None
    if env_vars and env_vars.strip():
        try:
            parsed_env = json.loads(env_vars)
            if not isinstance(parsed_env, dict):
                return _log_return("debug_start", "[错误] env_vars 必须是一个 JSON 对象（键值对），如 '{\"KEY\": \"VALUE\"}'\nnext=[debug_start]")
            # 确保所有键和值都是字符串
            parsed_env = {str(k): str(v) for k, v in parsed_env.items()}
        except json.JSONDecodeError as e:
            return _log_return("debug_start", f"[错误] env_vars 不是合法的 JSON 格式：{e}\nnext=[debug_start]")
    try:
        result = _debugger.start(env_vars=parsed_env)
    except FileNotFoundError as e:
        _logger.error("debug_start 失败（FileNotFoundError）: %s", e)
        return _log_return("debug_start", f"[错误] {e}\nnext=[debug_start]")
    except Exception as e:
        _logger.error("debug_start 失败: %s", e, exc_info=True)
        return _log_return("debug_start", f"[错误] 调试器启动失败：{e}\nnext=[debug_start]")
    _apply_run_timeout(0)
    return _log_return("debug_start", _debugger._format_output(0, result))


@mcp.tool()
def debug_stop() -> str:
    """
    停止调试会话并释放所有资源。

    调试完成后必须调用此工具来终止调试器进程。可在任何状态下调用。
    会依次停止所有调试器实例：先 detach 所有子实例（#1 及以上），再停止主实例（#0）。
    调用后调试器回到"未启动"状态，如需再次调试需重新 debug_start。

    Returns:
        停止结果信息。
    """
    _log_call("debug_stop")
    return _log_return("debug_stop", _debugger.stop())


@mcp.tool()
def debug_load(executable_path: str, debugger_id: int = 0) -> str:
    """
    加载要调试的 C++ 可执行文件。

    前置条件：必须先调用 debug_start 启动调试会话。
    后续步骤：调用 debug_set_breakpoint 设置断点，然后 debug_run 运行程序。

    注意：可执行文件必须使用 -g 选项编译（如 clang++ -g main.cpp -o main）才能正常显示源码和变量。

    Args:
        executable_path: 可执行文件的绝对或相对路径。如 "./build/my_program"、"C:/projects/test.exe"
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        加载结果。如果返回"文件不存在"错误，请检查路径是否正确。
    """
    _log_call("debug_load", executable_path=executable_path, debugger_id=debugger_id)
    if _debugger._is_attach_mode(debugger_id):
        return _log_return("debug_load", _debugger._format_output(debugger_id, "[错误] 附加模式下无法加载新的可执行文件。请使用 debug_continue 继续调试。\nnext=[debug_continue]"))
    if _debugger._is_waitfor_waiting(debugger_id):
        return _log_return("debug_load", _debugger._format_output(debugger_id, "[错误] 该实例正在 waitfor 等待中，尚未附加到任何进程。\nnext=[debug_detach, debug_list_debuggers]"))
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_load", str(e))
    result = backend.load_target(executable_path)
    # 更新元信息中的进程名
    meta = _debugger._instance_metadata.get(debugger_id, {})
    meta["process_name"] = os.path.basename(executable_path)
    return _log_return("debug_load", _debugger._format_output(debugger_id, result))


# ========== 断点管理 ==========


@mcp.tool()
def debug_set_breakpoint(location: str, condition: str = "", debugger_id: int = 0) -> str:
    """
    在指定位置设置断点。程序运行到该位置时会自动暂停，以便你检查程序状态。

    前置条件：必须先 debug_load 加载了可执行文件。可在 debug_run 之前或程序暂停时设置。

    Args:
        location: 断点位置，支持两种格式：
            - 函数名：如 "main"、"MyClass::method"、"calculate"
            - 文件名:行号：如 "main.cpp:10"、"src/utils.cpp:42"
        condition: （可选）条件表达式，仅当表达式为真时才暂停。
            示例："i == 5"、"count > 100"、"ptr != nullptr"、"name == \"test\""
            不传此参数则为无条件断点（每次到达都暂停）。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        断点设置结果，包含断点 ID（用于后续删除断点）。

    提示：用 debug_list_breakpoints 查看所有已设断点，用 debug_delete_breakpoint 删除不需要的断点。
    """
    _log_call("debug_set_breakpoint", location=location, condition=condition, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_set_breakpoint", str(e))
    result = backend.set_breakpoint(location, condition)
    return _log_return("debug_set_breakpoint", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_delete_breakpoint(breakpoint_id: str, debugger_id: int = 0) -> str:
    """
    删除指定 ID 的断点。

    前置条件：目标断点必须存在。可通过 debug_list_breakpoints 获取所有断点 ID。

    Args:
        breakpoint_id: 断点 ID 号（数字字符串），如 "1"、"3"
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        删除结果
    """
    _log_call("debug_delete_breakpoint", breakpoint_id=breakpoint_id, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_delete_breakpoint", str(e))
    result = backend.delete_breakpoint(breakpoint_id)
    return _log_return("debug_delete_breakpoint", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_list_breakpoints(debugger_id: int = 0) -> str:
    """
    列出所有已设置的断点及其详细信息（ID、位置、是否启用、命中次数、条件等）。

    前置条件：调试会话已启动。

    返回信息可用于：确认断点是否设置正确、获取断点 ID 以便删除、检查条件断点的条件表达式。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        断点列表。如果没有设置任何断点，会返回空列表或提示信息。
    """
    _log_call("debug_list_breakpoints", debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_list_breakpoints", str(e))
    result = backend.list_breakpoints()
    return _log_return("debug_list_breakpoints", _debugger._format_output(debugger_id, result))


# ========== 执行控制 ==========


@mcp.tool()
def debug_run(args: str = "", stop_at_entry: bool = False, debugger_id: int = 0) -> str:
    """
    运行被调试的程序。如果之前设置了断点，程序会在第一个命中的断点处暂停。

    前置条件：必须先 debug_load 加载了可执行文件。建议在 run 之前先设置好断点。
    后续步骤：程序暂停后，可用 debug_source_context 查看代码、debug_get_variables 查看变量、debug_step_over 单步执行等。

    重要：此工具可能耗时较长（等待程序运行到断点）。如果返回 [超时] 提示，说明程序还在运行中尚未到达断点。
    此时应立即调用 debug_get_pending_output 获取已缓存的输出，然后根据情况决定是继续等待（调用 debug_continue）还是检查程序状态。

    提示：如果程序已运行结束（正常退出或异常终止），可以再次调用 debug_run 重新启动程序，无需重新 debug_load。

    Args:
        args: （可选）传递给程序的命令行参数，多个参数用空格分隔。如 "input.txt --verbose" 或 "10 20 30"
        stop_at_entry: （可选）是否在程序入口（main 函数）处自动暂停，默认 False。
            设为 True 时，会自动在 main 处设置一次性临时断点，程序启动后立即暂停在 main 入口。
            适用场景：小型程序可能瞬间执行完毕，设为 True 可确保 agent 有时间查看代码和设置断点。
            临时断点命中后会自动删除，不影响用户手动设置的其他断点。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        运行结果。如果命中断点会显示暂停位置；如果超时会返回 [超时] 提示并缓存输出。
    """
    _log_call("debug_run", args=args, stop_at_entry=stop_at_entry, debugger_id=debugger_id)
    if _debugger._is_attach_mode(debugger_id):
        return _log_return("debug_run", _debugger._format_output(debugger_id, "[错误] 附加模式下无法使用 debug_run，请使用 debug_continue 继续执行。\nnext=[debug_continue]"))
    if _debugger._is_waitfor_waiting(debugger_id):
        return _log_return("debug_run", _debugger._format_output(debugger_id, "[错误] 该实例正在 waitfor 等待中，尚未附加到任何进程，无法 run。\nnext=[debug_detach, debug_list_debuggers]"))
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_run", str(e))
    result = backend.run(args, stop_at_entry=stop_at_entry)
    # 运行后尝试更新 PID 元信息
    _debugger._update_metadata_pid(debugger_id)
    return _log_return("debug_run", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_continue(debugger_id: int = 0) -> str:
    """
    从当前暂停位置继续执行程序，直到命中下一个断点或程序运行结束。

    前置条件：程序必须处于暂停状态（在断点处或单步后停下）。

    重要：此工具可能耗时较长。如果返回 [超时] 提示，说明程序还在运行中尚未到达断点。
    此时应立即调用 debug_get_pending_output 获取已缓存的输出，然后根据情况继续等待或检查程序状态。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        执行结果。会显示下一个暂停位置，或程序正常/异常退出信息，或 [超时] 提示。
    """
    _log_call("debug_continue", debugger_id=debugger_id)
    if _debugger._is_waitfor_waiting(debugger_id):
        return _log_return("debug_continue", _debugger._format_output(debugger_id, "[错误] 该实例正在 waitfor 等待中，尚未附加到任何进程，无法 continue。\nnext=[debug_detach, debug_list_debuggers]"))
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_continue", str(e))
    result = backend.continue_execution()
    return _log_return("debug_continue", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_step_over(debugger_id: int = 0) -> str:
    """
    单步执行当前行，不进入函数调用（Step Over）。如果当前行包含函数调用，会执行完整个函数后停在下一行。

    前置条件：程序必须处于暂停状态。
    适用场景：逐行查看程序执行流程，不关心被调函数的内部实现时使用。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        执行后的当前位置（文件名、行号、该行代码）。
    """
    _log_call("debug_step_over", debugger_id=debugger_id)
    if _debugger._is_waitfor_waiting(debugger_id):
        return _log_return("debug_step_over", _debugger._format_output(debugger_id, "[错误] 该实例正在 waitfor 等待中，尚未附加到任何进程，无法单步执行。\nnext=[debug_detach, debug_list_debuggers]"))
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_step_over", str(e))
    result = backend.step_over()
    result = _add_step_state_tag(backend, result)
    return _log_return("debug_step_over", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_step_into(debugger_id: int = 0) -> str:
    """
    单步执行，如果当前行包含函数调用，则进入该函数的第一行（Step Into）。

    前置条件：程序必须处于暂停状态。
    适用场景：需要深入查看某个函数的内部实现逻辑时使用。如果当前行没有函数调用，效果等同于 debug_step_over。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        执行后的当前位置（文件名、行号、该行代码）。
    """
    _log_call("debug_step_into", debugger_id=debugger_id)
    if _debugger._is_waitfor_waiting(debugger_id):
        return _log_return("debug_step_into", _debugger._format_output(debugger_id, "[错误] 该实例正在 waitfor 等待中，尚未附加到任何进程，无法单步执行。\nnext=[debug_detach, debug_list_debuggers]"))
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_step_into", str(e))
    result = backend.step_into()
    result = _add_step_state_tag(backend, result)
    return _log_return("debug_step_into", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_step_out(debugger_id: int = 0) -> str:
    """
    执行完当前函数的剩余部分，在返回到调用者后暂停（Step Out）。

    前置条件：程序必须处于暂停状态。
    适用场景：已经看完当前函数的关键逻辑，想快速返回调用者继续调试时使用。

    重要：如果当前函数执行时间较长，可能会超时。如果返回 [超时] 提示，
    请调用 debug_get_pending_output 获取已缓存的输出。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        返回后的执行位置及函数返回值（如果有）。
    """
    _log_call("debug_step_out", debugger_id=debugger_id)
    if _debugger._is_waitfor_waiting(debugger_id):
        return _log_return("debug_step_out", _debugger._format_output(debugger_id, "[错误] 该实例正在 waitfor 等待中，尚未附加到任何进程，无法单步执行。\nnext=[debug_detach, debug_list_debuggers]"))
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_step_out", str(e))
    result = backend.step_out()
    return _log_return("debug_step_out", _debugger._format_output(debugger_id, result))


# ========== 状态检查 ==========


@mcp.tool()
def debug_backtrace(debugger_id: int = 0) -> str:
    """
    获取当前的完整调用栈（backtrace），显示程序是如何执行到当前位置的。

    前置条件：程序必须处于暂停状态。

    返回内容包含所有栈帧的：帧编号（#0 为最内层/当前帧）、函数名、参数值、源文件路径和行号。
    可配合 debug_select_frame 切换到其他帧，查看该帧的局部变量和代码。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        完整的调用栈列表。
    """
    _log_call("debug_backtrace", debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_backtrace", str(e))
    result = backend.get_backtrace()
    return _log_return("debug_backtrace", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_get_variables(debugger_id: int = 0) -> str:
    """
    获取当前栈帧中的所有局部变量及其类型和值。

    前置条件：程序必须处于暂停状态。
    注意：默认显示的是当前帧（frame #0）的变量。如需查看其他帧的变量，先用 debug_select_frame 切换帧。
    如需查看特定表达式或全局变量的值，请使用 debug_evaluate。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        所有局部变量的名称、类型和当前值。
    """
    _log_call("debug_get_variables", debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_get_variables", str(e))
    result = backend.get_local_variables()
    return _log_return("debug_get_variables", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_evaluate(expression: str, debugger_id: int = 0) -> str:
    """
    在当前调试上下文中求值任意 C++ 表达式，返回其结果。

    前置条件：程序必须处于暂停状态。

    功能比 debug_get_variables 更灵活：可以查看单个变量、计算表达式、访问成员、调用函数等。

    ⚠️ 注意：如果表达式包含函数调用（如 vec.push_back(1)），会实际执行该函数，可能改变程序状态。
    仅用于只读查看时请使用纯表达式，避免带有副作用的函数调用。

    Args:
        expression: 合法的 C++ 表达式。常用示例：
            - 查看变量："x"、"myObj"
            - 算术运算："x + y"、"count * 2"
            - 成员访问："ptr->name"、"obj.value"、"vec.size()"
            - 数组索引："array[3]"、"matrix[i][j]"
            - 类型信息："sizeof(int)"、"sizeof(myStruct)"
            - 强制转换："(double)x"、"static_cast<int>(f)"
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        表达式的类型和求值结果。
    """
    _log_call("debug_evaluate", expression=expression, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_evaluate", str(e))
    result = backend.evaluate_expression(expression)
    return _log_return("debug_evaluate", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_source_context(line_count: int = 10, debugger_id: int = 0) -> str:
    """
    查看当前执行位置附近的源代码，帮助你理解程序正在执行什么。

    前置条件：程序必须处于暂停状态，且可执行文件包含调试信息（-g 编译）。
    建议：每次程序暂停后（断点命中、单步执行后）都先调用此工具了解当前代码上下文。

    Args:
        line_count: 显示的代码行数，默认 10 行。如需更多上下文可增大此值（如 20、30）。
                    注意：GDB 后端下此参数可能不精确，默认显示约 10 行。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        带行号的源代码片段，当前执行行会被高亮标记（箭头或标记指示）。
    """
    _log_call("debug_source_context", line_count=line_count, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_source_context", str(e))
    result = backend.get_source_context(line_count)
    return _log_return("debug_source_context", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_select_frame(frame_index: int, debugger_id: int = 0) -> str:
    """
    切换到调用栈中的指定帧。切换后，debug_get_variables 和 debug_source_context 会显示该帧的数据。

    前置条件：程序必须处于暂停状态。需先调用 debug_backtrace 查看可用的帧编号。
    适用场景：想查看调用者的局部变量、代码时，切换到对应帧。

    Args:
        frame_index: 帧索引号。0 = 最内层帧（当前执行点），数字越大越靠近调用链的外层（如 main）。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        选中帧的函数名、文件位置和行号。
    """
    _log_call("debug_select_frame", frame_index=frame_index, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_select_frame", str(e))
    result = backend.select_frame(frame_index)
    return _log_return("debug_select_frame", _debugger._format_output(debugger_id, result))


# ========== 线程管理 ==========


@mcp.tool()
def debug_thread_list(debugger_id: int = 0) -> str:
    """
    列出程序的所有线程及其状态（适用于多线程程序调试）。

    前置条件：程序必须处于暂停状态。
    后续步骤：如需调试特定线程，使用 debug_select_thread 切换，然后即可查看该线程的调用栈和变量。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        线程列表，包含线程 ID、名称、当前状态和执行位置。当前活跃线程会被标记。
    """
    _log_call("debug_thread_list", debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_thread_list", str(e))
    result = backend.get_thread_info()
    return _log_return("debug_thread_list", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_select_thread(thread_index: int, debugger_id: int = 0) -> str:
    """
    切换到指定线程进行调试。切换后，debug_backtrace、debug_get_variables 等工具会作用于该线程。

    前置条件：程序必须处于暂停状态。需先调用 debug_thread_list 查看可用的线程索引。

    Args:
        thread_index: 线程索引号（从 debug_thread_list 输出中获取）。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        切换后的线程信息，包含该线程的当前执行位置。
    """
    _log_call("debug_select_thread", thread_index=thread_index, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_select_thread", str(e))
    result = backend.select_thread(thread_index)
    return _log_return("debug_select_thread", _debugger._format_output(debugger_id, result))


# ========== 高级功能 ==========


@mcp.tool()
def debug_read_memory(address: str, count: int = 64, debugger_id: int = 0) -> str:
    """
    读取指定内存地址的原始字节内容，以十六进制和 ASCII 格式显示。

    前置条件：程序必须处于暂停状态。
    适用场景：检查缓冲区内容、验证内存布局、排查内存损坏问题。

    Args:
        address: 内存地址，支持以下格式：
            - 十六进制地址：如 "0x7fff5fbff8a0"
            - 取地址表达式：如 "&x"、"&array[0]"、"&obj.member"
        count: 要读取的字节数，默认 64。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        内存内容的十六进制 dump（含地址偏移和 ASCII 对照）。
    """
    _log_call("debug_read_memory", address=address, count=count, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_read_memory", str(e))
    result = backend.read_memory(address, count)
    return _log_return("debug_read_memory", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_disassemble(function_name: str = "", debugger_id: int = 0) -> str:
    """
    反汇编指定函数或当前位置的机器码，查看底层汇编指令。

    前置条件：程序必须处于暂停状态（查看当前位置时），或已加载可执行文件（指定函数名时）。
    适用场景：分析编译器优化、排查底层问题、理解函数的实际执行逻辑。

    Args:
        function_name: （可选）要反汇编的函数名，如 "main"、"calculate"。为空则反汇编当前暂停位置附近的代码。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        汇编指令列表，包含地址、指令助记符和操作数。当前执行位置会被标记。
    """
    _log_call("debug_disassemble", function_name=function_name, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_disassemble", str(e))
    result = backend.disassemble(function_name)
    return _log_return("debug_disassemble", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_set_watchpoint(variable: str, debugger_id: int = 0) -> str:
    """
    设置数据断点（watchpoint），当指定变量的值被修改时自动暂停程序。

    前置条件：程序必须处于暂停状态，且变量在当前作用域内可见。
    适用场景：不确定变量在哪里被意外修改时，设置 watchpoint 比手动在每个可能位置设断点更高效。

    Args:
        variable: 要监视的变量名，如 "x"、"counter"、"obj.member"。变量必须在当前作用域内。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        watchpoint 设置结果。程序继续运行后，一旦该变量被写入新值，就会自动暂停。
    """
    _log_call("debug_set_watchpoint", variable=variable, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_set_watchpoint", str(e))
    result = backend.set_watchpoint(variable)
    return _log_return("debug_set_watchpoint", _debugger._format_output(debugger_id, result))


@mcp.tool()
def debug_raw_command(command: str, debugger_id: int = 0) -> str:
    """
    直接发送原始的 lldb 或 gdb 命令（高级用法/兜底方案）。

    前置条件：调试会话已启动（debug_start 之后）。
    适用场景：当上面的专用工具无法满足需求时使用。

    ⚠️ 护栏约束：调用此工具前，请先在回复中说明：
    1. 为什么标准工具无法满足当前需求；
    2. 预期执行什么命令；
    3. 该命令可能产生什么影响。

    Args:
        command: 调试器原生命令字符串。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        命令的原始输出结果。
    """
    _log_call("debug_raw_command", command=command, debugger_id=debugger_id)
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return _log_return("debug_raw_command", str(e))
    result = backend.send_raw_command(command)
    return _log_return("debug_raw_command", _debugger._format_output(debugger_id, result))

# ========== 程序输出查看 ==========


@mcp.tool()
def debug_get_program_output(mode: str = "all", lines: int = 20, debugger_id: int = 0) -> str:
    """
    读取被调试程序的输出内容（stdout/stderr），用于查看程序的 printf、cout 等打印结果。

    前置条件：程序至少需要运行过一次（调用过 debug_run）。

    支持三种读取模式：
    - **all**（默认）：返回程序从启动到现在的全部输出。
    - **head**：返回前 N 行输出。
    - **tail**：返回后 N 行输出。

    Args:
        mode: 读取模式，可选值："all"、"head"、"tail"
        lines: head/tail 模式下返回的行数，默认 20。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        程序输出内容及统计信息。
    """
    _log_call("debug_get_program_output", mode=mode, lines=lines, debugger_id=debugger_id)
    valid_modes = ("all", "head", "tail")
    if mode not in valid_modes:
        return _log_return("debug_get_program_output", f"[错误] 无效的 mode 参数：'{mode}'。可选值：{', '.join(valid_modes)}\nnext=[debug_get_program_output]")
    if lines < 1:
        return _log_return("debug_get_program_output", "[错误] lines 参数必须 >= 1\nnext=[debug_get_program_output]")
    return _log_return("debug_get_program_output", _debugger.get_program_output_safe(mode=mode, lines=lines, debugger_id=debugger_id))

# ========== 超时输出恢复 ==========


@mcp.tool()
def debug_get_pending_output(debugger_id: int = 0) -> str:
    """
    获取因超时而缓存的调试器输出。

    当 debug_run、debug_continue 或 debug_step_out 返回 [超时] 提示时，
    说明程序仍在运行但等待时间已超过配置的超时阈值。
    此工具用于获取调试器的状态变化（断点命中、程序退出等）。
    如果调试器没有新输出，会自动附带程序最后 10 行打印输出供参考。
    若只需查看程序的 printf/cout 输出，可直接使用 debug_get_program_output(mode="tail")。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        缓存的调试器输出及程序状态判断。如果没有缓存则返回相应提示。
    """
    _log_call("debug_get_pending_output", debugger_id=debugger_id)
    return _log_return("debug_get_pending_output", _debugger.get_pending_output_safe(debugger_id=debugger_id))


# ========== 多进程调试 ==========


@mcp.tool()
def debug_list_children(debugger_id: int = 0) -> str:
    """
    列出当前被调试进程的所有直接子进程。

    用于多进程调试场景，帮助找到需要调试的子进程 PID。

    前置条件：程序必须已运行（至少调用过 debug_run）。
    后续步骤：使用 debug_attach_child(pid) 附加到目标子进程。

    Args:
        debugger_id: （可选）要查询哪个调试器实例所控制进程的子进程，默认 0（主调试器）。

    Returns:
        子进程列表，包含 PID 和进程名。如果没有子进程会返回提示。
    """
    _log_call("debug_list_children", debugger_id=debugger_id)
    return _log_return("debug_list_children", _debugger.list_children(debugger_id=debugger_id))


@mcp.tool()
def debug_attach_child(pid: int) -> str:
    """
    创建新的调试器实例并附加到指定 PID 的子进程。

    用于 IPC 等多进程调试场景。附加后可以在子进程中设置断点、查看变量等，
    而父进程的调试不受影响。

    新实例会被分配一个递增的编号（#1、#2...），后续所有工具可通过
    debugger_id 参数指定对哪个实例执行操作。

    前置条件：主调试器必须已启动。
    后续步骤：使用返回的 debugger_id 在子进程中执行操作（如 debug_set_breakpoint、debug_continue 等）。

    Args:
        pid: 要附加的子进程 PID。可通过 debug_list_children 获取。

    Returns:
        附加结果，包含新调试器实例编号和进程信息。
    """
    _log_call("debug_attach_child", pid=pid)
    if not _debugger.is_active:
        return _log_return("debug_attach_child", "[错误] 主调试器未启动，请先调用 debug_start 启动调试会话。\nnext=[debug_start]")
    # 创建新实例并附加（attach 逻辑在 attach_child 内部完成）
    result = _debugger.attach_child(pid)
    return _log_return("debug_attach_child", result)


@mcp.tool()
def debug_detach(debugger_id: int) -> str:
    """
    从子进程脱离并关闭对应的调试器实例。

    脱离后子进程会恢复运行，调试器实例被关闭并释放资源。

    注意：无法 detach 主调试器（#0），要结束整个调试会话请使用 debug_stop。

    Args:
        debugger_id: 要脱离的调试器实例编号（必须 > 0）。

    Returns:
        脱离结果。
    """
    _log_call("debug_detach", debugger_id=debugger_id)
    return _log_return("debug_detach", _debugger.detach_child(debugger_id))


@mcp.tool()
def debug_list_debuggers() -> str:
    """
    列出当前所有活跃的调试器实例。

    返回每个实例的编号、模式（launch/attach/waitfor）、进程名、PID 和状态。
    用于多进程调试时了解当前所有调试器的状态。

    Returns:
        调试器实例列表。
    """
    _log_call("debug_list_debuggers")
    return _log_return("debug_list_debuggers", _debugger.list_debuggers())


# ========== waitfor 子进程捕获 ==========

if _can_waitfor:
    @mcp.tool()
    def debug_create_instance(process_name: str) -> str:
        """
        创建新的调试器实例（waitfor 模式），等待指定名称的子进程启动后自动附加。

        在 IPC 多进程调试场景中，当子进程生命周期很短（启动后很快退出），
        无法通过常规的 debug_attach_child(pid) 方式及时附加时，使用此工具先布局等待。

        典型流程：
        1. debug_create_instance(process_name="child_app") → 创建 waitfor 实例
        2. 切换到主调试器 debug_continue(debugger_id=0) → 触发子进程启动
        3. 子进程启动时自动被捕获并暂停在入口点
        4. 工具返回值中会附带跨实例状态变化通知（⚡ [通知][事件:waitfor_triggered] waitfor 已触发）

        前置条件：主调试器必须已启动（debug_start 后）。
        后续步骤：切换到主调试器继续执行以触发子进程启动，之后在 waitfor 实例上操作。

        注意：waitfor 等待中的实例不能调用 debug_run/debug_load/debug_continue 等。
        等到 waitfor 成功捕获子进程后（通过返回值中的通知确认），才能对该实例执行调试操作。
        如需取消等待，使用 debug_detach(debugger_id=N)。

        Args:
            process_name: 要等待的子进程可执行文件名（不含路径），如 "child_app"、"worker"。

        Returns:
            创建结果，包含新调试器实例编号。
        """
        _log_call("debug_create_instance", process_name=process_name)
        result = _debugger.create_instance(process_name=process_name)
        return _log_return("debug_create_instance", result)


if __name__ == "__main__":
    mcp.run()
