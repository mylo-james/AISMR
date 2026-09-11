/* User-visible activity, derived from saved events, not a checkpoint receipt. */
((root, factory) => {
  const progress = factory();
  if (typeof module === "object" && module.exports) module.exports = progress;
  else root.AISMRProgress = progress;
})(globalThis, () => {
  "use strict";
  const nodes = ["ideate", "review_ideas", "generate", "wait_assets", "edit", "wait_render", "review_final"];
  const active = new Set(["ideating", "generating", "editing"]);
  const renderEvents = new Set(["render_queued", "render_running", "render_progress", "render_callback_received", "final_moderation_checked", "render_verified", "render_failed", "final_moderation_blocked"]);
  const received = new Set(["queued", "running", "ready", "gathered"]);

  function currentEvents(run) {
    const raw = run.telemetry?.events || run.events || [];
    const events = [...raw].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
    let boundary = -1;
    for (let i = 0; i < events.length; i += 1) {
      if (["ideation_submitting", "creative_context_prepared"].includes(events[i].type) && events[i].detail?.revision === run.revision) boundary = i;
    }
    // A later plan must not inherit a render event from an earlier revision.
    if (boundary < 0 && run.revision > 1) return [];
    return events.slice(Math.max(0, boundary)).filter(event => event.detail?.revision == null || event.detail.revision === run.revision);
  }

  function latestAssets(run) {
    const assets = new Map();
    for (const asset of run?.assets || []) {
      const key = `${asset.kind}:${asset.ordinal}`;
      if (!assets.has(key) || (asset.attempt || 1) >= (assets.get(key).attempt || 1)) assets.set(key, asset);
    }
    return [...assets.values()];
  }

  const creativeRoles = {
    prepare_context: "Preparing context",
    explore_object: "Exploring object ideas",
    explore_surreal: "Exploring surreal ideas",
    curate: "Selecting the set",
    write_shots: "Writing storyboards",
    replenish: "Replenishing ideas",
    recurate: "Selecting the set again",
    repair_1: "Repairing storyboards (1/2)",
    repair_2: "Repairing storyboards (2/2)",
    validating: "Checking the plan",
    input_moderation: "Checking the object",
    plan_moderation: "Checking the plan",
  };

  const repairTargets = {
    prepare_context: "context",
    explore_object: "object ideas",
    explore_surreal: "surreal ideas",
    curate: "the selected set",
    write_shots: "storyboards",
    replenish: "replacement ideas",
    recurate: "the selected set",
    validating: "the plan",
    input_moderation: "the object",
    plan_moderation: "the plan",
  };

  function repairLabel(role, detail) {
    const attempt = detail?.attempt === 2 ? 2 : role === "repair_2" ? 2 : 1;
    return `Repairing ${repairTargets[detail?.source_role] || "storyboards"} (${attempt}/2)`;
  }

  function creativePlanSteps(run) {
    if (!run) return [];
    const latestByRole = new Map();
    for (const event of currentEvents(run)) {
      if (!["creative_step_started", "creative_step_completed", "creative_step_validated"].includes(event.type)) continue;
      const role = event.detail?.role;
      if (!Object.hasOwn(creativeRoles, role)) continue;
      const isRepair = role === "repair_1" || role === "repair_2";
      latestByRole.set(role, {
        role,
        label: isRepair ? repairLabel(role, event.detail) : creativeRoles[role],
        state: event.type === "creative_step_validated" ? "Validated" : event.type === "creative_step_completed" ? "Received" : run.status === "ideating" ? "Submitted" : "Unconfirmed",
        operation: event.detail?.operation,
        counts: event.detail?.counts,
      });
    }
    return [...latestByRole.values()];
  }

  function project(run) {
    if (!run) return { index: 0, activity: "", working: false, source: "status" };
    const events = currentEvents(run);
    const latest = (types) => [...events].reverse().find(event => types.has(event.type));
    const statusIndex = { ideating: 0, idea_review: 1, plan_complete: 1, generating: 2, editing: 4, final_review: 6, video_complete: 6 };
    const exact = nodes.indexOf(run.graph?.current_node || run.graph?.current_node_id);
    let index = statusIndex[run.status] ?? (run.final ? 6 : run.assets?.length ? 2 : run.plan ? 1 : 0);
    let source = "status";
    if (exact >= 0 && active.has(run.status)) { index = exact; source = "checkpoint"; }
    let activity = { ideating: "Preparing your plan", idea_review: "Waiting for your plan review", plan_complete: "Ideas saved", generating: "Starting media tasks", editing: "Assembling your video", final_review: "Waiting for your video review", video_complete: "Complete", cancelled: "Cancelled", blocked: "Needs attention", failed: "Needs attention", submission_unknown: "Paused: result unconfirmed" }[run.status] || "Run status not observed";

    if (run.status === "ideating") {
      const planSteps = creativePlanSteps(run);
      const working = planSteps.filter(step => step.state === "Submitted");
      if (working.length) activity = working.length > 1 ? "Exploring ideas" : working[0].label;
      else if (planSteps.some(step => step.state === "Received")) activity = planSteps.some(step => step.role.startsWith("repair_") && step.state === "Received") ? "Checking corrections" : "Checking agent results";
    }

    if (run.status === "generating") {
      if (run.mode === "local") activity = "Loading saved Teacup media";
      const assets = latestAssets(run);
      const videos = assets.filter(asset => asset.kind === "video");
      const narration = assets.find(asset => asset.kind === "voice_batch");
      const submitted = videos.length === 12 && new Set(videos.map(asset => asset.ordinal)).size === 12 && narration && [...videos, narration].every(asset => received.has(asset.status));
      if (submitted || latest(new Set(["assets_gathering", "assets_ready"]))) {
        index = 3;
        source = submitted ? "assets" : "event";
        const ready = videos.filter(asset => ["ready", "gathered"].includes(asset.status)).length;
        activity = ready === 12 && narration && ["ready", "gathered"].includes(narration.status) ? "Checking the finished assets" : run.mode === "local" ? "Checking saved scenes and title recordings" : "Generating scenes and narration";
      }
    }

    const render = latest(renderEvents);
    if (run.status === "editing" && render) {
      index = 5;
      source = "event";
      activity = render.type === "render_progress" ? renderPhases[render.detail?.phase] || "Rendering your video" : render.type === "render_queued" ? "Waiting for the renderer" : render.type === "render_running" ? "Rendering your video" : ["render_callback_received", "final_moderation_checked", "render_verified"].includes(render.type) ? "Checking the render result" : "Checking a render problem";
    }
    const phase = latest(new Set(["workflow_phase"]));
    const phaseStageMatches = phase && (run.status === "generating" ? ["media_submission", "media_gathering", "narration_assembly"].includes(phase.detail?.role) : run.status === "editing" && ["render_assembly", "renderer", "final_media_verification", "final_moderation", "public_eligibility"].includes(phase.detail?.role));
    if (phaseStageMatches && phase.sequence >= (render?.sequence ?? 0) && Object.hasOwn(workflowPhases, phase.detail?.role)) {
      activity = workflowPhases[phase.detail.role];
      if (run.mode === "local" && phase.detail.role === "media_submission") activity = "Loading saved Teacup media";
      if (run.mode === "local" && phase.detail.role === "media_gathering") activity = "Checking saved scenes and title recordings";
      if (phase.detail.role === "media_gathering") index = 3;
      if (["final_media_verification", "final_moderation", "public_eligibility"].includes(phase.detail.role)) index = 5;
    }
    if (["failed", "blocked", "submission_unknown", "cancelled"].includes(run.status) && !run.final) {
      if (render) { index = 5; source = "event"; }
      else if (latest(new Set(["render_submitting", "render_unavailable"]))) { index = 4; source = "event"; }
    }
    if (run.review_expired && ["idea_review", "final_review"].includes(run.status)) activity = "Review expired";
    return { index, activity, working: active.has(run.status), source };
  }

  const renderPhases = {
    queued: "Waiting for the renderer", preparing: "Preparing media and audio",
    composition: "Preparing the video composition", frames: "Rendering video frames",
    encoding: "Encoding the video", verification: "Verifying the rendered video",
    complete: "Checking the render result", failed: "Checking a render problem",
  };
  const workflowPhases = {
    media_submission: "Submitting media requests", media_gathering: "Checking media assets",
    narration_assembly: "Preparing narration clips", render_assembly: "Assembling the edit",
    renderer: "Rendering your video", final_media_verification: "Checking the final video file",
    final_moderation: "Checking final video content", public_eligibility: "Checking sharing eligibility",
  };
  const agentNames = { explore_object: "Object explorer", explore_surreal: "Surreal explorer", curate: "Curator", write_shots: "Storyboard writer", replenish: "Concept explorer", recurate: "Curator", repair_1: "Repair agent", repair_2: "Repair agent" };
  function pollFailure(error) {
    return { connectionLost: error?.network === true, stop: error?.definitive === true, message: error?.message || "The status update could not be loaded." };
  }
  function workStatus(run, { connectionLost = false, shared = false, updateIssue = null } = {}) {
    if (!run) return null;
    const projection = project(run), events = currentEvents(run), last = events.at(-1);
    const state = {
      ideating: ["Working", "working"], generating: ["Working", "working"], editing: ["Working", "working"],
      idea_review: ["Waiting for you", "waiting"], final_review: ["Waiting for you", "waiting"],
      plan_complete: ["Complete", "complete"], video_complete: ["Complete", "complete"],
      failed: ["Needs attention", "error"], blocked: ["Needs attention", "error"],
      submission_unknown: ["Result unconfirmed", "error"], cancelled: ["Cancelled", "neutral"],
    }[run.status] || ["Status unavailable", "neutral"];
    let [label, tone] = state, task = projection.activity, detail = "", meter = null;
    if (run.status === "ideating") {
      const submitted = creativePlanSteps(run).filter(step => step.state === "Submitted");
      detail = submitted.map(step => agentNames[step.role] || "Workflow check").join(" · ");
      if (submitted.some(step => agentNames[step.role])) detail += " · awaiting result";
    } else if (run.status === "generating") {
      const assets = latestAssets(run), ready = asset => ["ready", "gathered"].includes(asset.status);
      const videos = assets.filter(asset => asset.kind === "video" && ready(asset)).length;
      const voices = assets.filter(asset => asset.kind === "voice" && ready(asset)).length;
      if (assets.length) detail = `${videos}/12 scenes ready · ${voices}/12 recordings ready`;
    } else if (run.status === "editing") {
      const latestRender = [...events].reverse().find(event => renderEvents.has(event.type));
      const latestPhase = [...events].reverse().find(event => event.type === "workflow_phase");
      if (latestRender?.type === "render_progress" && !(latestPhase?.sequence > latestRender.sequence)) {
        const d = latestRender.detail || {};
        if (["frames", "encoding"].includes(d.phase) && Number.isFinite(d.progress) && d.progress >= 0 && d.progress <= 1) {
          meter = Math.floor(d.progress * 100);
          const counter = d.phase === "frames" ? d.rendered_frames : d.encoded_frames;
          const verb = d.phase === "frames" ? "rendered" : "encoded";
          detail = Number.isInteger(counter) && Number.isInteger(d.total_frames) && d.total_frames > 0 && counter >= 0 && counter <= d.total_frames ? `${counter.toLocaleString()}/${d.total_frames.toLocaleString()} frames ${verb}` : "Renderer progress";
          if (d.stitch_stage === "muxing") { task = "Combining video and audio"; detail = "Renderer finishing the file"; meter = null; }
        }
      }
    } else if (run.status === "idea_review") task = "Storyboards ready for review";
    else if (run.status === "final_review") task = "Video ready for review";
    else if (run.status === "video_complete") task = "Video accepted";
    else if (run.status === "plan_complete") task = "Ideas saved";
    if (shared && tone === "waiting") label = "Waiting for review";
    if (run.review_expired && tone === "waiting") { label = "Review expired"; tone = "neutral"; task = "This review is closed"; }
    if (connectionLost) { label = "Updates paused"; tone = "neutral"; task = "Reconnecting…"; detail = "Last recorded state is shown below."; meter = null; }
    else if (updateIssue) { label = updateIssue.stop ? "Updates unavailable" : "Updates delayed"; tone = "neutral"; task = updateIssue.stop ? "Refresh to check this run" : "Retrying the status check…"; detail = updateIssue.message; meter = null; }
    return { label, tone, task, detail, meter, working: projection.working && !connectionLost && !updateIssue, updatedAt: last?.occurred_at || last?.created_at || null };
  }
  return { project, latestAssets, creativePlanSteps, workStatus, renderPhases, workflowPhases, pollFailure };
});
