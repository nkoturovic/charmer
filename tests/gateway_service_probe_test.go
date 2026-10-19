package cliproxy

// The independent probe calls only APIs present before the admitted series.
import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/api"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	sdkaccess "github.com/router-for-me/CLIProxyAPI/v8/sdk/access"
	sdkauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/auth"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	log "github.com/sirupsen/logrus"
)

func TestOmissionCredentialReceipt(t *testing.T) {
	store := sdkauth.NewFileTokenStore()
	root := t.TempDir()
	store.SetBaseDir(root)
	manager := coreauth.NewManager(store, nil, nil)
	var buf bytes.Buffer
	oldOut, oldLevel := log.StandardLogger().Out, log.GetLevel()
	log.SetOutput(&buf)
	log.SetLevel(log.InfoLevel)
	t.Cleanup(func() { log.SetOutput(oldOut); log.SetLevel(oldLevel) })
	auth := &coreauth.Auth{ID: "omission-save.json", FileName: "omission-save.json", Provider: "claude", Metadata: map[string]any{"type": "claude", "access_token": "dummy-omission"}}
	registered, err := manager.Register(context.Background(), auth)
	if err != nil {
		t.Fatal(err)
	}
	registered.Metadata["access_token"] = "dummy-omission-new"
	if _, err = manager.Update(context.Background(), registered); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(filepath.Join(root, "omission-save.json")); err != nil {
		t.Fatal("credential file missing")
	}
	if !strings.Contains(buf.String(), "credential_save_v1 operation=update result=persisted") {
		t.Fatal("durable update receipt missing")
	}
}

func TestOmissionAuthSnapshot(t *testing.T) {
	original := &coreauth.Auth{Metadata: map[string]any{"nested": []any{map[string]any{"value": "before"}}}, Attributes: map[string]string{}}
	clone := original.Clone()
	clone.Attributes["new"] = "value"
	clone.Metadata["nested"].([]any)[0].(map[string]any)["value"] = "after"
	if len(original.Attributes) != 0 || original.Metadata["nested"].([]any)[0].(map[string]any)["value"] != "before" {
		t.Fatal("clone aliases caller-owned containers")
	}
}

// This probe deliberately uses only the original Store/ProviderExecutor and
// Service.Shutdown APIs. It compiles without the refresh-join patch or its tests.
type omissionShutdownExecutor struct {
	coreauth.ProviderExecutor
	entered   chan struct{}
	cancelled chan struct{}
	release   <-chan struct{}
}

func (*omissionShutdownExecutor) Identifier() string { return "omission-shutdown" }
func (e *omissionShutdownExecutor) Refresh(ctx context.Context, a *coreauth.Auth) (*coreauth.Auth, error) {
	close(e.entered)
	<-ctx.Done()
	close(e.cancelled)
	<-e.release // an already-started, cancellation-shielded refresh still succeeds
	a = a.Clone()
	a.Metadata["refresh_token"] = "omission-replacement-refresh"
	return a, nil
}

type omissionShutdownStore struct {
	coreauth.Store
	entered chan struct{}
	release <-chan struct{}
	saved   chan string
}

func (s *omissionShutdownStore) Save(_ context.Context, a *coreauth.Auth) (string, error) {
	close(s.entered)
	<-s.release
	token, _ := a.Metadata["refresh_token"].(string)
	s.saved <- token
	return "", nil
}

func omissionShutdownRelease() (chan struct{}, func()) {
	ch := make(chan struct{})
	var once sync.Once
	return ch, func() { once.Do(func() { close(ch) }) }
}

