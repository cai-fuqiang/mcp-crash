package crash

import (
	"bufio"
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"sync"
	"time"

	"github.com/creack/pty"
	"github.com/google/uuid"
	"go.uber.org/zap"

	"github.com/opencode/crash-mcp/internal/logger"
)

type Session struct {
	ID       string
	Crash    string
	Vmlinux  string
	Vmcore   string
	cmd      *exec.Cmd
	pty      *os.File
	mu       sync.RWMutex
	created  time.Time
	lastUsed time.Time
	active   bool
}

func NewSession(crash, vmlinux, vmcore string) (*Session, error) {
	start := time.Now()

	if crash == "" {
		crash = "crash"
	}

	session := &Session{
		ID:       uuid.New().String(),
		Crash:    crash,
		Vmlinux:  vmlinux,
		Vmcore:   vmcore,
		created:  time.Now(),
		lastUsed: time.Now(),
		active:   false,
	}

	cmd := exec.Command(crash, vmlinux, vmcore)
	ptmx, err := pty.Start(cmd)
	if err != nil {
		return nil, fmt.Errorf("failed to start crash: %w", err)
	}

	session.cmd = cmd
	session.pty = ptmx
	session.active = true

	logger.L().Info("[NewSession] crash process started",
		zap.String("session_id", session.ID),
		zap.Duration("startup_time", time.Since(start)))

	// 等待初始提示符
	if err := session.waitForPrompt(60 * time.Second); err != nil {
		session.Close()
		return nil, fmt.Errorf("failed to initialize crash: %w", err)
	}

	// 禁用分页器
	logger.L().Info("[NewSession] disabling pager", zap.String("session_id", session.ID))
	if _, err := session.pty.WriteString("set scroll off\n"); err != nil {
		logger.L().Warn("[NewSession] failed to send scroll off", zap.Error(err))
	} else {
		time.Sleep(100 * time.Millisecond)
		if err := session.waitForPrompt(10 * time.Second); err != nil {
			logger.L().Warn("[NewSession] timeout waiting for scroll off", zap.Error(err))
		} else {
			logger.L().Info("[NewSession] pager disabled", zap.String("session_id", session.ID))
		}
	}

	logger.L().Info("[NewSession] session ready",
		zap.String("session_id", session.ID),
		zap.Duration("total_time", time.Since(start)))

	return session, nil
}

func (s *Session) Execute(command string, timeout time.Duration) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if !s.active {
		return "", fmt.Errorf("session is not active")
	}

	s.lastUsed = time.Now()

	if _, err := s.pty.WriteString(command + "\n"); err != nil {
		return "", fmt.Errorf("failed to write command: %w", err)
	}

	return s.readOutput(timeout)
}

func (s *Session) readOutput(timeout time.Duration) (string, error) {
	reader := bufio.NewReaderSize(s.pty, 1024*1024) // 1MB 缓冲区
	deadline := time.Now().Add(timeout)
	var output bytes.Buffer

	for {
		if time.Now().After(deadline) {
			return output.String(), fmt.Errorf("timeout after %v", timeout)
		}

		// 设置读取超时
		s.pty.SetReadDeadline(time.Now().Add(500 * time.Millisecond))

		// 读取数据块（高效）
		buf := make([]byte, 65536) // 64KB 每次读取
		n, err := reader.Read(buf)

		s.pty.SetReadDeadline(time.Time{})

		if err != nil {
			if os.IsTimeout(err) {
				// 超时但没有新数据，继续等待
				continue
			}
			return output.String(), fmt.Errorf("read error: %w", err)
		}

		if n == 0 {
			continue
		}

		// 检查数据块末尾是否包含提示符
		data := buf[:n]

		// 快速检查：最后6个字符是否是 "crash>"
		if n >= 6 && string(data[n-6:]) == "crash>" {
			// 找到了提示符，写入提示符前的内容
			if n > 6 {
				output.Write(data[:n-6])
			}
			return output.String(), nil
		}

		// 检查数据块中间是否包含 "crash>\n" 或 "crash>"
		if idx := bytes.Index(data, []byte("crash>")); idx != -1 {
			// 找到了提示符
			output.Write(data[:idx])
			return output.String(), nil
		}

		// 没有找到提示符，全部写入输出
		output.Write(data)
	}
}

func (s *Session) waitForPrompt(timeout time.Duration) error {
	_, err := s.readOutput(timeout)
	return err
}

func (s *Session) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if !s.active {
		return nil
	}

	s.active = false
	s.pty.WriteString("quit\n")
	time.Sleep(100 * time.Millisecond)

	if s.pty != nil {
		s.pty.Close()
	}

	if s.cmd != nil && s.cmd.Process != nil {
		s.cmd.Process.Kill()
		s.cmd.Wait()
	}

	logger.L().Info("[Close] session closed", zap.String("session_id", s.ID))
	return nil
}

func (s *Session) IsActive() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.active
}

func (s *Session) GetLastUsed() time.Time {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.lastUsed
}
