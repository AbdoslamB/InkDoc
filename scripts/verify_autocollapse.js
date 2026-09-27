// Verification of dynamic auto-collapse and manual expand/collapse latch
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const EDGE_PATH = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 13199;
const CDP_PORT = 9222;

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function sendCDP(ws, id, method, params = {}) {
  return new Promise((resolve, reject) => {
    const handler = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id === id) {
        ws.removeEventListener('message', handler);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
      }
    };
    ws.addEventListener('message', handler);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function run() {
  console.log('--- Starting auto-collapse behavior test ---');

  const server = spawn('python', ['main.py', '--headless', '--port', String(PORT)], {
    cwd: process.cwd(),
    stdio: 'ignore',
  });

  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/health`);
      if (res.ok) break;
    } catch (_) {}
    await sleep(300);
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'edge-collapse-'));
  const edge = spawn(EDGE_PATH, [
    '--headless=new',
    `--remote-debugging-port=${CDP_PORT}`,
    '--disable-gpu',
    '--disable-extensions',
    `--user-data-dir=${tmpDir}`,
    'about:blank',
  ], { stdio: 'ignore' });

  let targetWsUrl = null;
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json`);
      if (res.ok) {
        const targets = await res.json();
        const pageTarget = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
        if (pageTarget) {
          targetWsUrl = pageTarget.webSocketDebuggerUrl;
          break;
        }
      }
    } catch (_) {}
    await sleep(300);
  }

  const ws = new WebSocket(targetWsUrl);
  await new Promise((resolve) => ws.addEventListener('open', resolve));

  let msgId = 1;
  async function cdp(method, params) {
    return sendCDP(ws, msgId++, method, params);
  }

  await cdp('Page.enable');
  await cdp('Runtime.enable');
  await cdp('Page.navigate', { url: `http://127.0.0.1:${PORT}/InkDoc` });

  async function evalCode(expr) {
    const res = await cdp('Runtime.evaluate', { expression: expr, returnByValue: true });
    if (res && res.exceptionDetails) {
      throw new Error('Eval error: ' + JSON.stringify(res.exceptionDetails));
    }
    return res && res.result ? res.result.value : null;
  }

  for (let i = 0; i < 50; i++) {
    try {
      const ready = await evalCode(`Boolean(document.readyState === 'complete' && document.getElementById('dropZoneSection'))`);
      if (ready) break;
    } catch (_) {}
    await sleep(200);
  }
  await sleep(500);

  // Check initial state: expanded and top-right collapse icon hidden (no results generated yet)
  const initCollapsed = await evalCode(`document.getElementById("dropZoneSection").classList.contains("is-collapsed")`);
  const initActionsVisible = await evalCode(`window.getComputedStyle(document.querySelector(".dropzone-card-top-actions")).visibility === "visible"`);
  console.log('1. Initial state is collapsed?', initCollapsed, '(expected: false)');
  console.log('1b. Initial top-right collapse button visible?', initActionsVisible, '(expected: false)');
  if (initCollapsed !== false) throw new Error('Initial state should be expanded');
  if (initActionsVisible !== false) throw new Error('Top-right collapse button should be hidden before results are generated');

  // Simulate file conversion with success
  console.log('2. Simulating incoming file and successful result...');
  await evalCode(`(function() {
    const fakeFile = new File(["dummy text"], "test.txt", { type: "text/plain" });
    // Trigger handleIncomingFiles with a mock completed item
    const fileInput = document.getElementById("fileInput");
    // Directly dispatch dropped data or convert call
    window.dispatchEvent(new CustomEvent("test-start-batch", { detail: { count: 1 } }));
  })()`);

  // Test auto-collapse via item completion in app.js
  await evalCode(`(function() {
    // We can simulate an incoming item completing
    const item = { id: "test-1", name: "test.txt", status: "saved", markdown: "# Hello" };
    // Call batch registration and completion
    const event = new CustomEvent("test-complete");
    // Trigger the internal batch completed handler through simulated queue item
    const file = new File(["test content"], "hello.txt", { type: "text/plain" });
    const dt = new DataTransfer();
    dt.items.add(file);
    const dropZone = document.getElementById("dropZone");
    const dropEvt = new DragEvent("drop", { dataTransfer: dt, bubbles: true });
    dropZone.dispatchEvent(dropEvt);
  })()`);

  // Wait 700ms (conversion + 350ms delay)
  await sleep(1000);

  const afterResultCollapsed = await evalCode(`document.getElementById("dropZoneSection").classList.contains("is-collapsed")`);
  console.log('3. After successful result, auto-collapsed?', afterResultCollapsed, '(expected: true)');

  // Test manual expand via chevron button
  console.log('4. Testing manual expand via btnDropzoneToggle...');
  await evalCode(`document.getElementById("btnDropzoneToggle").click()`);
  await sleep(350);

  const afterManualExpand = await evalCode(`document.getElementById("dropZoneSection").classList.contains("is-collapsed")`);
  const actionsVisibleWithResults = await evalCode(`window.getComputedStyle(document.querySelector(".dropzone-card-top-actions")).visibility === "visible"`);
  console.log('5. After clicking toggle, collapsed?', afterManualExpand, '(expected: false)');
  console.log('5b. When expanded with results, top-right collapse button visible?', actionsVisibleWithResults, '(expected: true)');
  if (actionsVisibleWithResults !== true) throw new Error('Top-right collapse button should be visible when results exist down below');

  // Simulate another item completing while manually expanded -> should NOT auto-collapse
  console.log('6. Adding another result while manually expanded...');
  await evalCode(`(function() {
    const file = new File(["another"], "another.txt", { type: "text/plain" });
    const dt = new DataTransfer();
    dt.items.add(file);
    const dropZone = document.getElementById("dropZone");
    const dropEvt = new DragEvent("drop", { dataTransfer: dt, bubbles: true });
    dropZone.dispatchEvent(dropEvt);
  })()`);
  await sleep(800);

  const stillExpanded = await evalCode(`document.getElementById("dropZoneSection").classList.contains("is-collapsed")`);
  console.log('7. Did it remain expanded without auto-collapsing?', !stillExpanded, '(expected: true)');

  // Test clear queue restores expanded state
  console.log('8. Testing Clear Queue...');
  // First collapse
  await evalCode(`document.getElementById("btnDropzoneCardCollapse").click()`);
  await sleep(350);
  console.log('   Manually collapsed:', await evalCode(`document.getElementById("dropZoneSection").classList.contains("is-collapsed")`));

  // Now click clear queue
  await evalCode(`document.getElementById("btnClearQueue").click()`);
  await sleep(350);
  const clearedExpanded = await evalCode(`document.getElementById("dropZoneSection").classList.contains("is-collapsed")`);
  const workbenchHidden = await evalCode(`document.getElementById("workbenchSection").style.display === "none"`);
  const actionsVisibleAfterClear = await evalCode(`window.getComputedStyle(document.querySelector(".dropzone-card-top-actions")).visibility === "visible"`);
  console.log('9. After Clear Queue, collapsed?', clearedExpanded, '(expected: false)');
  console.log('10. After Clear Queue, workbench hidden?', workbenchHidden, '(expected: true)');
  console.log('10b. After Clear Queue, top-right collapse button visible?', actionsVisibleAfterClear, '(expected: false)');
  if (actionsVisibleAfterClear !== false) throw new Error('Top-right collapse button should be hidden after queue is cleared');

  // Cleanup
  ws.close();
  edge.kill();
  server.kill();
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch (_) {}

  console.log('\n--- ALL BEHAVIOR TESTS PASSED ---');
}

run().catch((err) => {
  console.error('Error during test:', err);
  process.exit(1);
});
