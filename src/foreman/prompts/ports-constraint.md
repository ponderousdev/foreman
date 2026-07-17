# Environment constraint — no port binding (the `ports` capability is absent)

This environment cannot give you exclusive ports: agents share one network
namespace, and a port you bind is a port a sibling agent collides with.
Non-negotiable here:

- Do NOT start long-lived dev servers (`vite dev`, `convex dev`,
  `npm start`, `next dev`, or similar). Run commands that terminate.
- Do NOT publish container ports (`docker run -p`, compose `ports:`
  mappings) — the Docker daemon binds in this same shared namespace, so a
  container-published port collides exactly like a dev server.
- There is no browser and no observer here. Verify behavior with tests;
  port-binding e2e checks are CI's job (GitHub Actions), not yours.
