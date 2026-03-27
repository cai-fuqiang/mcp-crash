package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/opencode/crash-mcp/internal/logger"
	"github.com/opencode/crash-mcp/internal/mcp"
)

func main() {
	var logFile string
	flag.StringVar(&logFile, "log-file", "", "日志文件路径")
	flag.Parse()

	if _, err := logger.Init(logFile); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to init logger: %v\n", err)
		os.Exit(1)
	}

	server := mcp.NewServer()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigCh
		server.Shutdown()
		os.Exit(0)
	}()

	if err := server.ServeStdio(); err != nil {
		fmt.Fprintf(os.Stderr, "Server error: %v\n", err)
		os.Exit(1)
	}
}
