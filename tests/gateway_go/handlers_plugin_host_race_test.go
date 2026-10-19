package handlers

// charmer Go diagnostic: plugin-host publication (SetPluginHost /
// SetModelRouterHost) against its runtime readers (interceptorHost,
// modelRouterHost, the request lifecycle tracker and the execution-host lookup)
// on the 8.x handlers, which lock publication themselves; the gateway carries
// no patch for it. In-process only: stub hosts, gin test contexts, no listener
// and no network. Repeat-safe; runs under -race -count=100.
//
// The residual 8.x keeps: a model-list response reads the interceptor host for
// the response interception and again for the request lifecycle tracker, so two
// different hosts published between those reads can each see half of the
// request. The shipped gateway cannot reach it (its handler's host is the
// server's one plugin host, re-set to the same pointer on reload), so the
// intercept/complete correspondence is checked where the gateway publishes:
// re-setting the same host. Mixed publications check everything else.

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	coreexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	sdkconfig "github.com/router-for-me/CLIProxyAPI/v8/sdk/config"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/pluginapi"
)

const (
	fxPluginModel    = "gwtest-plugin-host-model"
	fxPluginExecutor = "gwtest-plugin-host-executor"
	fxPluginRounds   = 200
	fxPluginReaders  = 4
	fxPluginDeadline = 20 * time.Second
	fxModelListBody  = `{"object":"list","data":[]}`
	fxModelsFormat   = "gwtest-models"
)

// fxPluginHost is a stub plugin host: interceptor, lifecycle observer,
// model router and executor host. Callbacks may re-enter the handler.
type fxPluginHost struct {
	tag         string
	rewrite     bool
	intercepted sync.Map // request ID -> struct{}
	completed   sync.Map // request ID -> struct{}
	onIntercept func()
	onComplete  func()
	onRoute     func()
}

func (p *fxPluginHost) InterceptRequestBeforeAuth(context.Context, pluginapi.RequestInterceptRequest) pluginapi.RequestInterceptResponse {
	return pluginapi.RequestInterceptResponse{}
}

func (p *fxPluginHost) InterceptRequestAfterAuth(context.Context, pluginapi.RequestInterceptRequest) pluginapi.RequestInterceptResponse {
	return pluginapi.RequestInterceptResponse{}
}

func (p *fxPluginHost) InterceptResponse(_ context.Context, req pluginapi.ResponseInterceptRequest) pluginapi.ResponseInterceptResponse {
	if req.SourceFormat == fxModelsFormat {
		p.intercepted.Store(req.RequestID, struct{}{})
	}
	if p.onIntercept != nil {
		p.onIntercept()
	}
	if !p.rewrite {
		return pluginapi.ResponseInterceptResponse{}
	}
	return pluginapi.ResponseInterceptResponse{
		Body:    []byte(`{"object":"list","data":[],"host":"` + p.tag + `"}`),
		Headers: http.Header{"X-Gwtest-Host": {p.tag}},
	}
}

func (p *fxPluginHost) InterceptStreamChunk(context.Context, pluginapi.StreamChunkInterceptRequest) pluginapi.StreamChunkInterceptResponse {
	return pluginapi.StreamChunkInterceptResponse{}
}

func (p *fxPluginHost) HasRequestInterceptors() bool { return false }

func (p *fxPluginHost) CompleteRequest(_ context.Context, completion pluginapi.RequestCompletion) {
	if completion.SourceFormat == fxModelsFormat {
		p.completed.Store(completion.RequestID, struct{}{})
	}
	if p.onComplete != nil {
		p.onComplete()
	}
}

func (p *fxPluginHost) HasModelRouters() bool { return true }

func (p *fxPluginHost) RouteModel(context.Context, pluginapi.ModelRouteRequest) (pluginapi.ModelRouteResponse, bool) {
	if p.onRoute != nil {
		p.onRoute()
	}
	return pluginapi.ModelRouteResponse{Handled: true, TargetKind: pluginapi.ModelRouteTargetExecutor, Target: fxPluginExecutor}, true
}

