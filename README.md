# kf-boot

`kf-boot` is the KERI Foundation boot service for hosted witnesses and watchers.

It is a thin control-plane service. It does not manage user keys, create user
wallets, or act as a second KERI app.

## Contract Status

This README is the canonical `locksmith` <-> `kf-boot` contract for the
current conference implementation.

If the code does not match this document, change the code or update this
document deliberately. Do not let the contract drift.

## Scope

`kf-boot` owns:

- public bootstrap discovery
- authenticated onboarding
- authenticated account management for onboarded accounts
- account, session, and hosted-resource metadata
- hosted witness and watcher allocation through local boot APIs

`kf-boot` does not own:

- local AID creation or custody
- signing on behalf of users
- witness registration or witness authentication flows
- watcher OOBI resolution or watcher protocol flows
- local watcher or sidecar behavior

## Public Surfaces

`kf-boot` exposes two public surfaces:

- onboarding surface: ephemeral first contact and onboarding
- approved-account surface: management for already onboarded accounts

The split between onboarding and approved-account access is part of the
contract. One process may serve both later, but that is an implementation
detail, not the interface.

## Transport And Auth

Public discovery stays simple:

- `GET /health`
- `GET /bootstrap/config`

All other onboarding and account-management operations use:

- CESR-over-HTTP over HTTPS/TLS
- KRAM for request authentication

ESSR is a post-conference lift.

Auth principals:

- onboarding is authenticated by a hidden ephemeral onboarding AID
- approved-account management is authenticated by the permanent account AID

First-contact rule:

- the first authenticated onboarding exchange must include or be preceded by
  the ephemeral AID inception or keystate material so the server can resolve
  sender state for KRAM

Boot-server authentication:

- the boot server has its own durable public AID
- signed replies prepend the boot server KEL so the client can verify the
  service by percolated discovery
- conference v1 must not require pre-installed boot-server inception material

## Onboarding Flow

1. `locksmith` fetches `GET /bootstrap/config` from the onboarding surface.
2. `locksmith` creates a hidden ephemeral onboarding AID locally.
3. `locksmith` sends the ephemeral AID inception or keystate material to the
   onboarding surface.
4. `locksmith` sends authenticated `exn /onboarding/session/start`.
5. `kf-boot` allocates the witness pool and creates the required hosted watcher.
6. `locksmith` creates the permanent public account AID locally using the
   returned witness list.
7. `locksmith` completes local witness registration and resolves returned
   witness and watcher OOBIs.
8. `locksmith` sends authenticated `exn /onboarding/account/create`.
9. `locksmith` sends authenticated `exn /onboarding/complete`.
10. The ephemeral onboarding AID disappears after the session.
11. Future operations use the approved-account surface with the permanent
    account AID.

V1 rules:

- one vault maps to one onboarded public account AID
- the permanent account AID is always a local wallet AID
- witness profile is either `1-of-1` or `3-of-4`
- one hosted watcher is required before onboarding completes

## Message Routes

The HTTP endpoint can stay small. Business routes live in authenticated KERI
messages.

Public routes:

- `GET /health`
- `GET /bootstrap/config`

Onboarding routes:

- `exn /onboarding/session/start`
- `qry /onboarding/session/status`
- `exn /onboarding/account/create`
- `exn /onboarding/complete`
- `exn /onboarding/cancel` optional cleanup

Approved-account routes:

- `qry /account/witnesses`
- `qry /account/watchers`
- `qry /account/watchers/status`
- `exn /account/witnesses/delete`
- `exn /account/watchers/delete`

## State And Retry Rules

Session states:

- `started`
- `witness_pool_allocated`
- `account_created`
- `watcher_created`
- `completed`
- `expired`
- `failed`
- `cancelled`

Account states:

- `pending_onboarding`
- `onboarded`
- `failed`

Required behavior:

- sessions must expire and support cleanup
- `/onboarding/account/create` must be idempotent within a session
- `/onboarding/complete` must be idempotent within a session
- created witness and watcher ids must be recorded before replying
- retries must return the same allocated resources, not create duplicates
- partial downstream failure must move the session to `failed`
- blind retry after failure must not create a second resource set

## Development

Development requires an environment that already has `keri` and `hio`
installed.

Required environment:

- `KF_BOOT_WIT_BOOT_URL`
- `KF_BOOT_WIT_PUBLIC_URL`
- `KF_BOOT_WAT_BOOT_URL`
- `KF_BOOT_WAT_PUBLIC_URL`

Run:

```bash
KF_BOOT_WIT_BOOT_URL=http://127.0.0.1:5631 \
KF_BOOT_WIT_PUBLIC_URL=https://example.com:5632 \
KF_BOOT_WAT_BOOT_URL=http://127.0.0.1:7631 \
KF_BOOT_WAT_PUBLIC_URL=https://example.com:7632 \
python -m kfboot.cli
```
