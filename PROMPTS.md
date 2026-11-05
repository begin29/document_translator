# Prompts

`PROMPT_VERSION` is `v1` in `backend/app/core/prompts.py`. Provider calls store the version so a prompt change is visible in `provider_calls`.

## Translate

System prompt tells the model the source and target languages, the glossary, and a short document summary. It must return JSON:

```json
{"blocks":[{"n":1,"text":"..."}]}
```

The user message is numbered plain text, one block per line:

```text
[1] source text
[2] source text
```

The parser accepts that JSON or a `[n] text` reply. Missing or extra numbers fail the attempt. Numbers, URLs, and glossary targets must be copied, not translated.

## Summary

One short paragraph in the source language, used as context on later segments. It is not a translation.

## Page class

Cheap vision, only when local metrics return `unknown`:

```json
{"kind":"document|photo_of_document|non_document","legibility":"good|partial|poor","layout":"prose|table|form|mixed","handwriting":false,"orientation_deg":0,"language":"en","image_contains_text":false}
```

## OCR

Verbatim transcription. The model must not translate. Unreadable spans are `[illegible]`. Block kinds are `heading`, `paragraph`, `list_item`, `table_cell`, `caption`.

## Review

The review agent does not get a free-form rewrite prompt. It uses tools: read the source segment, read neighbor translations, look up the glossary, retranslate with an instruction, or accept the segment. The retranslate tool calls the same completion path as bulk translation.
