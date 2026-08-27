/**
 * Key maps: the physical layout, and what each position plays.
 *
 * Shared by the keyboard engine (which reads a map) and the studio editor (which
 * writes one), so the two cannot disagree about what a binding means. Also has
 * no DOM in it, which is what makes the resolution testable headlessly -- the
 * part where a wrong answer is silent.
 *
 * **Positions, not characters.** A binding is stored against
 * `KeyboardEvent.code` -- `KeyA`, not `a`. `event.key` depends on the layout, so
 * a map authored on a Spanish keyboard would land on different physical keys on
 * a US one, and a piece is meant to be shareable. Position is also the right
 * musical model: a mapping is a layout, like a piano.
 *
 * The editor still has to *show* the author their own keys, though, which is the
 * opposite problem. `navigator.keyboard.getLayoutMap()` answers it where it
 * exists; the US table below is the fallback.
 */

/** The physical rows, in the order a keyboard has them. */
export const KEY_ROWS = [
  ['Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5', 'Digit6', 'Digit7', 'Digit8', 'Digit9', 'Digit0'],
  ['KeyQ', 'KeyW', 'KeyE', 'KeyR', 'KeyT', 'KeyY', 'KeyU', 'KeyI', 'KeyO', 'KeyP'],
  ['KeyA', 'KeyS', 'KeyD', 'KeyF', 'KeyG', 'KeyH', 'KeyJ', 'KeyK', 'KeyL', 'Semicolon'],
  ['KeyZ', 'KeyX', 'KeyC', 'KeyV', 'KeyB', 'KeyN', 'KeyM', 'Comma', 'Period', 'Slash'],
];

/** Every position a map may bind, flat. */
export const ALL_KEYS = KEY_ROWS.flat();

/** Fallback labels, US layout. Only for display -- never for binding. */
const US_LABELS = {
  Digit1: '1', Digit2: '2', Digit3: '3', Digit4: '4', Digit5: '5',
  Digit6: '6', Digit7: '7', Digit8: '8', Digit9: '9', Digit0: '0',
  Semicolon: ';', Comma: ',', Period: '.', Slash: '/',
  Quote: "'", BracketLeft: '[', BracketRight: ']', Backslash: '\\',
  Minus: '-', Equal: '=', Backquote: '`',
};

export function fallbackLabel(code) {
  if (US_LABELS[code]) return US_LABELS[code];
  if (code.startsWith('Key')) return code.slice(3);
  if (code.startsWith('Digit')) return code.slice(5);
  return code;
}

/**
 * The author's own key labels, where the browser will say.
 *
 * Chromium exposes the real layout; everything else gets the US fallback. Worth
 * the feature detection because the whole point of binding by position is that
 * the map outlives a layout change -- but an editor showing US labels to someone
 * on a Spanish keyboard would be unusable.
 */
export async function layoutLabels() {
  const labels = {};
  for (const code of ALL_KEYS) labels[code] = fallbackLabel(code);
  try {
    const map = await navigator.keyboard?.getLayoutMap?.();
    if (map) {
      for (const code of ALL_KEYS) {
        const found = map.get(code);
        if (found) labels[code] = found.toUpperCase();
      }
    }
  } catch {
    // Permissions or an unsupported browser. The fallback is already in place.
  }
  return labels;
}

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

export function noteName(midi) {
  const rounded = Math.round(midi);
  return `${NOTE_NAMES[((rounded % 12) + 12) % 12]}${Math.floor(rounded / 12) - 1}`;
}

export const PIANO_LOW = 21;
export const PIANO_HIGH = 108;

const clampToPiano = (midi) => Math.max(PIANO_LOW, Math.min(PIANO_HIGH, Math.round(midi)));

/**
 * A scale degree, as a MIDI note.
 *
 * Degrees rather than absolute pitches is the reason a map survives a change of
 * chain: `mod_reduce` or the output mapping can move the piece into a different
 * scale, and a map bound to pitches would go quietly out of key while a map
 * bound to degrees follows. Degree 7 of a 7-note scale is the root an octave up,
 * not an error -- same rule as the Python side.
 */
export function degreeToMidi(degree, scale, octaveOffset = 0) {
  const offsets = scale?.offsets?.length ? scale.offsets : [0, 2, 4, 5, 7, 9, 11];
  const span = scale?.span ?? 12;
  const root = scale?.root ?? 48;
  const count = offsets.length;
  const octave = Math.floor(degree / count);
  const index = ((degree % count) + count) % count;
  return clampToPiano(root + offsets[index] + (octave + octaveOffset) * span);
}

/**
 * What a binding plays. Returns null when it plays nothing yet.
 *
 * `sample` and `pattern` resolve to a descriptor rather than a note, because
 * they are triggered rather than pitched -- the engine decides what to do with
 * them, and right now it cannot do anything, which is reported honestly rather
 * than silently falling back to a note.
 */
export function resolveBinding(binding, scale, octaveOffset = 0) {
  if (!binding) return null;
  switch (binding.kind) {
    case 'degree':
      return {
        kind: 'note',
        midi: degreeToMidi(binding.degree ?? 0, scale, (binding.octave ?? 0) + octaveOffset),
      };
    case 'note': {
      const midi = Number(binding.midi);
      if (!Number.isFinite(midi)) return null;
      return { kind: 'note', midi: clampToPiano(midi + octaveOffset * (scale?.span ?? 12)) };
    }
    case 'sample':
      return binding.sample ? { kind: 'sample', sample: binding.sample } : null;
    case 'pattern':
      return binding.pattern ? { kind: 'pattern', pattern: binding.pattern } : null;
    default:
      return null;
  }
}

/** A short human label for a binding, for the editor and the panel. */
export function describeBinding(binding, scale, octaveOffset = 0) {
  if (!binding) return '';
  const resolved = resolveBinding(binding, scale, octaveOffset);
  if (!resolved) return '?';
  if (resolved.kind === 'note') {
    return binding.kind === 'degree'
      ? `${noteName(resolved.midi)} · deg ${binding.degree}${binding.octave ? `+${binding.octave}` : ''}`
      : noteName(resolved.midi);
  }
  if (resolved.kind === 'sample') return `▸ ${resolved.sample}`;
  return `▤ ${resolved.pattern}`;
}

/**
 * The default map: scale degrees laid across the two home rows.
 *
 * Mirrors ``default_keymap`` in serrin/piece.py -- same rows, same degrees, same
 * octave split -- so a map made in the browser and one made by the CLI are the
 * same map.
 */
export function defaultKeymap() {
  const keymap = {};
  // Home row first, then the row above an octave up: both hands get range
  // without either having to reach.
  const rows = [KEY_ROWS[2].slice(0, 9), KEY_ROWS[1].slice(0, 10)];
  rows.forEach((row, rowIndex) => {
    row.forEach((code, index) => {
      keymap[code] = { kind: 'degree', degree: index, octave: rowIndex };
    });
  });
  return keymap;
}

/** Fill every physical key with consecutive degrees. For the completists. */
export function chromaticKeymap() {
  const keymap = {};
  let degree = 0;
  // Bottom row upward, so pitch rises with the keys, the way a piano does.
  for (const row of [...KEY_ROWS].reverse()) {
    for (const code of row) {
      keymap[code] = { kind: 'degree', degree, octave: 0 };
      degree += 1;
    }
  }
  return keymap;
}
