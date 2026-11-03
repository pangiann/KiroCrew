"use strict";

// Wedge recovery on Windows when netstat names no owner for a port that is still
// held. The force-stop reports `ownerUnknown`; the supervisor must show the
// restart-required dialog and must not respawn into the held port. The bind
// check runs for real against a real socket; only netstat is scripted.

const net = require("node:net");
const { test } = require("node:test");
const assert = require("node:assert");

const { flush, harness, fakeDialogWindows, fakeTimers } = require("./gateway-supervisor-test-helpers");

function listenOn(host) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen({ port: 0, host }, () => resolve(server));
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

// netstat answers with no row for any port; any other tool is unexpected.
function emptyNetstat(file, _args, _options, callback) {
  if (String(file).toLowerCase().includes("netstat")) {
    callback(null, "", "");
    return;
  }
  callback(new Error(`unexpected command: ${file}`));
}

function windowsHarness(port) {
  const dialogs = fakeDialogWindows();
  const mainWindow = {
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    isMinimized: () => false,
    restore() {},
    show() {},
    focus() {},
    webContents: { isDestroyed: () => false, loadFile() {}, send() {} },
  };
  const timers = fakeTimers();
  const built = harness({
    port,
    mainWindow,
    timers,
    BrowserWindow: dialogs.FakeBrowserWindow,
    execFileFn: emptyNetstat,
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
  });
  return { ...built, dialogs, mainWindow, timers };
}

// Settle recovery: let promises run, and fire each one-shot timer it arms (the
// kill's grace waits) until nothing new is pending.
async function settle(timers) {
  for (let round = 0; round < 50; round += 1) {
    for (let i = 0; i < 5; i += 1) await flush();
    if (!timers.pending.length) return;
    const due = timers.pending.splice(0);
    for (const timer of due) timer.fn();
  }
}

// Recovery kills and respawns only a gateway this app spawned, so start one
// first, then fail an update install, which hands the window to recovery.
// Returns how many spawns recovery itself made.
async function spawnsDuringRecovery({ supervisor, mainWindow, spawnCalls, timers }) {
  await supervisor.start();
  timers.pending.splice(0);
  // The tree kill would ask the real OS about the fake child's pid; a child
  // that already exited skips it, and the port is still what recovery checks.
  for (const call of spawnCalls) call.child.exitCode = 1;
  const before = spawnCalls.length;
  supervisor.onInstallDispatched();
  supervisor.onInstallFailed(mainWindow);
  await settle(timers);
  return spawnCalls.length - before;
}

test("wedge recovery on a port netstat does not list shows restart-required and does not respawn", async () => {
  const held = await listenOn("127.0.0.1");
  const port = held.address().port;
  try {
    const built = windowsHarness(port);
    const spawned = await spawnsDuringRecovery(built);

    assert.strictEqual(spawned, 0, "no gateway is started into the held port");
    assert.ok(
      built.logs.some((l) => l.includes("port not confirmed free after force-stop (held by an unlisted owner)")),
      built.logs.join("\n"),
    );
    assert.strictEqual(built.dialogs.windows.length, 1, "the restart-required dialog is shown");
    const shown = decodeURIComponent(built.dialogs.windows[0].loaded.join("\n"));
    assert.match(shown, /verify what(?:'|&#39;|&#x27;)s using port/);
  } finally {
    await closeServer(held);
  }
});

test("wedge recovery on a free port with no netstat owner still respawns", async () => {
  const probe = await listenOn("127.0.0.1");
  const port = probe.address().port;
  await closeServer(probe);

  const built = windowsHarness(port);
  const spawned = await spawnsDuringRecovery(built);

  // The respawn then polls the backend on the real clock until the window goes.
  built.mainWindow.destroyed = true;

  assert.strictEqual(spawned, 1, `the gateway is respawned once:\n${built.logs.join("\n")}`);
  assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
  assert.ok(!built.logs.some((l) => l.includes("port not confirmed free after force-stop")));
});
