---
title: Cap how many attended chat turns run at once, at the one dispatch every send and Resume reaches
status: draft
author: kirocrew agent session, directed by jeeshofone
created: 2026-11-02
last-audited: 2026-11-02
audited-at: d6d40e23a2
doc-pr: 28633
implementation-prs: []
tracking-issues: [25055]
supersedes: []
superseded-by: []
---

# RFC: Cap how many attended chat turns run at once, at the one dispatch every send and Resume reaches

Status: draft. Acceptance requested from a maintainer of dashboard turn dispatch. Part of [#25055](https://github.com/kirodotdev/KiroCrew/issues/25055). Main-side references were read at `d6d40e23a2`. References to the bulk Resume were read on [#27512](https://github.com/kirodotdev/KiroCrew/pull/27512) at head `0dbab773cb`, which is open and not on main.

## Summary

Nothing limits how many dashboard chat turns a person starts can run together. The cold-start semaphore paces boots, and the background-turn cap counts only unattended app-owned turns. Resuming many interrupted sessions, one tab at a time or with the bulk Resume in #27512, leaves every one of them running at once after its start ends.

This document recommends one attended-turn semaphore, held by `DashboardState` beside `_bg_turn_sema`, taken by one helper that both turn-start paths call (`spawn_guarded_turn` and the run arm of `_ChatSlot.enqueue_or_run_prompt`), released when the turn ends and while the turn waits on a person. Turns queue at the cap and never fail because of it. A turn a person typed goes ahead of queued Resumes. The default is `auto`, sized from the host, and `0` turns the cap off.

## Motivation

### Current state on main

- `SessionManager._start_sem` holds a permit from runtime spawn until the session's MCP servers have reported (`src/kiro_crew/cold_start_sizing.py`). It paces boots, not turns.
- Every dashboard turn is started by `spawn_guarded_turn` (`src/kiro_crew/dashboard/turn_dispatch.py`), which sets only the wall-clock ceiling. Its callers on main are `chat_api/chat_send.py`, `chat_turn/queue_drain.py`, `chat_turn/queue_cycle.py`, `mate_welcome.py` and `slack/gateway_runtime/nudge_dashboard_fire.py`.
- A second path starts a turn without `spawn_guarded_turn`: `_ChatSlot.enqueue_or_run_prompt` (`src/kiro_crew/dashboard/state_parts/chat_slot.py`) runs a prompt on an idle slot with a bare `asyncio.create_task(run_chat_coro(...))`. Its callers on main are `session_deliver.py`, `slack/gateway_runtime/result_egress.py`, `server_runtime/workflow_startup.py` and the issue radar's `crew_runtime_session.py`.
- `run_background_turn` (`state_parts/dashboard_services.py`) waits on `_bg_turn_sema` only when `slot.unattended`, and passes an attended slot straight through. Only some send paths wrap a turn in it; the queue drain does not.
- `agent.admission_gate` defers cron firings, TaskRunner steps and workflow agent calls under memory pressure, and never gates a direct chat turn.

### The problem

#25055 measured it with a real `SessionManager` at the 4-permit floor of `agent.cold_start_concurrency`: 40 resumed sessions boot 5 at a time and then hold 40 turns at once. A session whose agent is still running needs no start, so its turn begins at once. Each of those turns holds a model stream, its MCP servers and its tool processes for the whole turn, so host memory and model rate limits take the load that the cold-start queue was meant to spread.

#27512 makes this one click. Its `_resume_candidates` runs one `gather` over every candidate and relies on `_start_sem` for pacing, which ends when each start ends.

## Goals

- At most N attended turns run at once, where N is a setting with a host-sized default.
- Every path that starts a dashboard turn passes the same check, so no path can bypass it.
- A turn over the cap waits and then runs. It never fails or is dropped because of the cap.
- A message a person typed is not stuck behind a bulk Resume.
- The waiting count is visible, as `background_turn_stats` makes the unattended queue visible.

## Non-goals

- Changing the unattended cap, `dashboard.max_background_turns`, or which turns count as unattended.
- Changing cold-start pacing or the person reserve on `_start_sem`.
- Gating cron, TaskRunner or workflow agent calls, which `agent.admission_gate` already covers.
- Steering into a turn that is already running. A steer is not a new turn and takes no permit.

## 1. Design

### 1.1 The semaphore

`DashboardState` gains an `_attended_turn_sema` field, created lazily by an accessor and resized only while idle, the same way the `_bg_turn_sema` field and its `_background_turn_sema()` accessor work today, with `_attended_turns_running` and `_attended_turns_waiting` counters. Two semaphores rather than one shared pool, so a crew of unattended turns cannot starve people and people cannot starve the crew.

### 1.2 Where it is taken

One helper, `run_attended_turn(state, slot, coro)`, wraps a turn coroutine. When the slot is attended and the cap is on, it acquires a permit, runs the turn, and releases the permit in a `finally`. Both turn-start paths call it: `spawn_guarded_turn` wraps the coroutine it is given, and the run arm of `enqueue_or_run_prompt` wraps `run_chat_coro(...)` before its `create_task`. Those two are the only changed call sites; the callers of each are unchanged. In `spawn_guarded_turn` the wall-clock ceiling starts after the permit is held, so time spent waiting is not counted against the turn. A turn that holds an attended permit and then waits on `SessionManager._start_sem` (the cold-start limit) keeps its permit while it waits there, because that wait is on the system, not on a person (section 1.2a covers the person case). Unattended slots skip the helper and stay under `run_background_turn` as today. A guard test lists every `asyncio.create_task` in `src/kiro_crew/dashboard/` whose coroutine runs a chat turn and fails if one does not pass through the helper.

### 1.2a Waiting on a person

A turn paused on a tool-approval prompt or a question card releases its permit while it waits and takes one again before it continues, queued at priority 0. Without this, N paused turns would hold every permit and a newly typed send would wait for as long as nobody answers. The release and re-acquire sit in the same approval and question waits that already mark the slot as waiting on a person.

### 1.3 Order

Waiters are served by priority, then arrival. A send a person typed, or a Resume pressed on one tab, is priority 0. A Resume from the bulk button (`via == RESUME_ALL_VIA` in #27512) is priority 1. This needs a small priority waiter queue in place of a bare `asyncio.Semaphore`; it sits behind the same acquire and release calls.

### 1.4 What the person sees

A waiting slot shows its existing queued state, with a line saying the turn starts when one of the running turns ends. The status payload gains `attended_turns: {cap, running, waiting}`.

### 1.5 Setting

`dashboard.max_attended_turns`: `auto` (default), `0` for off, or an integer from 1 to 64. `auto` is twice the effective `agent.cold_start_concurrency`, so 8 on the smallest host and 32 on the largest. This is an open question for the maintainers (§7).

## 2. Alternatives considered

| Option | Result |
| --- | --- |
| Do nothing | A bulk Resume of tens of sessions runs every turn at once once starts end, as #25055 measured. |
| Pace inside #27512's `_resume_candidates` only | Fixes the button but not tab-by-tab Resumes or ordinary sends, and leaves a second limit in a second place. |
| Count attended turns in `_bg_turn_sema` | One pool, but unattended crews and people then compete for the same permits, and `max_background_turns` changes meaning. |
| Gate attended turns in `agent.admission_gate` | That gate defers by memory pressure, not by count, and its contract says chat turns are never gated. |
| Reject a turn over the cap | Loses the person's message or Resume. Queueing only starts it late. |

## 3. Interaction with #27512

#27512 adds the bulk Resume and does not change dispatch. This design does not depend on any code in #27512 and can land before or after it on main. The only link is the priority in §1.3, which reads #27512's `RESUME_ALL_VIA`. If this lands first, every Resume is priority 0 until #27512 adds the marker. #27512's module docstring, which says starts are paced by `_start_sem`, gets one sentence naming this cap when both are on main.

## 4. Migration plan

| Phase | PR | Exit criteria |
| --- | --- | --- |
| 1. Cap | one PR on main | With the cap at N, N+K attended turns started together run at most N at once and all K finish; turns started through `enqueue_or_run_prompt` are counted too; a turn waiting on a person holds no permit; an unattended slot takes no attended permit; the ceiling excludes wait time; `attended_turns` is in the status payload; `0` turns the cap off |
| 2. Priority | same PR, or a follow-up once #27512 merges | A typed send queued after bulk Resumes starts before them |

## 5. Test plan

One regression test in `test/test_turn_dispatch.py`: set the cap to 2, call `spawn_guarded_turn` for 5 attended slots whose coroutines each wait on an event, and assert that only 2 are inside the coroutine and 3 are counted as waiting. Then release the events and assert all 5 finish. On main all 5 enter at once, so the test fails. The same test repeats the count with turns started through `enqueue_or_run_prompt`, and checks that a turn parked on an approval wait frees its permit for a waiting send. Phase 2 adds one ordering assertion.

## 6. Backward compatibility

The cap only delays turns. Setting `dashboard.max_attended_turns: 0` restores today's behaviour exactly. No stored format changes.

## 7. Open questions

1. Is `2 × cold_start_concurrency` the right `auto` default, or should it read memory headroom, as `agent.admission_gate` does?
2. Should a crewmate's own turn that a person opened count as attended, as `_ChatSlot.unattended` says today?