func (p *fxPluginHost) ExecutePluginExecutor(context.Context, string, coreexecutor.Request, coreexecutor.Options) (coreexecutor.Response, error) {
	return coreexecutor.Response{Payload: []byte(`{"id":"gwtest-` + p.tag + `","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`)}, nil
}

func (p *fxPluginHost) ExecutePluginExecutorStream(context.Context, string, coreexecutor.Request, coreexecutor.Options) (*coreexecutor.StreamResult, error) {
	return nil, errors.New("gwtest: stream not used")
}

func (p *fxPluginHost) CountPluginExecutor(context.Context, string, coreexecutor.Request, coreexecutor.Options) (coreexecutor.Response, error) {
	return coreexecutor.Response{}, errors.New("gwtest: count not used")
}

func fxModelList(t *testing.T, h *BaseAPIHandler) (string, string) {
	t.Helper()
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	h.WriteModelListResponse(c, fxModelsFormat, []byte(fxModelListBody))
	if recorder.Code != http.StatusOK {
		t.Errorf("model list status = %d, want 200", recorder.Code)
	}
	return recorder.Body.String(), recorder.Header().Get("X-Gwtest-Host")
}

// fxWithin runs work and fails the test if it does not finish in time (a
// lock held across a plugin callback deadlocks a re-entrant callback).
func fxWithin(t *testing.T, what string, work func()) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		work()
	}()
	select {
	case <-done:
	case <-time.After(fxPluginDeadline):
		t.Fatalf("%s did not finish within %s (lock held across a plugin callback?)", what, fxPluginDeadline)
	}
}

// fxIntercepted counts the model-list requests a host intercepted.
func fxIntercepted(host *fxPluginHost) int {
	n := 0
	host.intercepted.Range(func(any, any) bool {
		n++
		return true
	})
	return n
}

// fxSameRequests checks that one model-list request used one host: every
// model-list request the host intercepted was completed on that same host, and
// back. It holds when the host is the only one ever published.
func fxSameRequests(t *testing.T, host *fxPluginHost) int {
	t.Helper()
	n := 0
	host.intercepted.Range(func(id, _ any) bool {
		n++
		if _, ok := host.completed.Load(id); !ok {
			t.Errorf("host %s intercepted request %v but another host completed it", host.tag, id)
		}
		return true
	})
	host.completed.Range(func(id, _ any) bool {
		if _, ok := host.intercepted.Load(id); !ok {
			t.Errorf("host %s completed request %v that another host intercepted", host.tag, id)
		}
		return true
	})
	return n
}

