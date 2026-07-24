# Security

## Sensitive files

Do not commit:

- `.env`
- `cookies/`
- `outputs/`
- `.docker-config/`

`cookies.txt` is equivalent to a login credential. Treat it like a password.

## If a key leaks

If an API key appears in logs, screenshots, Git history, or shared chat:

1. Revoke or rotate the key in the provider console.
2. Update `.env` with the new key.
3. Avoid sharing `docker compose config` output because it expands `.env` values.

## Responsible use

Only process videos when you have the right to access and summarize the content.

When `--with-vision` is enabled, extracted keyframes are sent only to the configured `VISION_BASE_URL`.
With the documented defaults, both vision analysis and final summary use local Ollama. If `SUMMARY_PROVIDER=api` or
`--summary-provider api` is selected, generated visual descriptions and transcript text are sent to `OPENAI_BASE_URL`;
the original keyframe images are not sent to the summary provider.
