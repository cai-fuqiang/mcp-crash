#!/usr/bin/env python3
"""
crash-mcp - MCP Server for crash analysis

通过SSH多跳连接到远端机器，远端运行 crash-agent --server（持久化进程），
本地通过 SSH + Python socket 连接远端 crash-agent server 执行命令。

Usage:
    python3 crash_mcp.py --ssh p_loongarch --server-port 7890
"""

import json
import sys
import uuid
import subprocess
import time
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RemoteSession:
    """远程crash会话"""
    id: str
    vmlinux: str
    vmcore: str
    created: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)

    def update_activity(self):
        self.last_used = datetime.now()


class RemoteAgent:
    """通过SSH管理远端 crash-agent --server，用 Python socket 通信"""

    def __init__(self, ssh_host: str, ssh_port: int = 22,
                 server_port: int = 7890, timeout: int = 300):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.server_port = server_port
        self.timeout = timeout
        self._server_started = False

    def _run_ssh(self, command: str, input_data: str = None) -> Tuple[str, str, int]:
        """执行远程SSH命令，返回 (stdout, stderr, returncode)"""
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.ssh_port),
        ]

        if "@" in self.ssh_host:
            ssh_cmd.append(self.ssh_host)
        else:
            ssh_cmd.append(self.ssh_host)

        ssh_cmd.append(command)

        try:
            result = subprocess.run(
                ssh_cmd,
                input=input_data.encode() if input_data else None,
                capture_output=True,
                timeout=self.timeout,
            )
            return (
                result.stdout.decode("utf-8", errors="replace"),
                result.stderr.decode("utf-8", errors="replace"),
                result.returncode,
            )
        except subprocess.TimeoutExpired:
            return "", "SSH command timeout", -1
        except Exception as e:
            return "", str(e), -1

    def start_server(self) -> Optional[str]:
        """在远端启动 crash-agent --server（如未运行）"""
        # 用端口检测替代 pgrep，避免匹配到 nohup 包装进程
        out, _, code = self._run_ssh(
            f"ss -tlnp | grep -q ':{self.server_port}' && echo 'LISTENING' || echo 'NOT'"
        )
        if "LISTENING" in out:
            self._server_started = True
            print(f"crash-agent server already listening on port {self.server_port}",
                  file=sys.stderr)
            return None

        # 清理可能残留的旧进程
        self._run_ssh(f"pkill -f 'crash-agent --server' || true")
        time.sleep(0.5)

        # 启动 server
        _, err, code = self._run_ssh(
            f"nohup crash-agent --server --port {self.server_port} "
            f"> /tmp/crash-agent.log 2>&1 &"
        )
        time.sleep(1)

        # 验证端口已监听
        out, _, _ = self._run_ssh(
            f"ss -tlnp | grep -q ':{self.server_port}' && echo 'LISTENING' || echo 'NOT'"
        )
        if "LISTENING" not in out:
            return f"Failed to start crash-agent server on port {self.server_port}: {err}"

        self._server_started = True
        print(f"crash-agent server started on port {self.server_port}", file=sys.stderr)
        return None

    def _send_protocol(self, req_type: str, payload: str) -> Tuple[Optional[str], Optional[str]]:
        """
        通过 SSH + nc 连接远端 crash-agent server，
        发送协议消息，返回 (output, error)。
        """
        req_id = uuid.uuid4().hex[:8].upper()
        message = f"{req_id}|{req_type}|{payload}"

        # 转义单引号，用单引号包裹消息
        escaped = message.replace("'", "'\"'\"'")
        exec_timeout = self.timeout

        # echo 消息通过管道传给 nc，nc 连接 crash-agent server
        cmd = (
            f"echo '{escaped}' | "
            f"timeout 120 nc localhost {self.server_port}"
        )

        out, err, code = self._run_ssh(cmd)

        # nc -w 收到响应后可能被 timeout 杀掉（exit 124），
        out = out.strip()
        if not out:
            return None, f"SSH error: code={code}, err={err}"

        # 解析响应: req_id|status|output
        parts = out.split("|", 2)
        if len(parts) < 3:
            return None, f"Invalid response: {out}"

        resp_id, status_str, output = parts
        status = int(status_str)
        if status != 0:
            return None, f"Error {status}: {output}"

        return output, None

    def init_session(self, vmlinux: str, vmcore: str) -> Tuple[Optional[RemoteSession], Optional[str]]:
        """初始化 crash 会话"""
        output, err = self._send_protocol("init", f"{vmlinux}|{vmcore}")
        if err:
            return None, err

        # 解析: vmlinux|vmcore|session_id
        parts = output.split("|")
        if len(parts) < 3:
            return None, f"Invalid init response: {output}"

        session = RemoteSession(
            id=parts[2],
            vmlinux=parts[0],
            vmcore=parts[1],
        )
        return session, None

    def exec_command(self, session_id: str, command: str) -> Tuple[str, Optional[str]]:
        """执行 crash 命令"""
        output, err = self._send_protocol("exec", f"{session_id}|{command}")
        if err:
            return "", err
        return output.rstrip("\n"), None

    def close_session(self, session_id: str) -> Optional[str]:
        """关闭会话"""
        _, err = self._send_protocol("close", session_id)
        return err

    def list_sessions(self) -> Tuple[List[Dict], Optional[str]]:
        """列出所有会话"""
        output, err = self._send_protocol("list", "")
        if err:
            return [], err

        # 解析: count|id1|vmlinux1|vmcore1|...
        parts = output.split("|")
        if not parts:
            return [], None

        count = int(parts[0])
        sessions = []
        for i in range(count):
            idx = 1 + i * 3
            if idx + 2 < len(parts):
                sessions.append({
                    "id": parts[idx],
                    "vmlinux": parts[idx + 1],
                    "vmcore": parts[idx + 2],
                })
        return sessions, None


