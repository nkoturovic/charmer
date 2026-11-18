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
	levels := model.Thinking.Levels
	native := func(effort string) []byte {
		body, _ := json.Marshal(map[string]any{
			"model":     model.ID,
			"reasoning": map[string]any{"effort": effort, "summary": "concise"},
			"input": []any{map[string]any{"type": "configuration_update", "reasoning": map[string]any{"effort": levels[len(levels)-1]}},
				map[string]any{"role": "user", "content": "fixture"}},
		})
		return body
	}
	// A native update-capable request whose efforts the model supports keeps
	// its baseline and its update timeline byte for byte.
	body := native(levels[0])
	got, err := thinking.ApplyThinkingWithModelInfo(body, body, model.ID, "openai-response", "codex", "codex", model)
	if err != nil || !bytes.Equal(got, body) {
		t.Fatalf("native update-capable request must preserve its baseline and updates: err=%v", err)
	}
	// Native passthrough is no validation bypass (since 8.0.68): an effort the
	// model does not support is refused, as it is for a translated caller. No
	// provider acceptance is inferred from native passthrough.
	unsupported := native("fixture-unsupported")
	if _, err = thinking.ApplyThinkingWithModelInfo(unsupported, unsupported, model.ID, "openai-response", "codex", "codex", model); err == nil {
		t.Fatal("native request with an unsupported effort was not validated")
	}
	source := []byte(`{"reasoning_effort":"fixture-unsupported","reasoning":{"summary":"concise"}}`)
	if _, err = thinking.ApplyThinkingWithModelInfo(body, source, model.ID, "openai", "codex", "codex", model); err == nil {
		t.Fatal("translated caller with an unsupported effort was not validated")
	}
	t.Logf("actual static model=%s configuration_updates=true; supported native kept, unsupported native and translated refused", model.ID)
}
