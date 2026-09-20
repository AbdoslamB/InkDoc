// Viewport and scroll verification script using Microsoft Edge CDP
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const EDGE_PATH = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 13199;
const CDP_PORT = 9222;
const ARTIFACT_DIR = 'C:\\Users\\baaba\\.gemini\\antigravity-ide\\brain\\2da14e95-6f0b-401a-a370-83b4753cc4ac';

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
  console.log('--- Starting verification script ---');

  // 1. Start headless FastAPI server
  const server = spawn('python', ['main.py', '--headless', '--port', String(PORT)], {
    cwd: process.cwd(),
    stdio: 'ignore',
  });

  // Wait for server health
  let serverOk = false;
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/health`);
      if (res.ok) {
        serverOk = true;
        break;
      }
    } catch (_) {}
    await sleep(300);
  }

  if (!serverOk) {
    server.kill();
    throw new Error('Server failed to start on port ' + PORT);
  }
  console.log('FastAPI server running on port ' + PORT);

  // 2. Start Microsoft Edge in headless mode
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'edge-test-'));
  const edge = spawn(EDGE_PATH, [
    '--headless=new',
    `--remote-debugging-port=${CDP_PORT}`,
    '--disable-gpu',
    '--disable-extensions',
    `--user-data-dir=${tmpDir}`,
    'about:blank',
  ], { stdio: 'ignore' });

  let cdpOk = false;
  let targetWsUrl = null;
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json`);
      if (res.ok) {
        const targets = await res.json();
        const pageTarget = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
        if (pageTarget) {
          targetWsUrl = pageTarget.webSocketDebuggerUrl;
          cdpOk = true;
          break;
        }
      }
    } catch (_) {}
    await sleep(300);
  }

  if (!cdpOk) {
    edge.kill();
    server.kill();
    throw new Error('Edge CDP failed to start');
  }
  console.log('Connected to Edge CDP target:', targetWsUrl);

  const ws = new WebSocket(targetWsUrl);
  await new Promise((resolve) => ws.addEventListener('open', resolve));

  let msgId = 1;
  async function cdp(method, params) {
    return sendCDP(ws, msgId++, method, params);
  }

  await cdp('Page.enable');
  await cdp('Runtime.enable');

  // Navigate to InkDoc
  await cdp('Page.navigate', { url: `http://127.0.0.1:${PORT}/InkDoc` });

  // Evaluation helper
  async function evalCode(expr) {
    const res = await cdp('Runtime.evaluate', { expression: expr, returnByValue: true });
    if (res && res.exceptionDetails) {
      throw new Error('Eval error: ' + JSON.stringify(res.exceptionDetails));
    }
    return res && res.result ? res.result.value : null;
  }

  // Poll until DOM is ready
  for (let i = 0; i < 50; i++) {
    try {
      const ready = await evalCode(`Boolean(document.readyState === 'complete' && document.getElementById('dropZoneSection'))`);
      if (ready) break;
    } catch (_) {}
    await sleep(200);
  }
  await sleep(600);

  console.log('Current URL in Edge:', await evalCode('window.location.href'));
  console.log('Document title in Edge:', await evalCode('document.title'));
  console.log('Element dropZoneSection exists:', await evalCode('Boolean(document.getElementById("dropZoneSection"))'));

  const viewports = [
    { width: 1280, height: 800 },
    { width: 900, height: 600 },
    { width: 720, height: 480 },
  ];

  const resultsTable = [];

  for (const vp of viewports) {
    console.log(`\nTesting viewport ${vp.width}x${vp.height}...`);
    await cdp('Emulation.setDeviceMetricsOverride', {
      width: vp.width,
      height: vp.height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await sleep(300);


    // State 1: 0 items, Expanded
    await evalCode(`(function() {
      const btn = document.getElementById("btnClearQueue");
      if (btn) btn.click();
      const dropSec = document.getElementById("dropZoneSection");
      dropSec.classList.remove("is-collapsed");
    })()`);
    await sleep(350);

    let m1 = await evalCode(`({
      docScrollHeight: document.documentElement.scrollHeight,
      winHeight: window.innerHeight,
      bodyOverflow: window.getComputedStyle(document.body).overflow,
      htmlOverflow: window.getComputedStyle(document.documentElement).overflow,
    })`);

    async function saveScreenshot(filename) {
      const res = await cdp('Page.captureScreenshot', { format: 'png' });
      if (res && res.data) {
        fs.writeFileSync(path.join(ARTIFACT_DIR, filename), Buffer.from(res.data, 'base64'));
        console.log('Saved screenshot:', filename);
      }
    }

    if (vp.width === 1280 && vp.height === 800) {
      await saveScreenshot('screenshot_1280x800_expanded.png');
    }
    if (vp.width === 720 && vp.height === 480) {
      await saveScreenshot('screenshot_720x480_expanded.png');
    }

    resultsTable.push({
      viewport: `${vp.width}x${vp.height}`,
      state: '0 items, expanded',
      docScrollHeight: m1.docScrollHeight,
      winHeight: m1.winHeight,
      noScroll: m1.docScrollHeight <= m1.winHeight,
    });

    // State 2: 0 items, Collapsed
    await evalCode(`(function() {
      const dropSec = document.getElementById("dropZoneSection");
      dropSec.classList.add("is-collapsed");
    })()`);
    await sleep(350);

    let m2 = await evalCode(`({
      docScrollHeight: document.documentElement.scrollHeight,
      winHeight: window.innerHeight,
    })`);

    resultsTable.push({
      viewport: `${vp.width}x${vp.height}`,
      state: '0 items, collapsed',
      docScrollHeight: m2.docScrollHeight,
      winHeight: m2.winHeight,
      noScroll: m2.docScrollHeight <= m2.winHeight,
    });

    // State 3: 1 item (collapsed & expanded)
    await evalCode(`(function() {
      // Inject 1 queue item and show workbench
      const workbench = document.getElementById("workbenchSection");
      workbench.style.display = "grid";
      const qList = document.getElementById("queueList");
      qList.innerHTML = '<div class="queue-item selected"><div class="item-name">sample.docx</div></div>';
      const rendered = document.getElementById("renderedOutput");
      rendered.innerHTML = '<h1>Sample Document</h1><p>This is converted content.</p>';
      const dropSec = document.getElementById("dropZoneSection");
      dropSec.classList.add("is-collapsed");
    })()`);
    await sleep(350);

    let m3_col = await evalCode(`({
      docScrollHeight: document.documentElement.scrollHeight,
      winHeight: window.innerHeight,
    })`);

    resultsTable.push({
      viewport: `${vp.width}x${vp.height}`,
      state: '1 item, collapsed',
      docScrollHeight: m3_col.docScrollHeight,
      winHeight: m3_col.winHeight,
      noScroll: m3_col.docScrollHeight <= m3_col.winHeight,
    });

    // 1 item expanded
    await evalCode(`(function() {
      const dropSec = document.getElementById("dropZoneSection");
      dropSec.classList.remove("is-collapsed");
    })()`);
    await sleep(350);

    let m3_exp = await evalCode(`({
      docScrollHeight: document.documentElement.scrollHeight,
      winHeight: window.innerHeight,
    })`);

    resultsTable.push({
      viewport: `${vp.width}x${vp.height}`,
      state: '1 item, expanded',
      docScrollHeight: m3_exp.docScrollHeight,
      winHeight: m3_exp.winHeight,
      noScroll: m3_exp.docScrollHeight <= m3_exp.winHeight,
    });

    // State 4: 50 items (collapsed & expanded)
    await evalCode(`(function() {
      const qList = document.getElementById("queueList");
      let items = '';
      for (let i = 1; i <= 50; i++) {
        items += '<div class="queue-item"><div class="item-name">batch_document_' + i + '.pdf</div></div>';
      }
      qList.innerHTML = items;

      const rendered = document.getElementById("renderedOutput");
      let longText = '';
      for (let i = 1; i <= 80; i++) {
        longText += '<p>Paragraph ' + i + ': Testing internal pane scroll containment without page scroll.</p>';
      }
      rendered.innerHTML = longText;

      const dropSec = document.getElementById("dropZoneSection");
      dropSec.classList.add("is-collapsed");
    })()`);
    await sleep(350);

    let m50_col = await evalCode(`({
      docScrollHeight: document.documentElement.scrollHeight,
      winHeight: window.innerHeight,
      queueScrollHeight: document.getElementById("queueList").scrollHeight,
      queueClientHeight: document.getElementById("queueList").clientHeight,
      renderedScrollHeight: document.getElementById("renderedContainer").scrollHeight,
      renderedClientHeight: document.getElementById("renderedContainer").clientHeight,
    })`);

    resultsTable.push({
      viewport: `${vp.width}x${vp.height}`,
      state: '50 items, collapsed',
      docScrollHeight: m50_col.docScrollHeight,
      winHeight: m50_col.winHeight,
      noScroll: m50_col.docScrollHeight <= m50_col.winHeight,
      queueScrolls: m50_col.queueScrollHeight > m50_col.queueClientHeight,
      renderedScrolls: m50_col.renderedScrollHeight > m50_col.renderedClientHeight,
    });

    await saveScreenshot(`screenshot_${vp.width}x${vp.height}_collapsed.png`);

    // 50 items expanded
    await evalCode(`(function() {
      const dropSec = document.getElementById("dropZoneSection");
      dropSec.classList.remove("is-collapsed");
    })()`);
    await sleep(350);

    let m50_exp = await evalCode(`({
      docScrollHeight: document.documentElement.scrollHeight,
      winHeight: window.innerHeight,
      queueScrollHeight: document.getElementById("queueList").scrollHeight,
      queueClientHeight: document.getElementById("queueList").clientHeight,
      renderedScrollHeight: document.getElementById("renderedContainer").scrollHeight,
      renderedClientHeight: document.getElementById("renderedContainer").clientHeight,
    })`);

    resultsTable.push({
      viewport: `${vp.width}x${vp.height}`,
      state: '50 items, expanded',
      docScrollHeight: m50_exp.docScrollHeight,
      winHeight: m50_exp.winHeight,
      noScroll: m50_exp.docScrollHeight <= m50_exp.winHeight,
      queueScrolls: m50_exp.queueScrollHeight > m50_exp.queueClientHeight,
      renderedScrolls: m50_exp.renderedScrollHeight > m50_exp.renderedClientHeight,
    });
  }

  // 5. Test Settings Popover doesn't cause page scroll
  console.log('\nTesting Settings Popover...');
  await evalCode(`(function() {
    const pop = document.getElementById("settingsPopover");
    pop.classList.add("open");
  })()`);
  await sleep(200);

  const popCheck = await evalCode(`({
    docScrollHeight: document.documentElement.scrollHeight,
    winHeight: window.innerHeight,
    popHeight: document.getElementById("settingsPopover").offsetHeight,
  })`);
  resultsTable.push({
    viewport: 'Current',
    state: 'Settings Popover Open',
    docScrollHeight: popCheck.docScrollHeight,
    winHeight: popCheck.winHeight,
    noScroll: popCheck.docScrollHeight <= popCheck.winHeight,
  });

  // Cleanup
  ws.close();
  edge.kill();
  server.kill();
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch (_) {}

  console.log('\n=== VERIFICATION RESULTS TABLE ===');
  console.table(resultsTable);

  const allPassed = resultsTable.every((r) => r.noScroll === true);
  console.log('\nAll conditions satisfied (noScroll: true)?', allPassed);

  if (!allPassed) {
    process.exit(1);
  }
}

run().catch((err) => {
  console.error('Fatal error during test:', err);
  process.exit(1);
});
