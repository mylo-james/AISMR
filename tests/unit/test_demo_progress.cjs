const { test } = require('node:test');
const assert = require('node:assert/strict');
const { creativePlanSteps, latestAssets, project, workStatus, pollFailure } = require('../../web/demo/progress.js');

const event = (type, sequence = 1, detail = {}) => ({ type, sequence, detail });
const run = (status, events = [], extra = {}) => ({ status, events, revision: 1, ...extra });

test('assembly advances on a recorded render receipt while SQL remains editing', () => {
  assert.equal(project(run('editing')).index, 4);
  assert.equal(project(run('editing', [event('render_submitting')])).index, 4);
  const queued = project(run('editing', [event('render_submitting'), event('render_queued', 2)]));
  assert.equal(queued.index, 5);
  assert.equal(queued.activity, 'Waiting for the renderer');
  assert.equal(queued.working, true);
});

test('render running and signed callbacks have distinct feedback without claiming completion', () => {
  const events = [event('render_queued'), event('render_running', 2)];
  assert.equal(project(run('editing', events)).activity, 'Rendering your video');
  events.push(event('render_callback_received', 3));
  assert.equal(project(run('editing', events)).activity, 'Checking the render result');
  assert.equal(project(run('editing', events)).index, 5);
});

test('sequence order wins over response array order', () => {
  assert.equal(project(run('editing', [event('render_running', 2), event('render_queued', 1)])).activity, 'Rendering your video');
});

test('review gates and terminal states never show a working loader', () => {
  for (const status of ['idea_review', 'plan_complete', 'final_review', 'video_complete', 'failed', 'blocked', 'cancelled', 'submission_unknown']) {
    assert.equal(project(run(status, [event('render_running')])).working, false, status);
  }
  assert.equal(project(run('idea_review', [event('render_running')])).index, 1);
  assert.equal(project(run('final_review', [event('render_running')])).index, 6);
  assert.equal(project(run('failed', [event('render_running')])).index, 5);
});

test('a planning-only approval ends at review without implying a completed video', () => {
  const result = project(run('plan_complete'));
  assert.deepEqual(result, { index: 1, activity: 'Ideas saved', working: false, source: 'status' });
});

test('old revision render history cannot advance a revised plan', () => {
  const events = [event('render_running'), event('ideation_submitting', 2, { revision: 2 })];
  assert.equal(project(run('editing', events, { revision: 2 })).index, 4);
  assert.equal(project(run('ideating', events, { revision: 2 })).index, 0);
  assert.equal(project(run('editing', [event('render_running')], { revision: 2 })).index, 4);
  events.push(event('render_queued', 3));
  assert.equal(project(run('editing', events, { revision: 2 })).index, 5);
});

test('intents and partial submission stay at Generate until the batch has receipts', () => {
  const assets = Array.from({ length: 12 }, (_, i) => ({ kind: 'video', ordinal: i + 1, attempt: 1, status: 'pending' }));
  assets.push({ kind: 'voice_batch', ordinal: 0, attempt: 1, status: 'pending' });
  assert.equal(project(run('generating', [], { assets })).index, 2);
  assets[0].status = 'queued';
  assert.equal(project(run('generating', [], { assets })).index, 2);
  assets.forEach(asset => { asset.status = 'queued'; });
  assert.equal(project(run('generating', [], { assets })).index, 3);
  assets.push(...Array.from({ length: 12 }, (_, i) => ({ kind: 'voice', ordinal: i + 1, attempt: 1, status: 'ready' })));
  assert.equal(project(run('generating', [], { assets })).index, 3);
});

test('local media advances through real collection and render receipts without claiming generation', () => {
  assert.equal(project(run('generating', [], { mode: 'local' })).activity, 'Loading saved Teacup media');
  const assets = Array.from({ length: 12 }, (_, i) => ({ kind: 'video', ordinal: i + 1, status: 'queued' }));
  assets.push({ kind: 'voice_batch', ordinal: 0, status: 'queued' });
  const collecting = project(run('generating', [], { mode: 'local', assets }));
  assert.equal(collecting.index, 3);
  assert.equal(collecting.activity, 'Checking saved scenes and title recordings');
  assert.equal(project(run('editing', [event('render_running')], { mode: 'local' })).index, 5);
  assert.equal(project(run('final_review', [], { mode: 'local' })).index, 6);
});

test('superseded failed attempts do not hide current successful submissions', () => {
  const assets = Array.from({ length: 12 }, (_, i) => ({ kind: 'video', ordinal: i + 1, attempt: 2, status: 'ready' }));
  assets.push({ kind: 'video', ordinal: 1, attempt: 1, status: 'failed' }, { kind: 'voice_batch', ordinal: 0, attempt: 1, status: 'ready' });
  assert.equal(project(run('generating', [], { assets })).index, 3);
});

