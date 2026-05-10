# RunPod GraphQL API Notes

Source:

- <https://docs.runpod.io/sdks/graphql/manage-pods>
- <https://graphql-spec.runpod.io/>

The adapter uses RunPod GraphQL because the pod lifecycle examples and runtime
port discovery are documented there.

Endpoint:

```text
POST https://api.runpod.io/graphql?api_key=<RUNPOD_API_KEY>
```

The API key is passed as a query parameter — this is RunPod's own pattern.

## Operations used by the adapter

```graphql
mutation DeployPod($input: PodFindAndDeployOnDemandInput) {
  podFindAndDeployOnDemand(input: $input) {
    id
    imageName
    machineId
  }
}
```

### Warmup vs docker pull

RunPod’s public GraphQL **`Pod`** type has **no** field for Docker pull layer progress,
pull percentage, or a dedicated “image pulling” phase. Official pod examples emphasize
**`desiredStatus`**, **`machine` / `machineId`**, and **`runtime`** (with ports) as the
observable lifecycle ([manage pods](https://docs.runpod.io/sdks/graphql/manage-pods),
[schema **Pod**](https://graphql-spec.dev.runpod.io/)).

Practical signals that the workload is **past pure scheduling** (often still **before**
**`runtime`** is populated) include:

- **`lastStartedAt`** — timestamp RunPod associates with the container start lifecycle.
- Top-level **`uptimeSeconds`** — deprecated in docs in favour of **`runtime.uptimeInSeconds`**,
  but may appear earlier than a full **`runtime`** block in some responses.
- **`machine { podHostId }`** — host binding shown in RunPod’s own deploy examples.
- **`latestTelemetry.state`** — opaque string; usefulness depends on RunPod populating it during warmup.

The adapter treats **`runtime`** (especially **public TCP ports**) as **fully ready** for
the WhisperX wrapper. Until then, it combines the fields above into a **warmup fingerprint**;
any change resets the stuck-init redeploy timer (see adapter code). Stuck-init arms once
**`machineId`** is set and **`runtime`** is still missing (see `RUNPOD_STUCK_INIT_TIMEOUT_SECONDS`
in `docs/setup.md`).

The **`pod` poll query in code** requests a minimal field set (same lifecycle signals; omits `name`,
`imageName`, telemetry `time`, and `runtime.uptimeInSeconds`) to keep payloads small.

```graphql
query Pod($input: PodFilter) {
  pod(input: $input) {
    id
    desiredStatus
    lastStatusChange
    lastStartedAt
    version
    uptimeSeconds
    machineId
    machine {
      podHostId
    }
    latestTelemetry {
      state
    }
    runtime {
      ports {
        ip
        isIpPublic
        privatePort
        publicPort
        type
      }
    }
  }
}
```

```graphql
mutation ResumePod($input: PodResumeInput!) {
  podResume(input: $input) {
    id
    desiredStatus
  }
}
```

```graphql
mutation StopPod($input: PodStopInput!) {
  podStop(input: $input) {
    id
    desiredStatus
  }
}
```

```graphql
mutation TerminatePod($input: PodTerminateInput!) {
  podTerminate(input: $input)
}
```

## Deploy input fields (PodFindAndDeployOnDemandInput)

Verified fields sent by the adapter:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | String | no | pod display name |
| `templateId` | String | no | template sets image, env, ports |
| `gpuTypeIdList` | [String] | yes | list of acceptable GPU IDs (e.g. `"NVIDIA GeForce RTX 4090"`) |
| `gpuCount` | Int | yes | |
| `cloudType` | String | **yes** | `"SECURE"` or `"COMMUNITY"` — defaults to community if omitted, **must be `"SECURE"` for reliable availability** |
| `containerDiskInGb` | Int | **yes** | **not inherited from template** — must be passed explicitly or the API fails to match machines |
| `supportPublicIp` | Boolean | no | |
| `networkVolumeId` | String | no | omit if not using a persistent volume |

### Critical findings from live testing

**`cloudType: "SECURE"` is required for reliable deploys.**
Without it the API targets community cloud which frequently has no capacity even
when the RunPod UI shows GPUs available. The UI defaults to Secure cloud.

**`containerDiskInGb` is not inherited from the template.**
Even when `templateId` is set, the template's `containerDiskInGb` value is
ignored by `podFindAndDeployOnDemand`. If omitted, the API cannot match any
machine and returns "This machine does not have the resources to deploy your pod."
Pass a value that matches (or exceeds) the template's setting.

**Deploy response does not include runtime data.**
`podFindAndDeployOnDemand` returns only `id`, `imageName`, `machineId` (and a
few other slim fields). It does not return `desiredStatus`, `runtime`, or
`ports` — those are only available via the `pod(input: {podId})` query once the
pod is running.

**`podResume` and `podStop` also return slim responses.**
Do not request `runtime { ports }` on resume/stop mutations — only `id` and
`desiredStatus` are reliably present.

## Port discovery (PodRuntimePorts)

The `runtime.ports` array contains one entry per mapped port:

```json
{
  "ip": "1.2.3.4",
  "isIpPublic": true,
  "privatePort": 9000,
  "publicPort": 54321,
  "type": "tcp"
}
```

`isIpPublic` must be checked before using `ip` — a pod may return both public
and private port entries. Only entries where `isIpPublic: true` can be reached
from outside the RunPod network.

## GPU type IDs

GPU type IDs are full strings, not short names:

| ID | Display name | VRAM |
|----|-------------|------|
| `NVIDIA GeForce RTX 4090` | RTX 4090 | 24 GB |
| `NVIDIA GeForce RTX 3090` | RTX 3090 | 24 GB |
| `NVIDIA RTX A5000` | RTX A5000 | 24 GB |
| `NVIDIA RTX A6000` | RTX A6000 | 48 GB |

Recommended priority order for WhisperX (faster first, broadest fallback):

```
NVIDIA GeForce RTX 4090,NVIDIA GeForce RTX 3090,NVIDIA RTX A5000
```

RTX 4090 is prioritised because it is faster per job despite higher hourly rate.
RTX 3090 is the best high-availability fallback (typically "High" stock on
Secure cloud). RTX A5000 is a third option at a lower price point.

## Important schema fields

- `PodFindAndDeployOnDemandInput.templateId`
- `PodFindAndDeployOnDemandInput.gpuTypeIdList`
- `PodFindAndDeployOnDemandInput.gpuCount`
- `PodFindAndDeployOnDemandInput.cloudType`
- `PodFindAndDeployOnDemandInput.containerDiskInGb`
- `PodRuntimePorts.ip`
- `PodRuntimePorts.isIpPublic`
- `PodRuntimePorts.privatePort`
- `PodRuntimePorts.publicPort`
- `PodRuntimePorts.type`
