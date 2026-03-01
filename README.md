
# C++ Debugger MCP

基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 的 C++ 程序调试服务，允许 AI 助手通过 lldb/gdb 调试已编译好的 C++ 程序。

## 特性

- **双后端支持**：自动检测系统环境，优先使用 `lldb`（LLVM），若不可用则回退到 `gdb`
- **自定义工具路径**：支持通过命令行参数 `--llvm-path` 指定 LLVM 工具链路径
- **完整调试能力**：提供 28 个 MCP 工具（以服务实际注册为准），覆盖断点管理（含条件断点）、执行控制、状态检查、线程管理、内存查看、多进程调试等完整调试流程
- **入口暂停模式**：`debug_run` 支持 `stop_at_entry=True` 参数，程序启动后自动在 main 入口暂停，避免小程序瞬间执行完毕
- **环境变量透传**：`debug_start` 支持通过 `env_vars` 参数传入环境变量，自动透传给调试器和被调试程序
- **超时安全机制**：长时间运行命令超时后自动缓存输出，支持通过 `debug_get_pending_output` 恢复，不丢失任何调试器返回信息
- **程序输出查看**：通过 `debug_get_program_output` 工具查看被调试程序的 stdout/stderr 输出，支持全量/前 N 行/后 N 行三种读取模式
- **原生命令透传**：支持直接发送 lldb/gdb 原始命令，满足高级调试需求

## 项目结构

```
cpp-debugger-mcp/
├── server.py        # MCP 服务入口，注册所有调试工具
├── debugger.py      # 调试器封装层（lldb/gdb 双后端实现）
└── README.md
```

## 环境要求

