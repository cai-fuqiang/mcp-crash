package crash

import (
	"fmt"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/opencode/crash-mcp/internal/logger"
)

type Manager struct {
	sessions map[string]*Session
	mu       sync.RWMutex
	timeout  time.Duration
}

func NewManager() *Manager {
	m := &Manager{
		sessions: make(map[string]*Session),
		timeout:  5 * time.Minute,
	}
	go m.cleanupLoop()
	return m
}

func (m *Manager) CreateSession(crash, vmlinux, vmcore string) (*Session, error) {
	session, err := NewSession(crash, vmlinux, vmcore)
	if err != nil {
		return nil, err
	}

	m.mu.Lock()
	m.sessions[session.ID] = session
	m.mu.Unlock()

	logger.L().Info("session added", zap.String("session_id", session.ID))
	return session, nil
}

func (m *Manager) GetSession(id string) (*Session, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	session, ok := m.sessions[id]
	if !ok {
		return nil, fmt.Errorf("session not found: %s", id)
	}
	if !session.IsActive() {
		return nil, fmt.Errorf("session is not active: %s", id)
	}
	return session, nil
}

func (m *Manager) CloseSession(id string) error {
	m.mu.Lock()
	session, ok := m.sessions[id]
	if ok {
		delete(m.sessions, id)
	}
	m.mu.Unlock()

	if !ok {
		return fmt.Errorf("session not found: %s", id)
	}
	return session.Close()
}

type SessionInfo struct {
	ID       string    `json:"id"`
	Vmlinux  string    `json:"vmlinux"`
	Vmcore   string    `json:"vmcore"`
	Created  time.Time `json:"created"`
	LastUsed time.Time `json:"last_used"`
	Active   bool      `json:"active"`
}

func (m *Manager) ListSessions() []*SessionInfo {
	m.mu.RLock()
	defer m.mu.RUnlock()

	infos := make([]*SessionInfo, 0, len(m.sessions))
	for _, s := range m.sessions {
		infos = append(infos, &SessionInfo{
			ID:       s.ID,
			Vmlinux:  s.Vmlinux,
			Vmcore:   s.Vmcore,
			Created:  s.created,
			LastUsed: s.GetLastUsed(),
			Active:   s.IsActive(),
		})
	}
	return infos
}

func (m *Manager) cleanupLoop() {
	ticker := time.NewTicker(1 * time.Minute)
	defer ticker.Stop()

	for range ticker.C {
		m.cleanup()
	}
}

func (m *Manager) cleanup() {
	m.mu.Lock()
	defer m.mu.Unlock()

	now := time.Now()
	for id, session := range m.sessions {
		if !session.IsActive() || now.Sub(session.GetLastUsed()) > m.timeout {
			logger.L().Info("cleaning up session", zap.String("session_id", id))
			delete(m.sessions, id)
			go session.Close()
		}
	}
}

func (m *Manager) GetTimeout() time.Duration {
	return m.timeout
}

func (m *Manager) CloseAll() {
	m.mu.Lock()
	sessions := make([]*Session, 0, len(m.sessions))
	for _, s := range m.sessions {
		sessions = append(sessions, s)
	}
	m.sessions = make(map[string]*Session)
	m.mu.Unlock()

	for _, s := range sessions {
		s.Close()
	}
}
