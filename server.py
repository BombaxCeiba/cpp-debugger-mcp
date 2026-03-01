"""
C++ Debugger MCP 服务
通过 MCP 协议暴露 C++ 调试功能，允许 AI 助手调试 C++ 程序。
自动检测并使用 lldb（优先）或 gdb 作为调试后端。
"""

import os
import sys
import json
import argparse

# 将脚本所在目录加入模块搜索路径，确保从任意位置用绝对路径启动时都能正确导入
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from mcp.server.fastmcp import FastMCP
from debugger import CppDebugger


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
    # 使用 parse_known_args 避免与 MCP 框架自身的参数冲突
    args, _ = parser.parse_known_args()
    return args


def setup_llvm_path(llvm_path: str):
    """将用户指定的 LLVM 路径插入到 PATH 环境变量的最前面"""
    llvm_path = os.path.abspath(llvm_path)
    if not os.path.isdir(llvm_path):
        print(f"警告：指定的 LLVM 路径不存在：{llvm_path}", file=sys.stderr)
        sys.exit(1)
    # 将 llvm_path 插到 PATH 最前面，确保优先使用
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = llvm_path + os.pathsep + current_path
    print(f"已将 LLVM 路径添加到 PATH 最前面：{llvm_path}", file=sys.stderr)


# 解析命令行参数并设置 PATH
_args = parse_args()
if _args.llvm_path:
    setup_llvm_path(_args.llvm_path)

# 运行超时配置
_run_timeout: float = _args.run_timeout


