# AuthHub Tripo3D — 100% Parity Patch Specification (A–F)

> **Status:** Implementation-ready spec. Could NOT be applied from this dispatch because
> the engineering session was sandboxed to `C:\AiAppDeployments\Hydra` and the target repo
> `C:\AiAppDeployments\AuthHub` is outside the sandbox (all file tools hard-blocked there).
> Re-dispatch the engineering squad with **cwd = `C:\AiAppDeployments\AuthHub`** (register it
> in `~/.hydra/repos.json` / `HYDRA_EXTRA_REPOS`) and apply the changes below verbatim.
>
> Grounded in the official **Tripo3D OpenAPI v2** (`https://api.tripo3d.ai/v2/openapi`) and the
> approved plan Part 1. Constraints honored: **no change** to param passthrough, the 16 task
> types, model-version enums (except the additive B), or existing balance/upload/models/status/download routes.

---

## Tripo3D OpenAPI v2 ground truth (field shapes the parity depends on)

| Endpoint | Method | Success body |
|---|---|---|
| `/task` | POST | `{ "code": 0, "data": { "task_id": "<uuid>" } }` |
| `/task/{task_id}` | GET | `{ "code": 0, "data": { "task_id", "type", "status", "progress", "input", "output": {...}, "create_time" } }` |
| `/user/balance` | GET | `{ "code": 0, "data": { "balance": <num>, "frozen": <num> } }` |
| `/upload` | POST | **multipart/form-data**, field `file`. → `{ "code": 0, "data": { "image_token": "<token>" } }` |
| `/upload/sts/token` | POST | `{ "code": 0, "data": { /* temp STS credentials + resource handle */ } }` |

Key parity gap: Tripo's create endpoint returns **`task_id`**, and every downstream call
(`/task/{task_id}`) keys on that exact name. AuthHub currently surfaces it as `id`, breaking
round-trips against the official `tripo3d_api_ui` app and any official-SDK consumer.

---

## (F) Standardized response envelope — do this FIRST (A/status/balance/models depend on it)

Define one helper and use it in every Tripo3D controller handler. **Verify AuthHub's existing
`ApiResponse<T>` convention first** — if one exists, conform to it instead of introducing a new shape.

`authhub-api/src/modules/tripo3d/tripo3d.envelope.ts` (new, or fold into existing util):
```ts
export interface TripoEnvelope<T> {
  success: boolean;
  data?: T;
  error?: { code: number | string; message: string };
}

export const ok = <T>(data: T): TripoEnvelope<T> => ({ success: true, data });
export const fail = (code: number | string, message: string): TripoEnvelope<never> =>
  ({ success: false, error: { code, message } });
```
Apply to `create3DTaskHandler`, `getTaskStatusHandler`, `getBalanceHandler`, `getModelsHandler`.
Do **not** alter the request param passthrough — only wrap the response.

---

## (A) `create3DTaskHandler` must return `task_id`

`authhub-api/.../ai-3d.controller.ts` — in `create3DTaskHandler`, after the adapter call:
```ts
// adapter returns Tripo's data: { task_id }
const { task_id } = result;            // was: const { id } = ...
return res.status(200).json(ok({
  task_id,                             // PRIMARY — matches Tripo + tripo3d_api_ui
  id: task_id,                         // back-compat alias; remove once consumers migrate
}));
```
Mirror the field across the SDKs so create→status round-trips:
- `packages/sdk-typescript/src/tripo3d/types.ts` — `interface CreateTaskResponse { task_id: string; id?: string }`.
- `packages/sdk-typescript/src/tripo3d/client.ts` — `create3DTask()` returns `task_id`; have status/animate helpers accept `taskId` sourced from `task_id`.
- `packages/sdk-python/authhub/tripo3d/` — `CreateTaskResponse.task_id: str` (Pydantic/dataclass); `create_3d_task()` returns `task_id`.

---

## (B) Add `animate_rig` version `v2.0-20250506`

`authhub-api/src/modules/tripo3d/tripo3d-models.ts`:
```ts
export const TRIPO_ANIMATE_RIG_MODEL_VERSIONS = [
  // ...existing versions UNCHANGED...
  "v2.0-20250506",
] as const;
```
Mirror in:
- `authhub-api/src/.../types/tripo3d.types.ts` — extend the `AnimateRigModelVersion` union with `"v2.0-20250506"`.
- `TRIPO3D-API-COVERAGE.md` — add `v2.0-20250506` to the animate_rig coverage row.

