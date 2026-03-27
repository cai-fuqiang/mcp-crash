package logger

import (
	"os"
	"path/filepath"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

var globalLogger *zap.Logger

func Init(logFile string) (*zap.Logger, error) {
	encoderConfig := zap.NewProductionEncoderConfig()
	encoderConfig.TimeKey = "timestamp"
	encoderConfig.EncodeTime = zapcore.ISO8601TimeEncoder

	var cores []zapcore.Core

	if logFile != "" {
		dir := filepath.Dir(logFile)
		if err := os.MkdirAll(dir, 0755); err != nil {
			return nil, err
		}
		file, err := os.OpenFile(logFile, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
		if err != nil {
			return nil, err
		}
		fileCore := zapcore.NewCore(
			zapcore.NewJSONEncoder(encoderConfig),
			zapcore.AddSync(file),
			zapcore.InfoLevel,
		)
		cores = append(cores, fileCore)
	}

	stderrCore := zapcore.NewCore(
		zapcore.NewConsoleEncoder(encoderConfig),
		zapcore.AddSync(os.Stderr),
		zapcore.InfoLevel,
	)
	cores = append(cores, stderrCore)

	core := zapcore.NewTee(cores...)
	globalLogger = zap.New(core, zap.AddCaller())
	return globalLogger, nil
}

func L() *zap.Logger {
	if globalLogger == nil {
		return zap.NewNop()
	}
	return globalLogger
}
