"""
C++ 调试器封装层
通过子进程调用 lldb 或 gdb 命令行工具，实现 C++ 程序调试功能。
自动检测可用的调试器，优先使用 lldb，回退到 gdb。
"""

import subprocess
import threading
import queue
import time
import os
import re
import shutil
import platform
from typing import Optional


class DebuggerBackend:
    """调试器后端基类"""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._output_queue: queue.Queue = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._target_path: Optional[str] = None
        self._is_running = False
        # 超时缓存：存储因超时而未能完整返回的调试器输出
        self._pending_output: str = ""
        # 默认运行超时时间（秒），可通过启动参数 --run-timeout 配置
        self.run_timeout: float = 30.0
        # 用户指定的环境变量，会透传给调试器进程和被调试程序
        self._env_vars: Optional[dict] = None
        # 程序输出缓存：持续积累被调试程序的所有输出（按行存储）
        self._program_output_lines: list = []

    @property
    def is_active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def name(self) -> str:
        raise NotImplementedError

    def _read_output(self):
        """后台线程：持续逐字符读取子进程的 stdout。
        
        使用 read(1) 代替 readline()，因为 lldb/gdb 的交互式提示符
        （如 '(lldb) '、'(gdb) '）输出后不带换行符。
        如果使用 readline()，会一直阻塞等待 \\n，导致提示符行永远无法进入队列，
        主线程的提示符匹配也就永远超时。
        
        逐字符读取会在遇到换行符或检测到提示符模式时，将当前缓冲区内容放入队列。
        """
        try:
            buffer = ""
            prompt_re = re.compile(r"\((lldb|gdb)\)\s*$")
            while self._process and self._process.poll() is None:
                ch = self._process.stdout.read(1)
                if not ch:
                    # EOF：进程已关闭 stdout
                    if buffer:
                        self._output_queue.put(buffer)
                        buffer = ""
                    break
                buffer += ch
                if ch == "\n":
                    # 遇到换行符，将整行放入队列
                    self._output_queue.put(buffer)
                    buffer = ""
                elif prompt_re.search(buffer):
                    # 检测到提示符（不带换行符），立即将其放入队列
                    self._output_queue.put(buffer)
                    buffer = ""
        except Exception:
            pass

    def _collect_output(self, timeout: float = 2.0, prompt_pattern: str = "") -> str:
        raise NotImplementedError

    def _accumulate_program_output(self, output: str):
        """将调试器返回的输出追加到程序输出缓存中。
        由于调试器的 stdout 包含了被调试程序的 stdout/stderr，
        我们将所有非空输出按行累积，供 agent 随时查阅。
        """
        if output and output.strip():
            lines = output.splitlines()
            self._program_output_lines.extend(lines)

    def get_program_output(self, mode: str = "all", lines: int = 20) -> str:
        """获取被调试程序的输出内容。

        由于 lldb/gdb 以 CLI 模式运行时，被调试程序的 stdout/stderr 会混在调试器的输出流中，
        此方法返回的内容包含程序输出和调试器信息的混合体。

        同时，此方法也会先检查输出队列中是否有新到达的数据（不阻塞等待提示符），
        将其一并纳入输出缓存后再返回。

        Args:
            mode: 读取模式
                - "all": 返回全部输出
                - "head": 返回前 N 行
                - "tail": 返回后 N 行
            lines: head/tail 模式下返回的行数，默认 20

        Returns:
            程序输出内容及统计信息
        """
        # 先从队列中捞取最新的输出（不阻塞）
        new_lines = []
        while not self._output_queue.empty():
            try:
                line = self._output_queue.get_nowait()
                new_lines.append(line)
            except queue.Empty:
                break
        if new_lines:
            new_text = "".join(new_lines)
            new_text = self._clean_prompt(new_text).strip() if hasattr(self, '_clean_prompt') else new_text.strip()
            self._accumulate_program_output(new_text)

        total = len(self._program_output_lines)
        if total == 0:
            return ("[无输出] 目前没有捕获到任何程序输出。\n"
                    "可能原因：程序尚未运行、程序没有输出、或输出尚未到达。")

        if mode == "head":
            selected = self._program_output_lines[:lines]
            content = "\n".join(selected)
            shown = len(selected)
            return (f"[程序输出] 显示前 {shown} 行（共 {total} 行）：\n"
                    f"{'─' * 40}\n{content}\n{'─' * 40}\n"
                    f"{'（还有 ' + str(total - shown) + ' 行未显示，使用 tail 或 all 模式查看）' if total > shown else '（已显示全部）'}")
        elif mode == "tail":
            selected = self._program_output_lines[-lines:]
            content = "\n".join(selected)
            shown = len(selected)
            skipped = total - shown
            return (f"[程序输出] 显示后 {shown} 行（共 {total} 行）：\n"
                    f"{'（前面还有 ' + str(skipped) + ' 行未显示）' if skipped > 0 else ''}\n"
                    f"{'─' * 40}\n{content}\n{'─' * 40}")
        else:  # all
            content = "\n".join(self._program_output_lines)
            return (f"[程序输出] 全部输出（共 {total} 行）：\n"
                    f"{'─' * 40}\n{content}\n{'─' * 40}")

    def _drain_queue_to_output_cache(self):
        """将队列中的数据转存到程序输出缓存，避免清空队列时丢失程序输出"""
        drained = []
        while not self._output_queue.empty():
            try:
                drained.append(self._output_queue.get_nowait())
            except queue.Empty:
                break
        if drained:
            text = "".join(drained)
            text = self._clean_prompt(text).strip() if hasattr(self, '_clean_prompt') else text.strip()
            self._accumulate_program_output(text)

    def _send_command(self, command: str, timeout: float = 5.0) -> str:
        if not self.is_active:
            raise RuntimeError(f"{self.name} 进程未启动。请先调用 start() 启动调试会话。")

        with self._lock:
            # 将队列中的旧数据转存到输出缓存，避免丢失程序输出
            self._drain_queue_to_output_cache()

            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()

            result = self._collect_output(timeout=timeout)
            # 将输出追加到程序输出缓存
            self._accumulate_program_output(result)
            return result

    def _send_long_command(self, command: str, timeout: float = 30.0) -> str:
        """
        发送可能长时间运行的命令（如 run、continue、step_out）。

        特性：
        - 超时后将已收集的部分输出缓存到 _pending_output，而非丢弃
        - 返回值中会明确标注是否因超时而截断

        Args:
            command: 调试器命令
            timeout: 最大等待时间（秒）
        """
        if not self.is_active:
            raise RuntimeError(f"{self.name} 进程未启动。请先调用 start() 启动调试会话。")

        with self._lock:
            # 将队列中的旧数据转存到输出缓存，避免丢失程序输出
            self._drain_queue_to_output_cache()
            # 清空上次的 pending 缓存
            self._pending_output = ""

            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()

            # 分段收集输出
            prompt_pattern = self._get_prompt_pattern()
            lines = []
            deadline = time.time() + timeout
            start_time = time.time()
            timed_out = False

            while time.time() < deadline:
                try:
                    remaining = max(0.05, deadline - time.time())
                    line = self._output_queue.get(timeout=min(0.2, remaining))
                    lines.append(line)
                    accumulated = "".join(lines)
                    if re.search(prompt_pattern, accumulated):
                        # 命令已完成，收集剩余输出
                        time.sleep(0.05)
                        while not self._output_queue.empty():
                            try:
                                extra = self._output_queue.get_nowait()
                                lines.append(extra)
                            except queue.Empty:
                                break
                        break
                except queue.Empty:
                    pass
            else:
                # 循环自然结束 = 超时
                timed_out = True

            result = "".join(lines)
            result = self._clean_prompt(result).strip()

            if timed_out:
                # 缓存超时时已收集到的部分输出
                self._pending_output = result
                elapsed = time.time() - start_time
                return (f"[超时] [状态:运行中] 程序仍在运行中，已等待 {elapsed:.0f} 秒但未暂停。\n"
                        f"已收集到的部分输出已缓存。\n"
                        f"next=[debug_get_pending_output]\n"
                        f"提示：也可调用 debug_get_program_output(mode='tail') 查看程序最新的打印输出。")

            # 将输出追加到程序输出缓存
            self._accumulate_program_output(result)

            # 为正常完成的长时间命令添加状态标记和 next action 白名单
            state_tag = self._detect_program_state(result)
            if state_tag == "[状态:已结束]":
                return (f"{state_tag} {result}\n"
                        f"next=[debug_get_program_output, debug_run, debug_stop]")
            elif state_tag == "[状态:已暂停]":
                return (f"{state_tag} {result}\n"
                        f"next=[debug_source_context, debug_get_variables, debug_backtrace, debug_step_over, debug_step_into, debug_continue]")
            elif state_tag:
                return f"{state_tag} {result}"
            return result

    def _get_prompt_pattern(self) -> str:
        """返回当前后端的提示符正则（子类应覆盖）"""
        raise NotImplementedError

    def _clean_prompt(self, text: str) -> str:
        """清除输出中的提示符（子类应覆盖）"""
        raise NotImplementedError

    def get_pending_output(self) -> str:
        """
        获取因超时而缓存的调试器输出。
        调用后缓存会被清空。
        如果缓存为空，说明没有待处理的输出。
        同时也会检查队列中是否有新到达的输出（调试器可能在超时后才返回结果）。
        """
        # 先检查队列中是否有超时后新到达的输出
        new_lines = []
        while not self._output_queue.empty():
            try:
                line = self._output_queue.get_nowait()
                new_lines.append(line)
            except queue.Empty:
                break

        new_output = "".join(new_lines)
        if new_output:
            new_output = self._clean_prompt(new_output).strip() if hasattr(self, '_clean_prompt') else new_output.strip()

        # 合并 pending 缓存 + 新到达的输出
        combined = ""
        if self._pending_output:
            combined = self._pending_output
        if new_output:
            if combined:
                combined += "\n" + new_output
            else:
                combined = new_output

        # 清空缓存
        self._pending_output = ""

        if not combined:
            return "[状态:运行中] 没有待处理的缓存输出。程序可能仍在运行中，也可能已经在断点处暂停。\nnext=[debug_get_pending_output, debug_get_program_output, debug_stop]"

        # 将获取到的输出也追加到程序输出缓存
        self._accumulate_program_output(combined)

        # 判断是否已经在断点处暂停（检查是否有提示符出现）
        prompt_pattern = self._get_prompt_pattern()
        if re.search(prompt_pattern, "".join(new_lines)):
            # 进一步判断是暂停还是已结束
            state_tag = self._detect_program_state(combined)
            if state_tag == "[状态:已结束]":
                return (f"[状态:已结束] 程序已退出。以下是调试器的完整输出：\n{combined}\n"
                        f"next=[debug_get_program_output, debug_run, debug_stop]")
            return (f"[状态:已暂停] 断点已命中或程序已停下。以下是调试器的完整输出：\n{combined}\n"
                    f"next=[debug_source_context, debug_get_variables, debug_backtrace, debug_step_over, debug_step_into, debug_continue]")
        else:
            return (f"[状态:运行中] 程序可能仍在运行，尚未暂停。以下是目前收集到的输出：\n{combined}\n"
                    f"next=[debug_get_pending_output, debug_get_program_output, debug_stop]\n"
                    f"提示：可调用 debug_get_program_output(mode='tail') 查看程序最新的打印输出。")

    def _build_subprocess_env(self) -> Optional[dict]:
        """构建传给子进程的环境变量字典。如果没有自定义环境变量则返回 None（继承父进程环境）"""
        if not self._env_vars:
            return None
        env = os.environ.copy()
        env.update(self._env_vars)
        return env

    def _detect_program_state(self, output: str) -> str:
        """根据调试器输出判断程序当前状态，返回状态标记前缀。子类可覆盖以适配不同后端。"""
        lower = output.lower()
        # 检测程序退出
        if any(kw in lower for kw in ["exited with status", "exited normally",
                                       "process exited", "program exited",
                                       "exited with code", "terminated",
                                       "process finished", "inferior exited"]):
            return "[状态:已结束]"
        # 检测断点命中 / 程序暂停
        if any(kw in lower for kw in ["breakpoint", "stop reason", "stopped",
                                       "watchpoint", "signal", "frame #",
                                       "at line", "hit breakpoint"]):
            return "[状态:已暂停]"
        # 如果有提示符出现（说明命令已完成，程序已暂停在调试器控制下）
        prompt_pattern = self._get_prompt_pattern()
        if re.search(prompt_pattern, output):
            return "[状态:已暂停]"
        return ""

    def _apply_env_to_target(self) -> str:
        """在调试器中设置环境变量，使其透传给被调试程序（子类应覆盖）"""
        return ""

    def start(self, env_vars: Optional[dict] = None) -> str:
        self._env_vars = env_vars
        raise NotImplementedError

    def stop(self) -> str:
        raise NotImplementedError

    def load_target(self, executable_path: str) -> str:
        raise NotImplementedError

    def set_breakpoint(self, location: str, condition: str = "") -> str:
        raise NotImplementedError

    def delete_breakpoint(self, breakpoint_id: str) -> str:
        raise NotImplementedError

    def list_breakpoints(self) -> str:
        raise NotImplementedError

    def run(self, args: str = "", stop_at_entry: bool = False) -> str:
        raise NotImplementedError

    def continue_execution(self) -> str:
        raise NotImplementedError

    def step_over(self) -> str:
        raise NotImplementedError

    def step_into(self) -> str:
        raise NotImplementedError

    def step_out(self) -> str:
        raise NotImplementedError

    def get_backtrace(self) -> str:
        raise NotImplementedError

    def get_local_variables(self) -> str:
        raise NotImplementedError

    def evaluate_expression(self, expression: str) -> str:
        raise NotImplementedError

    def get_source_context(self, count: int = 10) -> str:
        raise NotImplementedError

    def select_frame(self, frame_index: int) -> str:
        raise NotImplementedError

    def get_thread_info(self) -> str:
        raise NotImplementedError

    def select_thread(self, thread_index: int) -> str:
        raise NotImplementedError

    def read_memory(self, address: str, count: int = 64) -> str:
        raise NotImplementedError

    def disassemble(self, function_name: str = "") -> str:
        raise NotImplementedError

    def set_watchpoint(self, variable: str) -> str:
        raise NotImplementedError

    def send_raw_command(self, command: str) -> str:
        raise NotImplementedError

    def attach(self, pid: int) -> str:
        """附加到指定 PID 的进程（子类应覆盖）"""
        raise NotImplementedError

    def detach(self) -> str:
        """从当前调试的进程脱离（子类应覆盖）"""
        raise NotImplementedError

    def get_pending_output_safe(self) -> str:
        """安全版本的 get_pending_output，即使未启动也不会抛异常"""
        if not self.is_active:
            return "[状态:未启动] 调试器未启动，没有待处理的输出。"
        return self.get_pending_output()

    def get_program_output_safe(self, mode: str = "all", lines: int = 20) -> str:
        """安全版本的 get_program_output，即使未启动也不会抛异常"""
        if not self.is_active:
            return "[状态:未启动] 调试器未启动，没有程序输出。"
        return self.get_program_output(mode=mode, lines=lines)

    def reset_program_output(self):
        """重置程序输出缓存（在程序重新启动时调用）"""
        self._program_output_lines = []


