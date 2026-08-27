/**
 * Just enough DOM to construct the view classes in node.
 *
 * Written after shipping a crash on load. `visible` was refactored from a field
 * to a getter in three classes and the stale assignment was removed from only
 * one, so `new Studio()` threw before the first frame and the page was blank.
 * Every test passed: 288 in Python, 152 in node, none of which had ever
 * constructed a view class, because they all need a document.
 *
 * A blank page is the worst failure mode there is, and it was the one thing with
 * no coverage. This is the smallest stub that fixes that -- enough to build the
 * panel, the studio and the console and run their first paint, which is where
 * constructor-time errors, missing element ids and getter mistakes all surface.
 *
 * It is deliberately not a DOM implementation. Nothing here checks layout, or
 * that anything *looks* right; it checks that the code runs. Bringing in a real
 * headless DOM to assert on a canvas painter would cost more than it catches --
 * the argument made when the visual engine was written, and still true.
 *
 * The element registry is built from the real `web/index.html`, so an id the
 * JavaScript asks for and the markup does not have is a failure here rather than
 * a silent null in a browser.
 */

import { readFile } from 'node:fs/promises';

/** Anything can be called on a canvas context, and nothing needs to happen. */
function stubContext() {
  const noop = () => undefined;
  return new Proxy(
    {
      canvas: { width: 600, height: 150, clientWidth: 600, clientHeight: 150 },
      createLinearGradient: () => ({ addColorStop: noop }),
      measureText: () => ({ width: 10 }),
      getImageData: () => ({ data: new Uint8ClampedArray(4) }),
    },
    {
      get(target, key) {
        if (key in target) return target[key];
        // Drawing state (fillStyle, font, lineWidth…) reads back as whatever was
        // written; everything else is a method that does nothing.
        if (typeof key === 'string' && /^[a-z]/.test(key)) return noop;
        return undefined;
      },
      set(target, key, value) {
        target[key] = value;
        return true;
      },
    },
  );
}

class StubClassList {
  constructor() {
    this.set = new Set();
  }

  add(...names) {
    for (const name of names) this.set.add(name);
  }

  remove(...names) {
    for (const name of names) this.set.delete(name);
  }

  contains(name) {
    return this.set.has(name);
  }

  toggle(name, force) {
    const wanted = force ?? !this.set.has(name);
    if (wanted) this.set.add(name);
    else this.set.delete(name);
    return wanted;
  }
}

class StubElement {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.classList = new StubClassList();
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.textContent = '';
    this.title = '';
    this.innerHTML = '';
    this.files = [];
    this.clientWidth = 600;
    this.clientHeight = 150;
    this.width = 600;
    this.height = 150;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  removeEventListener() {}

  /** Fire a listener, the way a test drives a button. */
  dispatch(type, event = {}) {
    const fired = this.listeners.get(type) ?? [];
    for (const handler of fired) {
      handler({ target: this, preventDefault() {}, stopPropagation() {}, ...event });
    }
    return fired.length;
  }

  click() {
    return this.dispatch('click');
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  appendChild(node) {
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }

  // The view classes query inside markup they generated themselves, which lives
  // in innerHTML as a string here. Returning nothing is correct: there are no
  // real nodes to wire, and the wiring is exercised through the real page.
  querySelectorAll() {
    return [];
  }

  querySelector() {
    return null;
  }

  getContext() {
    return stubContext();
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight };
  }

  setPointerCapture() {}

  focus() {}

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
}

/**
 * Install a global stub whose element ids come from the real markup.
 *
 * Returns a handle with the elements, plus what was *asked for and missing* --
 * which is the interesting output: a null from getElementById is a silent
 * failure in a browser and a test failure here.
 */
export async function installDom(indexHtmlPath) {
  const html = await readFile(indexHtmlPath, 'utf8');
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]);

  const elements = new Map();
  for (const id of ids) elements.set(id, new StubElement('div', id));

  const missing = new Set();
  const created = [];

  const doc = {
    body: new StubElement('body'),
    documentElement: new StubElement('html'),
    visibilityState: 'visible',
    activeElement: null,
    getElementById(id) {
      if (elements.has(id)) return elements.get(id);
      // Recorded rather than invented: handing back a fresh element would hide
      // exactly the bug this stub exists to catch.
      missing.add(id);
      return null;
    },
    createElement(tag) {
      const element = new StubElement(tag);
      created.push(element);
      return element;
    },
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    fullscreenElement: null,
    exitFullscreen() {},
  };

  const win = {
    addEventListener() {},
    removeEventListener() {},
    devicePixelRatio: 1,
    innerWidth: 1280,
    innerHeight: 720,
    location: { search: '' },
    AudioContext: undefined,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  };

  globalThis.document = doc;
  globalThis.window = win;
  // defineProperty, not assignment: node exposes `navigator` as a getter-only
  // accessor, so `globalThis.navigator = …` throws. Which is, with some irony,
  // the exact class of bug this file was written to catch.
  Object.defineProperty(globalThis, 'navigator', {
    value: { keyboard: undefined, clipboard: undefined },
    configurable: true,
    writable: true,
  });
  globalThis.performance = globalThis.performance ?? { now: () => 0 };
  globalThis.requestAnimationFrame = () => 0;
  globalThis.cancelAnimationFrame = () => {};
  globalThis.localStorage = {
    store: new Map(),
    getItem(key) {
      return this.store.get(key) ?? null;
    },
    setItem(key, value) {
      this.store.set(key, String(value));
    },
    removeItem(key) {
      this.store.delete(key);
    },
  };
  globalThis.CustomEvent = class {
    constructor(type, options = {}) {
      this.type = type;
      Object.assign(this, options);
    }
  };
  globalThis.Blob = class {
    constructor(parts = []) {
      this.size = parts.join('').length;
    }
  };
  globalThis.URL.createObjectURL = () => 'blob:stub';
  globalThis.URL.revokeObjectURL = () => {};
  globalThis.confirm = () => true;
  globalThis.prompt = () => null;
  // No server in a unit test: every call rejects, which is also the case the
  // views have to survive, since serve.py is frequently not running.
  globalThis.fetch = () => Promise.reject(new Error('no server in this test'));

  return { doc, win, elements, missing, created, ids };
}
