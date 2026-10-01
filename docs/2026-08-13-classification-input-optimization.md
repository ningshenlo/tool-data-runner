# Classification input optimization report (2026-08-13)

## Outcome

Both assets category enrichment and taxonomy shadow classification now use the
same bounded homepage input contract:

1. Fetch the rendered homepage HTML through Browser Rendering `/content`.
2. Reject invalid, anti-bot, parked, and otherwise unusable pages through the
   existing page-quality gate.
3. Prefer `<main>` or an element with `role="main"`; otherwise use `<body>`.
4. Remove navigation, footer, scripts, styles, templates, noscript, and SVG
   content, normalize whitespace, and remove adjacent duplicate lines.
5. Truncate the cleaned text to `CATEGORY_MAIN_CONTENT_MAX_CHARS` (default
   `10000`).
6. Put only that cleaned text in the classification prompt and use inline HTML
   for the Browser Rendering `/json` transport. The model-side request does not
   navigate to the real product URL or to a neutral external page.

The original source URL is retained only for evidence provenance and auditing.

## Problems found

- Taxonomy classification first attempted structured extraction against the
  real page, so the model could receive navigation, footer, and unrelated page
  content.
- Its visible-text fallback used a regex-based extractor capped at 16,000
  characters and the older direct-page fallback could bypass cleaning entirely.
- Assets category enrichment and taxonomy classification did not share one
  content contract.
- An empty DeepSeek structured result triggered a second paid "force pick"
  request to the same model. For the neutral HTTPS page, the generic URL retry
  loop could then repeat the work against HTTP.
- The automatic taxonomy worker was already running in primary-only mode, so
  capability classification was not the source of the observed automatic call
  volume. A normal successful taxonomy classification still has three model
  stages: profile, top-level candidates, and leaf adjudication.

## Cost and call-count effect

- Cleaned homepage text is capped at 10,000 characters instead of the older
  16,000-character visible-text ceiling, a maximum 37.5% reduction in that text
  component. Pages that previously used the real-page path can see a larger
  reduction because navigation, footer, scripts, styles, and unrelated markup
  are no longer model input.
- Each model is now attempted at most once per structured stage before moving
  to the configured fallback. The same-model force-pick retry was removed.
- Inline HTML removes the HTTPS-to-HTTP neutral-page retry. In the worst empty
  result path with two configured models, a stage falls from as many as six
  model attempts (four DeepSeek and two fallback attempts) to two (one per
  model). A normally successful first attempt remains one call.
- The taxonomy prompt version was intentionally not changed, so successful
  tools are not automatically reprocessed just to adopt the new extractor.
  Missing and retryable failed work uses the new path.

Actual currency savings depend on homepage length, model output length, and the
failure rate. The largest saving is expected on empty/invalid structured output
and verbose pages; a successful short page will see a smaller difference.

## Model controls

DeepSeek custom-AI payloads request `thinking.type=disabled` and set both output
token limit fields to `CATEGORY_DEEPSEEK_MAX_OUTPUT_TOKENS` (default `1024`).
DeepSeek [documents thinking as enabled by default](https://api-docs.deepseek.com/guides/thinking_mode/)
and documents this payload as the OpenAI-format switch for disabling it.

Cloudflare's public [Browser Rendering `/json` schema](https://developers.cloudflare.com/api/resources/browser_rendering/subresources/json/methods/create/)
currently documents only `model` and `authorization` inside `custom_ai`.
Therefore the runner logs this as a requested setting, not as confirmed provider
behavior. A production canary and provider usage comparison are required to
verify that Cloudflare forwards these extra fields.

## Configuration

The Compose file supplies defaults to both `assets-worker` and
`taxonomy-worker`:

```dotenv
CATEGORY_MAIN_CONTENT_MAX_CHARS=10000
CATEGORY_DEEPSEEK_MAX_OUTPUT_TOKENS=1024
```

No environment change is required to use the defaults. Set them in Dokploy only
when an override is needed.

## Observability and rollout

Each cleaned-text request emits `classification.cleaned_text.request` with the
stage, source URL, prompt character count, model list, and requested DeepSeek
thinking state. Stored category/taxonomy raw data includes the cleaned content
character count and a short SHA-256 hash without duplicating the full input.

After deployment, run a small canary before restoring full taxonomy concurrency:

1. Confirm requests contain `classification.cleaned_text.request`.
2. Confirm there are no `shadow_profile_direct_fallback` events.
3. Confirm a failed model stage is attempted once per configured model, without
   an HTTP `example.com` retry.
4. Compare DeepSeek input/output token usage for a fixed sample of 20-50 tools.
5. Only then increase taxonomy concurrency to its normal value.
