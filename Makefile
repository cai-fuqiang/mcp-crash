PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin

.PHONY: build tidy clean run install uninstall

tidy:
	go mod tidy

build: tidy
	go build -o crash-mcp ./cmd/server

clean:
	rm -f crash-mcp

run: build
	./crash-mcp --log-file /tmp/crash-mcp.log

install: build
	install -Dm755 crash-mcp $(DESTDIR)$(BINDIR)/crash-mcp

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/crash-mcp
