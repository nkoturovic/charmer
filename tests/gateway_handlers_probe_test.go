package handlers

// Seams that predate the admitted series stay compilable in every
// dependency-closed omission.
import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/config"
)

// 8.x locks the configuration pointer itself; what the patch adds is one
// snapshot per request. A request that started under passthrough headers keeps
// them on its error response after a reload turned them off.
func TestOmissionConfigSnapshot(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := NewBaseAPIHandlers(&config.SDKConfig{PassthroughHeaders: true}, nil)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	_, cancel := h.GetContextWithCancel(nil, c, context.Background())
	defer cancel()
	h.UpdateClients(&config.SDKConfig{})
	h.WriteErrorResponse(c, &interfaces.ErrorMessage{StatusCode: http.StatusBadGateway, Error: errors.New("gwtest"),
		Addon: http.Header{"X-Gwtest-Addon": {"1"}}})
	if recorder.Header().Get("X-Gwtest-Addon") != "1" {
		t.Fatal("an in-flight request read the reloaded configuration")
	}
	fresh := httptest.NewRecorder()
	next, _ := gin.CreateTestContext(fresh)
	next.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	h.WriteErrorResponse(next, &interfaces.ErrorMessage{StatusCode: http.StatusBadGateway, Error: errors.New("gwtest"),
		Addon: http.Header{"X-Gwtest-Addon": {"1"}}})
	if fresh.Header().Get("X-Gwtest-Addon") != "" {
		t.Fatal("a new request did not read the reloaded configuration")
	}
}
