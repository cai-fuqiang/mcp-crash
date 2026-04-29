#!/usr/bin/env python3
"""
crash-mcp - MCP Server for crash analysis

通过SSH连接到远端机器，利用SSH多跳能力，远端运行轻量级agent。
本地只需要Python 3.8+和SSH（无需Go）。

Usage:
    python3 crash_mcp.py --ssh user@loongson --remote-port 7890
"""

import json
import sys
import uuid
import socket
import subprocess
import threading
import time
import os
from typing import Optional, Dict, Any, List
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


class SSHClient:
    """通过SSH与远程crash-agent通信"""
    
    def __init__(self, ssh_host: str, ssh_port: int = 22, timeout: int = 300):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.timeout = timeout
        
    def _run_ssh(self, command: str, input_data: str = None) -> tuple:
        """执行远程SSH命令"""
        # SSH命令 - 利用SSH config的多跳配置
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", f"ServerAliveInterval=30",
            "-o", f"ServerAliveCountMax=3",
            "-p", str(self.ssh_port),
        ]
        
        # 如果SSH_HOST包含@，直接作为user@host
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
                timeout=self.timeout
            )
            return result.stdout.decode('utf-8', errors='replace'), result.stderr.decode('utf-8', errors='replace'), result.returncode
        except subprocess.TimeoutExpired:
            return "", "SSH command timeout", -1
        except Exception as e:
            return "", str(e), -1
            
    def init_session(self, vmlinux: str, vmcore: str) -> tuple:
        """通过SSH初始化crash会话"""
        # 通过SSH执行远程命令，让远程agent创建会话
        req_id = uuid.uuid4().hex[:8].upper()
        command = f"crash-agent --init '{vmlinux}' '{vmcore}'"
        
        output, err, code = self._run_ssh(command)
        if code != 0:
            return None, f"SSH error: {err}"
        
        # 解析输出: session_id|vmlinux|vmcore
        output = output.strip()
        parts = output.split("|")
        if len(parts) < 3:
            return None, f"Invalid response: {output}"
        
        session_id = parts[0]
        
        session = RemoteSession(
            id=session_id,
            vmlinux=vmlinux,
            vmcore=vmcore
        )
        return session, None
        
    def exec_command(self, session_id: str, command: str) -> tuple:
        """通过SSH执行crash命令"""
        # 需要转义命令中的特殊字符
        escaped_cmd = command.replace("'", "'\"'\"'")
        ssh_cmd = f"crash-agent --exec '{session_id}' '{escaped_cmd}'"
        
        output, err, code = self._run_ssh(ssh_cmd)
        if code != 0:
            return "", f"SSH error: {err}"
        
        return output.rstrip("\n"), None
        
    def close_session(self, session_id: str) -> tuple:
        """通过SSH关闭会话"""
        ssh_cmd = f"crash-agent --close '{session_id}'"
        output, err, code = self._run_ssh(ssh_cmd)
        if code != 0:
            return f"SSH error: {err}"
        return None
        
    def list_sessions(self) -> tuple:
        """通过SSH列出所有会话"""
        ssh_cmd = "crash-agent --list"
        output, err, code = self._run_ssh(ssh_cmd)
        if code != 0:
            return [], f"SSH error: {err}"
        
        # 解析: count|id1|vmlinux1|vmcore1|...
        parts = output.strip().split("|")
        if len(parts) < 1:
            return [], None
            
        count = int(parts[0])
        sessions = []
        for i in range(count):
            idx = 1 + i * 3
            if idx + 2 < len(parts):
                sessions.append({
                    "id": parts[idx],
                    "vmlinux": parts[idx + 1],
                    "vmcore": parts[idx + 2]
                })
        return sessions, None


class CrashMCP:
    """Crash MCP Server - 通过stdio与Claude通信"""
    
    def __init__(self, ssh_host: str, ssh_port: int = 22):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.client = SSHClient(ssh_host, ssh_port)
        self.sessions: Dict[str, RemoteSession] = {}
        
    def _create_session(self, vmlinux: str, vmcore: str) -> tuple:
        """创建会话"""
        session, err = self.client.init_session(vmlinux, vmcore)
        if err:
            return None, err
        self.sessions[session.id] = session
        return session, None
        
    def _execute_command(self, session_id: str, command: str) -> tuple:
        """执行命令"""
        session = self.sessions.get(session_id)
        if not session:
            return "", f"session not found: {session_id}"
            
        output, err = self.client.exec_command(session_id, command)
        if err:
            return output, err
            
        session.update_activity()
        return output, None
        
    def _close_session(self, session_id: str) -> tuple:
        """关闭会话"""
        session = self.sessions.pop(session_id, None)
        if not session:
            return f"session not found: {session_id}"
            
        err = self.client.close_session(session_id)
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
                "active": True
            })
        return result
        
    def handle_request(self, method: str, params: Dict) -> Dict:
        """处理MCP请求"""
        
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "crash-mcp", "version": "1.0.0"}
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
                                "vmcore": {"type": "string", "description": "vmcore 文件路径"}
                            },
                            "required": ["vmlinux", "vmcore"]
                        }
                    },
                    {
                        "name": "execute",
                        "description": "执行 crash 命令",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "会话 ID"},
                                "command": {"type": "string", "description": "要执行的 crash 命令"}
                            },
                            "required": ["session_id", "command"]
                        }
                    },
                    {
                        "name": "close",
                        "description": "关闭指定会话",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string", "description": "会话 ID"}
                            },
                            "required": ["session_id"]
                        }
                    },
                    {
                        "name": "list_sessions",
                        "description": "列出所有活动会话",
                        "inputSchema": {"type": "object", "properties": {}}
                    }
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
                    return {"content": [{"type": "text", "text": f"ERROR: {err}\n\n{output}"}], "isError": True}
                    
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
    
    parser = argparse.ArgumentParser(description="crash-mcp via SSH")
    parser.add_argument("--ssh", type=str, help="SSH target (user@hostname)")
    parser.add_argument("--host", type=str, help="SSH host")
    parser.add_argument("--port", "-p", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--user", "-u", type=str, help="SSH user")
    
    args = parser.parse_args()
    
    # 组合ssh目标
    if args.ssh:
        ssh_target = args.ssh
    elif args.host:
        ssh_target = f"{args.user + '@' if args.user else ''}{args.host}"
    else:
        print("Error: --ssh or --host required", file=sys.stderr)
        return 1
    
    server = CrashMCP(ssh_target, args.port)
    server.run()


if __name__ == "__main__":
    main()