test('an empty or expired run has no false activity', () => {
  assert.equal(project(null).working, false);
  assert.equal(project(run('idea_review', [], { review_expired: true })).activity, 'Review expired');
});

test('asset summaries use the latest attempt for each scene', () => {
  const assets = latestAssets(run('generating', [], { assets: [
    { kind: 'video', ordinal: 1, attempt: 1, status: 'failed' },
    { kind: 'video', ordinal: 1, attempt: 2, status: 'ready' },
    { kind: 'video', ordinal: 2, attempt: 1, status: 'queued' },
  ] }));
  assert.deepEqual(assets.map(asset => [asset.ordinal, asset.attempt, asset.status]), [[1, 2, 'ready'], [2, 1, 'queued']]);
});

test('creative plan steps are friendly, revision-bound saved event summaries', () => {
  const events = [
    event('creative_step_completed', 1, { revision: 1, role: 'prepare_context' }),
    event('creative_step_started', 2, { revision: 1, role: 'explore_object' }),
    event('creative_step_completed', 3, { revision: 1, role: 'explore_object', counts: { candidates: 8 } }),
    event('creative_step_started', 4, { revision: 1, role: 'unknown_role' }),
    event('ideation_submitting', 5, { revision: 2 }),
    event('creative_step_started', 6, { revision: 2, role: 'write_shots', operation: 'drafting' }),
  ];
  assert.deepEqual(creativePlanSteps(run('ideating', events, { revision: 2 })), [
    { role: 'write_shots', label: 'Writing storyboards', state: 'Submitted', operation: 'drafting', counts: undefined },
  ]);
});

test('empty page asset projection is safe before a run exists', () => {
  assert.deepEqual(latestAssets(null), []);
  assert.deepEqual(latestAssets(undefined), []);
});

test('creative revision events drive current activity without a v1 start event', () => {
  const run = {status:'ideating', revision:2, events:[
    {type:'creative_step_started',sequence:1,detail:{revision:1,role:'write_shots'}},
    {type:'creative_context_prepared',sequence:2,detail:{revision:2}},
    {type:'creative_step_started',sequence:3,detail:{revision:2,role:'explore_object'}},
    {type:'creative_step_completed',sequence:4,detail:{revision:2,role:'explore_object'}},
    {type:'creative_step_started',sequence:5,detail:{revision:2,role:'write_shots'}},
  ]};
  assert.equal(creativePlanSteps(run).length, 2);
  assert.equal(project(run).activity, 'Writing storyboards');
});

test('storyboard repair events show received output before the plan is validated', () => {
  const repair = run('ideating', [
    event('creative_step_started', 1, { revision: 1, role: 'repair_1', source_role: 'write_shots', attempt: 1 }),
  ]);
  assert.deepEqual(creativePlanSteps(repair), [
    { role: 'repair_1', label: 'Repairing storyboards (1/2)', state: 'Submitted', operation: undefined, counts: undefined },
  ]);
  assert.equal(project(repair).activity, 'Repairing storyboards (1/2)');

  repair.events.push(event('creative_step_completed', 2, { revision: 1, role: 'repair_1', source_role: 'write_shots', attempt: 1 }));
  assert.equal(creativePlanSteps(repair)[0].state, 'Received');
  assert.equal(project(repair).activity, 'Checking corrections');

  repair.events.push(event('creative_step_started', 3, { revision: 1, role: 'repair_2', source_role: 'write_shots', attempt: 2 }));
  assert.equal(project(repair).activity, 'Repairing storyboards (2/2)');
});


test('status distinguishes agent receipt from validation and shows concurrent roles', () => {
  const data = run('ideating', [
    event('creative_step_started', 1, {role:'explore_object'}),
    event('creative_step_started', 2, {role:'explore_surreal'}),
  ]);
  assert.equal(workStatus(data).label, 'Working');
  assert.equal(workStatus(data).detail, 'Object explorer · Surreal explorer · awaiting result');
  data.events.push(event('creative_step_completed', 3, {role:'explore_object'}));
  assert.equal(creativePlanSteps(data)[0].state, 'Received');
  assert.equal(workStatus(data).task, 'Exploring surreal ideas');
  data.events.push(event('creative_step_validated', 4, {role:'explore_object'}));
  assert.equal(creativePlanSteps(data)[0].state, 'Validated');
});

