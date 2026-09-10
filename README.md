# Run Cursor Cloud Agents on Tensorlake Sandboxes

Use Tensorlake Sandboxes as the self-hosted machines for Cursor Cloud Agents.

Cursor keeps the agent loop and the model in its cloud. Every command, file
edit, build, and repository checkout runs in a Tensorlake sandbox that you own.
A small orchestrator, which also runs in a Tensorlake sandbox, claims Cursor
requests and gives each one its own worker sandbox. Idle workers suspend.
Follow-up messages resume them with the checkout and caches intact.

```
Cursor cloud ── pending requests ──▶ orchestrator sandbox
                                       ├─ agent worker controller --spawn cursor-tl-spawn
                                       ├─ janitor: idle worker ➜ suspend, old ➜ terminate
                                       └─ wake: claimed-offline request ➜ resume sandbox
                                                       │
worker sandbox (one per agent) ── agent worker ── outbound HTTPS ──▶ Cursor
```

## Prerequisites

Cursor:

- A Cursor **Enterprise** team. Cursor offers pools only on Enterprise.
- A **service-account API key** from Dashboard → Settings → API Keys → Service
  Accounts. Pool workers reject personal and team keys.
- A team administrator who can enable self-hosted machines and GitHub token
  minting.
- The Cursor GitHub App connected at the team level, with access to each
  repository agents will work on.

