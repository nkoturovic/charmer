package cliproxy

// Copied into the matching diagnostic, never into the shipped gateway. This
// checks the real static capability bit without changing any model definition.
import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/thinking"
)

func TestGatewayV8NativeConfigurationUpdateBoundary(t *testing.T) {
	var model *registry.ModelInfo
	for _, candidate := range registry.GetCodexProModels() {
		if candidate.SupportConfigurationUpdate {
			model = candidate
			break
		}
	}
	if model == nil || model.Thinking == nil || len(model.Thinking.Levels) == 0 {
		t.Fatal("no actual static Codex configuration-update capability")
	}
	body, _ := json.Marshal(map[string]any{
		"model":     model.ID,
		"reasoning": map[string]any{"effort": "fixture-unsupported", "summary": "concise"},
		"input": []any{map[string]any{"type": "configuration_update", "reasoning": map[string]any{"effort": "high"}},
			map[string]any{"role": "user", "content": "fixture"}},
	})
	got, err := thinking.ApplyThinkingWithModelInfo(body, body, model.ID, "openai-response", "codex", "codex", model)
	if err != nil || !bytes.Equal(got, body) {
		t.Fatalf("native update-capable request must preserve its baseline and updates: err=%v", err)
	}
	// The same real definition does not exempt a translated caller from level
	// validation. No provider acceptance is inferred from native passthrough.
	source := []byte(`{"reasoning_effort":"fixture-unsupported","reasoning":{"summary":"concise"}}`)
	if _, err = thinking.ApplyThinkingWithModelInfo(body, source, model.ID, "openai", "codex", "codex", model); err == nil {
		t.Fatal("translated caller incorrectly gained native validation bypass")
	}
	t.Logf("actual static model=%s configuration_updates=true; native bypass and translated rejection observed", model.ID)
}
