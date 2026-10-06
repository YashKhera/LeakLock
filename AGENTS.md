# AGENTS.md: LeakLock

## What this project is
LeakLock is a water tank monitor for hostels and housing societies. An ESP32 with an ultrasonic sensor measures tank level and cuts the pump when the tank is full. The cloud (AWS) stores readings, detects overnight leaks, sends alerts and shows a dashboard. Solo hackathon project, deadline 10 Oct 2026.

Software is built first against a Python simulator that behaves exactly like the device. Hardware comes last and must need no cloud changes.

## Source of truth
- `docs/00-master-spec.md` holds requirements, architecture, contracts and phases. **Read the relevant section before every task.**
- The data contract (MQTT topics, telemetry JSON, DynamoDB tables, REST API) is in section 6 of the spec. **Do not change a contract without updating the spec in the same commit and saying so.**

## Architecture in brief
ESP32 or simulator -> AWS IoT Core (MQTT/TLS) -> IoT Rule -> ingest Lambda -> DynamoDB.
EventBridge schedules -> leak-check, offline-check, daily-stats Lambdas -> alert Lambda -> SNS email.
React dashboard (Amplify Hosting) -> API Gateway HTTP API -> api Lambda -> DynamoDB / Bedrock. Cognito for auth.
Infrastructure is defined with AWS SAM in `infra/template.yaml`.

## Stack and conventions
- Backend: Python 3.12, type hints, `pytest` + `moto` for AWS mocks.
- Frontend: React + Vite + TypeScript + Recharts.
- IaC: AWS SAM. Region `ap-south-1`. AWS CLI profile `leaklock`.
- Every AWS resource is named with the prefix `leaklock-` and tagged `Project=leaklock`.
- One branch per module (for example `feat/ingest`). Small commits with clear messages.

## Rules
1. Work on **one task at a time**, exactly as described in the task spec. Do not touch anything listed as out of scope.
2. No secrets in code or git: certificates, keys, `.env`, and AWS credentials are never committed or printed.
3. Thresholds, calibration values, model IDs and table names come from config or environment variables, never hard-coded.
4. Every Lambda gets unit tests. Run tests before saying a task is done.
5. Overflow protection must never depend on the cloud. Keep device safety logic in the firmware and simulator.
6. Do not add a dependency without stating why. Prefer the standard library and boto3.
7. Use least-privilege IAM for each Lambda.
8. If the spec is unclear or contradicts the task, stop and ask instead of guessing.
9. When finished, summarise what changed, how to verify it, and anything left undone.

## Commands (filled in as phases land)
- Tests: `pytest backend/tests`
- Build and deploy: `scripts/deploy.sh` (added in Phase 1)
- Simulator: `python simulator/sim.py` (added in Phase 1)
