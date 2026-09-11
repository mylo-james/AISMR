# MyloWare Knowledge Base

Single source of truth for AI agent expertise. Each topic is covered in exactly one place.

---

## Knowledge Structure

```
data/knowledge/
+-- asmr/                    # ASMR-specific content
|   +-- asmr-niche-guide.md
+-- composition/             # Visual framing
|   +-- vertical-video-framing.md
+-- editing/                 # Post-production
|   +-- text-overlay-guide.md
|   +-- transitions-guide.md
+-- ideation/                # Creative generation
|   +-- unique-object-generation.md
+-- platform/                # TikTok specifics
|   +-- community-guidelines.md
|   +-- tiktok-algorithm.md
|   +-- tiktok-specs.md
+-- production/              # Pre-production
|   +-- shot-types-reference.md
|   +-- video-scripting-guide.md
+-- psychology/              # Audience behavior
|   +-- engagement-psychology.md
+-- publishing/              # Distribution
|   +-- caption-writing.md
|   +-- hashtag-guide.md
+-- storytelling/            # Narrative craft
|   +-- hooks-and-retention.md
|   +-- viral-hooks.md
+-- video-generation/        # AI video (Veo3)
|   +-- veo3-pitfalls.md
|   +-- veo3-prompting-guide.md
+-- workflow/                # Agent coordination
|   +-- process-overview.md
+-- comedic-timing.md        # Humor/timing theory
+-- remotion-api-docs.md     # Remotion framework
+-- remotion-vertical-video-guide.md
```

---

## Agent -> Knowledge Mapping

| Agent | Primary Knowledge |
|-------|-------------------|
| **Ideator** | unique-object-generation, tiktok-algorithm, viral-hooks, engagement-psychology |
| **Producer** | veo3-prompting-guide, shot-types-reference, video-scripting-guide, vertical-video-framing |
| **Editor** | remotion-api-docs, remotion-vertical-video-guide, transitions-guide, text-overlay-guide |
| **Publisher** | caption-writing, hashtag-guide, tiktok-specs, community-guidelines |

---

## Knowledge Principles

### Single Source of Truth
Each topic exists in **one document only**. If you need to update a topic, update that one file.

### No Overlap
Documents don't repeat each other's content. Cross-reference instead.

### Focused Scope
Each doc covers one topic deeply rather than many topics shallowly.

### Retrieval-Optimized
- Clear H1/H2 headings for section retrieval
- Structured tables for quick reference
- Actionable examples, not just theory

---

## Project-Specific Knowledge

Project knowledge goes in `data/projects/{project_id}/knowledge/`, not here.

| Location | Content | Example |
|----------|---------|---------|
| Global KB (here) | Universal best practices | veo3-prompting-guide.md |
| Project KB | Project-specific context | zodiac-signs.md for AISMR |
| Agent YAML | Per-task instructions | system_prompt in agent config |

---

## Last Updated

2024-12-06