- Python 3.10+
- [mcp](https://pypi.org/project/mcp/) Python SDK（`pip install mcp`）
- 以下调试器之一：
  - **lldb**（推荐，LLVM 工具链的一部分）
  - **gdb**

> 被调试的 C++ 可执行文件需使用 `-g` 选项编译以包含调试信息。

## 安装与启动

### 安装方式

**方式一：pip 安装（推荐）**

```bash
# 从 PyPI 安装
pip install cpp-debugger-mcp

# 或从本地 whl 文件安装（从 GitHub Releases 下载后）
pip install cpp_debugger_mcp-0.1.0-py3-none-any.whl
```

**方式二：从源码运行**

```bash
git clone https://github.com/BombaxCeiba/cpp-debugger-mcp.git
cd cpp-debugger-mcp
pip install -r requirements.txt
```

### 启动方式

#### 基本启动

```bash
# pip 安装后
cpp-debugger-mcp

# 从源码运行
python server.py
```

服务会自动检测系统 PATH 中的 `lldb` 或 `gdb`。

#### 指定 LLVM 路径

```bash
cpp-debugger-mcp --llvm-path "C:\Program Files\LLVM\bin"
```

`--llvm-path` 参数会将指定路径插入到 `PATH` 环境变量的**最前面**，确保优先使用该路径下的调试器工具。

#### 配置运行超时

```bash
cpp-debugger-mcp --run-timeout 60
```

`--run-timeout` 参数设定程序运行类命令（`debug_run`、`debug_continue`、`debug_step_out`）的最大等待时间，默认 30 秒。超时后输出不会丢失，而是缓存起来，AI 可通过 `debug_get_pending_output` 工具获取。

### MCP 客户端配置示例

**通用（Claude Desktop / Cursor 等）**：

```json
{
  "mcpServers": {
    "cpp-debugger": {
      "command": "cpp-debugger-mcp",
      "args": ["--llvm-path", "C:\\Program Files\\LLVM\\bin", "--run-timeout", "60"]
    }
  }
}
```

**opencode**：

```json
{
  "cpp-debugger": {
    "type": "local",
    "command": [
      "cpp-debugger-mcp",
      "--llvm-path",
      "C:\\Program Files\\LLVM\\bin",
      "--run-timeout",
      "60"
    ]
  }
}
```

> **从源码运行时**：将上述配置中的 `cpp-debugger-mcp` 替换为 `python`，并在参数最前面加上 `server.py` 的路径即可。

## 调试器状态机

调试器有严格的状态流转，Agent 必须按顺序操作：

```
[未启动] ──debug_start──▶ [已启动] ──debug_load──▶ [已加载]
                                                      │
                                                 debug_set_breakpoint (可选)
                                                      │
                                                 debug_run
                                                      ▼
                                               [运行中/已暂停]
                                                      │
                                      ┌───────────────┼───────────────┐
                                      ▼               ▼               ▼
                               debug_continue   debug_step_*   检查类工具
                                      │               │        (仅暂停可用)
                                      └───────────────┘
                                              │
                                         debug_stop
                                              ▼
                                          [未启动]
```

**各状态可用的工具**：

| 状态 | 可用工具 |
|---|---|
| 未启动 | `debug_start` |
| 已启动 | `debug_load`, `debug_stop` |
| 已加载 | `debug_set_breakpoint`, `debug_run`, `debug_stop` |
| 已暂停 | 所有调试工具均可使用 |
| 运行中 | 等待程序在断点处暂停，超时后可用 `debug_get_pending_output` 检查状态，`debug_get_program_output` 查看输出 |

> **重要**：`debug_get_variables`、`debug_evaluate`、`debug_source_context`、`debug_backtrace` 等检查类工具**仅在程序暂停时有效**。

> **注意**：以上为简化说明。完整的状态机规则、行为约束和错误恢复策略以 `server.py` 中的 `instructions` 提示词为权威源，README 仅提供概览。

## Agent 行为约束（概览）

MCP 服务内置了面向 AI Agent 的行为约束规则（详见 `server.py` 中的 `instructions`），主要包括：

1. **状态变更后先验证再行动**：执行 run/continue/step 等状态改变命令后，必须先根据返回值中的 `[状态:xxx]` 标记和 `next=[...]` 白名单判断当前状态，再决定下一步操作
2. **超时后必须恢复**：收到 `[超时]` 返回时，下一步只能调用 `debug_get_pending_output`，也可查看 `debug_get_program_output(mode="tail")` 了解程序输出
3. **检查类工具仅限暂停态**：`debug_get_variables`、`debug_evaluate` 等仅在 `[状态:已暂停]` 时可用
4. **防空转机制**：同一无进展动作（同工具、同状态标记）最多重试 3 次，之后必须切换策略（中间穿插的查询类调用不重置计数）
5. **失败分型恢复**：工具返回值包含状态标记（`[状态:已暂停]`、`[状态:已结束]`、`[超时]`），Agent 可据此自动选择恢复动作，策略表中还包含禁止动作列，显式指明各状态下不允许调用的工具

## MCP 工具一览

### 调试会话管理

| 工具名 | 说明 |
|---|---|
| `debug_start` | **第一步**。启动调试会话，自动检测 lldb（优先）或 gdb。可选 `env_vars` 参数传入环境变量。后续需调用 `debug_load` |
| `debug_stop` | **最后一步**。停止调试会话并释放资源。可在任何状态下调用 |
| `debug_load` | 加载可执行文件（需 `-g` 编译）。前置：`debug_start`。后续：设断点 → `debug_run` |

### 断点管理

| 工具名 | 说明 |
|---|---|
| `debug_set_breakpoint` | 设置断点。支持函数名（`main`）或文件:行号（`main.cpp:10`）。可选 `condition` 参数设置条件断点（`i == 5`）。前置：已 load |
| `debug_delete_breakpoint` | 按 ID 删除断点。ID 可通过 `debug_list_breakpoints` 获取 |
| `debug_list_breakpoints` | 列出所有断点的 ID、位置、启用状态、命中次数和条件 |

### 执行控制

| 工具名 | 说明 |
|---|---|
| `debug_run` | 运行程序。可选 `args` 传递命令行参数，可选 `stop_at_entry=True` 在 main 入口自动暂停。程序结束后可再次调用无需重新 load |
| `debug_continue` | 从暂停处继续运行至下一个断点或程序结束。**前置：程序暂停** |
| `debug_step_over` | 单步执行当前行，不进入函数调用。**前置：程序暂停** |
| `debug_step_into` | 单步执行，遇到函数调用则进入该函数。**前置：程序暂停** |
| `debug_step_out` | 执行到当前函数返回。**前置：程序暂停** |

### 状态检查（均需程序暂停）

| 工具名 | 说明 |
|---|---|
| `debug_backtrace` | 查看完整调用栈（帧号、函数名、文件、行号）。配合 `debug_select_frame` 使用 |
| `debug_get_variables` | 查看当前帧的所有局部变量。需查看其他帧变量时先 `debug_select_frame` |
| `debug_evaluate` | 求值任意 C++ 表达式（变量、运算、成员访问、函数调用等），比 `get_variables` 更灵活 |
| `debug_source_context` | 查看当前位置附近源代码（带行号标记）。建议每次暂停后先调用此工具 |
| `debug_select_frame` | 切换到调用栈中指定帧，之后 `get_variables`/`source_context` 显示该帧数据 |

### 线程管理（均需程序暂停）

| 工具名 | 说明 |
|---|---|
| `debug_thread_list` | 列出所有线程及其 ID、名称、状态、执行位置 |
| `debug_select_thread` | 切换到指定线程，之后所有检查工具作用于该线程 |

### 高级功能

| 工具名 | 说明 |
|---|---|
| `debug_read_memory` | 读取内存地址的原始字节（hex + ASCII）。支持地址或 `&var` 格式 |
| `debug_disassemble` | 反汇编函数或当前位置的机器码 |
| `debug_set_watchpoint` | 设置数据断点，变量被修改时自动暂停。变量须在当前作用域可见 |
| `debug_raw_command` | 发送 lldb/gdb 原生命令（兜底方案，如修改变量值 `expr x = 42`） |

### 程序输出查看

| 工具名 | 说明 |
|---|---|
| `debug_get_program_output` | 查看被调试程序的 stdout/stderr 输出。支持三种模式：`all`（全部）、`head`（前 N 行）、`tail`（后 N 行）。程序重新 run 时自动清空 |

### 超时输出恢复

| 工具名 | 说明 |
|---|---|
| `debug_get_pending_output` | **关键恢复工具**。当 run/continue/step_out 超时后，获取缓存的调试器输出并判断程序状态 |

## 典型调试流程

```
debug_start                          # 1. 启动调试会话
  ↓
debug_load("./my_program")           # 2. 加载可执行文件
  ↓
debug_set_breakpoint("main")         # 3. 设置断点
debug_set_breakpoint("utils.cpp:42")
debug_set_breakpoint("main.cpp:15",   # 3.1 条件断点
    condition="i > 100")               #     仅当 i > 100 时暂停
  ↓
debug_run()                          # 4. 运行程序
```

### 小程序快速调试流程（推荐）

对于小型程序，建议使用 `stop_at_entry` 模式，程序会在 main 入口自动暂停，agent 有充足时间查看代码和设置断点：

```
debug_start()                        # 1. 启动调试会话
  ↓
debug_load("./my_program")           # 2. 加载可执行文件
  ↓
debug_run(stop_at_entry=True)        # 3. 运行并自动在 main 暂停（无需提前设断点）
  ↓
┌─── 程序在 main 入口暂停 ───┐
│                              │
│  debug_source_context()      │  ← 查看代码
│  debug_set_breakpoint(...)   │  ← 按需设置断点
│  debug_continue()            │  ← 继续运行到断点
│                              │
└──────────────────────────────┘
  ↓
┌─── 程序在断点处暂停 ───┐
│                         │
│  debug_get_variables()  │  ← 查看局部变量
│  debug_evaluate("x+1")  │  ← 求值表达式
│  debug_backtrace()      │  ← 查看调用栈
│  debug_source_context() │  ← 查看源代码
│                         │
│  debug_step_over()      │  ← 单步执行
│  debug_step_into()      │  ← 进入函数
│  debug_continue()       │  ← 继续运行
│                         │
└─────────────────────────┘
  ↓
debug_stop()                         # 5. 结束调试
```

### 使用环境变量的调试流程

```
debug_start(env_vars='{              # 1. 启动时传入环境变量
    "LD_LIBRARY_PATH": "/opt/lib",
    "MY_CONFIG": "/etc/app.conf",
    "DEBUG_LEVEL": "3"
}')
  ↓
debug_load("./my_program")           # 2. 加载时自动将环境变量设置给被调试程序
  ↓
... 后续流程与普通调试相同 ...
```

> **说明**：`env_vars` 参数接受 JSON 字符串格式的键值对。环境变量会同时透传给调试器进程和被调试的目标程序。

> **提示**：`stop_at_entry` 使用一次性临时断点（one-shot breakpoint），命中后自动删除，不影响用户手动设置的其他断点。

### 超时场景的调试流程

```
debug_run()                          # 程序开始运行
  ↓
返回 [超时]                        # 程序未在超时时间内到达断点
  ↓
debug_get_pending_output()           # 获取缓存输出并检查状态
  ↓
┌──────────────────────────────────────┐
│ [状态:已暂停]  →  正常调试流程          │
│ [状态:运行中]  →  debug_continue()        │
│                     →  再次等待断点命中        │
│ [状态:已结束]  →  查看输出或重新运行        │
└──────────────────────────────────────┘
```

## 半结构化返回值

工具返回值中嵌入了状态标记，方便 Agent 程序化判断当前状态：

| 标记 | 含义 | Agent 应采取的动作 |
|---|---|---|
| `[状态:未启动]` | 调试器未启动或已关闭 | 仅 `debug_start` |
| `[状态:已启动]` | 调试会话已启动，未加载程序 | `debug_load`, `debug_stop` |
| `[状态:已加载]` | 可执行文件已加载 | `debug_set_breakpoint`, `debug_run`, `debug_stop` |
| `[状态:已暂停]` | 程序在断点/单步处停下 | 所有工具均可使用 |
| `[状态:运行中]` | 程序正在执行中 | `debug_get_pending_output`, `debug_get_program_output`, `debug_stop` |
| `[状态:已结束]` | 程序已退出 | `debug_get_program_output`, `debug_run`, `debug_stop` |
| `[超时]` | 等待超时，程序可能仍在运行 | 必须调用 `debug_get_pending_output`，可查看 `debug_get_program_output(mode="tail")` |
| `[无输出]` | 程序没有产生输出 | 程序可能未运行或没有 print 语句 |

关键状态转换工具（`debug_run`、`debug_continue`、`debug_step_out`、`debug_get_pending_output`）的返回值中还包含 **`next=[...]`** 白名单，列出当前状态下推荐的后续工具，Agent 应优先从该列表中选择下一步操作。

## 技术实现

- **程序输出缓存**：持续累积被调试程序的 stdout/stderr 输出，通过 `debug_get_program_output` 工具随时查阅，支持全量/前 N 行/后 N 行三种读取模式
- **半结构化返回**：关键工具的返回值嵌入 `[状态:xxx]` 统一前缀标记（如 `[状态:已暂停]`、`[状态:已结束]`、`[超时]`）和 `next=[...]` 白名单，便于 Agent 程序化判断并选择下一步动作
- **超时安全机制**：长时间运行命令（run/continue/step_out）超时后不会丢失输出，而是缓存到内部队列，通过 `debug_get_pending_output` 工具即可获取
- **异步输出读取**：使用后台线程 + 队列（`threading.Thread` + `queue.Queue`）持续读取调试器输出，避免阻塞
- **提示符检测**：通过正则表达式匹配 `(lldb)` / `(gdb)` 提示符来判断命令执行完成
- **后端抽象**：`DebuggerBackend` 基类定义统一接口，`LLDBBackend` 和 `GDBBackend` 分别实现，`CppDebugger` 作为工厂自动选择后端