func TestGatewayPluginHostConcurrentModelInterception(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := NewBaseAPIHandlers(&sdkconfig.SDKConfig{}, nil)
	a := &fxPluginHost{tag: "a", rewrite: true}
	b := &fxPluginHost{tag: "b", rewrite: true}
	var typedNil *fxPluginHost
	h.SetPluginHost(a)

	var stop atomic.Bool
	var responses, plain atomic.Int64
	var wg sync.WaitGroup
	for reader := 0; reader < fxPluginReaders; reader++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for !stop.Load() {
				body, tag := fxModelList(t, h)
				responses.Add(1)
				switch {
				case tag == "" && body == fxModelListBody:
					plain.Add(1)
				case (tag == "a" || tag == "b") && strings.Contains(body, `"host":"`+tag+`"`):
				default:
					t.Errorf("model list response mixed hosts: header %q body %s", tag, body)
					return
				}
			}
		}()
	}
	fxWithin(t, "model-list interception under plugin-host reloads", func() {
		// Keep publishing until the readers have overlapped the writer for a
		// full bound (or a reader failed).
		for i := 0; (i < fxPluginRounds || responses.Load() < fxPluginRounds) && !t.Failed(); i++ {
			switch i % 4 {
			case 0, 1:
				h.SetPluginHost(a) // reassigning the same host is still a publication
			case 2:
				h.SetPluginHost(b)
			case 3:
				h.SetPluginHost(typedNil) // normalized to no host
			}
			runtime.Gosched()
		}
		stop.Store(true)
		wg.Wait()
	})
	if n := fxIntercepted(a) + fxIntercepted(b); n == 0 {
		t.Fatal("no model-list response was intercepted: the plugin host path did not run")
	}
	if responses.Load() < fxPluginRounds {
		t.Fatalf("model-list responses = %d, want >= %d", responses.Load(), fxPluginRounds)
	}

	// The gateway's own publication: the same host re-set on every reload. Each
	// request is intercepted and completed on that host.
	same := &fxPluginHost{tag: "same", rewrite: true}
	h = NewBaseAPIHandlers(&sdkconfig.SDKConfig{}, nil)
	h.SetPluginHost(same)
	stop.Store(false)
	responses.Store(0)
	for reader := 0; reader < fxPluginReaders; reader++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for !stop.Load() {
				if body, tag := fxModelList(t, h); tag != "same" || !strings.Contains(body, `"host":"same"`) {
					t.Errorf("model list response under same-host reloads = %q %s", tag, body)
					return
				}
				responses.Add(1)
			}
		}()
	}
	fxWithin(t, "model-list interception under same-host reloads", func() {
		for i := 0; (i < fxPluginRounds || responses.Load() < fxPluginRounds) && !t.Failed(); i++ {
			h.SetPluginHost(same)
			runtime.Gosched()
		}
		stop.Store(true)
		wg.Wait()
	})
	if n := fxSameRequests(t, same); n == 0 {
		t.Fatal("no model-list response was intercepted under same-host reloads")
	}
}

func TestGatewayPluginHostConcurrentExecutorLookup(t *testing.T) {
	h := NewBaseAPIHandlers(&sdkconfig.SDKConfig{}, nil)
	a := &fxPluginHost{tag: "a"}
	b := &fxPluginHost{tag: "b"}
	router := &fxPluginHost{tag: "router"}
	var typedNil *fxPluginHost
	h.SetPluginHost(a)

	var stop atomic.Bool
	var executed atomic.Int64
	var wg sync.WaitGroup
	for reader := 0; reader < fxPluginReaders; reader++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for !stop.Load() {
				if h.pluginExecutorHost() == nil || h.modelRouterHost() == nil {
					t.Error("a published plugin host was not visible to the execution-host lookup")
					return
				}
				body, _, errMsg := h.ExecuteWithAuthManager(context.Background(), "openai", fxPluginModel,
					[]byte(`{"model":"`+fxPluginModel+`","messages":[{"role":"user","content":"hi"}]}`), "")
				if errMsg != nil {
					t.Errorf("plugin executor request failed: %d %v", errMsg.StatusCode, errMsg.Error)
					return
				}
				if !strings.Contains(string(body), `gwtest-`) {
					t.Errorf("plugin executor response not from a published host: %s", body)
					return
				}
				executed.Add(1)
			}
		}()
	}
	fxWithin(t, "executor lookup under plugin-host reloads", func() {
		// Keep publishing until the readers have overlapped the writer for a
		// full bound (or a reader failed).
		for i := 0; (i < fxPluginRounds || executed.Load() < fxPluginRounds) && !t.Failed(); i++ {
			switch i % 4 {
			case 0:
				h.SetPluginHost(a)
			case 1:
				h.SetPluginHost(b)
			case 2:
				h.SetModelRouterHost(router)
			case 3:
				h.SetModelRouterHost(typedNil) // falls back to the interceptor host
			}
			runtime.Gosched()
		}
		stop.Store(true)
		wg.Wait()
	})
	if executed.Load() < fxPluginRounds {
		t.Fatalf("plugin executor requests = %d, want >= %d", executed.Load(), fxPluginRounds)
	}
}