func TestOmissionRefreshShutdown(t *testing.T) {
	executorHold, releaseExecutor := omissionShutdownRelease()
	storeHold, releaseStore := omissionShutdownRelease()
	exec := &omissionShutdownExecutor{entered: make(chan struct{}), cancelled: make(chan struct{}), release: executorHold}
	store := &omissionShutdownStore{entered: make(chan struct{}), release: storeHold, saved: make(chan string, 1)}
	manager := coreauth.NewManager(store, nil, nil)
	manager.RegisterExecutor(exec)
	_, err := manager.Register(coreauth.WithSkipPersist(context.Background()), &coreauth.Auth{
		ID: "omission-shutdown", Provider: "omission-shutdown",
		Metadata: map[string]any{"refresh_token": "omission-old-refresh", "refresh_interval_seconds": 3600},
	})
	if err != nil {
		t.Fatal(err)
	}
	wait := func(ch <-chan struct{}) {
		t.Helper()
		select {
		case <-ch:
		case <-time.After(5 * time.Second):
			t.Fatal("shutdown probe did not reach its barrier")
		}
	}
	manager.StartAutoRefresh(context.Background(), time.Hour)
	defer manager.StopAutoRefresh()
	defer releaseStore()
	defer releaseExecutor()
	wait(exec.entered)
	service := &Service{coreManager: manager}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- service.Shutdown(ctx) }()
	wait(exec.cancelled)
	// Record early exits without abandoning the fixture: release and observe the
	// replacement save even in the patch-omitted, behaviourally red control.
	returned := false
	checkBlocked := func() {
		t.Helper()
		if returned {
			return
		}
		select {
		case err := <-done:
			returned = true
			t.Errorf("Shutdown returned before replacement credential save: %v", err)
		case <-time.After(20 * time.Millisecond):
		}
	}
	checkBlocked()
	releaseExecutor()
	wait(store.entered)
	checkBlocked()
	releaseStore()
	if !returned {
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("Shutdown did not finish after replacement save")
		}
	}
	select {
	case token := <-store.saved:
		if token != "omission-replacement-refresh" {
			t.Fatalf("saved token = %q, want replacement refresh token", token)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("replacement token was not saved")
	}
}

func TestOmissionOverlay(t *testing.T) {
	// Both loaders must refuse an unknown overlay field; pristine ignores it.
	data := []byte("oauth-extra-models:\n  claude:\n    - id: omission-overlay-probe\n      unexpected: true\n")
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := config.LoadConfig(path); err == nil {
		t.Fatal("invalid file overlay accepted")
	}
	if _, err := config.ParseConfigBytes(data); err == nil {
		t.Fatal("invalid payload overlay accepted")
	}
}

// The proactive Codex refresh lead, through the provider registry the
// gateway links: 96 h, so a credential 95 h from expiry is refreshed.
func TestOmissionCodexRefreshLead(t *testing.T) {
	lead := coreauth.ProviderRefreshLead("codex", nil)
	if lead == nil || *lead != 96*time.Hour {
		t.Fatalf("codex refresh lead = %v, want 96h", lead)
	}
	auth := &coreauth.Auth{ID: "omission-codex-lead", Provider: "codex", Metadata: map[string]any{
		"type": "codex", "refresh_token": "dummy-omission", "expired": time.Now().Add(95 * time.Hour).Format(time.RFC3339)}}
	if _, has, need := coreauth.CodexOuterNeedsRefresh(auth, time.Now()); !has || !need {
		t.Fatalf("codex credential 95h from expiry: has=%t need=%t, want a refresh", has, need)
	}
}

// D217 counts only actors and sockets this fixture owns, not a noisy global
// goroutine total. The existing durable-save/join probes remain separate.
type d217ServiceResources struct {
	watchers, handlers, readers, servers, connections atomic.Int32
}

func (r *d217ServiceResources) snapshot() [5]int32 {
	return [5]int32{r.watchers.Load(), r.handlers.Load(), r.readers.Load(), r.servers.Load(), r.connections.Load()}
}

func d217ServiceReleased(value [5]int32) bool { return value == [5]int32{} }

