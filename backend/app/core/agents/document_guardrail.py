from __future__ import annotations

from agents import GuardrailFunctionOutput, input_guardrail


def pages_are_not_a_document(classes: list[str]) -> bool:
    return bool(classes) and all(item == "non_document" for item in classes)


@input_guardrail(name="not_a_document", run_in_parallel=False)
def not_a_document_guardrail(_ctx, _agent, payload) -> GuardrailFunctionOutput:
    text = payload if isinstance(payload, str) else ""
    triggered = text == "non_document" or text.startswith("non_document")
    return GuardrailFunctionOutput(
        output_info={"code": "not_a_document", "pages": text},
        tripwire_triggered=triggered,
    )