# 创建 MCP 服务实例
mcp = FastMCP(
    "C++ Debugger MCP",
    instructions="""这是一个 C++ 程序调试 MCP 服务，支持 lldb（优先）和 gdb 双后端。
仅用于调试**已编译好的** C++ 可执行文件（需使用 -g 编译以包含调试信息），不负责编译。

## 调试器状态机（重要）

调试器有 5 种状态，你必须按顺序操作，不能跳过状态：

  [状态:未启动] --debug_start--> [状态:已启动] --debug_load--> [状态:已加载]
  [状态:已加载] --debug_run--> [状态:运行中]/[状态:已暂停]
  [状态:已暂停] --debug_continue/step_*--> [状态:运行中]/[状态:已暂停]
  [任意状态] --debug_stop--> [状态:未启动]

### 各状态可用的工具：
- **未启动**：仅可调用 debug_start
- **已启动**：仅可调用 debug_load, debug_stop
- **已加载**：可调用 debug_set_breakpoint, debug_run, debug_stop（建议先设断点再 run）
- **已暂停**（程序在断点/单步处停下）：可调用所有检查和控制工具
- **运行中**：程序正在执行，等待其在断点处暂停。超时后可调用 debug_get_pending_output 检查状态

注意：debug_get_program_output 可在程序运行过（至少调用过 debug_run）的任何状态下使用，用于查看程序的 stdout/stderr 输出。

### 关键规则：
1. 检查变量(debug_get_variables)、求值(debug_evaluate)、查看源码(debug_source_context)、查看调用栈(debug_backtrace) 等工具，只有在程序处于**已暂停**状态时才有效
2. 如果程序已经结束（运行完毕），需要 debug_run 重新启动，无需再 load
3. debug_raw_command 可在已启动后的任何状态使用，但需自行确保命令语法正确
4. 调试多线程程序时，先用 debug_thread_list 查看线程，再用 debug_select_thread 切换
5. 调用栈中 frame 0 是最内层（当前执行点），可用 debug_select_frame 切换帧后再查看变量

## 超时处理（极其重要）

debug_run、debug_continue、debug_step_out 是长时间运行的工具，执行期间程序会运行直到断点或结束。
服务已通过 --run-timeout 参数配置了超时时间（默认 30 秒）。

### 超时处理流程（必须遵守）：
1. 当这三个工具返回 **[超时]** 提示时，说明程序仍在运行中但等待时间已超出
2. **第一步（必须）：立即调用 debug_get_pending_output** 获取缓存的输出，恢复调试器交互状态
3. 根据返回结果中的 **[状态:xxx]** 标记判断：
   - **[状态:已暂停]**：断点已命中，可以继续使用检查类工具
   - **[状态:运行中]**：程序还在执行，可再次调用 debug_continue 继续等待
   - **[状态:已结束]**：程序已退出，可查看输出或重新运行
4. 可以多次调用 debug_get_pending_output 来轮询状态
5. **第二步（可选）：** 在完成第一步后，可调用 debug_get_program_output(mode="tail") 查看程序最新的打印输出，帮助判断程序执行进度。注意：此步骤不能替代第一步，必须先完成 debug_get_pending_output

### 建议：
- 调用 debug_run/debug_continue/debug_step_out 时请适当调大客户端超时设置（如 60-120 秒）
- 不要因为超时就认为程序卡死，可能只是需要更长运行时间

## 环境变量

debug_start 支持传入 env_vars 参数（JSON 字符串格式），指定的环境变量会：
1. 透传给调试器子进程本身
2. 在 debug_load 加载目标程序时自动设置，确保被调试程序也能获取这些环境变量
示例：debug_start(env_vars='{"MY_VAR": "hello", "LD_LIBRARY_PATH": "/opt/lib"}')

## 典型调试流程

debug_start → debug_load("./程序") → debug_set_breakpoint("main") → debug_run()
→ [状态:已暂停] → debug_source_context() 看代码 → debug_get_variables() 看变量
→ debug_step_over()/debug_continue() → ... → debug_stop()

在任何暂停点或程序结束后，都可以调用 debug_get_program_output() 查看程序截至目前的所有打印输出。
输出量大时，使用 mode="tail" 只看最新的 N 行，避免 token 浪费。

### 小程序快速调试流程（推荐）
debug_start → debug_load("./程序") → debug_run(stop_at_entry=True)
→ [状态:已暂停 - main 入口] → debug_source_context() 看代码 → debug_set_breakpoint(...) 按需设断点
→ debug_continue() → ... → debug_stop()

注意：对于小型程序，建议使用 stop_at_entry=True 启动，这样程序会在 main 函数入口自动暂停，
agent 有充足时间查看代码、设置断点，不用担心程序瞬间执行完毕。

### 超时场景的调试流程
debug_run() → 返回 [超时] → debug_get_pending_output() → [状态:已暂停] → 正常调试
debug_run() → 返回 [超时] → debug_get_pending_output() → [状态:运行中] → debug_continue() → ...

## 返回值状态标记（重要）

所有关键工具的返回值都包含 **[状态:xxx]** 前缀标记，你应该优先根据此标记判断当前状态，而非仅靠自然语言理解：

| 状态标记 | 含义 | 允许的后续操作 |
|---|---|---|
| [状态:未启动] | 调试器未启动或已关闭 | 仅 debug_start |
| [状态:已启动] | 调试会话已启动，未加载程序 | debug_load, debug_stop |
| [状态:已加载] | 可执行文件已加载 | debug_set_breakpoint, debug_run, debug_stop |
| [状态:已暂停] | 程序在断点/单步处停下 | 所有工具均可用 |
| [状态:运行中] | 程序正在执行中 | debug_get_pending_output, debug_get_program_output, debug_stop |
| [状态:已结束] | 程序已退出（正常或异常） | debug_get_program_output 查看输出, debug_run 重启, debug_stop 结束 |
| [超时] | 等待超时，程序可能仍在运行 | 第一步（必须）：debug_get_pending_output；第二步（可选）：debug_get_program_output(mode="tail") |
| [错误] | 操作失败 | 根据失败分型策略表恢复 |

注意：关键状态转换工具（debug_run、debug_continue、debug_step_out）的返回值中还包含 **next=[...]** 白名单，
列出了当前状态下推荐的后续工具。你应优先从 next 列表中选择下一步操作。

**决策流程**：收到工具返回 → 读取 [状态:xxx] 标记 → 查看 next=[...] 白名单 → 优先从白名单中选择下一步工具 → 如无白名单则查表确认允许的操作。

## 排错提示
- 如果工具返回"进程未启动"，说明你需要先 debug_start
- 如果工具返回空或无效结果，可能程序未暂停或已结束
- 如果断点未命中，检查文件名/行号是否正确，程序是否带 -g 编译
- 如果工具返回 [超时]，不要慌张，按照上述超时处理流程操作即可

## 失败分型与恢复策略（必须遵守）

遇到错误时，根据以下策略表选择恢复动作，不要盲目猜测：

| 错误类型 | 典型返回信息 | 恢复策略 | 禁止动作 |
|---|---|---|---|
| 进程未启动 | "进程未启动"、RuntimeError | 只允许调用 debug_start | 禁止一切其他工具 |
| 目标未加载 | 加载相关错误 | 先 debug_load 加载可执行文件 | 禁止 debug_run, debug_set_breakpoint |
| 程序未暂停 | 检查类工具返回空/无效结果 | **先判定当前 [状态:xxx] 再选动作**（见下方明细） | **禁止** debug_get_variables, debug_evaluate, debug_source_context, debug_backtrace, debug_step_over, debug_step_into, debug_select_frame |
| 程序已结束 | "exited"、"process exited" | **先判定当前 [状态:xxx] 再选动作**（见下方明细） | **禁止** debug_continue, debug_step_*, debug_get_variables, debug_evaluate |
| 断点未命中 | 程序运行结束但未暂停 | 1. debug_list_breakpoints 检查断点 2. 确认文件名/行号 3. 确认 -g 编译 | 禁止反复 debug_run 而不检查断点 |
| 符号未找到 | "symbol not found" | 检查函数名拼写；确认 -g 编译且未被 strip | — |
| 变量不可用 | "variable not available" | 可能被优化掉，建议 -O0 编译 | — |
| 超时 | "[超时]" | 第一步（必须）：debug_get_pending_output 恢复状态；第二步（可选）：debug_get_program_output(mode="tail") 查看输出 | **禁止** debug_get_variables, debug_evaluate 等检查类工具（必须先完成第一步恢复） |
| 文件不存在 | "文件不存在" | 检查可执行文件路径是否正确 | — |

### 状态感知恢复明细

上表中标注"先判定当前 [状态:xxx] 再选动作"的错误类型，必须按以下分支选择恢复路径：

**"程序未暂停"恢复分支：**
- 若当前 [状态:运行中] → debug_get_pending_output 轮询，或 debug_continue 继续等待断点
- 若当前 [状态:未启动] → 需先 debug_start 启动调试会话
- 若当前 [状态:已启动] → 需先 debug_load 加载可执行文件，再 debug_run
- 若当前 [状态:已加载] → 需先 debug_run 启动程序
- 若当前 [状态:已结束] → 程序已退出，debug_get_program_output 查看输出，或 debug_run 重启

**"程序已结束"恢复分支：**
- 若当前 [状态:已结束] → debug_get_program_output 查看输出，debug_run 重新启动，或 debug_stop 结束会话
- 若当前 [状态:已暂停] → 状态判断有误，程序仍在暂停中，可正常使用检查类工具
- 若当前 [状态:运行中] → 状态判断有误，程序仍在运行，应 debug_get_pending_output 轮询

## 行为约束规则（必须遵守）

规则1（先验证后行动）：执行 debug_run、debug_continue、debug_step_* 等状态变更命令后，必须先根据返回值判断当前程序状态（已暂停/运行中/已结束），再决定下一步操作。
规则2（超时恢复）：收到 [超时] 返回后，第一恢复动作必须是 debug_get_pending_output；完成后可选调用 debug_get_program_output(mode="tail") 补充判断程序输出。在完成第一步之前，禁止调用任何其他工具。
规则3（状态门控）：检查类工具（debug_get_variables、debug_evaluate、debug_source_context、debug_backtrace 等）仅在程序处于 [状态:已暂停] 时调用。
规则4（防空转）：同一无进展动作最多重试 3 次，之后必须切换策略并向用户说明原因。
  - 判定标准：连续 N 次调用同一状态变更工具（如 debug_continue）且返回的 [状态:xxx] 标记未发生变化（如始终为 [状态:运行中]），即为无进展。
  - 中间穿插的纯查询类调用（debug_get_pending_output、debug_get_program_output）不重置重试计数。
  - 达到 3 次后应切换策略：用 debug_list_breakpoints 检查断点、用 debug_get_program_output(mode="tail") 查看程序输出、检查程序参数或编译选项。
规则5（结束确认）：如果返回值中包含程序退出信息（exited、terminated 等），当前状态为 [状态:已结束]，不要尝试检查变量或继续执行，应决定是 debug_run 重启还是 debug_stop 结束会话。
规则6（查看程序输出）：如果需要了解程序的 printf/cout 打印内容，使用 debug_get_program_output 而非从调试命令的返回值中拼凑。输出量大时优先使用 tail 模式查看最新内容，节省 token。

## 多进程调试（IPC 场景）

本服务支持同时调试多个进程（父进程 + 子进程），通过多调试器实例实现。
每个调试器实例独立控制一个进程，互不干扰。

### 核心概念
- **调试器 #0**：主调试器（launch 模式），通过 debug_start → debug_load → debug_run 启动
- **调试器 #1, #2, ...**：子调试器（attach 模式），通过 debug_attach_child(pid) 创建
- **debugger_id**：所有调试工具都支持此可选参数（默认 0），用于指定操作目标

### 多进程调试新增工具
- **debug_list_children(debugger_id=0)**：列出指定进程的所有直接子进程（PID + 进程名）
- **debug_attach_child(pid)**：创建新调试器实例并附加到子进程，返回分配的调试器编号
- **debug_detach(debugger_id)**：从子进程脱离并关闭对应调试器实例（不能 detach #0）
- **debug_list_debuggers()**：列出所有活跃的调试器实例及其状态

### 返回值标注
所有工具的返回值都包含 **[调试器 #N][进程: xxx (PID: xxx)]** 前缀标签，用于区分不同实例的输出：
```
[调试器 #0][进程: parent_app (PID: 12345)]
[状态:已暂停] Breakpoint 1, main() at parent.cpp:10

[调试器 #1][进程: PID-12346 (PID: 12346)]
[状态:已暂停] Breakpoint 1, on_message() at child.cpp:25
```

### 典型 IPC 调试流程
1. debug_start → debug_load("./parent") → debug_set_breakpoint("main") → debug_run(stop_at_entry=True)
2. debug_list_children() → 找到子进程 PID
3. debug_attach_child(child_pid) → 获得调试器 #1
4. debug_set_breakpoint("on_message", debugger_id=1) → 在子进程设断点
5. debug_continue(debugger_id=0) → 父进程继续执行
6. debug_get_pending_output(debugger_id=1) → 检查子进程是否命中断点
7. debug_get_variables(debugger_id=1) → 查看子进程变量
8. debug_detach(debugger_id=1) → 完成后脱离子进程
9. debug_stop() → 结束所有调试

### attach 模式限制
- attach 模式的调试器（#1+）不能调用 debug_run 和 debug_load，只能用 debug_continue 继续执行
- 最多同时运行 5 个调试器实例

### 失败分型（多进程相关）

| 错误类型 | 典型返回信息 | 恢复策略 |
|---|---|---|
| attach 失败 | "无法附加到进程" | 检查 PID 是否正确、进程是否存在、是否需要管理员权限 |
| 实例不存在 | "调试器 #N 不存在" | 调用 debug_list_debuggers 查看可用实例 |
| 实例数量上限 | "实例数量已达上限" | 先 debug_detach 不需要的实例 |
| 无法 detach 主调试器 | "无法脱离主调试器" | 使用 debug_stop 结束整个调试会话 |
| attach 模式下调用 run/load | "附加模式下无法..." | 使用 debug_continue 代替 debug_run |
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
    parsed_env = None
    if env_vars and env_vars.strip():
        try:
            parsed_env = json.loads(env_vars)
            if not isinstance(parsed_env, dict):
                return "错误：env_vars 必须是一个 JSON 对象（键值对），如 '{\"KEY\": \"VALUE\"}'"
            # 确保所有键和值都是字符串
            parsed_env = {str(k): str(v) for k, v in parsed_env.items()}
        except json.JSONDecodeError as e:
            return f"错误：env_vars 不是合法的 JSON 格式：{e}"
    try:
        result = _debugger.start(env_vars=parsed_env)
    except FileNotFoundError as e:
        return f"[错误] {e}\nnext=[debug_start]"
    except Exception as e:
        return f"[错误] 调试器启动失败：{e}\nnext=[debug_start]"
    _apply_run_timeout(0)
    return _debugger._format_output(0, result)


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
    return _debugger.stop()


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
    if _debugger._is_attach_mode(debugger_id):
        return _debugger._format_output(debugger_id, "[错误] 附加模式下无法加载新的可执行文件。请使用 debug_continue 继续调试。")
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.load_target(executable_path)
    # 更新元信息中的进程名
    meta = _debugger._instance_metadata.get(debugger_id, {})
    meta["process_name"] = os.path.basename(executable_path)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.set_breakpoint(location, condition)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.delete_breakpoint(breakpoint_id)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.list_breakpoints()
    return _debugger._format_output(debugger_id, result)


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
    if _debugger._is_attach_mode(debugger_id):
        return _debugger._format_output(debugger_id, "[错误] 附加模式下无法使用 debug_run，请使用 debug_continue 继续执行。")
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.run(args, stop_at_entry=stop_at_entry)
    # 运行后尝试更新 PID 元信息
    _debugger._update_metadata_pid(debugger_id)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.continue_execution()
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.step_over()
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.step_into()
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.step_out()
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.get_backtrace()
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.get_local_variables()
    return _debugger._format_output(debugger_id, result)


@mcp.tool()
def debug_evaluate(expression: str, debugger_id: int = 0) -> str:
    """
    在当前调试上下文中求值任意 C++ 表达式，返回其结果。

    前置条件：程序必须处于暂停状态。

    功能比 debug_get_variables 更灵活：可以查看单个变量、计算表达式、访问成员、调用函数等。

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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.evaluate_expression(expression)
    return _debugger._format_output(debugger_id, result)


@mcp.tool()
def debug_source_context(line_count: int = 10, debugger_id: int = 0) -> str:
    """
    查看当前执行位置附近的源代码，帮助你理解程序正在执行什么。

    前置条件：程序必须处于暂停状态，且可执行文件包含调试信息（-g 编译）。
    建议：每次程序暂停后（断点命中、单步执行后）都先调用此工具了解当前代码上下文。

    Args:
        line_count: 显示的代码行数，默认 10 行。如需更多上下文可增大此值（如 20、30）。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        带行号的源代码片段，当前执行行会被高亮标记（箭头或标记指示）。
    """
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.get_source_context(line_count)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.select_frame(frame_index)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.get_thread_info()
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.select_thread(thread_index)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.read_memory(address, count)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.disassemble(function_name)
    return _debugger._format_output(debugger_id, result)


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
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.set_watchpoint(variable)
    return _debugger._format_output(debugger_id, result)


@mcp.tool()
def debug_raw_command(command: str, debugger_id: int = 0) -> str:
    """
    直接发送原始的 lldb 或 gdb 命令（高级用法/兖底方案）。

    前置条件：调试会话已启动（debug_start 之后）。
    适用场景：当上面的专用工具无法满足需求时使用。

    Args:
        command: 调试器原生命令字符串。
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        命令的原始输出结果。
    """
    try:
        backend = _debugger._get_instance(debugger_id)
    except RuntimeError as e:
        return str(e)
    result = backend.send_raw_command(command)
    return _debugger._format_output(debugger_id, result)

# ========== 程序输出查看 ==========


@mcp.tool()
def debug_get_program_output(mode: str = "all", lines: int = 20, debugger_id: int = 0) -> str:
    """
    读取被调试程序的输出内容（stdout/stderr），用于查看程序的 printf、cout 等打印结果。

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
    valid_modes = ("all", "head", "tail")
    if mode not in valid_modes:
        return f"[错误] 无效的 mode 参数：'{mode}'。可选值：{', '.join(valid_modes)}"
    if lines < 1:
        return "[错误] lines 参数必须 >= 1"
    return _debugger.get_program_output_safe(mode=mode, lines=lines, debugger_id=debugger_id)

# ========== 超时输出恢复 ==========


@mcp.tool()
def debug_get_pending_output(debugger_id: int = 0) -> str:
    """
    获取因超时而缓存的调试器输出（**关键恢复工具**）。

    当 debug_run、debug_continue 或 debug_step_out 返回 [超时] 提示时，
    说明程序仍在运行但等待时间已超过配置的超时阈值。
    **你必须在收到 [超时] 提示后立即调用此工具**。

    Args:
        debugger_id: （可选）目标调试器实例编号，默认 0（主调试器）。

    Returns:
        缓存的调试器输出及程序状态判断。如果没有缓存则返回相应提示。
    """
    return _debugger.get_pending_output_safe(debugger_id=debugger_id)


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
    return _debugger.list_children(debugger_id=debugger_id)


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
    result = _debugger.attach_child(pid)
    return result


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
    return _debugger.detach_child(debugger_id)


@mcp.tool()
def debug_list_debuggers() -> str:
    """
    列出当前所有活跃的调试器实例。

    返回每个实例的编号、模式（launch/attach）、进程名、PID 和状态。
    用于多进程调试时了解当前所有调试器的状态。

    Returns:
        调试器实例列表。
    """
    return _debugger.list_debuggers()


if __name__ == "__main__":
    mcp.run()
