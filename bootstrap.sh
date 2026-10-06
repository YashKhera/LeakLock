#!/usr/bin/env bash
# Run once from the repo root: bash bootstrap.sh
set -euo pipefail

dirs=(
  docs
  infra
  backend/shared
  backend/ingest
  backend/alert
  backend/leakcheck
  backend/offlinecheck
  backend/dailystats
  backend/api
  backend/tests
  simulator/certs
  frontend
  firmware
  scripts
)

for d in "${dirs[@]}"; do
  mkdir -p "$d"
  # keep empty folders in git (certs folder content stays ignored)
  [ -f "$d/.gitkeep" ] || touch "$d/.gitkeep"
done

if [ ! -f README.md ]; then
  cat > README.md <<'EOF'
# LeakLock

Water tank overflow and leak monitor (ESP32 + AWS). Solo hackathon build.

See `docs/00-master-spec.md` for requirements, architecture and the phase plan.

Status: Phase 0 (foundations).
EOF
fi

echo "Skeleton ready. Next: copy the master spec to docs/00-master-spec.md and commit."
