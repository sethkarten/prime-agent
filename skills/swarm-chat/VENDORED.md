# Vendored: SwarmChat

`src/swarmchat/` is an unmodified copy of upstream SwarmChat:
https://github.com/Pranjal2041/swarmchat (MIT, no dependencies).

- Upstream commit: fb5aa6a05b40fba70f749cd3017aeb3219ab6ac1 (2026-10-29)
- License: LICENSE-SwarmChat.txt in this directory

It ships inside this skill's wheel next to the `swarm_chat` helper module so a
session kernel can run a family-local room server with no network access. Do
not edit files under `src/swarmchat/`; re-vendor from upstream instead.
