# Demo: a Cursor agent finds a bug by looking at the page

A Cursor cloud agent opens a web app in Chrome inside a Tensorlake sandbox,
clicks the broken button, works out why, fixes it, and clicks again to check.
You get a video and screenshots of the whole session.

The agent drives the desktop with Cursor's own `--computer-use`. Nothing in
this repository moves the pointer or presses a key. That is the point: the
whole Cursor agent runs inside a Tensorlake sandbox, not just a shell.

## What makes the bug worth a demo

The demo app is a one-product storefront in
[tensorlakeai/test_sandbox](https://github.com/tensorlakeai/test_sandbox)
under `visual-qa-demo/`. A decorative overlay covers the "Add to cart" button,
so clicking it does nothing and the cart badge stays at `0`.

Nothing else reports the problem:

- the page renders correctly, and the overlay is invisible;
- the server returns 200 and the markup is right;
- `app.js` defines the handler and binds it to the button;
- no JavaScript error is thrown, because the handler is never reached;
- the test suite is green.

An earlier developer met the same overlay and raised the `z-index` of the
"Size & details" link to get it working again. So one control on the card
works and the one next to it does not. Finding that needs a browser, a
pointer, and something that looks at the result.

## Prepare

```bash
uv run cursor-tl-up --computer-use --rebuild
```

This builds the desktop worker image, forwards the computer-use settings to
the orchestrator, and waits until the controller is watching. `--rebuild` is
needed the first time, because the orchestrator image carries this package.

Check that the orchestrator agrees:

```bash
uv run cursor-tl-orchestrator-sandbox --status
```

`computer_use` must be `true` and `display` must be `:1`.

## Run it

Start the recorder in one terminal. It waits for the worker sandbox and films
the desktop as soon as one appears.

```bash
uv run cursor-tl-demo watch
```

Send the task from the other terminal, or from cursor.com/agents:

```bash
uv run cursor-tl-pool agent "$(cat demo/prompt.txt)" \
  --repo https://github.com/tensorlakeai/test_sandbox
```

Watch the log while it works:

```bash
tl sbx exec cursor-<worker-id> tail -f /var/log/cursor-tl/worker.log
```

When the agent is done, stop the recorder and copy the evidence out:

```bash
uv run cursor-tl-demo collect --stop
```

You get `demo-artifacts/`:

| File | What it is |
|---|---|
| `session.mp4` | The desktop for the whole session, 1280x800 |
| `session.gif` | The same, for a slide or a message |
| `still-NNN.png` | A frame every 10 seconds |
| `recording.txt` | Display, geometry, frame rate, start and stop times |

`cursor-tl-demo status` says what is recording and what has been captured.

## No Cursor GitHub integration? Use My Machines

The pool path asks Cursor to check out the repository, so it needs a team
admin to connect the Cursor GitHub App. Without it, `POST /v1/agents` answers
`403 integration_not_connected`.

My Machines needs none of that. It runs on any plan, and this repository
clones the code itself with `GIT_TOKEN`, so Cursor's GitHub integration stays
out of the way.

```bash
# .env: CURSOR_USER_API_KEY, GIT_TOKEN, and
#       REPOS=https://github.com/tensorlakeai/test_sandbox
uv run cursor-tl-my-machine --name tl-visual-qa
uv run cursor-tl-demo watch --sandbox cursor-tl-mm-tl-visual-qa
```

The machine log prints a link that opens a chat already pointed at it:

```bash
uv run cursor-tl-my-machine --name tl-visual-qa --logs | grep 'cursor.com/agents#'
```

Open it, paste `demo/prompt.txt`, and send. Then collect as above:

```bash
uv run cursor-tl-demo collect --sandbox cursor-tl-mm-tl-visual-qa --stop
```

Suspend the machine when you finish, so it stops costing compute:

```bash
uv run cursor-tl-my-machine --name tl-visual-qa --suspend
```

## What to show

1. **The tests are green.** Run them in the sandbox. Nothing is wrong.
2. **Cursor's own preflight.** `agent worker debug` in the worker sandbox
   prints `Computer use  Ready yes` and `Display :1 reachable explicit`.
   Cursor, not this repository, decides that the desktop is usable.
3. **The video.** The agent opens Chrome, clicks "Add to cart", and the badge
   stays at `0`. It opens "Size & details" and that works. It reads the CSS,
   finds the overlay, and adds one line.
4. **The video again.** It clicks "Add to cart" and the badge shows `1`.
5. **The pull request.** One CSS property, and the `z-index` workaround gone.

## How the recording works

Cursor sends its screenshots to the model, not to disk, so a finished session
leaves nothing to show. `cursor-tl-demo` fills that gap from outside the
agent: it copies `demo_recorder.sh` into the worker sandbox and runs it as a
named process. The script reads the screen with `ffmpeg -f x11grab` and writes
a video and periodic stills.

The worker and the recorder look at the same desktop because
`WORKER_DISPLAY` pins the worker to `:1`, the display the image already boots.
With `WORKER_DISPLAY=managed` the worker starts its own desktop instead, and
the recorder then finds the display that has a browser window on it.

The video is fragmented MP4, so it stays playable if the sandbox is suspended
or the recorder is killed. `collect --stop` signals the recorder first, which
lets ffmpeg close the file on a whole frame.

## When it does not work

- **`403 integration_not_connected`.** The Cursor GitHub App is not connected
  to the team. Use My Machines above, or ask an admin to connect it at
  cursor.com/dashboard/integrations.
- **No worker sandbox appears.** `cursor-tl-pool pending` shows what Cursor is
  waiting for, and `cursor-tl-orchestrator-sandbox --logs` shows the claim.
- **`session.mp4` is a few dozen bytes.** The recorder started but no fragment
  is written yet. Wait a few seconds, or stop it with `collect --stop`.
- **The agent reports it cannot open a browser.** Check that
  `cursor-tl-orchestrator-sandbox --status` says `computer_use: true`. Before
  the settings reached the orchestrator, workers started with no desktop at
  all.
- **Chrome opens on a terms-of-service dialog.** The worker image clears
  Chrome's first-run state. An older image does not, so rebuild it with
  `cursor-tl-up --computer-use --rebuild`.

## Reset the bug

The agent's fix is a pull request, so `main` keeps the bug and the demo can
run again. If a fix was merged, revert the change to
`visual-qa-demo/styles.css`.
