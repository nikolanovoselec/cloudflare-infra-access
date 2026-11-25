# Cloudflare Infrastructure Access alpha reproducer

This repository reproduces the Workers VPC Gateway Identity alpha upload. It also has an optional, unattended Container TCP probe. It does not change or deploy Codeflare.

## Intended SSH experience

With Cloudflare One connected and the hostname route and Access policy already configured, the user runs `ssh -p 22 abc` (or just `ssh abc` for port 22). They do not enter Cloudflare target, network, application, or policy IDs, or type their WARP identity into the app. Cloudflare evaluates the connection against the configured routing and policy.

For a hosted container, the alpha example gets the signed-in user's email from verified Access claims and passes it to `newFetcher(email)` on the Worker side. That is not a user input. This repository has not verified the full hostname-based SSH flow from a container; its automated TCP probe checks only a greeting at one fixed IP.

## Run the diagnostic

1. In **Settings → Secrets and variables → Actions**, set **`CLOUDFLARE_ACCOUNT_ID`** to the 32-character hexadecimal test account ID and **`CLOUDFLARE_API_TOKEN`** to a dedicated, short-lived token restricted to that account. Do not paste the token into an issue, commit, or chat. The managed session credential from the original attempt is not portable to GitHub Actions.
2. In **Actions → Test VPC Identity Alpha Upload and Container TCP**, select **Run workflow** on `main`. Choose `upload` first. The `runtime` option is a maintainer-only CI check, not the user SSH flow. Run it only after upload succeeds and only with the approved test fixture configured. Pushes run mocked tests only; each manual run attempts at most one multipart upload.
3. Open **diagnose → Run selected Cloudflare diagnostic**. The log shows each stage, HTTP status, Cloudflare error code and allowlisted message (including `10015`), CF Ray and timestamp. Runtime results and cleanup proof are reported separately. Share the run link or the `cloudflare-alpha-evidence` artifact. Neither includes the account ID or token. Check cleanup status if the job fails or is interrupted; the run records the generated resource names. Revoke the dedicated token after diagnosis.

| Mode | What it does | Account-level token permissions |
|---|---|---|
| `upload` (default) | Uploads the Worker with the alpha binding and Container DO declaration, without enabling a URL, creating a Container app, or calling Zero Trust. Verifies URL state and retires the Worker after the test. | **Workers Scripts → Edit** for the Workers API calls. The SQLite DO migration is part of the upload; no separate DO grant is documented for that call. |
| `runtime` | Checks the existing fixed Infrastructure target, app and policy. If the fresh upload is admitted, associates one temporary Container app, enables a short-lived protected endpoint, runs one bounded TCP probe, and verifies retirement of both resources. | **Workers Scripts → Edit**, **Containers → Edit**, **Zero Trust → Read**, **Access: Apps and Policies → Read**. No policy or routing writes. |

Scope the token to the test account only. These are permissions for the workflow's documented API calls, not a verified minimum for the alpha binding. Ask Cloudflare whether the upload requires an additional VPC or Containers grant or account enablement. Keep write access restricted to people trusted to modify workflows using the deployment token. The repository owner can dispatch the run and give engineers read access to the result.

## Optional CI fixture (maintainers only)

GitHub Actions has no signed-in Cloudflare One user from whom it can derive an identity. For the optional `runtime` check, the maintainer provisions `CLOUDFLARE_RUNTIME_CONFIG` with an approved fixed identity, private IP and existing target/policy references. The runner checks that the fixture still matches before it sends traffic. This secret is not needed for `upload` or for ordinary SSH over Cloudflare One. The runner does not create or modify policy.

## Deployment credential and scopes

- **Failed upload below:** used the original session's managed Cloudflare OAuth credential. The effective scopes and selected OAuth tier were not recorded, so the actual grants are **unknown**. The OAuth configuration requests `workers-scripts.write` in every tier and `connectivity-directory.admin` only in Advanced. These are requested scopes, not proof of the grant. Successful read requests do not establish write permission or alpha entitlement.
- **GitHub Actions:** the upload-only run below used a dedicated Cloudflare API token stored in `CLOUDFLARE_API_TOKEN`. Its exact grants were not recorded. The workflow did not make any Zero Trust, Access or Containers management calls.

A missing permission is plausible, but **10015 does not distinguish it from account-level alpha enablement or another admission prerequisite**. Please confirm the required permissions and whether both OAuth credentials and dedicated API tokens can upload `cf1:network` with `identity: runtime-email-alpha`.

## Observed failure

| Attempt | UTC (2026-11-25) | Result | CF Ray |
|---|---|---|---|
| GitHub Actions, upload only (historical run removed during repository cleanup) | 11:03:20 | HTTP 401 / API error 10015 | `a6002a8dbb892d0a-IAD` |
| Standalone Container DO upload | 10:37:06 | HTTP 401 / API error 10015 | `a6000425fac4323a-ATH` |

> You do not have access to use Workers VPC Gateway Identity. Please ensure it is enabled. If you are an Enterprise user, reach out to your account team.

Both attempts stopped at upload. The GitHub run verified the Worker was absent both before and after the request (HTTP 404 / API error 10007). No Container app was created, no endpoint was enabled and no runtime traffic was sent. Can Cloudflare confirm alpha admission for the target account and identify any missing token permission or upload field? The timestamp and CF Ray above are sufficient to look up the rejected request.

## Upload shape

The multipart request uses compatibility date `2026-07-09`, flags `nodejs_compat` and `experimental`, and a dedicated SQLite Container Durable Object. In the Workers API metadata, the alpha binding is a top-level `bindings` entry (equivalent to `unsafe.bindings` in the [pinned Wrangler reference](https://github.com/nouvellonsteph/claudeflare-code/blob/8ea3267a37964cbb4515a849ab92e09a04fa9567/wrangler.jsonc)):

```json
{"name":"GATEWAY_IDENTITY","type":"vpc_network","network_id":"cf1:network","identity":"runtime-email-alpha"}
```

In `upload` mode the HTTP handler has no probe credentials and denies all requests. In `runtime` mode the runner verifies the configured, fixed account resources before uploading and will stop if the target or Access policy no longer matches. The runner generates a five-minute bearer in memory, stores only its digest in the temporary Worker, verifies an unauthenticated request is rejected, then calls the endpoint once. The Container DO passes the bound fetcher to native `interceptOutboundTcp` before startup, leaves direct internet disabled and reads only whether the fixed private service returns an SSH identification line. It does not log in, run commands, accept caller-supplied targets, or fall back to direct TCP.

Upload success proves admission only. Runtime success means registration, TCP connectivity, an SSH greeting, container shutdown and resource cleanup were reported. It does not prove SSH authentication, device posture, or the identity recorded by Gateway. Correlate the run timestamp with Gateway logs to check identity and policy separately.
