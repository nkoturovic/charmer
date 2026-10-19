package executor

// Independent omission probes: only APIs that predate the admitted series, no
// patch-provided helpers.
// Executed by Python inside bwrap --unshare-net, never against operator state.
import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	claudeauth "github.com/router-for-me/CLIProxyAPI/v8/internal/auth/claude"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/runtime/executor/helps"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	coreexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/httptransport"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
)

type probeOmissionTransport func(*http.Request) (*http.Response, error)

func (f probeOmissionTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func probeOmissionResponse(r *http.Request, status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(body)), Request: r}
}
func probeOmissionCompat(t *testing.T, transport http.RoundTripper) error {
	t.Helper()
	ctx := context.WithValue(context.Background(), "cliproxy.roundtripper", transport)
	auth := &coreauth.Auth{ID: "omission-auth", Provider: "openai-compatibility", Attributes: map[string]string{"api_key": "dummy-omission", "base_url": "https://omission.invalid/v1"}}
	payload := []byte(`{"model":"omission-probe","messages":[{"role":"user","content":"hi"}],"prompt_cache_retention":"24h"}`)
	_, err := NewOpenAICompatExecutor("openai-compatibility", &config.Config{}).Execute(ctx, auth, coreexecutor.Request{Model: "omission-probe", Payload: payload}, coreexecutor.Options{SourceFormat: translator.FormatOpenAI, OriginalRequest: payload})
	return err
}
func TestOmissionCompatRetention(t *testing.T) {
	hits := 0
	err := probeOmissionCompat(t, probeOmissionTransport(func(r *http.Request) (*http.Response, error) {
		hits++
		body, _ := io.ReadAll(r.Body)
		if strings.Contains(string(body), `"prompt_cache_retention"`) {
			t.Error("retention escaped final compat boundary")
		}
		return probeOmissionResponse(r, 200, `{"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`), nil
	}))
	if err != nil || hits != 1 {
		t.Fatalf("request did not complete: hits=%d err=%v", hits, err)
	}
}
func TestOmissionKeyedSafety(t *testing.T) {
	hits := 0
	err := probeOmissionCompat(t, probeOmissionTransport(func(r *http.Request) (*http.Response, error) {
		hits++
		return probeOmissionResponse(r, 401, `{"error":{"message":"omission-secret-echo-canary"}}`), nil
	}))
	if hits != 1 || err == nil {
		t.Fatal("expected one failed upstream request")
	}
	if strings.Contains(err.Error(), "omission-secret-echo-canary") {
		t.Fatal("untrusted provider error escaped")
	}
}
func TestOmissionCredentialedRedirect(t *testing.T) {
	var hits atomic.Int32
	ctx := context.WithValue(context.Background(), "cliproxy.roundtripper", probeOmissionTransport(func(r *http.Request) (*http.Response, error) {
		hits.Add(1)
		if r.URL.Host == "omission.invalid" {
			reply := probeOmissionResponse(r, 302, "")
			reply.Header.Set("Location", "https://other.omission.invalid/escaped")
			return reply, nil
		}
		return probeOmissionResponse(r, 200, "escaped"), nil
	}))
	request, _ := http.NewRequest("GET", "https://omission.invalid/probe", nil)
	reply, err := NewClaudeExecutor(&config.Config{}).HttpRequest(ctx, &coreauth.Auth{Attributes: map[string]string{"api_key": "dummy-omission"}}, request)
	if reply != nil && reply.Body != nil {
		reply.Body.Close()
	}
	if err != nil || reply == nil || reply.StatusCode != 302 || reply.Header.Get("Location") != "" || hits.Load() != 1 {
		t.Fatalf("cross-origin redirect followed: hits=%d error=%t", hits.Load(), err != nil)
	}
}
func TestOmissionMetadataLock(t *testing.T) {
	auth := &coreauth.Auth{Metadata: map[string]any{"setup_token": true}}
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 4000; i++ {
			claudeauth.StoreMetadataValue(&auth.Metadata, "other", i)
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 4000; i++ {
			if !isClaudeSetupToken(auth, "sk-ant-oat-omission-probe") {
				t.Error("setup flag lost")
				return
			}
		}
	}()
	close(start)
	wg.Wait()
}

