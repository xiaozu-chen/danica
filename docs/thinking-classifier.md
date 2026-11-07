# Thinking classifier

Danica uses one small classifier: the existing full-fine-tuned
SmolLM2-135M model decides only whether a user request should enable deliberate
thinking. Its complete target is `{"thinking": true}` or
`{"thinking": false}`. It does not choose tools, capabilities, or a general
intent label.
At inference, Danica requests a JSON `thinking_mode` boolean and also accepts
the classifier's single-quoted text form `{'thinking_mode': true}` (or
`false`). The previous JSON key `thinking` remains accepted for compatibility.

Tool selection remains with Danica's main chat model through its existing
function-calling loop. That model sees the available tool schemas and may call
them, answer directly, or ask a follow-up. The binary classifier only sets the
thinking mode used for that chat request. If the classifier is disabled,
unavailable, or returns invalid output, a small deterministic thinking heuristic
is used instead.

## ChatML training examples

Keep the training examples in ChatML, with the same classification instruction
and JSON-only assistant target:

```text
<|im_start|>system
Classify whether the user message needs deliberate reasoning. Reply only with {"thinking": true} or {"thinking": false}.<|im_end|>
<|im_start|>user
What is the capital of Japan?<|im_end|>
<|im_start|>assistant
{"thinking": false}<|im_end|>
```

```text
<|im_start|>system
Classify whether the user message needs deliberate reasoning. Reply only with {"thinking": true} or {"thinking": false}.<|im_end|>
<|im_start|>user
Compare these approaches, explain their trade-offs, and recommend one for a small team.<|im_end|>
<|im_start|>assistant
{"thinking": true}<|im_end|>
```

```text
<|im_start|>system
Classify whether the user message needs deliberate reasoning. Reply only with {"thinking": true} or {"thinking": false}.<|im_end|>
<|im_start|>user
Rewrite this sentence to be more concise: "Due to the fact that we were late, we missed the opening."<|im_end|>
<|im_start|>assistant
{"thinking": false}<|im_end|>
```

Include varied simple and reasoning-intensive requests from all parts of
Danica's use: coding, writing, local research, web research, tool use, planning,
and ordinary conversation. The label is based on reasoning complexity, not
which tool may eventually be useful. For example, a short request to search a
specific file can be `false`; comparing evidence across several documents can
be `true`.

Use consented or appropriately licensed examples, redact credentials and
personal data, and split related prompts or paraphrases together to prevent
leakage across train, validation, and test. Preserve the exact ChatML format and
JSON target used in serving. Evaluate binary precision/recall, false-positive
and false-negative rates, and end-to-end task success before changing the
classifier.

## Configuration

```toml
[thinking]
auto = true
classifier_enabled = true
classifier_url = "http://127.0.0.1:8082"
classifier_model = "SmolLM2-135M-thinking"
log_decisions = true
log_prompts = false
```

Automatic selection and classifier use are disabled by default. Decision logs
are opt-in; with `log_prompts = false`, the local log stores a text hash and
the binary decision but not the prompt. A legacy `[routing]` config section
with `auto_thinking` is still read for compatibility; new settings belong
under `[thinking]`.

## Run the 30-item accuracy check

Enable the classifier and point it at its running local endpoint, then run:

```powershell
.\.venv\Scripts\python.exe -m evals.thinking_accuracy
```

The held-out ChatML-style set contains 15 `true` and 15 `false` labels. The
report prints each expected/predicted pair, accuracy, thinking precision and
recall, and false-positive/false-negative counts. Rule-fallback results are
rejected so they cannot be mistaken for classifier accuracy. To save the
per-item outputs:

```powershell
.\.venv\Scripts\python.exe -m evals.thinking_accuracy --output evals\results\thinking-accuracy.json
```

Review individual mismatches before changing the dataset or threshold. These
30 examples are a quick smoke evaluation, not a statistically conclusive
benchmark.

No latency or success improvement is claimed until comparable baseline and
candidate eval runs are recorded. Prompt/KV-slot caching remains independent
of this classifier and disabled until supported by the configured llama-server
and verified by an integration test.