# ====================================================================
# LLDB 后端
# ====================================================================


class LLDBBackend(DebuggerBackend):
    """LLDB 调试器后端"""

    @property
    def name(self) -> str:
        return "lldb"

    def _get_prompt_pattern(self) -> str:
        return r"\(lldb\)\s*$"

    def _clean_prompt(self, text: str) -> str:
        return re.sub(r"\(lldb\)\s*", "", text)

    def _collect_output(self, timeout: float = 2.0, prompt_pattern: str = r"\(lldb\)\s*$") -> str:
        lines = []
        deadline = time.time() + timeout
        accumulated = ""

        while time.time() < deadline:
            try:
                remaining = max(0.05, deadline - time.time())
                line = self._output_queue.get(timeout=min(0.1, remaining))
                lines.append(line)
                accumulated = "".join(lines)
                if re.search(prompt_pattern, accumulated):
                    time.sleep(0.05)
                    while not self._output_queue.empty():
                        try:
                            extra = self._output_queue.get_nowait()
                            lines.append(extra)
                        except queue.Empty:
                            break
                    break
            except queue.Empty:
                continue

        result = "".join(lines)
        # 移除所有 (lldb) 提示符
        result = re.sub(r"\(lldb\)\s*", "", result).strip()
        return result

    def _apply_env_to_target(self) -> str:
        """在 lldb 中设置环境变量给被调试程序"""
        if not self._env_vars:
            return ""
        results = []
        for key, value in self._env_vars.items():
            result = self._send_command(f'settings set target.env-vars {key}={value}')
            if result.strip():
                results.append(result)
        return "\n".join(results)

    def start(self, env_vars: Optional[dict] = None) -> str:
        if self.is_active:
            return "lldb 会话已在运行中。"

        self._env_vars = env_vars
        lldb_path = shutil.which("lldb")
        if not lldb_path:
            raise FileNotFoundError("未找到 lldb。请确保 LLVM 工具链已安装且 lldb 在 PATH 中。")

        self._process = subprocess.Popen(
            [lldb_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=self._build_subprocess_env(),
        )

        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

        # 短暂等待，检查进程是否立即崩溃退出
        time.sleep(0.3)
        if self._process.poll() is not None:
            exit_code = self._process.returncode
            # 尝试收集崩溃前的错误输出
            error_output = ""
            while not self._output_queue.empty():
                try:
                    error_output += self._output_queue.get_nowait()
                except queue.Empty:
                    break
            self._process = None
            error_detail = error_output.strip()
            msg = f"[错误] lldb 启动失败（退出码: {exit_code}）。\n"
            if error_detail:
                msg += f"{error_detail}\n"
            msg += (f"可能原因：lldb 版本不兼容、依赖库缺失、或系统环境异常。\n"
                    f"请检查 lldb 是否能在命令行中正常启动。\n"
                    f"next=[debug_start]")
            return msg

        output = self._collect_output(timeout=3.0)

        # 收集输出后再次检查进程是否意外退出
        if self._process.poll() is not None:
            exit_code = self._process.returncode
            self._process = None
            return (f"[错误] lldb 启动后立即退出（退出码: {exit_code}）。\n"
                    f"输出信息：{output}\n"
                    f"请检查 lldb 是否能在命令行中正常运行。\n"
                    f"next=[debug_start]")

        return f"[状态:已启动] **调试后端: lldb**\nlldb 调试会话已启动。\n{output}"

    def stop(self) -> str:
        if not self.is_active:
            self._process = None
            return "[状态:未启动] lldb 会话未在运行。"
        try:
            self._process.stdin.write("quit\n")
            self._process.stdin.flush()
            self._process.wait(timeout=5)
        except Exception:
            self._process.kill()
        self._process = None
        self._is_running = False
        return "[状态:未启动] lldb 调试会话已关闭。"

    def load_target(self, executable_path: str) -> str:
        if not os.path.isfile(executable_path):
            return "[错误] 文件不存在：{}".format(executable_path)
        self._target_path = os.path.abspath(executable_path)
        result = self._send_command(f'file "{self._target_path}"')
        # 加载目标后，将环境变量设置给被调试程序
        env_result = self._apply_env_to_target()
        env_info = ""
        if self._env_vars:
            env_info = f"\n已设置 {len(self._env_vars)} 个环境变量：{', '.join(self._env_vars.keys())}"
            if env_result:
                env_info += f"\n{env_result}"
        return f"[状态:已加载] 已加载目标程序：{self._target_path}\n{result}{env_info}"

    def set_breakpoint(self, location: str, condition: str = "") -> str:
        # 构建基础断点命令
        if ":" in location:
            parts = location.rsplit(":", 1)
            if len(parts) == 2 and parts[1].isdigit():
                file_name, line_no = parts
                cmd = f"breakpoint set --file {file_name} --line {line_no}"
            else:
                cmd = f"breakpoint set --name {location}"
        else:
            cmd = f"breakpoint set --name {location}"
        # 添加条件
        if condition:
            cmd += f" --condition '{condition}'"
        return self._send_command(cmd)

    def delete_breakpoint(self, breakpoint_id: str) -> str:
        return self._send_command(f"breakpoint delete {breakpoint_id}")

    def list_breakpoints(self) -> str:
        return self._send_command("breakpoint list")

    def run(self, args: str = "", stop_at_entry: bool = False) -> str:
        self._is_running = True
        # 重新运行时清空程序输出缓存
        self.reset_program_output()
        if stop_at_entry:
            # 在 main 函数处设置临时断点，确保程序启动后立即暂停
            self._send_command("breakpoint set --name main --one-shot true")
        cmd = f"run {args}" if args else "run"
        result = self._send_long_command(cmd, timeout=self.run_timeout)
        if stop_at_entry and "[超时]" not in result:
            result += ("\n\n[入口暂停] 程序已在 main 函数入口处暂停（临时断点已自动删除）。\n"
                       "next=[debug_source_context, debug_set_breakpoint, debug_get_variables, debug_continue]")
        return result

    def continue_execution(self) -> str:
        return self._send_long_command("continue", timeout=self.run_timeout)

    def step_over(self) -> str:
        return self._send_command("next")

    def step_into(self) -> str:
        return self._send_command("step")

    def step_out(self) -> str:
        return self._send_long_command("finish", timeout=self.run_timeout)

    def get_backtrace(self) -> str:
        return self._send_command("bt")

    def get_local_variables(self) -> str:
        return self._send_command("frame variable")

    def evaluate_expression(self, expression: str) -> str:
        return self._send_command(f"expr {expression}")

    def get_source_context(self, count: int = 10) -> str:
        return self._send_command(f"source list -c {count}")

    def select_frame(self, frame_index: int) -> str:
        return self._send_command(f"frame select {frame_index}")

    def get_thread_info(self) -> str:
        return self._send_command("thread list")

    def select_thread(self, thread_index: int) -> str:
        return self._send_command(f"thread select {thread_index}")

    def read_memory(self, address: str, count: int = 64) -> str:
        return self._send_command(f"memory read {address} -c {count}")

    def disassemble(self, function_name: str = "") -> str:
        if function_name:
            return self._send_command(f"disassemble -n {function_name}")
        return self._send_command("disassemble")

    def set_watchpoint(self, variable: str) -> str:
        return self._send_command(f"watchpoint set variable {variable}")

    def send_raw_command(self, command: str) -> str:
        return self._send_command(command, timeout=10.0)

    def attach(self, pid: int) -> str:
        """附加到指定 PID 的进程"""
        return self._send_command(f"process attach -p {pid}", timeout=10.0)

    def detach(self) -> str:
        """从当前调试的进程脱离"""
        return self._send_command("process detach", timeout=5.0)


# ====================================================================
# GDB 后端
# ====================================================================


class GDBBackend(DebuggerBackend):
    """GDB 调试器后端"""

    @property
    def name(self) -> str:
        return "gdb"

    def _get_prompt_pattern(self) -> str:
        return r"\(gdb\)\s*$"

    def _clean_prompt(self, text: str) -> str:
        return re.sub(r"\(gdb\)\s*", "", text)

    def _collect_output(self, timeout: float = 2.0, prompt_pattern: str = r"\(gdb\)\s*$") -> str:
        lines = []
        deadline = time.time() + timeout
        accumulated = ""

        while time.time() < deadline:
            try:
                remaining = max(0.05, deadline - time.time())
                line = self._output_queue.get(timeout=min(0.1, remaining))
                lines.append(line)
                accumulated = "".join(lines)
                if re.search(prompt_pattern, accumulated):
                    time.sleep(0.05)
                    while not self._output_queue.empty():
                        try:
                            extra = self._output_queue.get_nowait()
                            lines.append(extra)
                        except queue.Empty:
                            break
                    break
            except queue.Empty:
                continue

        result = "".join(lines)
        # 移除所有 (gdb) 提示符（行首、行尾、行中间）
        result = re.sub(r"\(gdb\)\s*", "", result).strip()
        return result

    def _apply_env_to_target(self) -> str:
        """在 gdb 中设置环境变量给被调试程序"""
        if not self._env_vars:
            return ""
        results = []
        for key, value in self._env_vars.items():
            result = self._send_command(f'set environment {key} {value}')
            if result.strip():
                results.append(result)
        return "\n".join(results)

    def start(self, env_vars: Optional[dict] = None) -> str:
        if self.is_active:
            return "gdb 会话已在运行中。"

        self._env_vars = env_vars
        gdb_path = shutil.which("gdb")
        if not gdb_path:
            raise FileNotFoundError("未找到 gdb。请确保 GDB 已安装且在 PATH 中。")

        self._process = subprocess.Popen(
            [gdb_path, "-q"],  # -q: 静默启动，不打印版本信息
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=self._build_subprocess_env(),
        )

        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

        # 短暂等待，检查进程是否立即崩溃退出
        time.sleep(0.3)
        if self._process.poll() is not None:
            exit_code = self._process.returncode
            # 尝试收集崩溃前的错误输出
            error_output = ""
            while not self._output_queue.empty():
                try:
                    error_output += self._output_queue.get_nowait()
                except queue.Empty:
                    break
            self._process = None
            error_detail = error_output.strip()
            msg = f"[错误] gdb 启动失败（退出码: {exit_code}）。\n"
            if error_detail:
                msg += f"{error_detail}\n"
            msg += (f"可能原因：gdb 版本不兼容、依赖库缺失、或系统环境异常。\n"
                    f"请检查 gdb 是否能在命令行中正常启动。\n"
                    f"next=[debug_start]")
            return msg

        output = self._collect_output(timeout=3.0)

        # 收集输出后再次检查进程是否意外退出
        if self._process.poll() is not None:
            exit_code = self._process.returncode
            self._process = None
            return (f"[错误] gdb 启动后立即退出（退出码: {exit_code}）。\n"
                    f"输出信息：{output}\n"
                    f"请检查 gdb 是否能在命令行中正常运行。\n"
                    f"next=[debug_start]")

        return f"[状态:已启动] **调试后端: gdb**\ngdb 调试会话已启动。\n{output}"

    def stop(self) -> str:
        if not self.is_active:
            self._process = None
            return "[状态:未启动] gdb 会话未在运行。"
        try:
            self._process.stdin.write("quit\n")
            self._process.stdin.flush()
            # gdb 可能会提示确认退出
            time.sleep(0.2)
            if self._process.poll() is None:
                self._process.stdin.write("y\n")
                self._process.stdin.flush()
            self._process.wait(timeout=5)
        except Exception:
            self._process.kill()
        self._process = None
        self._is_running = False
        return "[状态:未启动] gdb 调试会话已关闭。"

    def load_target(self, executable_path: str) -> str:
        if not os.path.isfile(executable_path):
            return "[错误] 文件不存在：{}".format(executable_path)
        self._target_path = os.path.abspath(executable_path)
        result = self._send_command(f'file "{self._target_path}"')
        # 加载目标后，将环境变量设置给被调试程序
        env_result = self._apply_env_to_target()
        env_info = ""
        if self._env_vars:
            env_info = f"\n已设置 {len(self._env_vars)} 个环境变量：{', '.join(self._env_vars.keys())}"
            if env_result:
                env_info += f"\n{env_result}"
        return f"[状态:已加载] 已加载目标程序：{self._target_path}\n{result}{env_info}"

    def set_breakpoint(self, location: str, condition: str = "") -> str:
        # gdb 的 break 命令直接支持 函数名 和 文件:行号 格式
        cmd = f"break {location}"
        result = self._send_command(cmd)
        # 如果有条件，使用 condition 命令为刚设置的断点添加条件
        if condition:
            # 从结果中提取断点编号
            match = re.search(r'Breakpoint\s+(\d+)', result)
            if match:
                bp_id = match.group(1)
                cond_result = self._send_command(f"condition {bp_id} {condition}")
                result += f"\n已为断点 {bp_id} 设置条件：{condition}\n{cond_result}"
            else:
                result += f"\n警告：无法自动设置条件，请手动执行：condition <断点ID> {condition}"
        return result

    def delete_breakpoint(self, breakpoint_id: str) -> str:
        return self._send_command(f"delete {breakpoint_id}")

    def list_breakpoints(self) -> str:
        return self._send_command("info breakpoints")

    def run(self, args: str = "", stop_at_entry: bool = False) -> str:
        self._is_running = True
        # 重新运行时清空程序输出缓存
        self.reset_program_output()
        if args:
            self._send_command(f"set args {args}")
        if stop_at_entry:
            # 在 main 函数处设置临时断点，确保程序启动后立即暂停
            self._send_command("tbreak main")
        result = self._send_long_command("run", timeout=self.run_timeout)
        if stop_at_entry and "[超时]" not in result:
            result += ("\n\n[入口暂停] 程序已在 main 函数入口处暂停（临时断点已自动删除）。\n"
                       "next=[debug_source_context, debug_set_breakpoint, debug_get_variables, debug_continue]")
        return result

    def continue_execution(self) -> str:
        return self._send_long_command("continue", timeout=self.run_timeout)

    def step_over(self) -> str:
        return self._send_command("next")

    def step_into(self) -> str:
        return self._send_command("step")

    def step_out(self) -> str:
        return self._send_long_command("finish", timeout=self.run_timeout)

    def get_backtrace(self) -> str:
        return self._send_command("backtrace")

    def get_local_variables(self) -> str:
        return self._send_command("info locals")

    def evaluate_expression(self, expression: str) -> str:
        return self._send_command(f"print {expression}")

    def get_source_context(self, count: int = 10) -> str:
        return self._send_command(f"list")

    def select_frame(self, frame_index: int) -> str:
        return self._send_command(f"frame {frame_index}")

    def get_thread_info(self) -> str:
        return self._send_command("info threads")

    def select_thread(self, thread_index: int) -> str:
        return self._send_command(f"thread {thread_index}")

    def read_memory(self, address: str, count: int = 64) -> str:
        # gdb 使用 x 命令读取内存
        return self._send_command(f"x/{count}xb {address}")

    def disassemble(self, function_name: str = "") -> str:
        if function_name:
            return self._send_command(f"disassemble {function_name}")
        return self._send_command("disassemble")

    def set_watchpoint(self, variable: str) -> str:
        return self._send_command(f"watch {variable}")

    def send_raw_command(self, command: str) -> str:
        return self._send_command(command, timeout=10.0)

    def attach(self, pid: int) -> str:
        """附加到指定 PID 的进程"""
        return self._send_command(f"attach {pid}", timeout=10.0)

    def detach(self) -> str:
        """从当前调试的进程脱离"""
        return self._send_command("detach", timeout=5.0)


# ====================================================================
# 跨平台子进程列表获取（纯标准库实现）
# ====================================================================


def list_child_processes(parent_pid: int) -> list:
    """获取指定进程的所有直接子进程列表。

    返回值格式：[{"pid": int, "name": str}, ...]

    平台实现：
    - Linux/macOS：通过 subprocess 调用 pgrep -P 和 ps 命令
    - Windows：通过 ctypes 调用 Win32 API CreateToolhelp32Snapshot

    纯标准库实现，不依赖 psutil 等第三方库。
    """
    system = platform.system()
    if system == "Windows":
        return _list_child_processes_windows(parent_pid)
    else:
        return _list_child_processes_unix(parent_pid)


def _list_child_processes_unix(parent_pid: int) -> list:
    """Unix（Linux/macOS）实现：通过 pgrep 和 ps 命令获取子进程列表"""
    children = []
    try:
        # 使用 pgrep -P 获取直接子进程的 PID 列表
        output = subprocess.check_output(
            ["pgrep", "-P", str(parent_pid)],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        if not output:
            return []
        pids = [int(p) for p in output.splitlines() if p.strip().isdigit()]
        for pid in pids:
            name = "未知"
            try:
                # 使用 ps 获取进程名
                ps_output = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    text=True, stderr=subprocess.DEVNULL
                ).strip()
                if ps_output:
                    name = ps_output
            except Exception:
                pass
            children.append({"pid": pid, "name": name})
    except subprocess.CalledProcessError:
        # pgrep 返回非零退出码表示没有匹配的进程
        pass
    except FileNotFoundError:
        raise RuntimeError("pgrep 命令不可用，请确保系统已安装 procps 工具包。")
    return children


def _list_child_processes_windows(parent_pid: int) -> list:
    """Windows 实现：通过 ctypes 调用 Win32 API 获取子进程列表。

    使用 CreateToolhelp32Snapshot + Process32First/Process32Next 遍历进程快照，
    筛选 th32ParentProcessID 匹配的进程。纯标准库实现，不依赖外部命令。
    """
    import ctypes
    import ctypes.wintypes

    # Win32 API 常量
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    # PROCESSENTRY32W 结构体
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
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

    kernel32 = ctypes.windll.kernel32

    # 创建进程快照
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise RuntimeError("CreateToolhelp32Snapshot 调用失败")

    children = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)

        # 遍历进程快照
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32ParentProcessID == parent_pid:
                    children.append({
                        "pid": entry.th32ProcessID,
                        "name": entry.szExeFile
                    })
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    return children