// D217 uses real response bodies and the shipped transport selection. A fresh
// test process owns its CA before crypto/x509 caches roots; no transport or
// global keepalive policy is changed to make the uTLS cases pass.
func d217FreshTLSProcess(t *testing.T) bool {
	t.Helper()
	if os.Getenv("GWTEST_D217_CHILD") == t.Name() {
		return false
	}
	root := t.TempDir()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(217), Subject: pkix.Name{CommonName: "D217 fixture"},
		DNSNames: []string{"chatgpt.com"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage:    x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true, IsCA: true}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certPath, keyPath := filepath.Join(root, "ca.pem"), filepath.Join(root, "key.pem")
	if err := os.WriteFile(certPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, os.Args[0], "-test.run=^"+t.Name()+"$", "-test.count=1", "-test.v", "-test.timeout=30s")
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "GWTEST_D217_") && !strings.HasPrefix(value, "SSL_CERT_FILE=") && !strings.HasPrefix(value, "SSL_CERT_DIR=") {
			command.Env = append(command.Env, value)
		}
	}
	command.Env = append(command.Env, "GWTEST_D217_CHILD="+t.Name(), "GWTEST_D217_CERT="+certPath,
		"GWTEST_D217_KEY="+keyPath, "SSL_CERT_FILE="+certPath, "SSL_CERT_DIR="+root)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("isolated fixture process failed: %v\n%s", err, output)
	}
	t.Logf("fixture process completed: %s", output)
	return true
}

type d217Body struct {
	io.ReadCloser
	reads, activeReads                atomic.Int32
	readBlocked, closeEntered, closed chan struct{}
	readOnce, closeOnce               sync.Once
	holdClose                         <-chan struct{}
	closeErr                          error
}

func (b *d217Body) Read(p []byte) (int, error) {
	b.activeReads.Add(1)
	defer b.activeReads.Add(-1)
	if b.reads.Add(1) >= 2 {
		b.readOnce.Do(func() { close(b.readBlocked) })
	}
	return b.ReadCloser.Read(p)
}
func (b *d217Body) Close() error {
	b.closeOnce.Do(func() {
		close(b.closeEntered)
		if b.holdClose != nil {
			<-b.holdClose
		}
		b.closeErr = b.ReadCloser.Close()
		close(b.closed)
	})
	return b.closeErr
}

type d217HTTPFixture struct {
	listener                           net.Listener
	kind, wire                         string
	status                             int
	release, headers, done, acceptDone chan struct{}
	releaseOnce, headerOnce, doneOnce  sync.Once
	mu                                 sync.Mutex
	handlers                           sync.WaitGroup
	callers                            []<-chan struct{}
	connections                        map[net.Conn]bool
	active, readers, senders, hits     atomic.Int32
	peerClosed                         atomic.Bool
	protocol                           atomic.Value
	errors                             chan error
	bodies                             chan *d217Body
	holdClose                          <-chan struct{}
	ordinary                           *http.Transport
}

func d217Wait(ctx context.Context, done <-chan struct{}) bool {
	select {
	case <-done:
		return true
	default:
	}
	select {
	case <-done:
		return true
	case <-ctx.Done():
		return false
	}
}
func d217Open(ch <-chan struct{}) bool {
	select {
	case <-ch:
		return false
	default:
		return true
	}
}
func (f *d217HTTPFixture) releaseTail() { f.releaseOnce.Do(func() { close(f.release) }) }
func (f *d217HTTPFixture) worker(work func()) <-chan struct{} {
	done := make(chan struct{})
	f.callers = append(f.callers, done) // registered only by the test goroutine
	go func() { defer close(done); work() }()
	return done
}
func (f *d217HTTPFixture) report(err error) {
	select {
	case f.errors <- err:
	default:
	}
}
func (f *d217HTTPFixture) snapshot(body *d217Body) [5]int32 {
	closed := int32(0)
	if d217Open(body.closed) {
		closed = 1
	}
	return [5]int32{body.activeReads.Load(), f.readers.Load(), f.senders.Load(), f.active.Load(), closed}
}
func d217Released(snapshot [5]int32) bool { return snapshot == [5]int32{} }

