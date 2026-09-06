# Owner-local provider keys

Run these commands on your robot or in your own local simulator checkout:

```sh
innate keys set cartesia
innate keys set openai
innate keys status
```

`set` prompts with hidden input. In an uninstalled checkout, use
`INNATE_OS_ROOT="$PWD" python3 scripts/innate keys set openai` (Python needs `click`).
For automation, pipe a secret manager's output to `innate keys set openai --stdin`.
There is deliberately no key-value command argument or browser/ROS credential API.

The commands atomically save `.env` with mode `0600`, preserve unrelated settings,
and remove older saved copies of the same key on replacement. `status` reports
configuration presence, not whether the provider will accept the key. Restart
robot nodes with `innate restart`, or stop and start the local simulator, to pick
up a change. Existing processes retain their old environment until then.

`innate keys remove openai` writes an explicit empty override and removes old
commented copies, so the next launch cannot restore an inherited OpenAI key from
the shell or `/etc/innate.env`. The same commands accept `cartesia`, `gemini`, and
`innate`. These commands preserve the configured service route; adding a direct
key never changes account routing or the selected model. A configured Innate
service route takes precedence and errors do not silently switch accounts.

## Runtime dependencies

- Direct Cartesia speech is supplied by [PR #755](https://github.com/innate-inc/innate-os/pull/755).
  It preserves Alfred (`9fdaae0b-f885-4813-b589-3c07cf9d5fea`). That PR is blocked
  on Cartesia making the voice consistently available to non-owning accounts.
  Saving a key cannot grant access to a private voice, and no substitute voice
  is selected automatically.
- The OpenAI agent and provider/model settings are supplied by
  [PR #772](https://github.com/innate-inc/innate-os/pull/772). Select `brain_provider:
  "openai"` using its documented settings. The verified model is `gpt-6-astra`,
  using the Responses API with `low` reasoning. Key presence alone does not
  select OpenAI. See [OpenAI's model guide](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra).

Missing keys leave a direct provider unconfigured; rejected credentials or model/
voice access fail that request. No live account access is proven by offline tests.
Cartesia's [voice lookup](https://docs.cartesia.ai/api-reference/voices/get) reports
whether a key can access a voice; synthesis must also succeed with the intended key.

## Public simulator

`INNATE_PUBLIC_DEMO=1` disables these credential commands and refuses generating
a simulator environment containing provider/service keys. Public demo deployments
must use the external credential relay supplied by the simulator security work.
The flag is a deployment check; isolating credentials from visitor processes is
the security boundary. Never configure a shared public session with an owner key.