func TestGatewayPluginHostTypedNilAndReentrantCallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var typedNil *fxPluginHost

	// Typed-nil normalization: a typed nil never becomes a callable host.
	h := NewBaseAPIHandlers(&sdkconfig.SDKConfig{}, nil)
	h.SetPluginHost(typedNil)
	if h.interceptorHost() != nil || h.modelRouterHost() != nil || h.pluginExecutorHost() != nil {
		t.Fatal("typed-nil plugin host was published as a host")
	}
	if body, tag := fxModelList(t, h); body != fxModelListBody || tag != "" {
		t.Fatalf("model list with typed-nil host = %q %q, want untouched", body, tag)
	}
	a := &fxPluginHost{tag: "a", rewrite: true}
	router := &fxPluginHost{tag: "router"}
	h.SetPluginHost(a)
	h.SetModelRouterHost(typedNil)
	if h.modelRouterHost() != PluginModelRouterHost(a) || h.pluginExecutorHost() != PluginExecutorHost(a) {
		t.Fatal("typed-nil model router did not fall back to the plugin host")
	}
	h.SetModelRouterHost(router)
	if h.modelRouterHost() != PluginModelRouterHost(router) || h.pluginExecutorHost() != PluginExecutorHost(router) {
		t.Fatal("explicit model router host not preferred")
	}
	h.SetPluginHost(typedNil)
	if h.interceptorHost() != nil {
		t.Fatal("typed-nil plugin host did not clear the published host")
	}
	var nilHandler *BaseAPIHandler
	nilHandler.SetPluginHost(a)
	nilHandler.SetModelRouterHost(router)
	if nilHandler.interceptorHost() != nil || nilHandler.modelRouterHost() != nil || nilHandler.pluginExecutorHost() != nil {
		t.Fatal("nil handler reported a plugin host")
	}

	// Re-entrant callbacks: each host's callbacks republish the other host and
	// read the handler while other requests run. No lock may be held across a
	// callback, and each response body and header still come from one host.
	h = NewBaseAPIHandlers(&sdkconfig.SDKConfig{}, nil)
	x := &fxPluginHost{tag: "x", rewrite: true}
	y := &fxPluginHost{tag: "y", rewrite: true}
	reenter := func(next *fxPluginHost) func() {
		return func() {
			h.SetPluginHost(next)
			if h.interceptorHost() == nil || h.modelRouterHost() == nil || h.pluginExecutorHost() == nil {
				t.Error("re-entrant read saw no plugin host")
			}
		}
	}
	x.onIntercept, x.onComplete, x.onRoute = reenter(y), reenter(x), reenter(y)
	y.onIntercept, y.onComplete, y.onRoute = reenter(x), reenter(y), reenter(x)
	h.SetPluginHost(x)
	fxWithin(t, "re-entrant plugin callbacks", func() {
		var wg sync.WaitGroup
		for reader := 0; reader < fxPluginReaders; reader++ {
			wg.Add(1)
			go func(reader int) {
				defer wg.Done()
				for i := 0; i < fxPluginRounds/fxPluginReaders; i++ {
					if reader%2 == 0 {
						body, tag := fxModelList(t, h)
						if (tag != "x" && tag != "y") || !strings.Contains(body, `"host":"`+tag+`"`) {
							t.Errorf("re-entrant model list response = %q %s", tag, body)
							return
						}
						continue
					}
					_, _, errMsg := h.ExecuteWithAuthManager(context.Background(), "openai", fxPluginModel,
						[]byte(`{"model":"`+fxPluginModel+`","messages":[{"role":"user","content":"hi"}]}`), "")
					if errMsg != nil {
						t.Errorf("re-entrant plugin executor request failed: %d %v", errMsg.StatusCode, errMsg.Error)
						return
					}
				}
			}(reader)
		}
		wg.Wait()
	})
	if n := fxIntercepted(x) + fxIntercepted(y); n == 0 {
		t.Fatal("no re-entrant callback ran")
	}
}