func newD217HTTPFixture(t *testing.T, kind, wire string, status int, holdClose <-chan struct{}) *d217HTTPFixture {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f := &d217HTTPFixture{listener: listener, kind: kind, wire: wire, status: status,
		release: make(chan struct{}), headers: make(chan struct{}), done: make(chan struct{}), acceptDone: make(chan struct{}),
		connections: make(map[net.Conn]bool), errors: make(chan error, 4), bodies: make(chan *d217Body, 4),
		holdClose: holdClose, ordinary: &http.Transport{Proxy: nil}}
	go func() {
		defer close(f.acceptDone)
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			f.mu.Lock()
			f.connections[conn] = true
			f.mu.Unlock()
			f.active.Add(1)
			f.senders.Add(1)
			f.handlers.Add(1)
			go f.serve(conn)
		}
	}()
	t.Cleanup(func() {
		// Only fallback cleanup closes these resources by force. Positive
		// release observations are asserted before this callback runs.
		f.releaseTail()
		listener.Close()
		f.mu.Lock()
		for conn := range f.connections {
			conn.Close()
		}
		f.mu.Unlock()
		f.ordinary.CloseIdleConnections()
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if !d217Wait(ctx, f.acceptDone) {
			t.Error("fixture acceptor did not join")
			return
		}
		joined := make(chan struct{})
		go func() { f.handlers.Wait(); close(joined) }()
		if !d217Wait(ctx, joined) {
			t.Error("fixture sender/peer readers did not join")
		}
		for _, done := range f.callers {
			if !d217Wait(ctx, done) {
				t.Error("fixture caller did not join")
			}
		}
	})
	return f
}

func (f *d217HTTPFixture) serve(raw net.Conn) {
	defer f.handlers.Done()
	conn := raw
	defer func() {
		conn.Close()
		f.mu.Lock()
		delete(f.connections, raw)
		f.mu.Unlock()
		f.active.Add(-1)
		f.senders.Add(-1)
		f.doneOnce.Do(func() { close(f.done) })
	}()
	_ = raw.SetDeadline(time.Now().Add(8 * time.Second))
	reader := bufio.NewReader(raw)
	if f.kind == "utls" {
		connect, err := http.ReadRequest(reader)
		if err != nil {
			f.report(err)
			return
		}
		if connect.Method != http.MethodConnect || connect.Host != "chatgpt.com:443" {
			f.report(errors.New("unexpected fixture CONNECT"))
			return
		}
		connect.Body.Close()
		if _, err := io.WriteString(raw, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
			f.report(err)
			return
		}
		certificate, err := tls.LoadX509KeyPair(os.Getenv("GWTEST_D217_CERT"), os.Getenv("GWTEST_D217_KEY"))
		if err != nil {
			f.report(err)
			return
		}
		tlsConn := tls.Server(raw, &tls.Config{Certificates: []tls.Certificate{certificate}, NextProtos: []string{"http/1.1"}, MinVersion: tls.VersionTLS12})
		if err := tlsConn.Handshake(); err != nil {
			f.report(err)
			return
		}
		if tlsConn.ConnectionState().NegotiatedProtocol != "http/1.1" {
			f.report(errors.New("fixture did not negotiate HTTP/1.1"))
			return
		}
		conn, reader = tlsConn, bufio.NewReader(tlsConn)
	}
	request, err := http.ReadRequest(reader)
	if err != nil {
		f.report(err)
		return
	}
	_, _ = io.Copy(io.Discard, request.Body)
	request.Body.Close()
	f.protocol.Store(request.Proto)
	f.hits.Add(1)
	prefix, tail, framing := "AB", "CD", ""
	contentType := "application/octet-stream"
	switch f.wire {
	case "truncated":
		framing = "Content-Length: 8\r\n"
	case "trailer":
		framing = "Transfer-Encoding: chunked\r\nTrailer: X-D217\r\n"
		prefix, tail = "2\r\nAB\r\n", "2\r\nCD\r\n0\r\nX-D217: done\r\n\r\n"
	case "slow-error":
		contentType, prefix, tail = "application/json", "{", `"error":"fixture"}`
	case "sse":
		contentType = "text/event-stream"
		prefix = "data: {\"id\":\"fixture\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"one\"},\"finish_reason\":null}]}\n\n" +
			"data: {\"id\":\"fixture\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"two\"},\"finish_reason\":null}]}\n\n"
		tail = "data: [DONE]\n\n"
	}
	// Each fixture is explicitly a one-request, close-delimited connection;
	// no global DisableKeepAlives setting or pool cleanup supplies the proof.
	if _, err := fmt.Fprintf(conn, "HTTP/1.1 %d %s\r\nContent-Type: %s\r\n%sConnection: close\r\n\r\n%s", f.status, http.StatusText(f.status), contentType, framing, prefix); err != nil {
		f.report(err)
		return
	}
	peerDone := make(chan struct{})
	f.readers.Add(1)
	go func() {
		defer close(peerDone)
		defer f.readers.Add(-1)
		var b [1]byte
		_, err := conn.Read(b[:])
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, syscall.ECONNRESET) {
			f.peerClosed.Store(true)
		}
	}()
	f.headerOnce.Do(func() { close(f.headers) })
	select {
	case <-f.release:
		_, _ = io.WriteString(conn, tail)
	case <-peerDone:
	}
	conn.Close()
	<-peerDone
}

