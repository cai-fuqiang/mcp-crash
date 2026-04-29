# crash-mcp

通过SSH连接远端机器，运行轻量级crash-agent进行内核崩溃分析。本地只需Python 3.8+，无需Go。

## 架构

```
┌─────────────────┐     SSH (利用多跳配置)      ┌─────────────────┐
│   Claude CLI    │ ─────────────────────────▶ │   远端机器      │
│   + Python MCP │ ◀───────────────────────── │   crash-agent   │
└─────────────────┘                            └─────────────────┘
```

远端只需要: gcc编译的crash-agent (52KB) + crash工具

## 快速开始

### 1. 远端部署 (龙芯等)

```bash
# 编译
gcc -o crash-agent crash-agent.c
```

### 2. 配置Claude Desktop

编辑 `~/.config/claude-desktop/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "crash": {
      "command": "python3",
      "args": [
        "/path/to/crash_mcp.py",
        "--ssh",
        "user@loongson"
      ]
    }
  }
}
```

重启Claude Desktop后即可使用。

### 3. SSH多跳支持

利用已有的SSH配置：

```
# ~/.ssh/config
Host loongson
    HostName 192.168.x.x
    ProxyJump bastion1, bastion2
```

配置中直接使用别名：
```json
{
  "mcpServers": {
    "crash": {
      "command": "python3",
      "args": ["/path/to/crash_mcp.py", "--ssh", "loongson"]
    }
  }
}
```

## Agent命令行

```bash
./crash-agent --init /path/vmlinux /path/vmcore   # 创建会话
./crash-agent --exec SESSION_ID "bt"              # 执行命令
./crash-agent --close SESSION_ID                   # 关闭会话
./crash-agent --list                              # 列出会话
```

## MCP工具

| 工具 | 说明 |
|------|------|
| `init_crash` | 初始化crash会话，参数: vmlinux, vmcore |
| `execute` | 执行crash命令，参数: session_id, command |
| `close` | 关闭会话，参数: session_id |
| `list_sessions` | 列出所有会话 |

## 使用示例

```
# 初始化会话
MCP工具 init_crash:
  - vmlinux: /boot/vmlinux-5.15.0
  - vmcore: /var/crash/vmcore
返回: {"session_id": "abc123..."}

# 执行命令
MCP工具 execute:
  - session_id: abc123...
  - command: bt

# 关闭会话
MCP工具 close:
  - session_id: abc123...
```