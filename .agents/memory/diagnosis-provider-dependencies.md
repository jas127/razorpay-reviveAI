---
name: Diagnosis provider dependencies
description: Provider SDK and credential availability constraints for the diagnosis phase.
---

Optional LLM provider SDKs should be imported lazily inside provider calls, and the diagnosis batch must retain a deterministic fallback when SDKs or credentials are unavailable.

**Why:** The Replit Python runtime may reject installing packages into its immutable system environment, and hackathon runs may not have both provider credentials configured.

**How to apply:** Keep provider packages declared in requirements.txt, but never make database initialization or the diagnosis batch depend on importing them before the fallback path is available.