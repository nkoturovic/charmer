package cliproxy

// Copied into every omission build by the diagnostic derivation. Use only
// upstream APIs, so omitting a patch still builds and can go red on its own rows.
import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	runtimeexecutor "github.com/router-for-me/CLIProxyAPI/v8/internal/runtime/executor"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestOmissionCodexIdentity(t *testing.T) {
	captured := make(chan http.Header, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured <- r.Header.Clone()
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"id":"codexprobe","object":"response.compaction","output":[]}`)
	}))
	defer server.Close()
	cfg := &config.Config{}
	auth := &cliproxyauth.Auth{Provider: "codex", Attributes: map[string]string{"base_url": server.URL}, Metadata: map[string]any{"access_token": "fixture-only"}}
	_, err := runtimeexecutor.NewCodexExecutor(cfg).Execute(context.Background(), auth,
		cliproxyexecutor.Request{Model: "codexprobe-omission", Payload: []byte(`{"model":"codexprobe-omission","input":[]}`)},
		cliproxyexecutor.Options{SourceFormat: sdktranslator.FormatOpenAIResponse, Alt: "responses/compact"})
	if err != nil {
		t.Fatalf("fixture compact request: %v", err)
	}
	for path, h := range map[string]http.Header{
		"final-http": <-captured,
	} {
		for key, want := range map[string]string{
			"User-Agent": "codex-tui/0.159.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.159.1)",
			"Originator": "codex-tui", "Version": "0.159.1",
			// 8.x's routing hint: the resolved model, no tier in this body.
			"X-Codex-Routing-Hint": "model=codexprobe-omission",
		} {
			if got := h.Get(key); got != want {
				t.Errorf("%s %s = %q, want %q", path, key, got, want)
			}
		}
	}
}

// The probe consumes Python's actual T1 render, never a second hand-written Sol
// capability table. Dummy OAuth auths use a test-local base_url at the executor
// seam (the shipped OAuth executor otherwise targets a fixed HTTPS origin).
// This is the same service/manager/registry path as the overlay postCheck tests.
type codexProbeOverlaySpec struct {
	Wire     string            `json:"wire"`
	Aliases  map[string]string `json:"aliases"`
	Levels   bool              `json:"levels"`
	Channels map[string][]struct {
		Name     string `json:"name"`
		Context  int    `json:"max-context-length"`
		Output   int    `json:"max-completion-tokens"`
		Thinking struct {
			Levels []string `json:"levels"`
		} `json:"thinking"`
	} `json:"channels"`
}

// The file loader may reread or rewrite its input. Give it only a disposable,
// byte-identical copy; the payload parser independently consumes the original
// snapshot. The loader argument lets the regression controls exercise each guard.
func codexProbeLoadConfig(path, copyDir string, loadConfig func(string) (*config.Config, error)) (*config.Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	copyPath := filepath.Join(copyDir, "config.yaml")
	if err = os.WriteFile(copyPath, data, 0o600); err != nil {
		return nil, err
	}
	cfg, err := loadConfig(copyPath)
	if err != nil {
		return nil, err
	}
	after, err := os.ReadFile(copyPath)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(after, data) {
		return nil, fmt.Errorf("fixture file loader rewrote its copy")
	}
	if cfg.SourceHash != config.ComputeSourceHash(data) {
		return nil, fmt.Errorf("fixture file loader source hash differs from original bytes")
	}
	parsed, err := config.ParseConfigBytes(data)
	if err != nil {
		return nil, err
	}
	// Only the file loader adds the source identity; every other field must agree.
	parsed.SourceHash = cfg.SourceHash
	if !reflect.DeepEqual(cfg, parsed) {
		return nil, fmt.Errorf("file/payload config loaders differ")
	}
	return cfg, nil
}

func codexProbeLoadFixture(t *testing.T) (*config.Config, codexProbeOverlaySpec) {
	t.Helper()
	root := os.Getenv("CODEX_PROBE_FIXTURE_DIR")
	if root == "" {
		t.Fatal("CODEX_PROBE_FIXTURE_DIR required; no empty test selection")
	}
	cfg, err := codexProbeLoadConfig(filepath.Join(root, "config.yaml"), t.TempDir(), config.LoadConfig)
	if err != nil {
		t.Fatal(err)
	}
	specBytes, err := os.ReadFile(filepath.Join(root, "spec.json"))
	if err != nil {
		t.Fatal(err)
	}
	var spec codexProbeOverlaySpec
	if err = json.Unmarshal(specBytes, &spec); err != nil {
		t.Fatal(err)
	}
	if len(spec.Aliases) != 5 || len(spec.Channels) != 2 {
		t.Fatal("incomplete capability fixture")
	}
	return cfg, spec
}

func codexProbePipeline(t *testing.T, cfg *config.Config, channel, baseURL string) *cliproxyauth.Manager {
	t.Helper()
	id := "codexprobe-" + channel
	auth := &cliproxyauth.Auth{ID: id, Provider: channel, Status: cliproxyauth.StatusActive,
		Attributes: map[string]string{"auth_kind": "oauth", "plan_type": "pro", "base_url": baseURL},
		Metadata:   map[string]any{"type": channel, "access_token": "codexprobe-fixture", "account_id": "codexprobe-account"}}
	manager := cliproxyauth.NewManager(nil, nil, nil)
	manager.SetConfig(cfg)
	manager.SetOAuthModelAlias(cfg.OAuthModelAlias)
	if channel == "codex" {
		manager.RegisterExecutor(runtimeexecutor.NewCodexExecutor(cfg))
	}
	if _, err := manager.Register(context.Background(), auth); err != nil {
		t.Fatal(err)
	}
	reg := GlobalModelRegistry()
	reg.UnregisterClient(id)
	t.Cleanup(func() { reg.UnregisterClient(id) })
	service := &Service{cfg: cfg, coreManager: manager}
	service.completeModelRegistrationForAuth(context.Background(), auth)
	return manager
}

func TestCodexProbeOverlayLoaderParity(t *testing.T) {
	t.Run("fixture-loader-controls", func(t *testing.T) {
		for _, tc := range []struct {
			name      string
			wantError string
		}{
			{"unchanged", ""},
			{"copy-rewritten", "fixture file loader rewrote its copy"},
			{"source-hash-mismatch", "fixture file loader source hash differs from original bytes"},
			{"payload-mismatch", "file/payload config loaders differ"},
		} {
			t.Run(tc.name, func(t *testing.T) {
				original := []byte("# preserve fixture bytes\nhost: 127.0.0.1\nport: 18317\nerror-logs-max-files: -1\n")
				path := filepath.Join(t.TempDir(), "config.yaml")
				if err := os.WriteFile(path, original, 0o600); err != nil {
					t.Fatal(err)
				}
				copyDir := t.TempDir()
				loads := 0
				cfg, err := codexProbeLoadConfig(path, copyDir, func(copyPath string) (*config.Config, error) {
					loads++
					if copyPath == path || copyPath != filepath.Join(copyDir, "config.yaml") {
						t.Fatal("file loader must receive only the disposable copy")
					}
					copied, err := os.ReadFile(copyPath)
					if err != nil {
						return nil, err
					}
					if !bytes.Equal(copied, original) {
						t.Fatal("file loader did not receive byte-identical fixture data")
					}
					info, err := os.Stat(copyPath)
					if err != nil {
						return nil, err
					}
					if info.Mode().Perm() != 0o600 {
						t.Fatal("fixture copy must be private")
					}
					loaded, err := config.LoadConfig(copyPath)
					if err != nil {
						return nil, err
					}
					// Mutate one fact after the real file load, so each guard is
					// tested independently of optional patch behavior.
					switch tc.name {
					case "copy-rewritten":
						err = os.WriteFile(copyPath, append(copied, []byte("# changed copy\n")...), 0o600)
					case "source-hash-mismatch":
						loaded.SourceHash = ""
					case "payload-mismatch":
						loaded.Port++
					}
					return loaded, err
				})
				if loads != 1 {
					t.Fatalf("file loader calls = %d, want 1", loads)
				}
				after, readErr := os.ReadFile(path)
				if readErr != nil {
					t.Fatal(readErr)
				}
				if !bytes.Equal(after, original) {
					t.Fatal("loading or rejecting a fixture changed its original bytes")
				}
				if tc.wantError != "" {
					if err == nil || err.Error() != tc.wantError || cfg != nil {
						t.Fatalf("fixture guard: config returned = %t, err = %v, want %q", cfg != nil, err, tc.wantError)
					}
					return
				}
				if err != nil {
					t.Fatal(err)
				}
				if cfg.Port != 18317 || cfg.ErrorLogsMaxFiles != 10 || cfg.SourceHash != config.ComputeSourceHash(original) {
					t.Fatal("fixture load lost its parsed values, normalization or source identity")
				}
			})
		}
	})
	cfg, spec := codexProbeLoadFixture(t)
	if cfg.TLSHandshakeTimeout() != 20*time.Second {
		t.Fatalf("rendered TLS handshake timeout = %s, want 20s", cfg.TLSHandshakeTimeout())
	}
	if cfg.GitHubToken != "" || cfg.Streaming.SendKeepAliveBeforeFT || cfg.Claude.ToolSearch ||
		cfg.MaxRequestBodyBytes != 0 || cfg.Routing.SessionAffinityRebindOn != "" ||
		!reflect.DeepEqual(cfg.Models, config.ModelCatalogs{}) ||
		!reflect.DeepEqual(cfg.ClaudeHeaderDefaults, config.ClaudeHeaderDefaults{}) {
		t.Fatal("unrendered upstream options no longer have their reviewed defaults")
	}
	if cfg.Claude.SessionDatePinRetention() != 7*24*time.Hour {
		t.Fatal("unrendered session date pin default changed")
	}
	for channel, expected := range spec.Channels {
		codexProbePipeline(t, cfg, channel, "http://127.0.0.1:1") // registration only, no request
		models := GlobalModelRegistry().GetModelsForClient("codexprobe-" + channel)
		for _, row := range expected {
			count := 0
			for _, model := range models {
				if model.ID == row.Name || model.MetadataModelID == row.Name {
					count++
					if model.ContextLength != row.Context || model.MaxCompletionTokens != row.Output {
						t.Fatalf("%s capability limits lost", channel)
					}
					if len(row.Thinking.Levels) > 0 {
						if model.Thinking == nil || !reflect.DeepEqual(model.Thinking.Levels, row.Thinking.Levels) {
							t.Fatalf("%s levels lost", channel)
						}
					} else if model.Thinking != nil {
						t.Fatalf("%s invented thinking", channel)
					}
				}
			}
			if count == 0 {
				t.Fatalf("%s overlay registration absent", channel)
			}
		}
	}
}

func TestCodexProbeSolEfforts(t *testing.T) {
	cfg, spec := codexProbeLoadFixture(t)
	if len(cfg.Payload.Override) == 0 {
		t.Fatal("full-pipeline fixture must retain its rendered effort overrides")
	}
	t.Run("full-pipeline", func(t *testing.T) { codexProbeSolEfforts(t, cfg, spec, true) })
	if !spec.Levels {
		capabilityOnly := *cfg
		capabilityOnly.Payload = config.PayloadConfig{}
		t.Run("capability-only", func(t *testing.T) {
			if !reflect.DeepEqual(capabilityOnly.Payload, config.PayloadConfig{}) {
				t.Fatal("capability-only fixture must have no payload rules")
			}
			codexProbeSolEfforts(t, &capabilityOnly, spec, false)
		})
	}
}

func codexProbeSolEfforts(t *testing.T, cfg *config.Config, spec codexProbeOverlaySpec, overrides bool) {
	t.Helper()
	captured := make(chan []byte, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		captured <- body
		if r.URL.Path != "/responses" || r.Header.Get("Authorization") != "Bearer codexprobe-fixture" {
			t.Error("wrong route or credential")
		}
		if r.Header.Get("Version") != "0.159.1" {
			t.Error("a gateway without the Codex client identity")
		}
		output := `[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}]`
		if strings.Contains(string(body), "codexprobe_tool_setup") && !strings.Contains(string(body), "function_call_output") {
			output = `[{"type":"function_call","id":"fc_codexprobe","call_id":"call_codexprobe","name":"codexprobe_tool","arguments":"{}","status":"completed"}]`
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprintf(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_codexprobe\",\"model\":%q,\"status\":\"completed\",\"output\":%s,\"usage\":{\"input_tokens\":1,\"output_tokens\":1,\"total_tokens\":2}}}\n\n", spec.Wire, output)
	}))
	defer server.Close()
	manager := codexProbePipeline(t, cfg, "codex", server.URL)
	found := false
	for _, model := range GlobalModelRegistry().GetModelsForClient("codexprobe-codex") {
		if model.ID == spec.Wire || model.MetadataModelID == spec.Wire {
			found = true
			if model.UserDefined || model.SupportConfigurationUpdate || (model.Thinking != nil) != spec.Levels {
				t.Fatal("fixture capability precondition changed; do not manufacture the target branch")
			}
		}
	}
	if !found {
		t.Fatal("actual overlay definition absent")
	}
	for effort, alias := range spec.Aliases {
		// The setup is separately counted, and the continuation is built from its
		// returned function call (not an invented pre-existing provider response).
		for _, mode := range []string{"http", "stream", "tool", "chat-http", "chat-stream"} {
			t.Run(effort+"/"+mode, func(t *testing.T) {
				translated := strings.HasPrefix(mode, "chat-")
				input := []any{map[string]any{"role": "user", "content": "fixture"}}
				send := func(stream bool, items []any) []byte {
					t.Helper()
					defer func() {
						// An assertion must not leave the fixture channel full and
						// turn later independent cases into deadline failures.
						select {
						case <-captured:
						default:
						}
					}()
					payload, _ := json.Marshal(map[string]any{"model": alias, "input": items,
						"reasoning": map[string]any{"effort": effort, "summary": "concise"}, "include": []string{"reasoning.encrypted_content"},
						"tools": []any{map[string]any{"type": "function", "name": "codexprobe_tool", "parameters": map[string]any{"type": "object", "properties": map[string]any{}}}}})
					ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
					defer cancel()
					req := cliproxyexecutor.Request{Model: alias, Payload: payload}
					// The HTTP handlers preserve the client's alias in this metadata
					// before manager resolution; final payload rules match that alias.
					opts := cliproxyexecutor.Options{SourceFormat: sdktranslator.FormatOpenAIResponse, Stream: stream,
						Metadata: map[string]any{cliproxyexecutor.RequestedModelMetadataKey: alias}}
					if translated {
						opts.SourceFormat = sdktranslator.FormatOpenAI
						req.Payload, _ = json.Marshal(map[string]any{"model": alias,
							"messages":         []any{map[string]any{"role": "user", "content": "translated fixture"}},
							"reasoning_effort": effort, "reasoning": map[string]any{"summary": "concise"}})
					}
					var response []byte
					translatedFinished := false
					if stream {
						res, err := manager.ExecuteStream(ctx, []string{"codex"}, req, opts)
						if err != nil {
							t.Fatal(err)
						}
						for chunk := range res.Chunks {
							if chunk.Err != nil {
								t.Fatal(chunk.Err)
							}
							response = append(response, chunk.Payload...)
							if gjson.GetBytes(chunk.Payload, "choices.0.finish_reason").String() == "stop" {
								translatedFinished = true
							}
						}
						// Executor chunks precede HTTP framing: Chat's handler adds
						// [DONE], while its executor emits the finish_reason chunk.
						if translated && !translatedFinished || !translated && !strings.Contains(string(response), "response.completed") {
							t.Fatal("stream completion absent")
						}
					} else {
						res, err := manager.Execute(ctx, []string{"codex"}, req, opts)
						if err != nil {
							t.Fatal(err)
						}
						response = res.Payload
						output := "output"
						if translated {
							output = "choices"
						}
						if !gjson.GetBytes(response, output).IsArray() {
							t.Fatal("HTTP completion absent")
						}
					}
					var upstream []byte
					select {
					case upstream = <-captured:
					default:
						t.Fatal("no upstream capture")
					}
					if gjson.GetBytes(upstream, "model").String() != spec.Wire {
						t.Fatal("alias retargeted")
					}
					gotEffort := gjson.GetBytes(upstream, "reasoning.effort")
					if spec.Levels || overrides {
						if gotEffort.String() != effort {
							t.Fatal("capability or explicit payload effort lost")
						}
					} else if gotEffort.Exists() {
						t.Fatal("capability-only no-level control retained effort")
					}
					// v8 treats summary visibility independently of effort. The old
					// assertion that no levels strips summary is intentionally retired.
					if gjson.GetBytes(upstream, "reasoning.summary").String() != "concise" {
						t.Fatal("source summary lost")
					}
					if gjson.GetBytes(upstream, "include").Raw != `["reasoning.encrypted_content"]` {
						t.Fatal("encrypted include lost")
					}
					return response
				}
				if mode == "tool" {
					setup := []any{map[string]any{"role": "user", "content": "codexprobe_tool_setup"}}
					response := send(false, setup)
					call := gjson.GetBytes(response, "output.0")
					if call.Get("type").String() != "function_call" {
						t.Fatal("setup did not return tool call")
					}
					var item any
					if err := json.Unmarshal([]byte(call.Raw), &item); err != nil {
						t.Fatal(err)
					}
					input = append(setup, item, map[string]any{"type": "function_call_output", "call_id": call.Get("call_id").String(), "output": "fixture result"})
				}
				send(mode == "stream" || mode == "chat-stream", input)
			})
		}
	}
}
