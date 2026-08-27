/**
 * Which surface is showing. A state machine, because four booleans were not.
 *
 * The app grew four independently hidden elements -- gate, studio, panel,
 * console -- each toggled by whoever happened to need it. Nothing stopped them
 * from contradicting each other: the studio open over a playing stage with the
 * panel also up, or the gate showing behind a view that had already loaded. Every
 * one of those was reachable, none was intended, and none was testable without a
 * browser.
 *
 * So visibility is derived rather than set. There is one piece of state, the
 * transitions are named, and `snapshot()` is a pure function from state to what
 * should be on screen. main.js applies that snapshot in one place and nowhere
 * else touches `hidden`.
 *
 * The design this encodes:
 *
 *   **studio** is where you start. Configuring a piece is the first thing you do
 *   and nothing sounds while you do it -- the piece is the document, and a
 *   document is not playing when you open it.
 *
 *   **stage** is the piece itself. Entered by playing something, deliberately,
 *   which is also the user gesture browsers require before audio.
 *
 *   **panel** is a stage overlay and only a stage overlay. It holds
 *   performance-time controls; showing it over the studio would put two design
 *   surfaces on screen at once, each with its own idea of the same settings.
 *
 *   **console** shows over either, because "what did it just do" is a question
 *   worth asking in both.
 */

export const VIEWS = ['studio', 'stage'];
export const OVERLAYS = ['panel', 'console'];

export class ViewState {
  constructor(initial = 'studio') {
    this.view = VIEWS.includes(initial) ? initial : 'studio';
    this.panel = false;
    this.console = false;
    this.error = null;
    //: Notified after any change, with the snapshot. main.js paints from this.
    this.onChange = null;
  }

  // -- views ---------------------------------------------------------------
  /**
   * Move to a view. Returns what actually happened.
   *
   * The return value matters: callers need to know whether a transition *did*
   * anything, because entering the stage should start the transport and
   * re-entering it should not restart it.
   */
  go(next) {
    if (!VIEWS.includes(next)) {
      throw new Error(`no such view: ${next}`);
    }
    const from = this.view;
    if (from === next) return { from, to: next, changed: false };
    this.view = next;
    // The panel belongs to the stage, so leaving the stage closes it rather than
    // leaving it hidden-but-open and surprising on return.
    if (next !== 'stage') this.panel = false;
    this._changed();
    return { from, to: next, changed: true };
  }

  get inStudio() {
    return this.view === 'studio';
  }

  get inStage() {
    return this.view === 'stage';
  }

  // -- overlays ------------------------------------------------------------
  /** Show or hide an overlay. Returns whether it ended up visible. */
  setOverlay(name, visible) {
    if (!OVERLAYS.includes(name)) throw new Error(`no such overlay: ${name}`);
    const wanted = Boolean(visible);
    // Asked for on the wrong view: refused rather than remembered, so nothing
    // pops up later for a reason the author has forgotten.
    const allowed = wanted && this._overlayAllowed(name);
    if (this[name] === allowed) return allowed;
    this[name] = allowed;
    this._changed();
    return allowed;
  }

  toggleOverlay(name) {
    return this.setOverlay(name, !this[name]);
  }

  _overlayAllowed(name) {
    return name === 'panel' ? this.view === 'stage' : true;
  }

  // -- errors --------------------------------------------------------------
  /**
   * A message that blocks the view it belongs to.
   *
   * Held here rather than in a floating element because "cannot load the piece"
   * has to survive a view change: showing the error and then quietly navigating
   * away from it was one of the odd behaviours this file exists to remove.
   */
  fail(message) {
    this.error = String(message ?? 'something went wrong');
    this._changed();
    return this.error;
  }

  clearError() {
    if (this.error === null) return;
    this.error = null;
    this._changed();
  }

  // -- the derived truth ---------------------------------------------------
  /** What should be on screen. A pure function of the state above. */
  snapshot() {
    return {
      view: this.view,
      studio: this.view === 'studio',
      stage: this.view === 'stage',
      // Never visible outside the stage, whatever anyone set.
      panel: this.panel && this.view === 'stage',
      console: this.console,
      error: this.error,
      // The stage has no pointer unless something is layered over it: a cursor
      // hovering over the piece is chrome the piece does not want.
      pointer: this.view !== 'stage' || this.panel || this.console,
    };
  }

  _changed() {
    this.onChange?.(this.snapshot());
  }
}