# ====================================================================
# 调试器工厂
# ====================================================================


class CppDebugger:
    """
    C++ 调试器统一接口（多实例管理器）。
    自动检测可用的调试器，优先使用 lldb，回退到 gdb。
    支持同时管理多个调试器实例，用于多进程调试场景。
    #0 为主调试器实例（launch 模式），#1+ 为 attach 到子进程的实例。
    """

    # 最大调试器实例数量
    MAX_INSTANCES = 5

    def __init__(self):
        self._backend_name: Optional[str] = None
        # 多实例管理：key 为调试器编号，value 为后端实例
        self._instances: dict[int, DebuggerBackend] = {}
        # 递增的调试器编号计数器（下一个可分配的编号）
        self._next_id: int = 1
        # 每个实例的元信息：{"process_name": str, "pid": int, "mode": "launch"|"attach"}
        self._instance_metadata: dict[int, dict] = {}

    def _detect_backend(self) -> DebuggerBackend:
        """自动检测并创建调试器后端"""
        # 优先使用 lldb
        if shutil.which("lldb"):
            self._backend_name = "lldb"
            return LLDBBackend()
        # 回退到 gdb
        if shutil.which("gdb"):
            self._backend_name = "gdb"
            return GDBBackend()
        raise FileNotFoundError(
            "未找到任何调试器（lldb 或 gdb）。\n"
            "请安装 LLVM（包含 lldb）或 GDB，并确保它们在 PATH 中。"
        )

    def _create_backend(self) -> DebuggerBackend:
        """根据已检测到的后端类型创建一个新的后端实例"""
        if self._backend_name == "lldb":
            return LLDBBackend()
        elif self._backend_name == "gdb":
            return GDBBackend()
        else:
            raise RuntimeError("后端类型未初始化，请先调用 start() 启动主调试器。")

    def _get_instance(self, debugger_id: int) -> DebuggerBackend:
        """获取指定编号的调试器实例，不存在则抛出错误"""
        if debugger_id not in self._instances:
            raise RuntimeError(f"[错误] 调试器 #{debugger_id} 不存在")
        return self._instances[debugger_id]

    def _get_metadata(self, debugger_id: int) -> dict:
        """获取指定实例的元信息"""
        return self._instance_metadata.get(debugger_id, {
            "process_name": "未知",
            "pid": 0,
            "mode": "unknown"
        })

    def _is_attach_mode(self, debugger_id: int) -> bool:
        """判断指定实例是否为 attach 模式"""
        meta = self._get_metadata(debugger_id)
        return meta.get("mode") == "attach"

    def _format_output(self, debugger_id: int, output: str) -> str:
        """为返回内容添加调试器实例标识前缀（半结构化文本标注）"""
        meta = self._get_metadata(debugger_id)
        process_name = meta.get("process_name", "未知")
        pid = meta.get("pid", 0)
        prefix = f"[调试器 #{debugger_id}][进程: {process_name} (PID: {pid})]"
        return f"{prefix}\n{output}"

    def _update_metadata_pid(self, debugger_id: int):
        """尝试从调试器输出中获取并更新被调试进程的 PID"""
        if debugger_id not in self._instances:
            return
        backend = self._instances[debugger_id]
        if not backend.is_active:
            return
        try:
            pid = self._get_process_pid(backend)
            if pid:
                self._instance_metadata.setdefault(debugger_id, {})["pid"] = pid
        except Exception:
            pass

    def _get_process_pid(self, backend: DebuggerBackend) -> Optional[int]:
        """从调试器中获取当前被调试进程的 PID"""
        try:
            if isinstance(backend, LLDBBackend):
                output = backend._send_command("process status")
                # 匹配类似：Process 12345 stopped 或 process 12345
                match = re.search(r'[Pp]rocess\s+(\d+)', output)
                if match:
                    return int(match.group(1))
            elif isinstance(backend, GDBBackend):
                output = backend._send_command("info inferior")
                # 匹配类似：process 12345
                match = re.search(r'process\s+(\d+)', output)
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        return None

    @property
    def is_active(self) -> bool:
        """主调试器（#0）是否活跃"""
        return 0 in self._instances and self._instances[0].is_active

    @property
    def backend_name(self) -> str:
        return self._backend_name or "未初始化"

    def start(self, env_vars: Optional[dict] = None) -> str:
        if self.is_active:
            return f"[状态:已启动] **调试后端: {self._backend_name}**\n{self._backend_name} 会话已在运行中。"
        backend = self._detect_backend()
        result = backend.start(env_vars=env_vars)
        # 检查后端是否真正启动成功，如果进程已退出则清理
        if not backend.is_active:
            return result
        # 注册为主实例 #0
        self._instances[0] = backend
        self._instance_metadata[0] = {
            "process_name": "未加载",
            "pid": 0,
            "mode": "launch"
        }
        return result

    def stop(self) -> str:
        if not self._instances:
            return "[状态:未启动] 调试会话未启动。"
        results = []
        # 先 detach 所有子实例（#1 及以上）
        child_ids = sorted([k for k in self._instances if k > 0], reverse=True)
        for cid in child_ids:
            try:
                inst = self._instances[cid]
                if inst.is_active:
                    inst.detach()
                inst.stop()
            except Exception:
                try:
                    inst.stop()
                except Exception:
                    pass
            results.append(f"调试器 #{cid} 已关闭")
            del self._instances[cid]
            self._instance_metadata.pop(cid, None)
        # 再停止主实例 #0
        if 0 in self._instances:
            result = self._instances[0].stop()
            results.append(result)
            del self._instances[0]
            self._instance_metadata.pop(0, None)
        else:
            results.append("[状态:未启动] 主调试器未启动。")
        self._next_id = 1
        return "\n".join(results)

    def attach_child(self, pid: int) -> str:
        """创建新的调试器实例并附加到指定 PID 的子进程"""
        if not self.is_active:
            return "[错误] 主调试器未启动，请先调用 debug_start 启动调试会话。"
        if len(self._instances) >= self.MAX_INSTANCES:
            return f"[错误] 调试器实例数量已达上限（{self.MAX_INSTANCES}），请先 detach 不需要的实例。"
        # 创建新后端实例
        backend = self._create_backend()
        result = backend.start()
        if not backend.is_active:
            return f"[错误] 无法启动新的调试器实例：\n{result}"
        # 发送 attach 命令
        attach_result = backend.attach(pid)
        if not backend.is_active:
            return f"[错误] 无法附加到进程 {pid}：\n{attach_result}"
        # 分配编号
        debugger_id = self._next_id
        self._next_id += 1
        self._instances[debugger_id] = backend
        # 获取进程名
        process_name = f"PID-{pid}"
        try:
            actual_pid = self._get_process_pid(backend)
            if actual_pid:
                pid = actual_pid
        except Exception:
            pass
        self._instance_metadata[debugger_id] = {
            "process_name": process_name,
            "pid": pid,
            "mode": "attach"
        }
        return self._format_output(debugger_id,
                                    f"[状态: 已暂停] 已附加到进程 (PID: {pid})\n{attach_result}")

    def detach_child(self, debugger_id: int) -> str:
        """从子进程脱离并关闭对应的调试器实例"""
        if debugger_id == 0:
            return "[错误] 无法脱离主调试器，请使用 debug_stop 结束整个调试会话"
        if debugger_id not in self._instances:
            return f"[错误] 调试器 #{debugger_id} 不存在"
        backend = self._instances[debugger_id]
        result = ""
        try:
            if backend.is_active:
                result = backend.detach()
        except Exception as e:
            result = f"detach 失败: {e}"
        # 无论 detach 是否成功，都清理实例资源
        try:
            backend.stop()
        except Exception:
            pass
        del self._instances[debugger_id]
        meta = self._instance_metadata.pop(debugger_id, {})
        process_name = meta.get("process_name", "未知")
        pid = meta.get("pid", 0)
        return f"[调试器 #{debugger_id}] 已从进程 {process_name} (PID: {pid}) 脱离并关闭\n{result}"

    def list_children(self, debugger_id: int = 0) -> str:
        """列出指定调试器实例所控制进程的子进程"""
        if debugger_id not in self._instances:
            return f"[错误] 调试器 #{debugger_id} 不存在"
        backend = self._instances[debugger_id]
        if not backend.is_active:
            return "[错误] 调试器未启动，无法获取子进程列表"
        # 获取被调试进程的 PID
        pid = self._get_process_pid(backend)
        if not pid:
            return "[错误] 无法获取被调试进程的 PID。程序可能尚未运行（未调用 debug_run），不存在子进程。"
        # 调用平台相关的子进程列表获取函数
        try:
            children = list_child_processes(pid)
        except Exception as e:
            return f"[错误] 获取子进程列表失败：{e}\n建议：请手动提供子进程 PID，使用 debug_attach_child(pid) 附加。"
        if not children:
            return self._format_output(debugger_id, f"当前进程 (PID: {pid}) 没有子进程。")
        lines = [f"当前进程 (PID: {pid}) 的子进程列表："]
        for child in children:
            lines.append(f"  - PID: {child['pid']}, 进程名: {child['name']}")
        return self._format_output(debugger_id, "\n".join(lines))

    def list_debuggers(self) -> str:
        """返回所有活跃调试器实例的列表"""
        if not self._instances:
            return "[状态:未启动] 没有活跃的调试器实例。"
        lines = ["当前活跃的调试器实例："]
        for did in sorted(self._instances.keys()):
            backend = self._instances[did]
            meta = self._get_metadata(did)
            process_name = meta.get("process_name", "未知")
            pid = meta.get("pid", 0)
            mode = meta.get("mode", "unknown")
            status = "活跃" if backend.is_active else "已结束"
            mode_label = "主调试器(launch)" if mode == "launch" else "子调试器(attach)"
            lines.append(f"  调试器 #{did} | {mode_label} | 进程: {process_name} (PID: {pid}) | 状态: {status}")
        return "\n".join(lines)

    def get_program_output_safe(self, mode: str = "all", lines: int = 20, debugger_id: int = 0) -> str:
        """安全版本：即使后端未初始化也不会抛异常"""
        if debugger_id not in self._instances:
            return "[状态:未启动] 调试会话未启动，没有程序输出。"
        backend = self._instances[debugger_id]
        result = backend.get_program_output_safe(mode=mode, lines=lines)
        return self._format_output(debugger_id, result)

    def get_pending_output_safe(self, debugger_id: int = 0) -> str:
        """安全版本的 get_pending_output，即使未启动也不会抛异常"""
        if debugger_id not in self._instances:
            return "[状态:未启动] 调试器未启动，没有待处理的输出。"
        backend = self._instances[debugger_id]
        result = backend.get_pending_output_safe()
        return self._format_output(debugger_id, result)

    def __getattr__(self, name):
        """将所有其他方法代理到主调试器后端（#0），保持向后兼容"""
        if 0 not in self._instances:
            raise RuntimeError("调试会话未启动。请先调用 start() 启动调试会话。")
        return getattr(self._instances[0], name)