test('native render progress advances timeline and exposes bounded frame work', () => {
  const data = run('editing', [event('render_progress', 1, {phase:'frames', progress:.35, total_frames:2400, rendered_frames:840})]);
  assert.equal(project(data).index, 5);
  assert.equal(workStatus(data).task, 'Rendering video frames');
  assert.equal(workStatus(data).detail, '840/2,400 frames rendered');
  assert.equal(workStatus(data).meter, 35);
  data.events.push(event('render_progress', 2, {phase:'encoding', progress:.94, stitch_stage:'muxing'}));
  assert.equal(workStatus(data).task, 'Combining video and audio');
  assert.equal(workStatus(data).meter, null);
  data.events.push(event('render_progress', 3, {phase:'verification', progress:1}));
  assert.equal(workStatus(data).label, 'Working');
  assert.equal(workStatus(data).task, 'Verifying the rendered video');
  assert.equal(workStatus(data).meter, null);
});

test('final checks and terminal states supersede renderer progress', () => {
  const events = [event('render_progress', 1, {phase:'frames', progress:.5}), event('final_moderation_checked', 2)];
  assert.equal(workStatus(run('editing', events)).meter, null);
  for (const [status, label, task] of [
    ['final_review', 'Waiting for you', 'Video ready for review'],
    ['video_complete', 'Complete', 'Video accepted'],
    ['plan_complete', 'Complete', 'Ideas saved'],
    ['idea_review', 'Waiting for you', 'Storyboards ready for review'],
    ['failed', 'Needs attention', 'Needs attention'],
  ]) {
    const result = workStatus(run(status, events));
    assert.equal(result.label, label);
    assert.equal(result.task, task);
    assert.equal(result.working, false);
    assert.equal(result.meter, null);
  }
});

test('connection loss and old revisions cannot imply current work progress', () => {
  const events = [{...event('render_progress', 1, {phase:'frames', progress:.8}), occurred_at:'2026-09-10T12:00:00Z'}];
  const result = workStatus(run('editing', events), {connectionLost:true});
  assert.equal(result.label, 'Updates paused');
  assert.equal(result.working, false);
  assert.equal(result.meter, null);
  assert.equal(result.updatedAt, '2026-09-10T12:00:00Z');
  assert.equal(workStatus(run('editing', events, {revision:2})).meter, null);
  assert.equal(workStatus(null), null);
  assert.equal(workStatus(run('idea_review'), {shared:true}).label, 'Waiting for review');
  assert.equal(workStatus(run('idea_review', [], {review_expired:true})).label, 'Review expired');
});

test('optional malformed renderer data cannot create false percent or frame counts', () => {
  for (const progress of [-1, 1.1, NaN, '50', null]) {
    assert.equal(workStatus(run('editing', [event('render_progress', 1, {phase:'frames', progress})])).meter, null);
  }
  assert.equal(workStatus(run('editing', [event('render_progress', 1, {phase:'frames', progress:.4, rendered_frames:100, total_frames:50})])).detail, 'Renderer progress');
});

test('closed runs do not keep submitted agents apparently active', () => {
  const data = run('failed', [event('creative_step_started', 1, {role:'write_shots'})]);
  assert.equal(creativePlanSteps(data)[0].state, 'Unconfirmed');
  assert.equal(workStatus(data).working, false);
});


test('definitive status errors stop polling without claiming network loss', () => {
  const missing = pollFailure({definitive:true, message:'This run is unavailable.'});
  assert.equal(missing.stop, true);
  assert.equal(missing.connectionLost, false);
  const status = workStatus(run('editing'), {updateIssue:missing});
  assert.equal(status.label, 'Updates unavailable');
  assert.equal(status.working, false);
  assert.equal(status.detail, 'This run is unavailable.');
  const network = pollFailure({network:true});
  assert.equal(network.stop, false);
  assert.equal(network.connectionLost, true);
  const retry = pollFailure({message:'Service temporarily unavailable'});
  assert.equal(retry.stop, false);
  assert.equal(workStatus(run('editing'), {updateIssue:retry}).label, 'Updates delayed');
});

test('observed final checks replace render meters and stale media phases', () => {
  const events = [event('workflow_phase', 1, {role:'media_gathering'}),event('render_progress', 2, {phase:'frames', progress:.5})];
  assert.equal(workStatus(run('editing', events)).task, 'Rendering video frames');
  events.push(event('workflow_phase', 3, {role:'final_moderation'}));
  assert.equal(workStatus(run('editing', events)).task, 'Checking final video content');
  assert.equal(workStatus(run('editing', events)).meter, null);
  assert.equal(project(run('editing', events)).index, 5);
});

test('media work cannot pull the assembly stage backwards', () => {
  const events = [event('workflow_phase', 1, {role:'media_gathering'}), event('assets_ready', 2), event('render_submitting', 3)];
  const data = run('editing', events);
  assert.equal(project(data).index, 4);
  assert.equal(workStatus(data).task, 'Assembling your video');
  assert.equal(creativePlanSteps(run('editing', [event('creative_step_started',1,{role:'write_shots'})]))[0].state, 'Unconfirmed');
});
