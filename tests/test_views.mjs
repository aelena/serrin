/**
 * Headless checks for the view state machine.
 *
 * This file is the answer to "the UI has odd side effects that are hard to
 * test". The side effects were four independently hidden elements toggled by
 * whoever needed them, and the odd states were all reachable: the studio over a
 * playing stage, the panel over the studio, an error banner behind a view that
 * had already loaded. None of it could be tested without a browser.
 *
 * Visibility is now derived from one piece of state by a pure function, so the
 * illegal combinations are unrepresentable and the whole thing runs in node.
 *
 *   node tests/test_views.mjs
 */

import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const mod = (rel) => pathToFileURL(path.join(root, rel)).href;

const { OVERLAYS, VIEWS, ViewState } = await import(mod('web/js/views.js'));

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}

// ---------------------------------------------------------------------------
console.log('where the app starts');

test('the studio is the default view', () => {
  // The correction this refactor exists for: a piece is a document, and you do
  // not open a document already playing.
  const views = new ViewState();
  assert.equal(views.view, 'studio');
  assert.ok(views.inStudio);
  assert.ok(!views.inStage);
  assert.equal(views.snapshot().studio, true);
  assert.equal(views.snapshot().stage, false);
});

test('nothing else is showing at the start', () => {
  const snapshot = new ViewState().snapshot();
  assert.equal(snapshot.panel, false);
  assert.equal(snapshot.console, false);
  assert.equal(snapshot.error, null);
});

test('an explicit initial view is honoured, a nonsense one is not', () => {
  assert.equal(new ViewState('stage').view, 'stage');
  assert.equal(new ViewState('nowhere').view, 'studio');
});

// ---------------------------------------------------------------------------
console.log('moving between views');

test('going somewhere reports whether it changed anything', () => {
  // The caller needs this: entering the stage should start the transport and
  // re-entering it should not restart it.
  const views = new ViewState();
  assert.deepEqual(views.go('stage'), { from: 'studio', to: 'stage', changed: true });
  assert.deepEqual(views.go('stage'), { from: 'stage', to: 'stage', changed: false });
});

test('an unknown view is refused loudly', () => {
  assert.throws(() => new ViewState().go('kitchen'), /no such view/);
});

test('exactly one view is ever showing', () => {
  const views = new ViewState();
  for (const target of ['stage', 'studio', 'stage']) {
    views.go(target);
    const snapshot = views.snapshot();
    assert.equal([snapshot.studio, snapshot.stage].filter(Boolean).length, 1);
  }
});

test('changes are announced once, with the snapshot', () => {
  const views = new ViewState();
  const seen = [];
  views.onChange = (snapshot) => seen.push(snapshot.view);
  views.go('stage');
  views.go('stage'); // no change, no notification
  views.go('studio');
  assert.deepEqual(seen, ['stage', 'studio']);
});

// ---------------------------------------------------------------------------
console.log('overlays');

test('the panel only exists on the stage', () => {
  // Two design surfaces on screen at once, each with its own idea of the same
  // settings, was one of the reachable-but-unintended states.
  const views = new ViewState('studio');
  assert.equal(views.setOverlay('panel', true), false);
  assert.equal(views.snapshot().panel, false);

  views.go('stage');
  assert.equal(views.setOverlay('panel', true), true);
  assert.equal(views.snapshot().panel, true);
});

test('leaving the stage closes the panel rather than remembering it', () => {
  const views = new ViewState('stage');
  views.setOverlay('panel', true);
  views.go('studio');
  assert.equal(views.panel, false, 'the panel stayed open behind the studio');
  views.go('stage');
  assert.equal(views.snapshot().panel, false, 'it reappeared unasked');
});

test('the snapshot never shows a panel off-stage, whatever the flag says', () => {
  // Belt and braces: even if something sets the field directly, the derived
  // truth cannot contradict the view.
  const views = new ViewState('studio');
  views.panel = true;
  assert.equal(views.snapshot().panel, false);
});

test('the console shows over either view', () => {
  for (const view of VIEWS) {
    const views = new ViewState(view);
    assert.equal(views.setOverlay('console', true), true, view);
    assert.equal(views.snapshot().console, true, view);
  }
});

test('the console survives a view change', () => {
  const views = new ViewState('stage');
  views.setOverlay('console', true);
  views.go('studio');
  assert.equal(views.snapshot().console, true, 'the console closed itself');
});

test('toggling flips, and reports where it landed', () => {
  const views = new ViewState('stage');
  assert.equal(views.toggleOverlay('panel'), true);
  assert.equal(views.toggleOverlay('panel'), false);
});

test('an unknown overlay is refused loudly', () => {
  assert.throws(() => new ViewState().setOverlay('sidebar', true), /no such overlay/);
  assert.deepEqual(OVERLAYS, ['panel', 'console']);
});

// ---------------------------------------------------------------------------
console.log('the pointer');

test('the stage hides the cursor, and only the bare stage', () => {
  // The piece has no interaction layer, so it has no pointer -- but anything
  // layered over it does, or the author cannot click their own controls.
  const views = new ViewState('stage');
  assert.equal(views.snapshot().pointer, false);
  views.setOverlay('panel', true);
  assert.equal(views.snapshot().pointer, true);
  views.setOverlay('panel', false);
  views.setOverlay('console', true);
  assert.equal(views.snapshot().pointer, true);
});

test('the studio always has a pointer', () => {
  assert.equal(new ViewState('studio').snapshot().pointer, true);
});

// ---------------------------------------------------------------------------
console.log('errors');

test('an error is state, not a floating element', () => {
  // It used to be a banner that a later view change would quietly navigate away
  // from, leaving the author with no idea what had failed.
  const views = new ViewState();
  views.fail('cannot reach the server');
  assert.equal(views.snapshot().error, 'cannot reach the server');
  views.go('stage');
  assert.equal(views.snapshot().error, 'cannot reach the server', 'the error vanished');
  views.clearError();
  assert.equal(views.snapshot().error, null);
});

test('clearing nothing changes nothing', () => {
  const views = new ViewState();
  let calls = 0;
  views.onChange = () => {
    calls += 1;
  };
  views.clearError();
  assert.equal(calls, 0);
});

test('a failure with no message still says something', () => {
  const views = new ViewState();
  assert.ok(views.fail().length > 0);
});

// ---------------------------------------------------------------------------
console.log('the snapshot is the only truth');

test('it is a pure function of the state', () => {
  const views = new ViewState('stage');
  views.setOverlay('panel', true);
  views.setOverlay('console', true);
  assert.deepEqual(views.snapshot(), views.snapshot());
});

test('every reachable combination is legal', () => {
  // Exhaustive over the whole state space, which is small on purpose. Anything
  // the machine can reach must be a combination someone meant.
  for (const view of VIEWS) {
    for (const panel of [false, true]) {
      for (const consoleOn of [false, true]) {
        const views = new ViewState(view);
        views.setOverlay('panel', panel);
        views.setOverlay('console', consoleOn);
        const snapshot = views.snapshot();
        assert.equal(
          [snapshot.studio, snapshot.stage].filter(Boolean).length,
          1,
          'more or less than one view',
        );
        assert.ok(
          !(snapshot.panel && snapshot.studio),
          'the panel is showing over the studio',
        );
        assert.equal(snapshot.view, view);
      }
    }
  }
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