func (f *d217HTTPFixture) url() string {
	if f.kind == "utls" {
		return "https://chatgpt.com/fixture"
	}
	return "http://" + f.listener.Addr().String() + "/fixture"
}
func (f *d217HTTPFixture) client(ctx context.Context, observe bool) *http.Client {
	cfg := &config.Config{}
	if f.kind == "utls" {
		cfg.ProxyURL = "http://" + f.listener.Addr().String()
	} else {
		ctx = context.WithValue(ctx, "cliproxy.roundtripper", http.RoundTripper(f.ordinary))
	}
	if observe {
		ctx = f.observe(ctx)
	}
	return helps.NewUtlsHTTPClient(ctx, cfg, nil, 0)
}
func (f *d217HTTPFixture) observe(ctx context.Context) context.Context {
	return httptransport.WithDecorator(ctx, func(next http.RoundTripper) http.RoundTripper {
		return probeOmissionTransport(func(request *http.Request) (*http.Response, error) {
			response, err := next.RoundTrip(request)
			if err == nil && response != nil {
				body := &d217Body{ReadCloser: response.Body, readBlocked: make(chan struct{}), closeEntered: make(chan struct{}), closed: make(chan struct{}), holdClose: f.holdClose}
				response.Body = body
				select {
				case f.bodies <- body:
				case <-request.Context().Done():
					response.Body.Close()
					return nil, request.Context().Err()
				}
			}
			return response, err
		})
	})
}
func (f *d217HTTPFixture) body(t *testing.T, ctx context.Context) *d217Body {
	t.Helper()
	select {
	case body := <-f.bodies:
		return body
	case <-ctx.Done():
		t.Fatal("selected transport did not expose its response body")
		return nil
	}
}
func (f *d217HTTPFixture) assertReleased(t *testing.T, ctx context.Context, body *d217Body) {
	t.Helper()
	if !d217Wait(ctx, body.closed) || !d217Wait(ctx, f.done) || !d217Released(f.snapshot(body)) {
		t.Fatalf("fixture resources retained before cleanup: %v", f.snapshot(body))
	}
	if f.hits.Load() != 1 {
		t.Fatalf("upstream hits=%d, want one", f.hits.Load())
	}
	if f.protocol.Load() != "HTTP/1.1" {
		t.Fatal("fixture did not exercise HTTP/1.1")
	}
	select {
	case err := <-f.errors:
		t.Fatalf("fixture error: %v", err)
	default:
	}
}