class CrashMCP:
    """Crash MCP Server - 通过stdio与Claude通信"""

    def __init__(self, ssh_host: str, ssh_port: int = 22, server_port: int = 7890):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.agent = RemoteAgent(ssh_host, ssh_port, server_port)
        self.sessions: Dict[str, RemoteSession] = {}

    def _ensure_server(self) -> Optional[str]:
        """确保远端 server 已启动"""
        return self.agent.start_server()

    def _create_session(self, vmlinux: str, vmcore: str) -> tuple:
        """创建会话"""
        err = self._ensure_server()
        if err:
            return None, err

        session, err = self.agent.init_session(vmlinux, vmcore)
        if err:
            return None, err
        self.sessions[session.id] = session
        return session, None

    def _execute_command(self, session_id: str, command: str) -> tuple:
        """执行命令"""
        session = self.sessions.get(session_id)
        if not session:
            return "", f"session not found: {session_id}"

        output, err = self.agent.exec_command(session_id, command)
        if err:
            return output, err

        session.update_activity()
        return output, None

    def _close_session(self, session_id: str) -> tuple:
        """关闭会话"""
        session = self.sessions.pop(session_id, None)
        if not session:
            return f"session not found: {session_id}"

        err = self.agent.close_session(session_id)
        if err:
            return err
        return None

    def _list_sessions(self) -> List[Dict]:
        """列出所有会话"""
        result = []
        for sid, s in self.sessions.items():
            result.append({
                "id": sid,
                "vmlinux": s.vmlinux,
                "vmcore": s.vmcore,
                "created": s.created.isoformat(),
                "last_used": s.last_used.isoformat(),
                "active": True,
            })
        return result

    def handle_request(self, method: str, params: Dict) -> Dict:
        """处理MCP请求"""

        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "crash-mcp", "version": "2.0.0"},
            }

        elif method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "init_crash",
                        "description": "初始化 crash 会话",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "vmlinux": {"type": "string", "description": "vmlinux 文件路径"},
                                "vmcore": {"type": "string", "description": "vmcore 文件路径"},
                            },
                            "required": ["vmlinux", "vmcore"],
                        },
                    },
                    {
                        "name": "execute",
                        "description": "执行 crash 命令",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "会话 ID"},
                                "command": {"type": "string", "description": "要执行的 crash 命令"},
                            },
                            "required": ["session_id", "command"],
                        },
                    },
                    {
                        "name": "close",
                        "description": "关闭指定会话",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "会话 ID"},
                            },
                            "required": ["session_id"],
                        },
                    },
                    {
                        "name": "list_sessions",
                        "description": "列出所有活动会话",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name == "init_crash":
                vmlinux = arguments.get("vmlinux", "")
                vmcore = arguments.get("vmcore", "")
                if not vmlinux or not vmcore:
                    return {"error": "vmlinux and vmcore are required"}

                session, err = self._create_session(vmlinux, vmcore)
                if err:
                    return {"error": err}

                return {"content": [{"type": "text", "text": json.dumps({"session_id": session.id})}]}

            elif tool_name == "execute":
                session_id = arguments.get("session_id", "")
                command = arguments.get("command", "")
                if not session_id or not command:
                    return {"error": "session_id and command are required"}

                output, err = self._execute_command(session_id, command)
                if err:
                    return {
                        "content": [{"type": "text", "text": f"ERROR: {err}\n\n{output}"}],
                        "isError": True,
                    }

                return {"content": [{"type": "text", "text": output}]}

            elif tool_name == "close":
                session_id = arguments.get("session_id", "")
                if not session_id:
                    return {"error": "session_id is required"}

                err = self._close_session(session_id)
                if err:
                    return {"error": err}

                return {"content": [{"type": "text", "text": f"Session {session_id} closed"}]}

            elif tool_name == "list_sessions":
                sessions = self._list_sessions()
                return {"content": [{"type": "text", "text": json.dumps(sessions, indent=2)}]}

            else:
                return {"error": f"unknown tool: {tool_name}"}

        else:
            return {"error": f"unknown method: {method}"}

    def run(self):
        """运行MCP Server"""
        print("crash-mcp server starting...", file=sys.stderr)
        print(f"SSH target: {self.ssh_host}:{self.ssh_port}", file=sys.stderr)

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue

                method = request.get("method", "")
                params = request.get("params", {})
                req_id = request.get("id")

                result = self.handle_request(method, params)

                if req_id is not None:
                    response = {"jsonrpc": "2.0", "id": req_id}
                    if "error" in result:
                        response["error"] = {"code": -32603, "message": result["error"]}
                    else:
                        response["result"] = result
                    print(json.dumps(response), flush=True)

            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="crash-mcp via SSH (TCP server mode)")
    parser.add_argument("--ssh", type=str, help="SSH target (user@hostname)")
    parser.add_argument("--host", type=str, help="SSH host")
    parser.add_argument("--port", "-p", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--user", "-u", type=str, help="SSH user")
    parser.add_argument("--server-port", type=int, default=7890,
                        help="crash-agent server port on remote (default: 7890)")

    args = parser.parse_args()

    # 组合ssh目标
    if args.ssh:
        ssh_target = args.ssh
    elif args.host:
        ssh_target = f"{args.user + '@' if args.user else ''}{args.host}"
    else:
        print("Error: --ssh or --host required", file=sys.stderr)
        return 1

    server = CrashMCP(ssh_target, args.port, args.server_port)
    server.run()


if __name__ == "__main__":
    main()
