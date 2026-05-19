name: {{name}}
description: >
  {{description}}
source_pack: null
entrypoint: stub
industries: [{{industries}}]

agents:
  - slug: {{slug}}-supervisor
    role: "Coordinator for the {{name}} squad"
    authority: gatekeeper

tools: []

accepts:
  - HANDOFF
  - HITL_REQUEST

emits:
  - DECISION_RECORD

gates: []

invoke:
  notes: "Stub. Flip to a real entrypoint after wiring tools/MCP/skill."