func d217ServiceJoin(ctx context.Context, done <-chan struct{}) bool {
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

func TestGatewayShutdownResources(t *testing.T) {
	for index := 0; index < 5; index++ {
		retained := [5]int32{}
		retained[index] = 1
		if d217ServiceReleased(retained) {
			t.Fatal("resource accounting accepted a retained fixture actor or connection")
		}
	}
	for _, retainWatcher := range []bool{false, true} {
		t.Run(fmt.Sprintf("retained-control=%t", retainWatcher), func(t *testing.T) {
			root := t.TempDir()
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			port := listener.Addr().(*net.TCPAddr).Port
			listener.Close()
			cfg, err := config.ParseConfigBytes([]byte(fmt.Sprintf("host: 127.0.0.1\nport: %d\nauth-dir: %q\napi-keys: [dummy-d217]\ndiscovery: {enabled: false}\nremote-management: {disable-control-panel: true}\n", port, root)))
			if err != nil {
				t.Fatal(err)
			}
			var resources d217ServiceResources
			watcherCtx, watcherCancel := context.WithCancel(context.Background())
			watcherDone, handlerDone, entered, readerDone := make(chan struct{}), make(chan struct{}), make(chan struct{}), make(chan struct{})
			release, releaseWatcher := omissionShutdownRelease()
			watcher := &WatcherWrapper{stop: func() error {
				if retainWatcher {
					return nil
				} // fault control: falsely claims it stopped
				select {
				case <-watcherDone:
					return nil
				case <-time.After(time.Second):
					return errors.New("fixture watcher did not stop")
				}
			}}
			manager := coreauth.NewManager(nil, nil, nil)
			server := api.NewServer(cfg, manager, sdkaccess.NewManager(), filepath.Join(root, "config.yaml"), api.WithMiddleware(func(c *gin.Context) {
				if c.Request.URL.Path != "/healthz" || c.GetHeader("X-D217-Hold") != "1" {
					return
				}
				resources.handlers.Add(1)
				defer close(handlerDone)
				defer resources.handlers.Add(-1)
				close(entered)
				<-c.Request.Context().Done()
				c.Abort()
			}))
			service := &Service{cfg: cfg, coreManager: manager, server: server, watcher: watcher, watcherCancel: watcherCancel}
			serverDone := make(chan struct{})
			serverError := make(chan error, 1)
			var connection net.Conn
			readerStarted := false
			t.Cleanup(func() {
				// These are cleanup fallbacks, not evidence of successful shutdown.
				releaseWatcher()
				watcherCancel()
				if connection != nil {
					connection.Close()
				}
				ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
				defer cancel()
				_ = server.Stop(ctx)
				if !d217ServiceJoin(ctx, watcherDone) || !d217ServiceJoin(ctx, serverDone) {
					t.Error("fixture cleanup could not join owned actors")
				}
				if readerStarted && !d217ServiceJoin(ctx, readerDone) {
					t.Error("fixture cleanup could not join reader")
				}
				select {
				case <-entered:
					if !d217ServiceJoin(ctx, handlerDone) {
						t.Error("fixture cleanup could not join handler")
					}
				default:
				}
			})
			// Register fallback joins before starting any fixture-owned worker.
			resources.watchers.Add(1)
			go func() {
				defer close(watcherDone)
				defer resources.watchers.Add(-1)
				if retainWatcher {
					<-release
				} else {
					<-watcherCtx.Done()
				}
			}()
			resources.servers.Add(1)
			go func() {
				defer close(serverDone)
				defer resources.servers.Add(-1)
				serverError <- server.Start()
			}()
			deadline := time.Now().Add(3 * time.Second)
			for time.Now().Before(deadline) {
				connection, err = net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 50*time.Millisecond)
				if err == nil {
					break
				}
				select {
				case <-serverDone:
					t.Fatal("API server exited before listening")
				default:
				}
				time.Sleep(time.Millisecond)
			}
			if err != nil {
				t.Fatal("fixture listener did not start")
			}
			resources.connections.Add(1)
			resources.readers.Add(1)
			readerStarted = true
			go func() {
				defer close(readerDone)
				defer resources.readers.Add(-1)
				defer resources.connections.Add(-1)
				defer connection.Close()
				_, _ = io.ReadAll(connection)
			}()
			_, err = fmt.Fprintf(connection, "GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-D217-Hold: 1\r\nConnection: close\r\n\r\n", port)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			if !d217ServiceJoin(ctx, entered) {
				t.Fatal("fixture request did not enter its barrier")
			}
			if err := service.Shutdown(ctx); err != nil {
				t.Fatalf("shutdown: %v", err)
			}
			if !d217ServiceJoin(ctx, serverDone) || !d217ServiceJoin(ctx, handlerDone) || !d217ServiceJoin(ctx, readerDone) {
				t.Fatal("shutdown retained a server, request worker, reader or connection")
			}
			if err := <-serverError; err != nil && !errors.Is(err, http.ErrServerClosed) {
				t.Fatalf("server stop: %v", err)
			}
			if retainWatcher {
				select {
				case <-watcherDone:
					t.Fatal("retained watcher control exited early")
				case <-time.After(50 * time.Millisecond):
				}
				if resources.watchers.Load() != 1 || d217ServiceReleased(resources.snapshot()) {
					t.Fatal("resource accounting accepted false shutdown completion")
				}
				releaseWatcher()
			}
			if !d217ServiceJoin(ctx, watcherDone) || !d217ServiceReleased(resources.snapshot()) {
				t.Fatalf("owned resources remain before cleanup: %v", resources.snapshot())
			}
			if next, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 100*time.Millisecond); err == nil {
				next.Close()
				t.Fatal("shutdown listener still accepts connections")
			}
			t.Logf("D217 shutdown retained_control=%t owned_resources=%v", retainWatcher, resources.snapshot())
		})
	}
}
