# SkellyPings agent guidance

The lightweight telemetry client and separately configured server are distinct
Python projects. The client belongs under `skellypings/`; server behavior belongs
under `server/`, with its own dependencies. Keep server dependencies out of the
client package and preserve the existing event/privacy contract.

At milestones, checkpoint the objective, decisions and reasons, exact checkout,
which side of the client/server boundary is affected, existing interfaces,
validation evidence and next action with targeted source paths. After compaction
or handoff, read the checkpoint and [README.md](README.md), then the relevant
`pyproject.toml`, code and diff for that side. Search existing event definitions
and configuration before inventing replacements. Deployment examples in the
README do not authorize cloud changes or sending test telemetry.

Continue authorized work once purpose, ownership and validation are clear;
ask only for consequential intent that inspection cannot resolve. Follow
workspace policy when present; preserve user edits/data and leave Git and gCloud
state to the human. Use isolated tests with mocked network effects unless live
traffic is explicitly authorized, and use ignored or external temporary outputs.
