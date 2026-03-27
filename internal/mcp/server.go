package mcp

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
	"go.uber.org/zap"

	"github.com/opencode/crash-mcp/internal/crash"
	"github.com/opencode/crash-mcp/internal/logger"
)

type Server struct {
	server  *server.MCPServer
	manager *crash.Manager
}

func NewServer() *Server {
	s := &Server{
		manager: crash.NewManager(),
	}

	s.server = server.NewMCPServer("crash-mcp", "1.0.0", server.WithLogging(), server.WithToolCapabilities(true))
	s.registerTools()
	return s
}

func (s *Server) registerTools() {
	// init_crash tool
	initCrashSchema := map[string]interface{}{
		"crash": map[string]interface{}{
			"type":        "string",
			"description": "crash 可执行文件路径",
			"default":     "crash",
		},
		"vmlinux": map[string]interface{}{
			"type":        "string",
			"description": "vmlinux 文件路径",
		},
		"vmcore": map[string]interface{}{
			"type":        "string",
			"description": "vmcore 文件路径",
		},
	}
	s.server.AddTool(
		mcp.NewTool("init_crash", "初始化 crash 会话，加载 vmlinux 和 vmcore", initCrashSchema),
		s.handleInitCrash,
	)

	// execute tool
	executeSchema := map[string]interface{}{
		"session_id": map[string]interface{}{
			"type":        "string",
			"description": "会话 ID",
		},
		"command": map[string]interface{}{
			"type":        "string",
			"description": "要执行的 crash 命令",
		},
	}
	s.server.AddTool(
		mcp.NewTool("execute", "执行 crash 命令", executeSchema),
		s.handleExecute,
	)

	// close tool
	closeSchema := map[string]interface{}{
		"session_id": map[string]interface{}{
			"type":        "string",
			"description": "会话 ID",
		},
	}
	s.server.AddTool(
		mcp.NewTool("close", "关闭指定会话", closeSchema),
		s.handleClose,
	)

	// list_sessions tool
	listSessionsSchema := map[string]interface{}{}
	s.server.AddTool(
		mcp.NewTool("list_sessions", "列出所有活动会话", listSessionsSchema),
		s.handleListSessions,
	)
}

func (s *Server) handleInitCrash(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	start := time.Now()
	crashBin, _ := arguments["crash"].(string)
	vmlinux, _ := arguments["vmlinux"].(string)
	vmcore, _ := arguments["vmcore"].(string)

	logger.L().Info("handle init_crash request",
		zap.String("crash", crashBin),
		zap.String("vmlinux", vmlinux),
		zap.String("vmcore", vmcore))

	if vmlinux == "" || vmcore == "" {
		logger.L().Error("init_crash failed: missing required parameters")
		return nil, fmt.Errorf("vmlinux and vmcore are required")
	}

	session, err := s.manager.CreateSession(crashBin, vmlinux, vmcore)
	if err != nil {
		logger.L().Error("init_crash failed",
			zap.Error(err),
			zap.Duration("duration", time.Since(start)))
		return &mcp.CallToolResult{
			Content: []interface{}{mcp.NewTextContent(fmt.Sprintf("failed to create session: %v", err))},
			IsError: true,
		}, nil
	}

	logger.L().Info("init_crash success",
		zap.String("session_id", session.ID),
		zap.Duration("duration", time.Since(start)))

	result := map[string]string{"session_id": session.ID}
	resultJSON, _ := json.Marshal(result)
	return &mcp.CallToolResult{
		Content: []interface{}{mcp.NewTextContent(string(resultJSON))},
	}, nil
}

func (s *Server) handleExecute(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	start := time.Now()
	sessionID, _ := arguments["session_id"].(string)
	command, _ := arguments["command"].(string)

	logger.L().Info("handle execute request",
		zap.String("session_id", sessionID),
		zap.String("command", command))

	if sessionID == "" || command == "" {
		logger.L().Error("execute failed: missing required parameters")
		return nil, fmt.Errorf("session_id and command are required")
	}

	session, err := s.manager.GetSession(sessionID)
	if err != nil {
		logger.L().Error("execute failed: session not found",
			zap.String("session_id", sessionID),
			zap.Error(err),
			zap.Duration("duration", time.Since(start)))
		return &mcp.CallToolResult{
			Content: []interface{}{mcp.NewTextContent(fmt.Sprintf("session error: %v", err))},
			IsError: true,
		}, nil
	}

	output, err := session.Execute(command, s.manager.GetTimeout())
	if err != nil {
		logger.L().Error("execute failed",
			zap.String("session_id", sessionID),
			zap.String("command", command),
			zap.Error(err),
			zap.Int("output_len", len(output)),
			zap.Duration("duration", time.Since(start)))
		return &mcp.CallToolResult{
			Content: []interface{}{mcp.NewTextContent(fmt.Sprintf("ERROR: %v\n\nOutput:\n%s", err, output))},
		}, nil
	}

	logger.L().Info("execute success",
		zap.String("session_id", sessionID),
		zap.String("command", command),
		zap.Int("output_len", len(output)),
		zap.Duration("duration", time.Since(start)))

	return &mcp.CallToolResult{
		Content: []interface{}{mcp.NewTextContent(output)},
	}, nil
}

func (s *Server) handleClose(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	start := time.Now()
	sessionID, _ := arguments["session_id"].(string)

	logger.L().Info("handle close request",
		zap.String("session_id", sessionID))

	if sessionID == "" {
		logger.L().Error("close failed: missing session_id")
		return nil, fmt.Errorf("session_id is required")
	}

	if err := s.manager.CloseSession(sessionID); err != nil {
		logger.L().Error("close failed",
			zap.String("session_id", sessionID),
			zap.Error(err),
			zap.Duration("duration", time.Since(start)))
		return &mcp.CallToolResult{
			Content: []interface{}{mcp.NewTextContent(fmt.Sprintf("failed to close session: %v", err))},
			IsError: true,
		}, nil
	}

	logger.L().Info("close success",
		zap.String("session_id", sessionID),
		zap.Duration("duration", time.Since(start)))

	return &mcp.CallToolResult{
		Content: []interface{}{mcp.NewTextContent(fmt.Sprintf("Session %s closed", sessionID))},
	}, nil
}

func (s *Server) handleListSessions(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	start := time.Now()
	logger.L().Info("handle list_sessions request")

	sessions := s.manager.ListSessions()
	sessionsJSON, _ := json.Marshal(sessions)

	logger.L().Info("list_sessions success",
		zap.Int("session_count", len(sessions)),
		zap.Duration("duration", time.Since(start)))

	return &mcp.CallToolResult{
		Content: []interface{}{mcp.NewTextContent(string(sessionsJSON))},
	}, nil
}

func (s *Server) ServeStdio() error {
	logger.L().Info("starting stdio server")
	return server.ServeStdio(s.server)
}

func (s *Server) Shutdown() {
	logger.L().Info("shutting down server")
	s.manager.CloseAll()
}
