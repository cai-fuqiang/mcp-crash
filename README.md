# Crash MCP Server

[English](README.en.md) | 中文

针对大模型直接调用 crash 时 **每次命令都需重新启动进程、重新加载 vmcore（耗时数分钟）** 的效率瓶颈，该服务通过 PTY 伪终端启动 crash 进程并保持会话常驻，使 crash 自身加载的符号表和内核数据结构持续驻留内存。大模型通过 MCP 协议提交命令，服务透传至后台进程并实时返回输出，规避进程重复创建和 vmcore 重复加载的开销，将响应延迟从分钟级降至秒级，支持多转储文件并发会话管理。

## 功能

- 多会话管理（同时分析多个 vmcore）
- crash 进程常驻，避免重复启动
- 命令超时保护（5分钟）
- 自动清理过期会话
- 完整日志记录

## 安装

```bash
make build                    # 编译
make install                  # 安装到 ~/.local/bin/
make install PREFIX=~/.local  # 安装到用户目录
sudo make install             # 安装到 /usr/local/bin
```

卸载：
```bash
sudo make uninstall
```

## 使用
### 示例
`opencode/claudecode` 配置好后，通过类似如下提示词创建 **crash会话**
```
请帮我初始化 crash 会话分析 vmcore：
- vmcore: /path/to/vmcore
- vmlinux: /path/to/vmlinux
- crash: /path/to/crash
```

### 客户端配置

**ClaudeCode:**
```json
{
  "mcpServers": {
    "crash": {
      "command": "/path/to/crash-mcp",
      "args": ["--log-file", "/tmp/crash-mcp.log"]
    }
  }
}
```

**opencode:**
```json
{
  "mcp": {
    "crash": {
      "type": "local",
      "command": ["/path/to/crash-mcp", "--log-file", "/tmp/crash-mcp.log"],
      "enabled": true
    }
  }
}
```

## MCP 工具

### 1. **init_crash** - 初始化会话
   ```json
   {"crash": "/usr/bin/crash", "vmlinux": "/path/vmlinux", "vmcore": "/path/vmcore"}
   ```
   返回：`{"session_id": "uuid"}`

  ![init](doc/images/init.png)

### 2. **execute** - 执行命令
   ```json
   {"session_id": "uuid", "command": "bt"}
   ```

  ![exec](doc/images/exec.png)

### 3. **list_sessions** - 列出所有会话
   ```json
   {}
   ```
   返回：会话列表数组，每个元素包含会话信息
   ```json
   [
     {
       "id": "uuid-string",
       "vmlinux": "/path/to/vmlinux",
       "vmcore": "/path/to/vmcore",
       "created": "2024-01-01T00:00:00Z",
       "last_used": "2024-01-01T00:00:00Z",
       "active": true
     }
   ]
   ```

  ![list](doc/images/sessions.png)

### 4. **close** - 关闭会话
   ```json
   {"session_id": "uuid"}
   ```

  ![close](doc/images/close.png)