func TestGatewayResponseReadClose(t *testing.T) {
	if d217FreshTLSProcess(t) {
		return
	}
	for index := 0; index < 5; index++ {
		bad := [5]int32{}
		bad[index] = 1
		if d217Released(bad) {
			t.Fatal("accounting accepted a retained reader/sender/connection")
		}
	}
	for _, kind := range []string{"http1", "utls"} {
		for _, wire := range []string{"eof", "truncated", "trailer"} {
			t.Run(kind+"/"+wire, func(t *testing.T) {
				// First prove the wire variant, not merely a generic closed socket.
				f := newD217HTTPFixture(t, kind, wire, 200, nil)
				ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				request, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.url(), nil)
				response, err := f.client(ctx, true).Do(request)
				if err != nil {
					t.Fatal(err)
				}
				body := f.body(t, ctx)
				f.releaseTail()
				data, readErr := io.ReadAll(response.Body)
				if string(data) != "ABCD" {
					t.Fatal("fixture body content changed")
				}
				if wire == "truncated" {
					if !errors.Is(readErr, io.ErrUnexpectedEOF) {
						t.Fatalf("truncation=%v", readErr)
					}
				} else if readErr != nil {
					t.Fatal(readErr)
				}
				if wire == "trailer" && response.Trailer.Get("X-D217") != "done" {
					t.Fatal("chunked trailer missing")
				}
				response.Body.Close()
				f.assertReleased(t, ctx, body)
			})
			for _, retain := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/%s/concurrent-retain=%t", kind, wire, retain), func(t *testing.T) {
					hold := make(chan struct{})
					var once sync.Once
					releaseClose := func() { once.Do(func() { close(hold) }) }
					defer releaseClose()
					var gate <-chan struct{}
					if retain {
						gate = hold
					}
					f := newD217HTTPFixture(t, kind, wire, 200, gate)
					ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
					defer cancel()
					request, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.url(), nil)
					response, err := f.client(ctx, true).Do(request)
					if err != nil {
						t.Fatal(err)
					}
					body := f.body(t, ctx)
					readDone := f.worker(func() { _, _ = io.ReadAll(response.Body) })
					if !d217Wait(ctx, body.readBlocked) || !d217Wait(ctx, f.headers) {
						t.Fatal("reader did not reach the held body")
					}
					closeDone := f.worker(func() { _ = response.Body.Close() })
					if !d217Wait(ctx, body.closeEntered) {
						t.Fatal("close did not enter")
					}
					if retain {
						select {
						case <-readDone:
							t.Fatal("retained reader control exited")
						case <-time.After(30 * time.Millisecond):
						}
						if d217Released(f.snapshot(body)) || f.active.Load() != 1 || f.senders.Load() != 1 {
							t.Fatal("retained-resource control did not discriminate")
						}
						releaseClose()
					}
					if !d217Wait(ctx, readDone) || !d217Wait(ctx, closeDone) {
						t.Fatal("simultaneous Read/Close did not join")
					}
					f.assertReleased(t, ctx, body)
					if !f.peerClosed.Load() {
						t.Fatal("body Close did not close the fixture peer before tail release")
					}
				})
			}
		}
	}
}

