# MyloWare Workflow Overview

What happens at each stage of the content pipeline.

---

## Pipeline Overview

```
User Request
     |
     ▼
+-------------+
| SUPERVISOR  |  Routes request, maintains context
+-------------+
     |
     ▼
+-------------+
|  IDEATOR    |  Generates creative concepts
+-------------+
     |
     ▼
+-------------+
|  PRODUCER   |  Creates script & video prompts
+-------------+
     |
     ▼
+-------------+
|   EDITOR    |  Composes final video
+-------------+
     |
     ▼
+-------------+
|  PUBLISHER  |  Posts to TikTok
+-------------+
     |
     ▼
Published Content
```

---

## Agent Responsibilities

### Supervisor Agent

**Role**: Routes requests and maintains conversation context.

| Input | Process | Output |
|-------|---------|--------|
| User message | Understands intent | Route to appropriate agent |
| User preferences | Stores in memory | Personalized responses |
| Status queries | Checks pipeline | Status update |

**Key Functions**:
- Parse user requests
- Route to correct workflow stage
- Remember user preferences
- Provide status updates
- Handle errors gracefully

---

### Ideator Agent

**Role**: Generates creative video concepts.

| Input | Process | Output |
|-------|---------|--------|
| Topic/theme | Research trends | Video concept |
| Niche context | Apply knowledge | Hook + premise |
| Style preferences | Consider brand | Creative direction |

**Key Functions**:
- Generate unique concepts
- Research current trends
- Apply engagement patterns
- Create compelling hooks
- Ensure brand alignment

**Output Format**:
```
- Concept title
- Hook (first 3 seconds)
- Core idea/premise
- Key visual elements
- Suggested duration
- Target emotion
```

---

### Producer Agent

**Role**: Transforms concepts into production-ready scripts.

| Input | Process | Output |
|-------|---------|--------|
| Concept from Ideator | Script structure | Scene-by-scene script |
| Platform specs | Apply constraints | Video prompts |
| Niche knowledge | Style application | Technical specs |

**Key Functions**:
- Write detailed scripts
- Create video generation prompts
- Specify camera angles/movements
- Plan pacing and beats
- Include technical requirements

**Output Format**:
```
Scene 1 (0-3s):
- Visual: [description]
- Camera: [movement]
- Audio: [sound/music]
- Text: [overlay if any]

Scene 2 (3-10s):
...
```

---

### Editor Agent

**Role**: Composes final video from generated assets.

| Input | Process | Output |
|-------|---------|--------|
| Script + prompts | Generate video clips | Assembled video |
| Brand style | Apply editing style | Final render |
| Audio specs | Add sound/music | Complete package |

**Key Functions**:
- Generate video clips (via Veo3 or similar)
- Assemble sequences
- Add text overlays
- Apply transitions
- Add sound/music
- Final quality check

**Output**:
- Rendered video file
- Thumbnail options
- Asset list

---

### Publisher Agent

**Role**: Posts content to TikTok with optimization.

| Input | Process | Output |
|-------|---------|--------|
| Final video | Prepare upload | Posted content |
| Brand context | Write caption | Optimized metadata |
| Hashtag knowledge | Select hashtags | Discovery optimization |

**Key Functions**:
- Upload video to TikTok
- Write engaging caption
- Select relevant hashtags
- Schedule optimal posting time
- Handle upload verification

**Output**:
- Posted video URL
- Metadata (caption, tags)
- Post confirmation

---

## Workflow States

### Request States

| State | Description |
|-------|-------------|
| `pending` | Waiting in queue |
| `in_progress` | Currently being processed |
| `awaiting_approval` | Human review needed |
| `completed` | Successfully finished |
| `failed` | Error occurred |
| `cancelled` | User cancelled |

### Human-in-the-Loop Gates

Points where human approval may be required:

| Gate | When | Why |
|------|------|-----|
| **Concept Review** | After Ideator | Approve creative direction |
| **Script Review** | After Producer | Check script accuracy |
| **Pre-Publish** | Before Publisher | Final approval |

---

## Data Flow

### Knowledge Flow

```
Global KB -------------+
                       +--► Agent Context
Project KB -----------+

Agent receives:
- Universal best practices (Global)
- Project-specific context (Project)
- Agent-specific instructions (YAML)
```

### Memory Flow

```
Conversation --► Short-term Memory --► Session Context
Preferences --► Long-term Memory --► Personalization
```

---

## Error Handling

### Common Failure Points

| Stage | Potential Failure | Recovery |
|-------|-------------------|----------|
| Ideator | No good concepts | Retry with different params |
| Producer | Prompt too complex | Simplify, split scenes |
| Editor | Video gen failure | Regenerate, adjust prompt |
| Publisher | Upload failure | Retry, check credentials |

### Retry Strategy

1. Log error details
2. Attempt retry (up to 3 times)
3. If retry fails, notify user
4. Provide manual recovery options

---

## Timing Expectations

### Typical Processing Times

| Stage | Duration | Notes |
|-------|----------|-------|
| Supervisor routing | < 5s | Fast |
| Ideator generation | 30-60s | Depends on research |
| Producer scripting | 30-60s | Based on complexity |
| Editor rendering | 2-10 min | Video generation slowest |
| Publisher posting | 30-60s | Upload dependent |

**Total**: 5-15 minutes for full pipeline

---

## Quality Checkpoints

### Automated Checks

| Checkpoint | What's Checked |
|------------|----------------|
| After Ideator | Concept completeness |
| After Producer | Script structure valid |
| After Editor | Video renders successfully |
| After Publisher | Post confirmed |

### Quality Signals

| Good Signs | Bad Signs |
|------------|-----------|
| Complete outputs | Partial/truncated |
| Consistent style | Style drift |
| Clean transitions | Jarring cuts |
| Clear audio | Muffled/distorted |

---

## Quick Reference

### Pipeline at a Glance

```
User -> Supervisor -> Ideator -> Producer -> Editor -> Publisher -> TikTok
        (route)      (concept)  (script)   (video)   (post)
```

### Key Handoffs

| From | To | What's Passed |
|------|-----|---------------|
| User | Supervisor | Request + context |
| Supervisor | Ideator | Parsed intent + preferences |
| Ideator | Producer | Concept + hook + direction |
| Producer | Editor | Script + prompts + specs |
| Editor | Publisher | Video + assets |

---

## Last Updated

2024-12-06
