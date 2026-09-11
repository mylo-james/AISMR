(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const state = { config: null, session: null, run: null, selected: "ideate", follow: true, share: params.get("share"), poll: null, pending: false, pendingAction: "", connectionLost: false, updateIssue: null, allIdeas: false, ideaIndex: 0, planIdentity: "", planKey: "", assetsKey: "", activityKey: "", revisionOpen: false, revisionIdentity: "", selection: 0 };
  const steps = [
    { id: "ideate", title: "Plan ideas", purpose: "Turn one object into a structured plan.", note: "Start here", aside: "Twelve original scenes to review.", about: "Live planning can use a single ideator or two concept explorers, a curator and a shot writer. The creative planner uses recent concepts and revision feedback from your session. Sample mode uses fixed examples. Every plan is saved before review.", action: "Preparing and checking twelve ideas. Video and narration have not started." },
    { id: "review_ideas", title: "Review ideas", purpose: "Check the ideas and approve to continue.", note: "Your decision", aside: "Review and approve the plan.", about: "LangGraph pauses at this review. Your decision is tied to the exact plan revision on screen. Approve to begin production or request a revised plan.", action: "Review the twelve variations. Video and narration start after your approval." },
    { id: "generate", title: "Generate assets", purpose: "Create video scenes and one narration batch.", note: "Video and narration", aside: "Provider tasks can run together.", about: "One graph node coordinates a service that submits twelve video requests and one narration batch concurrently. The narration is split into twelve measured sections. These provider tasks are not separate LangGraph nodes.", action: "Video scenes and narration are being prepared. Each task below shows its last observed state." },
    { id: "wait_assets", title: "Wait for assets", purpose: "Pause until asset generation is complete.", note: "Wait for providers", aside: "Keep the run while background work finishes.", about: "The graph can pause while providers work. Recorded assets and receipts are checked before the workflow continues, so a worker can resume the run without starting from the beginning.", action: "The workflow is waiting for verified assets. You can leave and return to this run." },
    { id: "edit", title: "Assemble video", purpose: "Combine the pieces into one finished video.", note: "Build the video", aside: "Arrange scenes, narration, music and labels.", about: "The editor combines the twelve ordered video scenes with matching narration, music, month labels and object labels. The render request is recorded before waiting for its result.", action: "The editor is assembling the ordered scenes and narration into one finished video." },
    { id: "wait_render", title: "Render video", purpose: "Render the final video and check the result.", note: "Render and verify", aside: "Wait for a finished, checked artifact.", about: "The graph pauses for rendering. The application verifies the returned video and required checks before it offers a final review. A callback or worker wake-up continues the stored run.", action: "The workflow is waiting for the finished video and its verification results." },
    { id: "review_final", title: "Review result", purpose: "Watch the video and decide whether to share it.", note: "Finish", aside: "Review the result and close the run.", about: "The final review is another human decision. Accepting and showing the result grants Recent creations permission for this exact video. Playback alone does not grant that permission.", action: "Watch the finished video. You decide whether to accept it and show it in Recent creations." },
  ];
  const recordedAbout = {
    ideate: "Load the archived Teacup plan into a new saved workflow. No agent is generating ideas now.",
    review_ideas: "The workflow pauses for your approval of this exact saved plan.",
    generate: "Load references to twelve saved video scenes and twelve narration sections. No provider requests are made.",
    wait_assets: "Check that the asset references match the approved plan and pinned archive manifest.",
    edit: "Select the existing final video associated with the approved plan and assets. No new render starts.",
    wait_render: "Read the saved final from storage and verify its size and content hash before review.",
    review_final: "Watch the recorded result and accept it to complete your walkthrough. This does not post or add it to a gallery."
  };
  const activeStatuses = new Set(["ideating", "generating", "editing"]);
  const knownStates = { pending: "Not started", not_started: "Not started", complete: "Complete", completed: "Complete", done: "Complete", waiting: "Waiting", interrupted: "Paused", active: "In progress", running: "In progress", coarse: "In this stage", unknown: "Not observed", blocked: "Needs attention", failed: "Stopped", stopped: "Stopped" };
  const errors = { visitor_allowance_exhausted: "You've used your run allowance for this 24-hour window. Recent creations are still available.", ip_allowance_exhausted: "This network has reached its run allowance. Try again later, or explore Recent creations.", studio_busy: "All workflow slots are occupied, including runs waiting for review. Finish or cancel an open run, then try again.", invalid_item: "Enter one short object name, up to 48 characters, without a link or line break.", stale_review: "This review changed. Refresh the run before deciding.", revision_allowance_exhausted: "This run has used its plan revision allowance. Review the current plan or cancel the run.", session_required: "Refresh the page to restore your visitor session.", input_moderation_blocked: "This object did not pass the content check. Try another object.", budget_exhausted: "The studio has reached its generation budget. You can still explore recent results.", csrf_required: "Refresh the page to restore your visitor session before deciding.", session_expired: "This session or review expired. The stored workflow is available for inspection, but this decision can no longer be changed.", recorded_item_required: "This recorded session can replay its recorded object only. New objects need live generation.", studio_disabled: "Generation is unavailable. You can still explore the workflow and Recent creations.", live_runtime_not_ready: "New generation isn't available yet. You can still explore the workflow and saved results.", gallery_consent_required: "Confirm acceptance using the button that also names Recent creations.", public_receipts_required: "Required public-use checks are not available for this video. It remains private for review.", share_expired: "This shared view has expired.", run_not_found: "This run isn't available in your current session.", generation_paused: "New generation is paused. Recent creations remain available.", decision_expired: "This review has expired. Refresh to see its recorded state." };
  Object.assign(errors, {local_runtime_not_ready: "The local workflow is not ready. Refresh after its saved media and renderer are available.", local_media_archive_invalid: "The saved media bundle is unavailable. This run has not requested new media.", local_scene_asset_provenance_mismatch: "The saved media did not match this run’s recorded source details. The run stopped before rendering. Start a new workflow after the source details are corrected.", local_media_receipt_mismatch: "The saved media bundle changed after this run started. Start a new run once the bundle is restored.", network_allowance_exhausted: errors.ip_allowance_exhausted, admissions_paused: errors.generation_paused, ideas_review_expired: errors.decision_expired, final_review_expired: errors.decision_expired, gallery_eligibility_unverified: errors.public_receipts_required, creative_plan_invalid: "The agents could not produce a complete, valid set of scenes. Start a new run to try another set.", creative_repair_exhausted: "The agents exhausted their allowed corrections without producing a valid set of scenes. Start a new run to try another set.", creative_submission_unknown: "The agent did not return a confirmed result. This run is stopped. Choose New run to make a fresh set.", planner_version_unavailable: "This saved plan uses a planning version that is not available for a new revision. Start a new plan to continue.", unresolved_cost_amount_missing: "Generation is paused while an earlier cost is reconciled. Recent creations remain available.", render_rejected: "The renderer no longer has a valid receipt for this request. The run stopped without starting another render.", render_receipt_unknown: "The render receipt could not be confirmed. The run is paused for reconciliation, and no new render was submitted.", render_result_unverified: "The returned video did not include the verification needed for review. No new render was submitted."});
  const text = (node, value) => { node.textContent = value == null ? "" : String(value); return node; };
  const el = (tag, content, cls) => { const node = document.createElement(tag); if (cls) node.className = cls; if (content != null) text(node, content); return node; };
  const show = (id, visible) => { $(id).hidden = !visible; };
  const uuid = () => crypto.randomUUID();
  const remember = (name) => { try { let value = sessionStorage.getItem(name); if (!value) { value = uuid(); sessionStorage.setItem(name, value); } return value; } catch { return uuid(); } };
  const forget = (name) => { try { sessionStorage.removeItem(name); } catch {} };
  const localUrl = (value) => { try { const url = new URL(value, location.origin); return value && url.origin === location.origin && /^https?:$/.test(url.protocol) ? url.href : null; } catch { return null; } };
  const api = async (path, options = {}) => {
    const method = options.method || "GET", headers = new Headers(options.headers || {});
    if (options.body) headers.set("Content-Type", "application/json");
    if (method !== "GET" && path !== "/v1/studio/session") headers.set("X-CSRF-Token", state.session?.csrf || "");
    let response;
    try { response = await fetch(path, { ...options, method, headers, credentials: "same-origin" }); }
    catch { const error = new Error("The connection was interrupted. Refresh this run to check whether your request was received."); error.network = true; throw error; }
    let body; try { body = await response.json(); } catch {}
    if (!response.ok) { const code = body?.error || body?.code; const error = new Error(errors[code] || (typeof body?.message === "string" ? body.message : `The request could not be completed (${response.status}). Refresh and try again.`)); error.code = code; error.definitive = response.status >= 400 && response.status < 500 && ![408, 409, 425, 429].includes(response.status); throw error; }
    return body;
  };
  const mode = () => state.run?.mode || state.config?.mode;
  const isPlanning = () => mode() === "planning";
  const isLocal = () => mode() === "local";
  const localStageTitles = { ideate: "Generate ideas", review_ideas: "Review storyboards", generate: "Load saved media", wait_assets: "Check assets" };
  const recordedTitles = {ideate: "Load saved plan", review_ideas: "Review plan", generate: "Load saved assets", wait_assets: "Check saved assets", edit: "Select saved result", wait_render: "Verify saved result", review_final: "Review result"};
  const stageTitle = (step) => isRecordedReuse() ? recordedTitles[step.id] || step.title : isLocal() ? localStageTitles[step.id] || step.title : isPlanning() && step.id === "ideate" ? "Generate ideas" : isPlanning() && step.id === "review_ideas" ? "Agent review" : step.title;
  const isRecordedReuse = () => Boolean(state.run?.recorded_final_reuse ?? state.config?.recorded_final_reuse);
  const canAcceptFinal = () => isRecordedReuse() || ["fixture", "local"].includes(mode()) || state.run?.gallery_eligibility?.eligible === true;
  const canGenerate = () => Boolean(state.config?.enabled && state.config?.mode !== "recorded" && state.config?.generation?.available !== false && (state.config?.mode !== "live" || state.config?.live_ready));
  const projectProgress = (run) => {
    const project = window.AISMRProgress?.project;
    if (typeof project !== "function") throw new Error("AISMR progress projection is unavailable.");
    return project(run);
  };
  const latestAssets = (run) => window.AISMRProgress?.latestAssets?.(run) || [];
  const creativePlanSteps = (run) => window.AISMRProgress?.creativePlanSteps?.(run) || [];
  const planItems = (run = state.run) => Array.isArray(run?.plan?.scenes) ? run.plan.scenes : Array.isArray(run?.plan?.ideas) ? run.plan.ideas : [];
  const currentIndex = () => projectProgress(state.run).index;
  const nodeObservation = (id) => state.run?.graph?.nodes?.find((node) => node.id === id);
  const nodeState = (index) => {
    if (!state.run) return index === 0 ? "Start here" : "Not started";
    if (index < currentIndex()) return "Complete";
    const observed = nodeObservation(steps[index].id);
    const explicit = observed?.state || observed?.status;
    if (state.run.status === "video_complete") return "Complete";
    if (state.run.status === "plan_complete") return index < 1 ? "Complete" : index === 1 ? "Ideas saved" : "Not started";
    if (index === currentIndex() && ["failed", "blocked", "submission_unknown", "cancelled"].includes(state.run.status)) return state.run.status === "cancelled" ? "Cancelled" : "Needs attention";
    if (state.run.status === "idea_review" && index === 1 || state.run.status === "final_review" && index === 6) return state.run.review_expired ? "Review expired" : state.share ? "Review paused" : "Waiting for you";
    if (explicit && knownStates[explicit]) return knownStates[explicit];
    if (index === currentIndex() && state.updateIssue && !state.connectionLost) return "Updates unavailable";
    if (index === currentIndex() && state.connectionLost) return "Reconnecting";
    return index === currentIndex() ? "In progress" : "Not started";
  };
  const notice = (message) => { text($("run-notice"), message); show("run-notice", Boolean(message)); };
  const date = (value) => { const parsed = new Date(value); return value && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString() : "Not recorded"; };
  const receipt = (parent, rows) => { const dl = el("dl", null, "receipt"); for (const [name, value] of rows) { if (value == null) continue; dl.append(el("dt", name), el("dd", typeof value === "object" ? JSON.stringify(value) : value)); } parent.append(dl); };
  function setPendingButton(button, pending, label) {
    if (!button) return;
    if (pending) {
      if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
      text(button, label); button.classList.add("is-working");
    } else if (button.dataset.idleLabel) {
      text(button, button.dataset.idleLabel); delete button.dataset.idleLabel; button.classList.remove("is-working");
    }
  }
  function renderPendingButtons() {
    const starting = state.pending && state.pendingAction === "start";
    setPendingButton($("start-button"), starting, isPlanning() ? "Generating ideas…" : "Preparing plan…");
    document.querySelectorAll('[data-decision="approve"][data-gate="ideas"]').forEach((button) => setPendingButton(button, state.pending && state.pendingAction === "approve-ideas", isPlanning() ? "Saving ideas…" : isLocal() ? "Loading saved media…" : mode() === "fixture" ? "Starting assembly…" : "Starting production…"));
  }
  function selectStep(id, focus = false) {
    state.selected = id; state.follow = false; render();
    if (focus) { $("inspector").scrollTop = 0; $("selected-title").focus({ preventScroll: true }); }
  }
  function buildGraph() {
    const shortLabels = ["Plan", "Review", "Generate", "Assets", "Assemble", "Render", "Finish"];
    const graph = $("workflow-graph"); graph.replaceChildren();
    steps.forEach((step, index) => {
      const row = el("li", null, "graph-stage"); row.dataset.node = step.id;
      const button = el("button", null, "stage-button"); button.type = "button"; button.dataset.node = step.id; button.setAttribute("aria-controls", "node-inspector");
      const number = el("span", index + 1, "step-number"); number.setAttribute("aria-hidden", "true");
      const body = el("span", null, "stage-body"); body.append(el("strong", stageTitle(step), "stage-name"), el("span", shortLabels[index], "stage-short"));
      button.append(number, body, el("span", "Not started", "status-label")); button.addEventListener("click", () => selectStep(step.id, true)); row.append(button); graph.append(row);
    });
  }
  function renderGraph() {
    const progress = projectProgress(state.run);
    $("workflow-graph").classList.toggle("is-planning", isPlanning());
    $("workflow-graph").setAttribute("aria-label", isPlanning() ? "Two planning steps" : "Seven workflow steps");
    document.querySelectorAll(".graph-stage").forEach((row, index) => {
      row.hidden = isPlanning() && index > 1;
      const label = nodeState(index), selected = row.dataset.node === state.selected;
      const active = index === progress.index && state.run?.status !== "video_complete", working = active && progress.working && !state.connectionLost && !state.updateIssue;
      row.classList.toggle("is-current", active); row.classList.toggle("is-working", working);
      row.classList.toggle("is-selected", selected); row.classList.toggle("is-done", label === "Complete");
      const button = row.querySelector("button"); button.setAttribute("aria-pressed", String(selected));
      if (active) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current");
      text(row.querySelector(".status-label"), label);
      text(row.querySelector(".stage-name"), stageTitle(steps[index]));
      text(row.querySelector(".stage-short"), isLocal() && index === 2 ? "Load" : ["Plan", "Review", "Generate", "Assets", "Assemble", "Render", "Finish"][index]);
      button.setAttribute("aria-label", `${stageTitle(steps[index])}: ${label}${active && !state.connectionLost && !state.updateIssue && progress.activity ? `, ${progress.activity}` : ""}`);
    });
    text($("graph-footnote"), ""); show("graph-footnote", false);
  }
  function eventStage(event) {
    const subject = event.subject || event.stage;
    if (steps.some((step) => step.id === subject)) return subject;
    const type = event.type || event.event_type;
    if (type === "workflow_phase") {
      const role = event.detail?.role;
      if (role === "media_submission") return "generate";
      if (["media_gathering", "narration_assembly"].includes(role)) return "wait_assets";
      if (role === "render_assembly") return "edit";
      if (["renderer", "final_media_verification", "final_moderation", "public_eligibility"].includes(role)) return "wait_render";
    }
    const byType = {
      admitted: "ideate", ideation_submitting: "ideate", knowledge_loaded: "ideate", knowledge_context_submitted: "ideate", input_moderation_checked: "ideate", input_moderation_blocked: "ideate", plan_moderation_checked: "ideate", plan_moderation_blocked: "ideate", plan_ready: "ideate", ideation_failed: "ideate", ideation_submission_unknown: "ideate",
      creative_context_prepared: "ideate", creative_step_started: "ideate", creative_step_completed: "ideate", creative_step_validated: "ideate", creative_repair_exhausted: "ideate", creative_submission_unknown: "ideate",
      ideas_approved: "review_ideas", plan_completed: "review_ideas",
      asset_intents_created: "generate", assets_submitting: "generate", asset_submission_failed: "generate",
      assets_gathering: "wait_assets", asset_moderation_checked: "wait_assets", assets_ready: "wait_assets", asset_gather_failed: "wait_assets", asset_retries_exhausted: "wait_assets",
      render_submitting: "edit", render_unavailable: "edit",
      render_queued: "wait_render", render_running: "wait_render", render_progress: "wait_render", render_callback_received: "wait_render", final_moderation_checked: "wait_render", final_moderation_blocked: "wait_render", render_verified: "wait_render", render_failed: "wait_render",
      video_completed: "review_final", library_pruned: "review_final", renderer_copy_removed: "review_final", renderer_cleanup_deferred: "review_final",
    };
    if (byType[type]) return byType[type];
    if (type === "visitor_decision") {
      if (["idea_review", "ideating", "generating"].includes(subject)) return "review_ideas";
      if (["final_review", "video_complete"].includes(subject)) return "review_final";
      return null;
    }
    if (type === "ideation_submission_unknown") return "ideate";
    if (subject === "ideating") return "ideate";
    if (subject === "idea_review") return "review_ideas";
    if (subject === "generating") return ["assets_gathering", "asset_moderation_checked", "assets_ready", "asset_gather_failed", "asset_retries_exhausted"].includes(type) ? "wait_assets" : "generate";
    if (subject === "editing") return ["render_running", "render_callback_received", "final_moderation_checked", "render_verified", "render_failed"].includes(type) ? "wait_render" : "edit";
    if (["final_review", "video_complete"].includes(subject) || type === "video_completed") return "review_final";
    return null;
  }
  function renderActivity(step, current) {
    const section = $("activity-section"), list = $("activity-list"), empty = $("activity-empty"), heading = $("activity-heading"), announcement = $("activity-announcement");
    if (!section || !list || !empty || !heading || !announcement) return 0;
    const run = state.run, events = run?.telemetry?.events || run?.events || [];
    const selectedEvents = events.filter((event) => eventStage(event) === step.id), otherEvents = events.filter((event) => eventStage(event) === null), latestEvents = selectedEvents.slice(-8).reverse(), earlierEvents = selectedEvents.slice(0, -8).reverse();
    show("activity-section", Boolean(run));
    text(heading, "Recorded updates");
    const signature = JSON.stringify([run?.id, step.id, selectedEvents, otherEvents]);
    const visibleEventCount = selectedEvents.length + otherEvents.length;
    if (signature === state.activityKey) { text(announcement, run ? `${stageTitle(step)}: ${selectedEvents.length} recorded update${selectedEvents.length === 1 ? "" : "s"}.` : ""); return visibleEventCount; }
    state.activityKey = signature;
    const priorEarlier = section.querySelector(".activity-earlier"), priorOther = section.querySelector(".activity-other"), earlierWasOpen = Boolean(priorEarlier?.open), earlierHadFocus = Boolean(priorEarlier?.contains(document.activeElement)), otherWasOpen = Boolean(priorOther?.open), otherHadFocus = Boolean(priorOther?.contains(document.activeElement));
    list.replaceChildren();
    priorEarlier?.remove(); priorOther?.remove();
    for (const event of latestEvents) {
      list.append(activityItem(event));
    }
    if (earlierEvents.length) {
      const earlier = el("details", null, "activity-earlier"), summary = el("summary", `Show ${earlierEvents.length} earlier update${earlierEvents.length === 1 ? "" : "s"}`), olderList = el("ol", null, "activity-list activity-list--earlier");
      for (const event of earlierEvents) olderList.append(activityItem(event));
      earlier.open = earlierWasOpen; earlier.append(summary, olderList); section.append(earlier);
      if (earlierHadFocus) summary.focus({ preventScroll: true });
    }
    if (otherEvents.length) {
      const other = el("details", null, "activity-other"), summary = el("summary", "Other run updates"), otherList = el("ol", null, "activity-list activity-list--other");
      for (const event of otherEvents.slice().reverse()) otherList.append(activityItem(event));
      other.open = otherWasOpen; other.append(summary, otherList); section.append(other);
      if (otherHadFocus) summary.focus({ preventScroll: true });
    }
    show("activity-empty", selectedEvents.length === 0); text(empty, selectedEvents.length ? "" : "No recorded updates for this step yet.");
    text(announcement, run ? `${stageTitle(step)}: ${selectedEvents.length} recorded update${selectedEvents.length === 1 ? "" : "s"}.` : ""); return visibleEventCount;
  }
  function activityItem(event) {
    const labels = {
      creative_context_prepared: "Planning context saved", creative_step_started: "Planning step started", creative_step_completed: "Planning step saved", creative_repair_exhausted: "Storyboard corrections exhausted", creative_submission_unknown: "Planning receipt needs review", plan_completed: "Ideas saved",
      admitted: "Run queued", ideation_submitting: "Planning started", knowledge_loaded: "Project knowledge loaded", knowledge_context_submitted: "Project knowledge added", input_moderation_checked: "Item checked", plan_moderation_checked: "Plan checked", plan_ready: "Plan saved", visitor_decision: "Review decision saved", ideas_approved: "Plan approved",
      asset_intents_created: "Video tasks prepared", assets_submitting: "Video tasks submitted", assets_gathering: "Assets gathering", asset_moderation_checked: "Assets checked", assets_ready: "Assets verified",
      render_submitting: "Render submitted", render_queued: "Render queued", render_running: "Render started", render_callback_received: "Render update received", final_moderation_checked: "Video checked", render_verified: "Video verified", video_completed: "Result accepted", library_pruned: "Library updated",
      ideation_failed: "Planning stopped", ideation_submission_unknown: "Planning receipt needs review", asset_submission_failed: "Video task submission stopped", asset_gather_failed: "Asset gathering stopped", render_failed: "Render stopped", render_unavailable: "Render submission unavailable", input_moderation_blocked: "Item blocked", plan_moderation_blocked: "Plan blocked", final_moderation_blocked: "Video blocked", submission_unknown: "Receipt needs review",
    };
    if (isLocal()) Object.assign(labels, { asset_intents_created: "Saved media tasks prepared", assets_submitting: "Saved media requested", assets_gathering: "Loading saved media" });
    const type = event.type || event.event_type, timestamp = event.occurred_at || event.created_at, item = el("li"), detail = event.detail?.revision;
    const planningWork = {
      prepare_context: ["Preparing context", "Planning context saved"],
      input_moderation: ["Checking the object", "Object check complete"],
      explore_object: ["Exploring object concepts", "Object concepts saved"],
      explore_surreal: ["Exploring surreal concepts", "Surreal concepts saved"],
      curate: ["Selecting the set", "Selected set saved"],
      write_shots: ["Writing storyboards", "Storyboards saved"],
      replenish: ["Exploring replacement concepts", "Replacement concepts saved"],
      recurate: ["Selecting the revised set", "Revised set saved"],
      validating: ["Checking storyboards", "Storyboard check complete"],
      plan_moderation: ["Checking plan content", "Plan content check complete"],
    };
    const repairTarget = { prepare_context: "context", explore_object: "object ideas", explore_surreal: "surreal ideas", curate: "the selected set", write_shots: "storyboards", replenish: "replacement ideas", recurate: "the selected set", validating: "the plan", input_moderation: "the object", plan_moderation: "the plan" };
    const role = event.detail?.role, work = Object.hasOwn(planningWork, role) ? planningWork[role] : null;
    const isRepair = role === "repair_1" || role === "repair_2";
    const repairAttempt = event.detail?.attempt === 2 ? 2 : role === "repair_2" ? 2 : 1;
    const repairName = repairTarget[event.detail?.source_role] || "storyboards";
    const repairLabel = isRepair ? type === "creative_step_completed" ? `Corrected ${repairName} received (${repairAttempt}/2)` : `Repairing ${repairName} (${repairAttempt}/2)` : null;
    const validatedLabel = type === "creative_step_validated" ? isRepair ? `Corrected ${repairName} validated (${repairAttempt}/2)` : work ? `${work[0]} · validated` : "Agent result validated" : null;
    const planningLabel = validatedLabel || repairLabel || (work && ["creative_step_started", "creative_step_completed"].includes(type) ? `${work[0]} · ${type === "creative_step_completed" ? "received" : "submitted"}` : null);
    const renderLabel = type === "render_progress" ? window.AISMRProgress.renderPhases[event.detail?.phase] || "Renderer update" : null;
    const phaseLabel = type === "workflow_phase" ? isLocal() && event.detail?.role === "media_submission" ? "Loading saved Teacup media" : isLocal() && event.detail?.role === "media_gathering" ? "Checking saved media" : window.AISMRProgress.workflowPhases[event.detail?.role] : null;
    const outcome = planningLabel || renderLabel || phaseLabel || labels[type] || (type ? `Recorded event: ${String(type).replaceAll("_", " ")}` : "Recorded event"), label = detail == null ? outcome : `${outcome} · rev ${detail}`;
    const time = el("time", timestamp ? shortTime(timestamp) : "Time not recorded");
    if (timestamp) { time.dateTime = timestamp; time.title = date(timestamp); }
    item.append(el("span", label), time);
    if (renderLabel && ["frames", "encoding"].includes(event.detail?.phase) && Number.isFinite(event.detail?.progress)) item.append(el("span", `${Math.floor(event.detail.progress * 100)}% renderer progress`, "event-detail"));
    return item;
  }
  function shortTime(value) { const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? "Time not recorded" : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); }
  function placeActivity(current, review, eventCount) {
    const activity = $("activity-section"), ideas = $("idea-section"), waiting = $("waiting-section"); if (!activity) return;
    show("activity-section", Boolean(state.run) && !(current && review && eventCount === 0));
    if (current && review && waiting && activity.nextElementSibling !== waiting) waiting.before(activity);
    else if (!current && ideas && ideas.previousElementSibling !== activity) ideas.before(activity);
  }
  function buildAbout() {
    const target = $("about-stages"); if (!target) return;
    target.replaceChildren();
    const localAbout = {
      ideate: "Two real concept explorers, a curator and a storyboard writer create twelve numbered scenes. The agents receive no month assignments. Real content checks run before your review.",
      review_ideas: "Review the actual agent output and request revisions. Approval continues this private test using saved Teacup media.",
      generate: "Local providers return the twelve saved Teacup videos and a batch of the saved title-only recordings. No video or speech is generated. These stand-ins keep their original content and may differ from the new storyboards and titles.",
      wait_assets: "The workflow checks the saved files, their source receipts and content. Requested titles and actual spoken titles are recorded separately.",
      edit: "The editor passes twelve ordered scenes to the local renderer. The renderer alone adds the saved January through December clips and labels, then combines the title audio, videos and music.",
      wait_render: "One local render runs at a time. The workflow checks the finished video before offering it for review.",
      review_final: "Watch and accept the local result to complete the workflow. This test remains private and is not added to Recent creations."
    };
    for (const step of steps) { const item = el("li"); item.append(el("h3", stageTitle(step)), el("p", isRecordedReuse() ? recordedAbout[step.id] : isLocal() ? localAbout[step.id] : step.about)); target.append(item); }
  }
  function renderPlan() {
    const ideas = planItems();
    const planName = isPlanning() || isLocal() ? "storyboards" : "plan";
    const ideasTitle = $("ideas-title"); if (ideasTitle) text(ideasTitle, state.run && currentIndex() > 1 ? `Saved ${planName} · v${state.run.revision}` : state.run ? `Latest ${planName} · v${state.run.revision}` : isPlanning() ? "Your storyboards" : "Your scenes");
    const identity = JSON.stringify([state.run?.id, state.run?.revision, state.run?.plan_hash]);
    if (identity !== state.planIdentity) { state.planIdentity = identity; state.ideaIndex = 0; state.revisionOpen = false; state.revisionIdentity = ""; }
    renderPlanSubsteps();
    state.ideaIndex = Math.max(0, Math.min(state.ideaIndex, Math.max(ideas.length - 1, 0)));
    const signature = JSON.stringify([identity, state.allIdeas, state.ideaIndex]);
    if (signature === state.planKey) return;
    state.planKey = signature;
    const browser = $("idea-browser"); if (browser) browser.hidden = state.allIdeas || ideas.length < 2;
    const position = $("idea-position"); if (position) text(position, ideas.length ? `${state.ideaIndex + 1} of ${ideas.length}` : "0 of 0");
    const previous = $("previous-idea"), next = $("next-idea");
    if (previous) previous.disabled = state.ideaIndex === 0;
    if (next) next.disabled = state.ideaIndex >= ideas.length - 1;
    const grid = $("idea-grid"); grid.replaceChildren();
    ideas.forEach((idea, index) => {
      const card = el("li", null, "idea-card"); card.hidden = !state.allIdeas && index !== state.ideaIndex;
      const ordinal = Number.isInteger(idea.ordinal) ? idea.ordinal : index + 1;
      card.append(el("span", idea.month || `Scene ${ordinal}`, "idea-month"), el("h5", idea.title || idea.label || "Untitled variation"));
      const details = el("details"), summary = el("summary", isPlanning() ? "Storyboard prompt" : "Scene details");
      details.append(summary, el("p", idea.visual_prompt || "No visual prompt recorded."));
      if (idea.spoken_text) details.append(el("p", `Narration: ${idea.spoken_text}`));
      card.append(details); grid.append(card);
    });
    text($("toggle-ideas"), state.allIdeas ? "Show one" : `View all ${ideas.length}`); $("toggle-ideas").setAttribute("aria-expanded", String(state.allIdeas));
  }
  function renderPlanSubsteps() {
    const target = $("plan-substeps"), steps = creativePlanSteps(state.run);
    target.replaceChildren();
    for (const step of steps) {
      const countText = step.counts && typeof step.counts === "object" ? Object.entries(step.counts).filter(([, value]) => Number.isFinite(value)).map(([name, value]) => `${value} ${name.replaceAll("_", " ")}`).join(", ") : "";
      const operationText = typeof step.operation === "string" && step.operation ? step.operation.replaceAll("_", " ") : "";
      const detail = [step.state.toLowerCase(), operationText, countText].filter(Boolean).join(" · ");
      const item = el("li"); item.append(el("strong", step.label), el("span", detail)); target.append(item);
    }
    show("agent-work", steps.length > 0 && state.selected === "ideate");
  }
  function renderWorkStatus() {
    const status = window.AISMRProgress.workStatus(state.run, { connectionLost: state.connectionLost, shared: Boolean(state.share), updateIssue: state.updateIssue });
    show("run-status", Boolean(status));
    if (!status) return;
    const section = $("run-status"); section.dataset.tone = status.tone; section.classList.toggle("is-working", status.working);
    text($("run-status-label"), status.label); text($("run-activity"), status.task);
    text($("run-work-detail"), status.detail); show("run-work-detail", Boolean(status.detail));
    show("render-meter", status.meter !== null);
    if (status.meter !== null) { $("render-progress").value = status.meter; text($("render-percent"), `${status.meter}%`); }
    const updated = $("run-updated"); show("run-updated", Boolean(status.updatedAt));
    if (status.updatedAt) { updated.dateTime = status.updatedAt; updated.title = date(status.updatedAt); text(updated, `Last update ${shortTime(status.updatedAt)}`); }
  }
  function canRequestRevision(run, index) {
    const allowed = typeof run?.can_revise === "boolean" ? run.can_revise : mode() !== "recorded" && (run?.revision || 1) <= (state.config?.limits?.plan_revisions ?? 1);
    return Boolean(run?.status === "idea_review" && run.can_act && !state.share && index === 1 && allowed);
  }
  function supportsTargetedRevision() { return isPlanning() || state.run?.planner_version === "creative-v2"; }
  function renderRevisionControls(canRevise) {
    const controls = $("revision-controls"), opener = $("open-revision"), ideas = planItems();
    const targeted = supportsTargetedRevision();
    text(opener, targeted ? "Request revision" : "Request full revision");
    opener.hidden = !canRevise; opener.disabled = state.pending || !canRevise;
    if (!canRevise || !targeted) { state.revisionOpen = false; show("revision-controls", false); return; }
    show("revision-controls", state.revisionOpen);
    if (!state.revisionOpen) return;
    const identity = state.planIdentity;
    const scenes = $("revision-scenes");
    if (state.revisionIdentity !== identity) {
      state.revisionIdentity = identity; scenes.replaceChildren();
      ideas.forEach((idea, index) => {
        const ordinal = index + 1, label = el("label", null, "revision-scene"), input = document.createElement("input");
        const sceneLabel = idea.month || `Scene ${ordinal}`;
        input.type = "checkbox"; input.value = String(ordinal); input.checked = true; input.setAttribute("aria-label", `Replace scene ${ordinal}: ${idea.title || idea.label || sceneLabel}`);
        label.append(input, document.createTextNode(idea.month ? sceneLabel : `${sceneLabel}: ${idea.title || "Untitled scene"}`)); scenes.append(label);
      });
      $("revision-reason").value = ""; $("revision-note").value = ""; text($("revision-note-count"), "0 of 500 characters");
    }
    const disabled = state.pending;
    scenes.querySelectorAll("input").forEach((input) => { input.disabled = disabled; });
    $("revision-reason").disabled = disabled; $("revision-note").disabled = disabled; $("select-all-scenes").disabled = disabled; $("submit-revision").disabled = disabled; $("cancel-revision").disabled = disabled;
  }
  function renderAssets() {
    const assets = state.run?.assets || [], signature = JSON.stringify(assets); if (signature === state.assetsKey) return;
    state.assetsKey = signature; const grid = $("asset-grid"); grid.replaceChildren();
    const currentAssets = latestAssets(state.run), scenes = currentAssets.filter((asset) => asset.kind === "video"), segments = currentAssets.filter((asset) => asset.kind === "voice"), voiceBatch = currentAssets.find((asset) => asset.kind === "voice_batch"), readyScenes = scenes.filter((asset) => asset.status === "ready").length;
    const narration = voiceBatch ? ` · narration ${voiceBatch.status || "not observed"}` : segments.length ? ` · narration ${segments.every((asset) => asset.status === "ready") ? "ready" : "in progress"}` : "";
    text($("asset-count"), assets.length ? `${readyScenes}/${scenes.length} scenes ready${narration}` : "No provider tasks recorded.");
    for (const asset of assets) {
      const row = el("li"), label = asset.kind === "voice_batch" ? "Narration batch" : asset.kind === "voice" ? `Narration section ${asset.ordinal ?? "?"} · derived from batch` : `Scene ${asset.ordinal ?? "?"} · ${asset.kind || "asset"}`;
      row.append(el("span", label, "asset-label"), el("span", `${asset.status || "Not observed"}${asset.queue_position != null ? ` · queue ${asset.queue_position}` : ""}`)); grid.append(row);
    }
  }
  function renderFinal() {
    const run = state.run, video = $("preview-video"), url = localUrl(run?.final?.url), evicted = run?.retention === "evicted";
    show("preview-video", Boolean(url) && !evicted);
    if (url && video.getAttribute("src") !== url) video.src = url;
    if ((!url || evicted) && video.hasAttribute("src")) { video.pause(); video.removeAttribute("src"); video.load(); }
    text($("final-copy"), evicted ? "This older result is no longer retained. The library keeps up to three accepted creations." : !url ? "The verified final video is not available in this view yet." : run.status === "video_complete" ? "This is the accepted result. Use the player's controls to watch and listen." : "Use the player's controls to watch and listen before making your decision.");
    const fixture = mode() === "fixture";
    text($("final-actions").querySelector(".button--approve"), isRecordedReuse() ? "Accept recorded result" : isLocal() ? "Accept local result" : fixture ? "Accept test result" : "Accept and show in Recent creations");
    text($("final-actions").querySelector(".consent-copy"), isRecordedReuse() ? "Accepting completes this walkthrough with the saved result. No new generation, rendering, gallery publication or social posting occurs." : isLocal() ? "This uses saved Teacup footage and its original spoken titles, which may differ from this plan. Accepting completes this private workflow." : fixture ? "This local test uses sample media. Acceptance completes the test workflow and does not add sample media to the public library." : !canAcceptFinal() ? `This result is private. Required public-use checks are not currently verified, so it cannot be added to Recent creations. You can keep reviewing${run?.private_review_expires_at ? ` until ${date(run.private_review_expires_at)}` : " until the review expires"}, or discard it.` : "Accepting this result also makes it publicly visible in Recent creations. The library keeps up to three results; a newer accepted result can replace the oldest.");
    show("final-actions", run?.status === "final_review" && Boolean(run?.can_act) && !state.share && Boolean(run?.final?.review_hash));
  }
  function renderTechnical(step) {
    const target = $("technical-details"), run = state.run, observation = nodeObservation(step.id); target.replaceChildren();
    receipt(target, [["Node", step.id], ["State", observation?.state || observation?.status || (run ? "Not observed" : "No run")], ["Revision", run?.revision], ["Plan", run?.plan_hash], ["Final", step.id === "review_final" ? run?.final?.sha256 : null], ["Review expires", run?.expires_at ? date(run.expires_at) : null]]);
    if (run?.graph?.checkpoint?.observed) receipt(target, [["Checkpoint", run.graph.checkpoint]]);
    if (run?.cost) receipt(target, [["Reserved USD", run.cost.reserved], ["Estimated USD", run.cost.estimated ?? "Not confirmed"], ["Confirmed USD", run.cost.confirmed ?? "Not confirmed"], ["Cost state", run.cost.state]]);
  }
  function render() {
    const run = state.run, progress = projectProgress(run), index = steps.findIndex((step) => step.id === state.selected), step = steps[index], current = index === progress.index;
    const review = current && ["idea_review", "final_review"].includes(run?.status), issue = ["failed", "blocked", "cancelled", "submission_unknown"].includes(run?.status);
    renderGraph(); text($("run-title"), run ? run.item : "Choose an object");
    $("run-title").closest(".run-heading").hidden = !run;
    $("inspector").classList.toggle("is-start", !run && index === 0);
    $("workspace").classList.toggle("is-empty", !run);
    text($("run-subtitle"), ""); show("run-subtitle", false);
    const actionTitle = !run && index === 0 ? "What should we make?" : current && run?.status === "idea_review" ? (isPlanning() || isLocal()) ? "Review storyboards" : "Review plan" : current && run?.status === "plan_complete" ? "Ideas saved" : current && run?.status === "final_review" ? "Watch your video" : current && run?.status === "video_complete" ? "Your video is ready" : stageTitle(step);
    text($("selected-title"), actionTitle); text($("selected-number"), index + 1); text($("selected-status"), nodeState(index)); show("selected-status", Boolean(run) && !current);
    $("node-inspector").classList.toggle("is-decision", review);
    text($("stage-copy"), state.share ? "" : run?.review_expired && current ? "This review has expired. Decisions are unavailable." : current && run?.status === "idea_review" ? isPlanning() ? "Real ideas and scripts are ready. Keep them, request a targeted revision, or cancel. This review does not generate media." : isLocal() ? "Approve to continue with the saved Teacup videos and title recordings." : isRecordedReuse() ? "Approve to load this plan’s saved media and final video." : mode() === "fixture" ? "Approve the plan to assemble a sample." : "Approve the plan to start production." : current && run?.status === "plan_complete" ? state.config?.mode === "local" ? "These ideas are saved. Choose New workflow to try all seven steps with real agents and saved media." : "Your real ideas and scripts are saved. Start a new item when you are ready." : current && progress.working && !state.connectionLost && !state.updateIssue ? "No action needed. You can leave this page and return." : ""); show("stage-copy", Boolean($("stage-copy").textContent));
    const modeCopy = isRecordedReuse() ? "Recorded walkthrough · saved plan, assets and final video · no new generation" : isLocal() ? "Real agents · saved Teacup media" : mode() === "recorded" ? "Recorded example." : mode() === "fixture" ? "Sample mode · no AI generation" : isPlanning() ? "Agent review · real idea and script planning with content checks. No media generation." : "";
    text($("mode-note"), modeCopy); show("mode-note", Boolean(modeCopy)); show("readonly-notice", Boolean(state.share));
    show("make-section", !run && index === 0); show("idea-section", Boolean(planItems(run).length) && [0, 1].includes(index));
    show("production-section", !isPlanning() && Boolean(run) && [2, 3, 4, 5].includes(index)); show("final-section", !isPlanning() && Boolean(run) && index === 6 && Boolean(run.final || ["final_review", "video_complete"].includes(run.status) || run.retention === "evicted"));
    const canPlan = run?.status === "idea_review" && run.can_act && !state.share && index === 1, canRevise = canRequestRevision(run, index);
    const ideaApproval = document.querySelector('[data-decision="approve"][data-gate="ideas"]');
    if (ideaApproval) text(ideaApproval, isPlanning() ? "Keep ideas" : isLocal() ? "Approve and load media" : "Approve plan");
    show("idea-actions", canPlan || canRevise); show("cancel-plan", canPlan);
    renderPlan(); renderRevisionControls(canRevise);
    const waiting = Boolean(run) && current && (issue || state.connectionLost || state.updateIssue);
    show("waiting-section", waiting); text($("waiting-title"), state.connectionLost ? "Connection interrupted" : run?.status === "cancelled" ? "Run cancelled" : "Needs attention");
    text($("waiting-copy"), state.updateIssue && !state.connectionLost ? state.updateIssue.message : state.connectionLost ? "Reconnecting… You can refresh this run now." : run?.status === "submission_unknown" ? "The saved steps remain available. This request will not be repeated automatically." : run?.status === "cancelled" ? "This run is closed." : errors[run?.error_code] || "Refresh to check for a recorded update.");
    show("return-current", Boolean(run) && (!current || !state.follow)); show("run-tools", Boolean(run)); show("share-run", Boolean(run?.share_url) && !state.share); text($("run-id"), run ? `Run ${run.id.slice(0,8)}` : "");
    const readLink = localUrl(run?.share_url); show("read-only-view", Boolean(readLink) && !state.share); if (readLink) $("read-only-view").href = readLink;
    if (run?.error_code) notice(errors[run.error_code] || `The workflow reported ${run.error_code}. Refresh to check its current state.`);
    renderAssets(); if (index === 6) renderFinal(); else $("preview-video").pause(); renderTechnical(step); const eventCount = renderActivity(step, current); placeActivity(current, review, eventCount); renderWorkStatus();
    document.querySelectorAll("[data-decision]").forEach((button) => { button.disabled = state.pending || button.dataset.decision === "revise" && (mode() === "recorded" || (run?.revision || 1) > (state.config?.limits?.plan_revisions ?? 1)) || button.dataset.gate === "final" && button.dataset.decision === "approve" && !canAcceptFinal(); });
    $("start-button").disabled = state.pending || !canGenerate(); $("start-recorded").disabled = state.pending || !state.config?.enabled || !state.config?.recorded_item; $("new-run").disabled = state.pending; text($("new-run"), isPlanning() && state.config?.mode === "local" ? "New workflow" : isPlanning() && run?.status === "plan_complete" ? "New item" : "New run");
    renderPendingButtons();
    renderConnection();
  }
  function recordUpdateFailure(error) {
    state.updateIssue = window.AISMRProgress.pollFailure(error); state.connectionLost = state.updateIssue.connectionLost;
    notice(state.connectionLost ? "Connection interrupted. Reconnecting…" : state.updateIssue.message); render();
  }
  function schedule() {
    clearTimeout(state.poll);
    if (!state.run || !activeStatuses.has(state.run.status) || state.updateIssue?.stop) return;
    state.poll = setTimeout(async () => { try { await loadRun(state.run.id); } catch (error) { recordUpdateFailure(error); schedule(); } }, 2500);
  }
  async function loadRun(id) {
    const selection = state.selection;
    const suffix = state.share ? `?share=${encodeURIComponent(state.share)}` : "";
    const run = await api(`/v1/studio/runs/${encodeURIComponent(id)}${suffix}`);
    if (selection !== state.selection) return;
    state.run = run; if (state.connectionLost || state.updateIssue) { state.connectionLost = false; state.updateIssue = null; notice(""); } if (state.follow) state.selected = steps[currentIndex()].id; render(); schedule();
  }
  function suggestions() {
    const list = $("suggestion-list"); list.replaceChildren();
    for (const item of (state.config?.suggestions || []).slice(0, 5)) { const button = el("button", item.label || item.item_text || item.item_id, "suggestion"); button.type = "button"; button.addEventListener("click", () => { $("item-input").value = button.textContent; $("item-input").focus(); }); list.append(button); }
  }
  function renderConnection() {
    const config = state.config;
    text($("connection-status"), state.connectionLost ? "Reconnecting…" : !config ? "Connection unavailable" : config.mode === "recorded" ? "Recorded example" : config.mode === "fixture" ? "Test demo" : config.mode === "planning" ? "Agent review" : config.mode === "local" ? "Real agents · local media" : canGenerate() ? "Generation available" : "Generation unavailable");
  }
  function availability() {
    const config = state.config;
    renderConnection();
    const limits = config?.limits;
    const count = limits?.visitor_runs_24h ?? 1;
    text($("start-button"), config?.mode === "fixture" ? "Try a sample plan" : ["planning", "local"].includes(config?.mode) ? "Generate ideas" : "Prepare 12 ideas");
    text($("allowance"), !canGenerate() ? config?.mode === "recorded" ? "New generation is unavailable. Explore the saved walkthrough below." : "New generation is unavailable. You can browse Recent creations." : config.mode === "fixture" ? limits?.fixture_unlimited ? "Unlimited sample runs during development." : `${count} sample runs per 24 hours.` : config.mode === "local" ? "Real agents create the ideas and storyboards. After approval, saved Teacup videos and original title recordings stand in for generation." : config.mode === "planning" ? `${count} ${count === 1 ? "run" : "runs"} per visitor every 24 hours. Generates real ideas and scripts with content checks. This review does not generate media.` : `${count} ${count === 1 ? "run" : "runs"} per visitor every 24 hours. Preparing ideas uses a run allowance and may use paid planning and content checks. Video production waits for your approval.`);
    buildGraph(); buildAbout();
    show("recorded-option", config?.mode === "recorded"); text($("recorded-copy"), `Follow the saved ${config?.recorded_item || "example"} plan through its recorded stages. This does not generate a new object.`);
  }
  async function createRun(item) {
    if (state.pending) return;
    state.selection += 1; clearTimeout(state.poll);
    state.pending = true; state.pendingAction = "start"; text($("form-message"), ""); notice(""); render();
    const name = `aismr:start:${item}`;
    try {
      if (!state.session) state.session = await api("/v1/studio/session", { method:"POST", body:"{}" });
      const run = await api("/v1/studio/runs", { method: "POST", body: JSON.stringify({ item, request_key: remember(name) }) }); forget(name); state.run = run; state.share = null; state.follow = true; state.selected = steps[currentIndex()].id; state.allIdeas = false; state.ideaIndex = 0; state.planIdentity = ""; state.activityKey = "";
      history.replaceState(null, "", `${location.pathname}?run=${encodeURIComponent(run.id)}#workspace`); showRoute(); $("run-title").focus({ preventScroll: true });
    } catch (error) { if (error.definitive) forget(name); text($("form-message"), error.message); }
    finally { state.pending = false; state.pendingAction = ""; render(); schedule(); }
  }
  function revisionFeedback() {
    const selected = [...$("revision-scenes").querySelectorAll('input:checked')].map((input) => Number(input.value)).filter(Number.isInteger);
    if (!selected.length) { notice("Choose at least one scene to replace, or select all scenes."); return null; }
    const total = planItems().length || 0, feedback = {}, reason = $("revision-reason").value, note = $("revision-note").value.trim();
    if (selected.length !== total) feedback.replace_ordinals = selected;
    if (reason) feedback.reason = reason;
    if (note) feedback.note = note;
    return feedback;
  }
  async function decide(button, feedback = null) {
    const run = state.run, gate = button.dataset.gate, decision = button.dataset.decision;
    if (!run?.can_act || state.share || state.pending) return;
    const hash = gate === "ideas" ? run.plan_hash : run.final?.review_hash;
    if (!hash) { notice("The review is missing its content receipt. Refresh the run before deciding."); return; }
    const name = `aismr:decision:${run.id}:${gate}:${run.revision}:${hash}:${decision}`;
    state.pending = true; state.pendingAction = gate === "ideas" && decision === "approve" ? "approve-ideas" : "decision"; notice(""); render();
    try {
      const body = { gate, decision, revision: run.revision, subject_hash: hash, request_key: remember(name) };
      if (gate === "ideas" && decision === "revise" && feedback) body.revision_feedback = feedback;
      if (gate === "final" && decision === "approve" && !isRecordedReuse() && !["fixture", "local"].includes(mode())) { body.publish_to_gallery = true; body.visibility_version = "recent-creations-v1"; }
      const next = await api(`/v1/studio/runs/${encodeURIComponent(run.id)}/decisions`, { method: "POST", body: JSON.stringify(body) }); forget(name); state.run = next; state.follow = true; state.selected = steps[currentIndex()].id; state.allIdeas = false; state.ideaIndex = 0; state.planIdentity = ""; state.activityKey = "";
      if (gate === "final" && decision === "approve") await loadGallery();
    } catch (error) { if (error.definitive) forget(name); notice(error.message); }
    finally { state.pending = false; state.pendingAction = ""; render(); schedule(); }
  }
  async function loadGallery() {
    try {
      const payload = await api("/v1/studio/gallery"), items = (Array.isArray(payload?.items) ? payload.items : []).filter((item) => item.mode !== "fixture").slice(0,3), list = $("gallery-list"); list.replaceChildren();
      text($("gallery-count"), items.length ? `${items.length} of 3 retained` : ""); text($("gallery-message"), items.length ? "" : "No public results yet. An accepted result appears here after its public-use checks pass.");
      for (const item of items) {
        const entry = el("li", null, "gallery-entry"); entry.append(el("h3", item.item || item.item_label || "Completed result"), el("p", `${item.mode === "recorded" ? "Recorded example" : "Generated result"}${item.duration_seconds != null ? ` · ${Math.round(item.duration_seconds)} seconds` : ""}`));
        const url = localUrl(item.url), play = el("button", "Watch result", "button"); play.type = "button";
        if (url) { play.addEventListener("click", () => { let video = entry.querySelector("video"); if (!video) { video = document.createElement("video"); video.controls = true; video.playsInline = true; video.preload = "none"; video.src = url; entry.append(video); } play.hidden = true; video.focus(); }); entry.append(play); }
        const details = el("details"); details.append(el("summary", "How this was made"), el("p", "Plan ideas → human review → video and narration → asset checks → assembly → render checks → final review."));
        if (item.history && typeof item.history === "object") receipt(details, Object.entries(item.history).map(([key, value]) => [key.replaceAll("_", " "), value]));
        if (item.sha256) receipt(details, [["Final SHA-256", item.sha256]]); entry.append(details); list.append(entry);
      }
    } catch { text($("gallery-message"), "Recent creations could not be loaded. Refresh the page to try again; this does not start generation."); }
  }
  function routeName() { return location.hash === "#recent" ? "recent" : location.hash === "#introduction" ? "introduction" : "workspace"; }
  function showRoute() {
    const route = routeName();
    show("workspace", route === "workspace"); show("recent", route === "recent"); show("introduction", route === "introduction");
    document.querySelectorAll("[data-nav]").forEach((node) => node.classList.toggle("is-current", node.dataset.nav === route));
  }
  function newRun() { state.selection += 1; clearTimeout(state.poll); state.run = null; state.share = null; state.follow = true; state.pendingAction = ""; state.connectionLost = false; state.updateIssue = null; state.selected = "ideate"; state.allIdeas = false; state.ideaIndex = 0; state.planIdentity = ""; state.planKey = ""; state.assetsKey = ""; state.activityKey = ""; notice(""); history.replaceState(null, "", `${location.pathname}#workspace`); showRoute(); render(); $("item-input").focus({ preventScroll: true }); $("inspector").scrollTop = 0; }
  $("run-form").addEventListener("submit", (event) => { event.preventDefault(); const item = $("item-input").value.trim(); if (!item || /[\r\n]|https?:\/\//i.test(item) || item.length > 48) { text($("form-message"), errors.invalid_item); $("item-input").focus(); return; } if (canGenerate()) createRun(item); });
  $("random-button").addEventListener("click", () => { const items = (state.config?.suggestions || []).map((item) => item.label || item.item_text || item.item_id).filter(Boolean); const choices = items.length ? items : ["Lantern", "Seashell", "Piano", "Snow globe", "Umbrella"]; const current = $("item-input").value; const pool = choices.filter((item) => item !== current); const next = (pool.length ? pool : choices)[Math.floor(Math.random() * (pool.length || choices.length))]; $("item-input").value = next; text($("form-message"), ""); $("item-input").focus(); });
  $("start-recorded").addEventListener("click", () => { if (state.config?.recorded_item) createRun(state.config.recorded_item); });
  $("new-run").addEventListener("click", newRun);
  $("toggle-ideas").addEventListener("click", () => { state.allIdeas = !state.allIdeas; renderPlan(); });
  $("open-revision").addEventListener("click", () => { if (!supportsTargetedRevision()) { decide({ dataset: { gate: "ideas", decision: "revise" } }); return; } state.revisionOpen = true; renderRevisionControls(true); $("revision-scenes").querySelector("input")?.focus({ preventScroll: true }); });
  $("select-all-scenes").addEventListener("click", () => { $("revision-scenes").querySelectorAll("input").forEach((input) => { input.checked = true; }); });
  $("cancel-revision").addEventListener("click", () => { state.revisionOpen = false; render(); });
  $("revision-note").addEventListener("input", () => { text($("revision-note-count"), `${$("revision-note").value.length} of 500 characters`); });
  $("previous-idea")?.addEventListener("click", () => { if (state.ideaIndex > 0) { state.ideaIndex -= 1; renderPlan(); } });
  $("next-idea")?.addEventListener("click", () => { const count = planItems().length; if (state.ideaIndex < count - 1) { state.ideaIndex += 1; renderPlan(); } });
  $("current-step-button").addEventListener("click", () => { state.follow = true; state.selected = steps[currentIndex()].id; render(); $("selected-title").focus(); });
  $("refresh-run").addEventListener("click", async () => { if (!state.run) return; try { await loadRun(state.run.id); } catch (error) { recordUpdateFailure(error); schedule(); } });
  $("keep-reviewing").addEventListener("click", () => { notice(`The result remains private for review${state.run?.private_review_expires_at ? ` until ${date(state.run.private_review_expires_at)}` : " until this review expires"}. Nothing has been accepted or added to Recent creations.`); });
  $("share-run").addEventListener("click", async () => { const url = localUrl(state.run?.share_url); if (!url) return; try { await navigator.clipboard.writeText(url); text($("share-run"), "Read-only link copied"); } catch { notice(`Read-only link: ${url}`); } });
  document.addEventListener("click", (event) => { const decision = event.target.closest("[data-decision]"); if (!decision) return; if (decision.id === "submit-revision") { const feedback = revisionFeedback(); if (feedback) decide(decision, feedback); return; } decide(decision); });
  $("enter-workflow").addEventListener("click", () => { try { localStorage.setItem("aismr:introduced", "1"); } catch {} showRoute(); $("run-title").focus({ preventScroll:true }); });
  window.addEventListener("hashchange", showRoute);
  async function init() {
    const selection = state.selection;
    buildGraph(); buildAbout(); showRoute(); render(); loadGallery();
    try {
      state.config = await api("/v1/studio/config"); availability(); suggestions(); render();
      if (state.share && params.get("run")) { await loadRun(params.get("run")); return; }
      state.session = await api("/v1/studio/session", { method:"POST", body:"{}" });
      if (state.selection !== selection) return;
      const id = params.get("run") || state.session.latest_run_id;
      if (id) { await loadRun(id); showRoute(); }
    } catch (error) { availability(); render(); notice(error.message); }
  }
  init();
})();