func TestGatewaySlowNon2xxBodyRelease(t *testing.T) {
	if d217FreshTLSProcess(t) {
		return
	}
	for _, kind := range []string{"http1", "utls"} {
		for _, earlyClose := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/early-close=%t", kind, earlyClose), func(t *testing.T) {
				f := newD217HTTPFixture(t, kind, "slow-error", 400, nil)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				join, stop := context.WithTimeout(context.Background(), 3*time.Second)
				defer stop()
				if earlyClose {
					request, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.url(), nil)
					response, err := f.client(ctx, true).Do(request)
					if err != nil || response.StatusCode != 400 {
						t.Fatalf("non2xx response: %v", err)
					}
					body := f.body(t, join)
					if !d217Wait(join, f.headers) {
						t.Fatal("slow body did not start")
					}
					closed := f.worker(func() { response.Body.Close() })
					if !d217Wait(join, closed) {
						t.Fatal("early body Close blocked")
					}
					f.assertReleased(t, join, body)
					return
				}
				// Observe the actual executor's deferred Close around its bounded
				// non2xx reader; the selected real transport is not replaced.
				transport := f.client(context.Background(), false).Transport
				ctx = f.observe(context.WithValue(ctx, "cliproxy.roundtripper", transport))
				auth := &coreauth.Auth{ID: "d217-slow", Provider: "openai-compatibility", Attributes: map[string]string{"api_key": "dummy-d217", "base_url": f.url()}}
				done := make(chan error, 1)
				workerDone := f.worker(func() {
					_, err := NewOpenAICompatExecutor("openai-compatibility", &config.Config{}).Execute(ctx, auth,
						coreexecutor.Request{Model: "d217-fixture", Payload: []byte(`{"model":"d217-fixture","messages":[{"role":"user","content":"fixture"}]}`)},
						coreexecutor.Options{SourceFormat: translator.FormatOpenAI})
					done <- err
				})
				var body *d217Body
				select {
				case body = <-f.bodies:
				case <-join.Done():
					t.Fatal("executor did not receive non2xx headers")
				}
				if !d217Wait(join, body.readBlocked) {
					t.Fatal("executor did not block in the slow error body")
				}
				cancel()
				select {
				case err := <-done:
					if err == nil {
						t.Fatal("canceled slow non2xx returned success")
					}
				case <-join.Done():
					t.Fatal("slow error reader did not join after cancellation")
				}
				if !d217Wait(join, workerDone) {
					t.Fatal("executor caller did not join")
				}
				f.assertReleased(t, join, body)
			})
		}
	}
}

func TestGatewayAbandonedExecutorSSE(t *testing.T) {
	if d217FreshTLSProcess(t) {
		return
	}
	for _, kind := range []string{"http1", "utls"} {
		for _, retain := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/retain-sender=%t", kind, retain), func(t *testing.T) {
				hold := make(chan struct{})
				var once sync.Once
				release := func() { once.Do(func() { close(hold) }) }
				defer release()
				var gate <-chan struct{}
				if retain {
					gate = hold
				}
				f := newD217HTTPFixture(t, kind, "sse", 200, gate)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				join, stop := context.WithTimeout(context.Background(), 3*time.Second)
				defer stop()
				ctx = f.observe(context.WithValue(ctx, "cliproxy.roundtripper", f.client(context.Background(), false).Transport))
				auth := &coreauth.Auth{ID: "d217-sse", Provider: "openai-compatibility", Attributes: map[string]string{"api_key": "dummy-d217", "base_url": f.url()}}
				stream, err := NewOpenAICompatExecutor("openai-compatibility", &config.Config{}).ExecuteStream(ctx, auth,
					coreexecutor.Request{Model: "d217-fixture", Payload: []byte(`{"model":"d217-fixture","stream":true,"messages":[{"role":"user","content":"fixture"}]}`)},
					coreexecutor.Options{SourceFormat: translator.FormatOpenAI, Stream: true})
				if err != nil {
					t.Fatal(err)
				}
				body := f.body(t, join)
				if cap(stream.Chunks) != 0 {
					t.Fatal("reconcile abandonment proof for changed stream buffering")
				}
				select {
				case chunk, ok := <-stream.Chunks:
					if !ok || chunk.Err != nil || len(chunk.Payload) == 0 {
						t.Fatal("stream payload never arrived")
					}
				case <-join.Done():
					t.Fatal("stream setup timed out")
				}
				// Stop consuming, then cancel. Never drain a blocked sender into
				// appearing joined; the deferred body Close must occur first.
				cancel()
				if !d217Wait(join, body.closeEntered) {
					t.Fatal("abandoned executor sender retained its body")
				}
				if retain {
					select {
					case <-body.closed:
						t.Fatal("retained sender control exited")
					case <-time.After(30 * time.Millisecond):
					}
					if d217Released(f.snapshot(body)) {
						t.Fatal("retained sender was counted as released")
					}
					release()
				}
				f.assertReleased(t, join, body)
				select {
				case _, ok := <-stream.Chunks:
					if ok {
						t.Fatal("abandoned sender still delivered a payload")
					}
				case <-join.Done():
					t.Fatal("abandoned sender channel never closed")
				}
			})
		}
	}
}
