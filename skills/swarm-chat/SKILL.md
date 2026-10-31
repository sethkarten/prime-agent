---
name: swarm-chat
description: Team chat room for a whole agent tree - a family-local SwarmChat server (vendored upstream, no network access needed) with one shared #swarm channel that the root session and every spawned subagent join. Use when a task is solved by a team of subagents: post findings, claims, and results to the room channel instead of relaying them pairwise through the parent, read what teammates posted, and wait for teammate messages. Ships the swarm_chat module in the session kernel and the swarmchat CLI for bash().
---

# SwarmChat team room

Every session in this agent tree — you and each of your spawned subagents —
shares one local chat channel, `#swarm`. The vendored SwarmChat server (MIT, no
dependencies) boots lazily on first use, binds 127.0.0.1 only, and keeps its
data under this rollout's agent directory, so the room needs no network access
and dies with the sandbox. Each session joins under its own identity
(`root` for the top session, `s-<id>` for children).

## When to use it

Team work: a task decomposed into lanes run by subagents. The room replaces
pairwise relaying through the parent with one shared feed — a lane sees what
every other lane found, and the orchestrator reads the channel instead of
polling each child. Post:

- claims before starting a unit of work ("taking the flag matrix") so two
  lanes do not do the same work
- findings and results ("cmatrix `--help` exits 2 on unknown flags; details in
  /notes/flags.txt") so lanes build on each other instead of rediscovering
- completions ("lane done: wrote compile.sh + main.c, compiles")

Keep deliverables in files and post pointers to them; the channel carries the
coordination, files carry the work.

## From the session kernel

The `swarm_chat` module is pre-imported in every session:

    room = await swarm_chat.room(name="flags-lane")     # join; idempotent
    await swarm_chat.send("claimed the flag matrix lane")
    msgs = (await swarm_chat.read())["messages"]         # new for me, oldest first
    history = (await swarm_chat.read(all_=True))["messages"]
    res = await swarm_chat.wait(timeout=30)              # block for the next message

`room()` boots the room the first time any session calls it and returns
`{"server", "user", "name", "channel", "data_dir"}`. `send`, `read` and
`wait` boot it too (room membership is implied), so a lane can start with a
single `send`. `read()` with no arguments returns the messages new for this
session and marks them read; `last=N` reads the newest N of history.

## Waiting without spending turns

A turn should not idle on `wait()`. Use `wait_command()` for the
notification-shaped wait:

    cmd = await swarm_chat.wait_command()     # env for the CLI is exported
    h = bash(cmd)                             # background, ends the turn
    # ... the [bash-done] wake delivers the message as JSON in the output

`wait_command(chat="#swarm", timeout=300, from_="root")` narrows what it waits
for. The command exits 0 with the new message on stdout (JSON, `--json`), or 4
on timeout.

## From bash

`swarmchat` is installed in the session kernel's venv; the same environment
variables are exported, so from a kernel `bash()` the plain CLI works too:

    python -m swarmchat read --json
    python -m swarmchat send "#swarm" "lane done" --attach ./notes

The CLI offers everything the room has (threads, mentions, history search,
attachments); `python -m swarmchat --help` lists it. Use it from bash when
you want `--json` output or file attachments.

## Notes for the orchestrator

- `room()` once at the start, then spawn lanes; each child joins itself with
  its first `send` — no room setup belongs in a lane's prompt beyond "post to
  #swarm".
- Read the channel (`read(all_=True)`) after wakes to catch everything lanes
  posted while you were away, then collect deliverable files from disk.
- The room is not a budget: children still report completion through their
  replies so the roster can be collected and torn down.