No Enterprise team? A personal key on any plan can run one worker in one
sandbox. See [My Machines](#my-machines).

Tensorlake:

- An account and API key from <https://cloud.tensorlake.ai>.
- The `tl` CLI, optional: `curl -fsSL https://tensorlake.ai/install | sh`. This
  guide uses it to list sandboxes and read worker logs.

Local machine:

- Python 3.10 or newer and [uv](https://docs.astral.sh/uv/).
- Nothing stays running here after setup.

## Set up Cursor

A Cursor team administrator does this once:

1. Open **Dashboard → Cloud Agents → Self-Hosted**.
2. Enable **Self-hosted Machines**.
3. Enable GitHub token minting for self-hosted pool workers.
4. Give the Cursor GitHub App access to each repository.

## Quick start

```bash
git clone https://github.com/tensorlakeai/cursor-cloud-agents-tensorlake
cd cursor-cloud-agents-tensorlake
uv sync --all-extras
uv run cursor-tl-up
```

`cursor-tl-up` asks for the Cursor and Tensorlake keys once and saves them to
`.env`. Then it checks the Cursor key, registers the pool, builds or reuses
both images, launches the orchestrator sandbox, and waits until the controller
reports `watching`. No other flag is needed.

The pool is a Cursor Team Pool: the queue that self-hosted workers serve. Its
name comes from `CURSOR_POOL`, default `tensorlake`, and is what you pick at
cursor.com/agents under **Remote Machines**. It is not a Tensorlake sandbox name.
Sandboxes are named after it: the orchestrator sandbox carries a hash of the
pool name, and each worker sandbox is `cursor-<worker-id>`.

The command is idempotent. Run it again after you edit `.env`, or to repair a
suspended orchestrator sandbox or a dead process. It skips steps that are
already done, and it restarts the orchestrator when the pool settings in
`.env` changed.

Options:

```bash
uv run cursor-tl-up --repo-url URL       # serve one repository only; see Repositories
uv run cursor-tl-up --any-repo           # let Cursor clone after the claim; see Repositories
uv run cursor-tl-up --computer-use --rebuild  # desktop worker image; workers start with --computer-use
uv run cursor-tl-up --restart            # restart the orchestrator process even if .env did not change
uv run cursor-tl-up --rebuild            # after a code change: rebuild the orchestrator image and recreate its sandbox
uv run cursor-tl-up --non-interactive    # fail instead of asking for missing keys, for CI and cron
uv run cursor-tl-up --ready-timeout 600  # seconds Cursor waits for a hibernated worker to reconnect, default 900
uv run cursor-tl-up --no-wait            # return without waiting for the orchestrator heartbeat
```

When the command reports the pool as registered and the controller as
`watching`, go to [Submit a request](#submit-a-request).

## Repositories

By default the pool serves every repository the Cursor GitHub App can reach.
Each worker starts with the request's repository as its git `origin`, and a
checkout hook fetches the requested ref with a short-lived token that Cursor
mints for the run. No long-lived git credential is stored.

Two optional flags change how the pool maps to repositories. Both are saved in
`.env`.

| | Default | `--repo-url URL` | `--any-repo` |
|---|---|---|---|
| Use when | You want the simplest setup | The service-account key is scoped to one repository, or you want one pool per repository | You want Cursor to clone, and the GitHub App is connected |
| Repositories served | All | One | All |
| Worker | `origin` set, checkout hook fetches | Same | No `origin`; Cursor clones after the claim with `--clone-git-repos` |
| Web picker | Pick the repository, then the pool | Same | Pick **Any repo**, then the pool |
| Saved as | `CURSOR_POOL_MODE=repo` | `CURSOR_POOL_REPO_URL` | `CURSOR_POOL_MODE=any-repo` |

Background: Cursor keys a pool row by name plus repository. A worker with
`origin` set joins the row for that repository. A worker with no `origin` joins
the **Any repo** row. The default registers the **Any repo** row too, but no
default worker joins it. A request sent to that row fails with "No self-hosted
worker matches the requested labels", so in default mode always pick the
repository in the web picker, not **Any repo**. `--repo-url` removes that row.

## Step by step

The same setup, one command at a time.

1. Install and configure:

   ```bash
   git clone https://github.com/tensorlakeai/cursor-cloud-agents-tensorlake
   cd cursor-cloud-agents-tensorlake
   uv sync --all-extras
   cp .env.example .env
   ```

   In `.env`, set `CURSOR_API_KEY` and `TENSORLAKE_API_KEY`. Every other
   variable is documented in `.env.example` and has a working default. To
   change the repository mode, see [Repositories](#repositories).

2. Register the pool:

   ```bash
   uv run cursor-tl-pool register
   uv run cursor-tl-pool list
   ```

   With `CURSOR_POOL_REPO_URL` the list must show one row for the pool, with
   `repoUrl` set. Remove a leftover any-repo row with
   `uv run cursor-tl-pool deregister`.

3. Build the images:

   ```bash
   uv run cursor-tl-build-image              # prints cursor-tl-worker-<sha8>
   uv run cursor-tl-build-orchestrator-image
   ```

   Copy the printed worker image name into `IMAGE_NAME` in `.env`.

4. Launch the orchestrator:

   ```bash
   uv run cursor-tl-orchestrator-sandbox
   uv run cursor-tl-orchestrator-sandbox --status
   ```

   The command is idempotent. Schedule it so a suspended orchestrator sandbox
   is resumed and re-checked:

   ```cron
   */15 * * * * cd /path/to/cursor-cloud-agents-tensorlake && uv run cursor-tl-orchestrator-sandbox >/dev/null 2>&1
   ```

## Submit a request

1. Open <https://cursor.com/agents>.
2. Pick the repository. In `any-repo` mode pick **Any repo** instead.
3. Open the machine picker, choose **Remote Machines**, and pick **tensorlake**.
4. Send the task. In `any-repo` mode name the repository in the prompt.

Other triggers: `@Cursor pool=tensorlake ...` in Slack, `@cursoragent
pool=tensorlake ...` on GitHub, `pool=tensorlake` in a Linear issue, or the API:

```bash
uv run cursor-tl-pool agent "Add a unit test for the parser" --repo https://github.com/acme/widgets
uv run cursor-tl-pool agent "Clone https://github.com/acme/widgets and add a unit test for the parser"   # any-repo mode: no --repo
```

Watch it land:

```bash
tl sbx ls                                         # a cursor-<worker-id> sandbox appears
uv run cursor-tl-orchestrator-sandbox --logs      # claim, spawn, worker start
tl sbx exec cursor-<worker-id> tail -n 50 /var/log/cursor-tl/worker.log
```

After the session ends, the worker waits `WORKER_IDLE_RELEASE_SECS` (default
300) and exits. The janitor then suspends the sandbox. A follow-up message
resumes it. A sandbox suspended for longer than `SESSION_RETENTION_SECS`
(default one day) is terminated.

## Computer use

Cursor agents can drive a desktop and a browser on Linux workers. Run
`cursor-tl-up --computer-use --rebuild`, or set `WORKER_COMPUTER_USE=true` in
`.env`. The worker image is then built from `tensorlake/ubuntu-vnc`, it clears
Chrome's first-run dialog, and each worker starts with `--computer-use`. No
inbound port opens.

`--rebuild` matters the first time: the orchestrator image carries this
package, and the orchestrator is what passes `--computer-use` to a worker.
Confirm it took with `cursor-tl-orchestrator-sandbox --status`, which reports
`computer_use` and `display`.

The desktop image boots a TigerVNC and Xfce session on display `:1` before any
worker starts, so `WORKER_DISPLAY` defaults to `:1` and the worker attaches to
that one. One desktop per sandbox, at a number you can predict and record.
`WORKER_DISPLAY=managed` lets the worker start its own desktop instead.

Set `WORKER_SHARE_DESKTOP=view` or `view_and_control` to let authorized
viewers watch the agent desktop from Cursor. That shares a desktop the worker
creates itself, so it turns the `WORKER_DISPLAY` default off.

Test it with a task such as "open a browser, visit example.com, and take a
screenshot". Desktop workers need at least 4096 MB of memory. For proof that
the desktop is usable before you send a task, run Cursor's own preflight in a
worker sandbox:

```bash
tl sbx exec cursor-<worker-id> agent worker --pool tensorlake --computer-use --display :1 debug
```

### Visual QA demo

[demo/README.md](demo/README.md) is a full walkthrough: an agent opens a web
app in Chrome inside a sandbox, finds a bug that only a click reveals, fixes
it, and checks the fix by looking again. `cursor-tl-demo` records the desktop
and copies the video and screenshots out.

```bash
uv run cursor-tl-demo watch      # wait for a worker sandbox, start recording
uv run cursor-tl-demo collect    # video and stills into ./demo-artifacts
```

## My Machines

My Machines works on any Cursor plan and needs no orchestrator. One long-lived
worker runs in one Tensorlake sandbox, and you pick it from the environment
dropdown at cursor.com/agents. Hibernation is by hand.

1. Install and configure:

   ```bash
   git clone https://github.com/tensorlakeai/cursor-cloud-agents-tensorlake
   cd cursor-cloud-agents-tensorlake
   uv sync --all-extras
   cp .env.example .env
   ```

   In `.env`, set `TENSORLAKE_API_KEY`, `CURSOR_USER_API_KEY` (a personal key
   from Dashboard → API Keys), and `REPOS` (one or more HTTPS repository URLs,
   comma separated). Leave `CURSOR_API_KEY` empty. Private repositories also
   need `GIT_USERNAME` and `GIT_TOKEN`.

2. Build the worker image and copy the printed name into `IMAGE_NAME`:

   ```bash
   uv run cursor-tl-build-image
   ```

3. Start the machine:

   ```bash
   uv run cursor-tl-my-machine --name tl-demo
   ```

   The machine `tl-demo` appears in the environment dropdown at
   cursor.com/agents within a few seconds.

4. Send a task with `tl-demo` selected. Watch it:

   ```bash
   uv run cursor-tl-my-machine --name tl-demo --status
   uv run cursor-tl-my-machine --name tl-demo --logs
   ```

5. Hibernate, resume, or remove:

   ```bash
   uv run cursor-tl-my-machine --name tl-demo --suspend   # memory, filesystem, checkout kept
   uv run cursor-tl-my-machine --name tl-demo             # resume and restart the worker
   uv run cursor-tl-my-machine --name tl-demo --terminate # remove the sandbox
   ```

## Operations

| Command | Effect |
|---|---|
| `cursor-tl-orchestrator-sandbox` | Create or resume the orchestrator sandbox and ensure the process runs |
| `cursor-tl-orchestrator-sandbox --status` | State, pool, session counts, controller restarts |
| `cursor-tl-orchestrator-sandbox --logs [--lines N]` | Tail the orchestrator log |
| `cursor-tl-orchestrator-sandbox --restart` | Restart the orchestrator process so it reads the current `.env` |
| `cursor-tl-orchestrator-sandbox --terminate` | Terminate the orchestrator sandbox. Worker sandboxes stay |
| `cursor-tl-demo watch` / `collect` / `stop` / `status` | Record a worker's desktop and copy the video out. See [demo/README.md](demo/README.md) |
| `cursor-tl-pool pending` | Pending and claimed-offline requests for the pool |
| `cursor-tl-pool workers` / `summary` | Connected and idle workers |
| `cursor-tl-pool release <request-id>` | Release a stuck claim so Cursor re-queues it |
| `cursor-tl-pool deregister [--repo-url URL]` | Remove the any-repo pool row, or the repo-bound row |
| `tl sbx ls` | All sandboxes, including `cursor-*` workers and their state |
| `tl sbx terminate cursor-<worker-id>` | Remove one worker sandbox by hand |

## Configuration

`.env.example` documents every variable. Values in `.env` win over variables
exported in your shell. To change a value, edit `.env` and run `cursor-tl-up`
again. The orchestrator reads its settings at process start, and `cursor-tl-up`
restarts it when the pool settings changed.

| Variable | Required | Notes |
|---|---|---|
| `TENSORLAKE_API_KEY` | Yes | Never enters a worker sandbox. |
| `IMAGE_NAME` | Yes | Worker image. `cursor-tl-up` writes it. |
| `CURSOR_API_KEY` | Pools | Enterprise service-account key. |
| `CURSOR_USER_API_KEY` | My Machines | Personal key. Works on any plan. |
| `REPOS` | My Machines | HTTPS repositories to clone, comma separated. |
| `CURSOR_POOL_MODE` | No | `repo` (default) or `any-repo`. See [Repositories](#repositories). |
| `CURSOR_POOL_REPO_URL` | No | Serve one repository only. `repo` mode only. |
| `WORKER_COMPUTER_USE` | No | Desktop image, and workers start with `--computer-use`. |
| `WORKER_DISPLAY` | No | X display for computer use. Blank reuses `:1`; `managed` lets the worker start its own. |

Common changes: `CURSOR_POOL` (pool name, default `tensorlake`), `MAX_WORKERS`
(cap on live worker sandboxes), `SANDBOX_CPUS` / `SANDBOX_MEMORY_MB` /
`SANDBOX_DISK_MB` (worker sizing), and `SANDBOX_ALLOW_OUT` (egress allowlist,
which must include `api2.cursor.sh`, `api2direct.cursor.sh`, and your git host).

Do not set `CURSOR_AGENT_WORKER_ID` or `CURSOR_REQUEST_ID` in `.env`. The
controller supplies them to the spawn hook.

The worker process runs with the Cursor service-account key in its
environment, and repository code runs as the same OS user. Use one
least-privilege service account per customer and set `SANDBOX_ALLOW_OUT`.

## Troubleshooting

- **"No self-hosted worker matches the requested labels".** The request was sent
  to the **Any repo** row, and the pool runs in default mode. Pick the
  repository in the web picker instead, or see [Repositories](#repositories)
  for `--repo-url` and `--any-repo`.
- **Any-repo worker has an empty workspace.** Cursor clones only when the
  request names a repository the GitHub App can reach. Name the repository in
  the prompt and check `/var/log/cursor-tl/worker.log` in the worker sandbox.
- **`POST /v1/agents` returns 403 `integration_not_connected`.** The request
  named a repository, and the Cursor GitHub integration is not connected to the
  team. A team admin connects it at <https://cursor.com/dashboard/integrations>.
- **Request stays queued.** Check that the pool name matches `CURSOR_POOL`, that
  **Self-hosted Machines** is enabled, and that `--logs` shows the controller
  connected. `cursor-tl-pool pending` lists what Cursor is waiting on. A log
  line such as `pending-request watch failed` with HTTP 503 means Cursor's API
  was unavailable. The controller reconnects on its own.
- **Changed `.env`, but the orchestrator still uses the old settings.** Run
  `cursor-tl-up` again. It compares the running settings with `.env` and
  restarts the process when they differ. `--restart` forces it.
- **The agent says it cannot open a browser, or `--computer-use` seems ignored.**
  Check `cursor-tl-orchestrator-sandbox --status`. If `computer_use` is absent
  or `false` while `.env` says `true`, the orchestrator is running an older
  build of this package: `cursor-tl-up --computer-use --rebuild`.
- **`tl sbx ls` shows no `cursor-*` sandboxes.** The `tl` CLI uses the key in
  your shell, and `.env` may hold a key for another Tensorlake project. Export
  the `TENSORLAKE_API_KEY` from `.env` in that shell, or use
  `cursor-tl-orchestrator-sandbox --status` and `--logs`, which read `.env`.
- **Controller rejects the key.** Only an Enterprise service-account key starts
  pool workers. A personal key works only with `cursor-tl-my-machine`.
- **Workspace is empty after a run.** Read `/var/log/cursor-tl/checkout.log` in
  the worker sandbox. Confirm GitHub token minting is enabled and the GitHub App
  has access to the repository. HTTPS remotes only.
- **A new chat started a sandbox in another Tensorlake project.** Two
  orchestrators serve the same pool, each launched with a different
  `TENSORLAKE_API_KEY`. Cursor splits new requests between them. Run
  `cursor-tl-orchestrator-sandbox --status` with each key, and `--terminate`
  the one you do not want. A re-launch always uses the key in `.env`.
- **A follow-up landed on a new sandbox.** The reconnect window lapsed. Raise
  it with `cursor-tl-pool register --ready-timeout 1800`, which updates the
  existing pool. `cursor-tl-up --ready-timeout` applies only when it creates
  the pool. Or check `--logs` for a failed resume.
- **Worker sandbox stays running after the agent finished.** The worker waits
  `WORKER_IDLE_RELEASE_SECS` before it exits. Then the next janitor pass suspends it.
- **A failed spawn left a claimed request.** Run `cursor-tl-pool release <request-id>`.

## Related

- [Cursor: Self-Hosted Machines](https://cursor.com/docs/cloud-agent/self-hosted)
- [Cursor: Team Pools](https://cursor.com/docs/cloud-agent/self-hosted/pool)
- [Cursor: Partner integrations](https://cursor.com/docs/cloud-agent/self-hosted/integrations)
- [Cursor: Cloud Agents API, workers and pools](https://cursor.com/docs/cloud-agent/api/endpoints)
- [Tensorlake Sandboxes: lifecycle](https://docs.tensorlake.ai/sandboxes/lifecycle)
- [Tensorlake Sandboxes: commands and processes](https://docs.tensorlake.ai/sandboxes/commands)