Do not touch any other model-version enum.

---

## (C) `uploadFile` / `uploadFileSTS` must send `multipart/form-data` (not base64-JSON)

**TypeScript** `packages/sdk-typescript/src/tripo3d/client.ts`:
```ts
async uploadFile(file: Buffer | Blob, filename: string): Promise<{ image_token: string }> {
  const form = new FormData();
  // Node 18+: use Blob/File; or `form-data` pkg if targeting older Node.
  form.append("file", file instanceof Buffer ? new Blob([file]) : file, filename);
  const res = await this.http.post("/upload", form);   // let runtime set multipart boundary
  return res.data.data;                                 // { image_token }
}
```
`uploadFileSTS` → same multipart pattern against the STS upload endpoint. Remove the
base64 string + `application/json` body entirely. Do **not** hand-set `Content-Type` (the
boundary must be auto-generated).

**Python** `packages/sdk-python/authhub/tripo3d/client.py`:
```python
def upload_file(self, file_path: str) -> dict:
    with open(file_path, "rb") as fh:
        resp = self._session.post(f"{self.base}/upload", files={"file": fh})  # multipart
    return resp.json()["data"]            # {"image_token": ...}
```
Use `files=` (requests) / multipart for `httpx`. Never send base64 JSON.

---

## (D) Expose STS token

`ai-3d.controller.ts`:
```ts
export async function getSTSTokenHandler(req: Request, res: Response) {
  const data = await tripo3dAdapter.getSTSToken(req.body /* passthrough unchanged */);
  return res.status(200).json(ok(data));
}
```
`ai.routes.ts`:
```ts
router.post("/api/v1/ai/3d/upload/sts/token", getSTSTokenHandler);
```
SDK methods: `client.getSTSToken()` (TS) and `get_sts_token()` (Python) → `POST /upload/sts/token`.

---

## (E) Generic Tripo passthrough escape hatch

`tripo3d.adapter.ts`:
```ts
async tripoPassthrough(method: string, subPath: string, body?: unknown, query?: Record<string,string>) {
  // subPath is the part after /v2/openapi/ ; forward verbatim with auth header
  return this.http.request({ method, url: `/${subPath}`, data: body, params: query });
}
```
`ai-3d.controller.ts`:
```ts
export async function tripoPassthroughHandler(req: Request, res: Response) {
  const subPath = req.params[0];                  // captured by the wildcard route
  const data = await tripo3dAdapter.tripoPassthrough(req.method, subPath, req.body, req.query);
  return res.status(200).json(ok(data));
}
```
`ai.routes.ts`:
```ts
router.all("/api/v1/ai/3d/raw/*", tripoPassthroughHandler);
```
SDK escape hatch: `client.tripoRaw(method, subPath, { body, query })` (TS) / `tripo_raw(...)` (Python).
Keep auth injection + base URL centralized; do not re-validate params (it's a passthrough).

---

## Build & test (run AFTER applying, in the AuthHub repo)

```bash
# from C:\AiAppDeployments\AuthHub
npm run build            # @authhub/sdk + api
npm test                 # or: pnpm -r test / npm run test -w authhub-api
```
Expected new/updated coverage:
- create→status round-trip asserts response carries `task_id`.
- `animate_rig` accepts `v2.0-20250506`.
- upload sends `multipart/form-data` (assert request `Content-Type` startsWith `multipart/form-data`; no base64 in body).
- `POST /api/v1/ai/3d/upload/sts/token` returns 200 + STS payload.
- `ALL /api/v1/ai/3d/raw/*` forwards verbatim and returns Tripo's body.
- envelope shape identical across create/status/balance/models.

---

## Integration caveats to verify against live AuthHub code (could not be read from this session)

1. Exact existing envelope/`ApiResponse<T>` helper name — conform to it if present.
2. Adapter HTTP client symbol (`this.http` vs axios instance vs fetch wrapper).
3. Whether `id` is still consumed anywhere → keep the alias until those callers migrate.
4. Node version for `FormData`/`Blob` (native ≥18 vs `form-data` package).
5. Router base-path mounting (whether `/api/v1/ai` is a prefix already applied at mount).
