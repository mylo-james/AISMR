import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

// Basic perf smoke for /v1/runs/start + webhook fan-in.
// Defaults are intentionally light so it can run on laptops:
//   VUS=5 ITERATIONS=20 p95 targets: start<800ms, remotion webhook<500ms.
// Configure with env vars:
//   BASE_URL=http://localhost:8000 API_KEY=dev-api-key k6 run scripts/perf/runs_and_webhooks.js

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const API_KEY = __ENV.API_KEY || "dev-api-key";
const WORKFLOW = __ENV.WORKFLOW || "aismr";
const BRIEF = __ENV.BRIEF || "Perf sanity brief for AISMR";

const startTrend = new Trend("runs_start_duration", true);
const remotionTrend = new Trend("remotion_webhook_duration", true);

export const options = {
  vus: Number(__ENV.VUS || 5),
  iterations: Number(__ENV.ITERATIONS || 20),
  thresholds: {
    "runs_start_duration{endpoint:start}": ["p(95)<800"],
    "remotion_webhook_duration{endpoint:remotion}": ["p(95)<500"],
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  const headers = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
  };

  const startRes = http.post(
    `${BASE_URL}/v1/runs/start`,
    JSON.stringify({ workflow: WORKFLOW, brief: BRIEF }),
    { headers }
  );
  startTrend.add(startRes.timings.duration, { endpoint: "start" });
  check(startRes, { "start 200": (r) => r.status === 200 });

  const runId = startRes.json("run_id");
  if (!runId) {
    return;
  }

  // Simulate clip delivery (Sora) to advance workflow
  const soraRes = http.post(
    `${BASE_URL}/v1/webhooks/sora?run_id=${runId}`,
    JSON.stringify({
      code: 200,
      msg: "success",
      data: {
        taskId: `video_${__VU}_${__ITER}`,
        state: "success",
        info: { resultUrls: [`https://cdn.example.com/video-${__VU}-${__ITER}.mp4`] },
        metadata: { videoIndex: 0 },
      },
    }),
    {
      headers: {
        "Content-Type": "application/json",
      },
    }
  );
  check(soraRes, { "sora accepted": (r) => r.status === 200 });

  const remotionRes = http.post(
    `${BASE_URL}/v1/webhooks/remotion?run_id=${runId}`,
    JSON.stringify({
      status: "done",
      output_url: `https://cdn.example.com/render-${__VU}-${__ITER}.mp4`,
      id: `job-${__VU}-${__ITER}`,
    }),
    {
      headers: {
        "Content-Type": "application/json",
        "X-Remotion-Signature": "",
      },
    }
  );
  remotionTrend.add(remotionRes.timings.duration, { endpoint: "remotion" });
  check(remotionRes, {
    "remotion accepted": (r) => r.status === 200 || r.status === 202,
  });

  sleep(Number(__ENV.SLEEP || 0.5));
}
